import {
  decodeAudioWaveform,
  reviveMessage,
  type AudioArrayPayload,
  type RuntimeMessage,
  type RuntimeValue,
} from "./binary";

type AudioOp =
  | {
      op: "add";
      name: string;
      sampleRate: number;
      waveform: AudioArrayPayload;
      volume: number;
    }
  | {
      op: "set_volume";
      name: string;
      volume: number;
    }
  | {
      op: "set_waveform";
      name: string;
      waveform: AudioArrayPayload;
    }
  | {
      op: "append";
      name: string;
      waveform: AudioArrayPayload;
    }
  | {
      op: "remove";
      name: string;
    };

type RuntimeConfig = {
  numSteps: number;
  fps: number;
  baseFps: number | null;
  loop: boolean;
  timestepSyncUuid: string | null;
};

type ViewerLike = {
  mutable: {
    current: {
      messageQueue: RuntimeMessage[];
      sendMessage(message: {
        type: "GuiUpdateMessage";
        uuid: string;
        updates: { [key: string]: RuntimeValue | undefined };
      }): void;
    };
  };
  useSceneTree: unknown;
};

type ReactFiberNode = {
  memoizedProps?: {
    value?: Partial<ViewerLike> & Record<string, unknown>;
  };
  child?: ReactFiberNode | null;
  sibling?: ReactFiberNode | null;
};

type TimelineRuntimeWindow = Window & {
  __VISER4D__?: TimelineRuntime;
  AudioContext?: typeof AudioContext;
  webkitAudioContext?: typeof AudioContext;
};

type TrackState = {
  channels: number;
  sampleRate: number;
  waveform: Float32Array[];
  volume: number;
  startStep: number;
  removed: boolean;
};

type RuntimeTrack = {
  source: AudioBufferSourceNode | null;
  gain: GainNode | null;
  buffer: AudioBuffer | null;
  bufferWaveform: Float32Array[] | null;
  bufferSampleRate: number | null;
  token: number;
};

const debugState = {
  enabled: false,
  logs: [] as Array<{ time: number; event: string; payload: RuntimeValue }>,
  maxLogs: 400,
  push(event: string, payload: RuntimeValue): void {
    this.logs.push({
      time: Number(performance.now().toFixed(1)),
      event,
      payload,
    });
    if (this.logs.length > this.maxLogs) {
      this.logs.shift();
    }
    if (this.enabled) {
      console.debug("[viser4d]", event, payload);
    }
  },
  clear(): void {
    this.logs.length = 0;
  },
  setEnabled(enabled: boolean): void {
    this.enabled = !!enabled;
  },
};

function getWindow(): TimelineRuntimeWindow {
  return window as TimelineRuntimeWindow;
}

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object";
}

function isViewerLike(value: unknown): value is ViewerLike {
  return (
    isObjectRecord(value) &&
    isObjectRecord(value.mutable) &&
    "useSceneTree" in value
  );
}

function findViewer(): ViewerLike | null {
  const root = document.getElementById("root");
  if (!root) {
    return null;
  }
  const rootRecord = root as unknown;
  if (!isObjectRecord(rootRecord)) {
    return null;
  }
  const containerKey = Object.keys(rootRecord).find((key) =>
    key.startsWith("__reactContainer$"),
  );
  const reactRoot = containerKey ? rootRecord[containerKey] : null;
  if (!isObjectRecord(reactRoot)) {
    return null;
  }
  const seen = new Set<unknown>();
  const stack: ReactFiberNode[] = [reactRoot as ReactFiberNode];
  while (stack.length) {
    const fiber = stack.pop();
    if (!fiber || seen.has(fiber)) {
      continue;
    }
    seen.add(fiber);
    const candidate = fiber.memoizedProps?.value;
    if (isViewerLike(candidate)) {
      return candidate;
    }
    if (fiber.child) {
      stack.push(fiber.child);
    }
    if (fiber.sibling) {
      stack.push(fiber.sibling);
    }
  }
  return null;
}

function makeTrackState(step: number, sampleRate = 44100): TrackState {
  return {
    channels: 1,
    sampleRate,
    waveform: [new Float32Array(0)],
    volume: 1.0,
    startStep: step,
    removed: false,
  };
}

function getOpSampleRate(op: AudioOp): number | undefined {
  return op.op === "add" ? op.sampleRate : undefined;
}

function mergeTrackState(
  base: TrackState | undefined,
  override: Partial<TrackState> | undefined,
): TrackState | null {
  if (!base && !override) {
    return null;
  }
  if (!base) {
    if (!override?.waveform || override.sampleRate == null) {
      return null;
    }
    return {
      channels: override.channels ?? override.waveform.length,
      sampleRate: override.sampleRate,
      waveform: override.waveform,
      volume: override.volume ?? 1.0,
      startStep: override.startStep ?? 0,
      removed: override.removed ?? false,
    };
  }
  if (!override) {
    return base;
  }
  return {
    channels: override.channels ?? base.channels,
    sampleRate: override.sampleRate ?? base.sampleRate,
    waveform: override.waveform ?? base.waveform,
    volume: override.volume ?? base.volume,
    startStep: override.startStep ?? base.startStep,
    removed: override.removed ?? base.removed,
  };
}

function appendWaveforms(head: Float32Array[], tail: Float32Array[]): Float32Array[] {
  return head.map((samples, channel) => {
    const merged = new Float32Array(samples.length + (tail[channel]?.length ?? 0));
    merged.set(samples, 0);
    merged.set(tail[channel] ?? new Float32Array(0), samples.length);
    return merged;
  });
}

function trackFrameCount(track: Pick<TrackState, "waveform">): number {
  return track.waveform[0]?.length ?? 0;
}

class AudioRuntime {
  private ctx: AudioContext | null = null;
  private timelineTracks = new Map<string, TrackState>();
  private liveOverrides = new Map<string, Partial<TrackState>>();
  private runtimeTracks = new Map<string, RuntimeTrack>();
  private playing = false;
  private currentStep = 0;
  private fps = 30;
  private baseFps = 30;
  private nextSourceToken = 1;

  constructor(private getTransportStep: () => number) {}

  private ensureContext(): AudioContext | null {
    if (!this.ctx) {
      const AudioContextClass =
        getWindow().AudioContext || getWindow().webkitAudioContext;
      this.ctx = AudioContextClass ? new AudioContextClass() : null;
    }
    return this.ctx;
  }

  private getPlaybackStep(): number {
    return this.playing ? this.getTransportStep() : this.currentStep;
  }

  private getTrackNames(): Set<string> {
    return new Set([
      ...this.timelineTracks.keys(),
      ...this.liveOverrides.keys(),
      ...this.runtimeTracks.keys(),
    ]);
  }

  private getEffectiveTrack(name: string): TrackState | null {
    return mergeTrackState(this.timelineTracks.get(name), this.liveOverrides.get(name));
  }

  private getRuntimeTrack(name: string): RuntimeTrack {
    const runtimeTrack = this.runtimeTracks.get(name);
    if (runtimeTrack) {
      return runtimeTrack;
    }
    const created: RuntimeTrack = {
      source: null,
      gain: null,
      buffer: null,
      bufferWaveform: null,
      bufferSampleRate: null,
      token: 0,
    };
    this.runtimeTracks.set(name, created);
    return created;
  }

  setBaseFps(baseFps: number): void {
    this.baseFps = Math.max(1e-6, baseFps || this.baseFps || 30);
  }

  private buildBuffer(track: TrackState, runtimeTrack: RuntimeTrack): AudioBuffer | null {
    const ctx = this.ensureContext();
    if (!ctx) {
      return null;
    }
    if (
      runtimeTrack.buffer &&
      runtimeTrack.bufferWaveform === track.waveform &&
      runtimeTrack.bufferSampleRate === track.sampleRate
    ) {
      return runtimeTrack.buffer;
    }
    const buffer = ctx.createBuffer(track.channels, trackFrameCount(track), track.sampleRate);
    for (let channel = 0; channel < track.channels; channel += 1) {
      buffer.copyToChannel(
        new Float32Array(track.waveform[channel] ?? new Float32Array(0)),
        channel,
      );
    }
    runtimeTrack.buffer = buffer;
    runtimeTrack.bufferWaveform = track.waveform;
    runtimeTrack.bufferSampleRate = track.sampleRate;
    return buffer;
  }

  private applyOp(
    target: TrackState | Partial<TrackState> | null,
    step: number,
    op: AudioOp,
    partial: boolean,
  ): { track: TrackState | Partial<TrackState>; effect: "volume" | "reschedule" | "none" } {
    const next = target || (partial ? {} : makeTrackState(step, getOpSampleRate(op)));
    let effect: "volume" | "reschedule" | "none" = "none";
    switch (op.op) {
      case "add":
        next.sampleRate = op.sampleRate;
        next.channels = op.waveform.numChannels;
        next.waveform = decodeAudioWaveform(op.waveform);
        next.volume = op.volume;
        next.startStep = step;
        next.removed = false;
        effect = "reschedule";
        break;
      case "set_volume":
        next.volume = op.volume;
        effect = "volume";
        break;
      case "set_waveform":
        next.channels = op.waveform.numChannels;
        next.waveform = decodeAudioWaveform(op.waveform);
        next.removed = false;
        effect = "reschedule";
        break;
      case "append": {
        const tail = decodeAudioWaveform(op.waveform);
        next.channels = op.waveform.numChannels;
        next.waveform = next.waveform ? appendWaveforms(next.waveform, tail) : tail;
        effect = "reschedule";
        break;
      }
      case "remove":
        next.removed = true;
        effect = "reschedule";
        break;
    }
    return { track: next, effect };
  }

  private applyOps(
    targetMap: Map<string, TrackState | Partial<TrackState>>,
    step: number,
    ops: AudioOp[],
    eventName: string,
    partial: boolean,
  ): void {
    for (const op of ops) {
      const current = targetMap.get(op.name);
      const target =
        partial || (current && "waveform" in current && current.waveform)
          ? current || null
          : makeTrackState(step, getOpSampleRate(op));
      const result = this.applyOp(target, step, op, partial);
      targetMap.set(op.name, result.track);
      debugState.push(eventName, {
        name: op.name,
        step,
        op: op.op,
        effect: result.effect,
      });
      if (result.effect === "volume") {
        this.updateTrackVolume(op.name);
      } else if (result.effect === "reschedule") {
        this.reconcileTrack(op.name);
      }
    }
  }

  applyTimelineOps(step: number, ops: AudioOp[]): void {
    this.applyOps(
      this.timelineTracks as Map<string, TrackState | Partial<TrackState>>,
      step,
      ops,
      "audio.timeline_op",
      false,
    );
  }

  applyLiveOps(step: number, ops: AudioOp[]): void {
    this.applyOps(
      this.liveOverrides as Map<string, TrackState | Partial<TrackState>>,
      step,
      ops,
      "audio.live_op",
      true,
    );
  }

  private updateTrackVolume(name: string): void {
    const runtimeTrack = this.runtimeTracks.get(name);
    const effective = this.getEffectiveTrack(name);
    if (runtimeTrack?.gain && effective) {
      runtimeTrack.gain.gain.value = effective.volume;
    }
  }

  private stopRuntimeTrack(runtimeTrack: RuntimeTrack): void {
    runtimeTrack.token += 1;
    if (runtimeTrack.source) {
      try {
        runtimeTrack.source.stop();
      } catch (error) {
        debugState.push("audio.stop_error", {
          error: error instanceof Error ? error.message : String(error),
        });
      }
      runtimeTrack.source.disconnect();
      runtimeTrack.source = null;
    }
    if (runtimeTrack.gain) {
      runtimeTrack.gain.disconnect();
      runtimeTrack.gain = null;
    }
  }

  private stopAllNodes(): void {
    for (const runtimeTrack of this.runtimeTracks.values()) {
      this.stopRuntimeTrack(runtimeTrack);
    }
  }

  private getClipDurationSteps(track: TrackState): number {
    return (trackFrameCount(track) / track.sampleRate) * this.baseFps;
  }

  private isTrackActiveAtStep(track: TrackState, playbackStep: number): boolean {
    return (
      playbackStep >= track.startStep &&
      playbackStep < track.startStep + this.getClipDurationSteps(track)
    );
  }

  reconcileTrack(name: string): void {
    const ctx = this.ensureContext();
    if (!ctx) {
      return;
    }
    const runtimeTrack = this.getRuntimeTrack(name);
    this.stopRuntimeTrack(runtimeTrack);
    if (!this.playing) {
      return;
    }
    if (typeof ctx.resume === "function" && ctx.state === "suspended") {
      ctx.resume().catch(() => {});
    }
    const track = this.getEffectiveTrack(name);
    if (!track || track.removed || !trackFrameCount(track)) {
      return;
    }
    const playbackStep = this.getPlaybackStep();
    const endStep = track.startStep + this.getClipDurationSteps(track);
    if (playbackStep >= endStep) {
      return;
    }
    const source = ctx.createBufferSource();
    const gain = ctx.createGain();
    source.buffer = this.buildBuffer(track, runtimeTrack);
    if (!source.buffer) {
      return;
    }
    gain.gain.value = track.volume;
    source.playbackRate.value = this.fps / this.baseFps;
    source.connect(gain);
    gain.connect(ctx.destination);
    const token = ++this.nextSourceToken;
    runtimeTrack.token = token;
    source.onended = () => {
      if (runtimeTrack.token !== token) {
        return;
      }
      runtimeTrack.source = null;
      if (runtimeTrack.gain === gain) {
        runtimeTrack.gain.disconnect();
        runtimeTrack.gain = null;
      }
      const effective = this.getEffectiveTrack(name);
      if (
        this.playing &&
        effective &&
        !effective.removed &&
        trackFrameCount(effective) &&
        this.isTrackActiveAtStep(effective, this.getPlaybackStep())
      ) {
        this.reconcileTrack(name);
      }
    };
    source.start(
      ctx.currentTime + Math.max(0, (track.startStep - playbackStep) / this.fps),
      Math.max(0, (playbackStep - track.startStep) / this.baseFps),
    );
    runtimeTrack.source = source;
    runtimeTrack.gain = gain;
  }

  private rescheduleAll(): void {
    for (const name of this.getTrackNames()) {
      this.reconcileTrack(name);
    }
  }

  play(step: number, fps: number): void {
    this.currentStep = step;
    this.fps = fps;
    this.playing = true;
    this.rescheduleAll();
  }

  pause(step: number, fps: number): void {
    this.currentStep = step;
    this.fps = fps;
    this.playing = false;
    this.stopAllNodes();
  }

  seek(step: number, fps: number, playing: boolean): void {
    this.currentStep = step;
    this.fps = fps;
    this.playing = playing;
    if (playing) {
      this.rescheduleAll();
      return;
    }
    this.stopAllNodes();
  }

  setFps(step: number, fps: number, playing: boolean): void {
    this.currentStep = step;
    this.fps = fps;
    if (playing) {
      this.playing = true;
      this.rescheduleAll();
      return;
    }
    this.playing = false;
    this.stopAllNodes();
  }

  resetTimeline(): void {
    this.stopAllNodes();
    this.timelineTracks.clear();
    this.currentStep = 0;
  }
}

export class TimelineRuntime {
  sceneSteps: RuntimeMessage[][] = [];
  appliedStep = -1;
  readonly debug = debugState;

  private viewer: ViewerLike | null = null;
  private config: RuntimeConfig = {
    numSteps: 1,
    fps: 30,
    baseFps: null,
    loop: false,
    timestepSyncUuid: null,
  };
  private audioSteps: AudioOp[][] = [];
  private timelineNodeNames = new Set<string>();
  private baselineByName = new Map<string, RuntimeMessage[]>();
  private currentStep = 0;
  private playStartStep = 0;
  private playStartPerfTime = 0;
  private playing = false;
  private rafId: number | null = null;
  private lastSyncedStep = -1;
  private lastSyncSentAt = 0;
  private readonly syncIntervalMs = 250;
  private readonly audio = new AudioRuntime(() => this.getTransportStep());

  private getViewer(): ViewerLike | null {
    if (!this.viewer) {
      this.viewer = findViewer();
    }
    return this.viewer;
  }

  private pushMessages(messages: RuntimeMessage[]): void {
    this.getViewer()?.mutable.current.messageQueue.push(...messages);
  }

  private sendGuiUpdate(uuid: string, value: number): void {
    this.getViewer()?.mutable.current.sendMessage({
      type: "GuiUpdateMessage",
      uuid,
      updates: { value },
    });
  }

  private ensureSceneStep(step: number): RuntimeMessage[] {
    const bucket = this.sceneSteps[step];
    if (bucket) {
      return bucket;
    }
    const created: RuntimeMessage[] = [];
    this.sceneSteps[step] = created;
    return created;
  }

  private ensureAudioStep(step: number): AudioOp[] {
    const bucket = this.audioSteps[step];
    if (bucket) {
      return bucket;
    }
    const created: AudioOp[] = [];
    this.audioSteps[step] = created;
    return created;
  }

  private anchorTransport(step: number, timestamp = performance.now()): void {
    this.currentStep = step;
    this.playStartStep = step;
    this.playStartPerfTime = timestamp;
  }

  private syncAudioTransport(): void {
    this.audio.seek(this.currentStep, this.config.fps, this.playing);
  }

  getTransportStep(timestamp = performance.now()): number {
    if (!this.playing) {
      return this.currentStep;
    }
    return (
      this.playStartStep +
      ((timestamp - this.playStartPerfTime) / 1000) * this.config.fps
    );
  }

  configure(config: Partial<RuntimeConfig>): void {
    this.config = { ...this.config, ...config };
    if (!this.config.baseFps) {
      this.config.baseFps = this.config.fps;
    }
    this.audio.setBaseFps(this.config.baseFps);
    while (this.sceneSteps.length < this.config.numSteps) {
      this.sceneSteps.push([]);
    }
    while (this.audioSteps.length < this.config.numSteps) {
      this.audioSteps.push([]);
    }
    debugState.push("runtime.configure", this.config);
    this.syncAudioTransport();
  }

  setBaseline(payload: { name: string; messages: RuntimeMessage[] }): void {
    this.baselineByName.set(payload.name, payload.messages.map(reviveMessage));
    this.timelineNodeNames.add(payload.name);
  }

  preloadSceneStep(payload: {
    step: number;
    messages: RuntimeMessage[];
    nodeNames?: string[];
  }): void {
    this.sceneSteps[payload.step] = this.ensureSceneStep(payload.step).concat(
      payload.messages.map(reviveMessage),
    );
    for (const name of payload.nodeNames || []) {
      this.timelineNodeNames.add(name);
    }
  }

  preloadAudioStep(payload: { step: number; ops: AudioOp[] }): void {
    this.audioSteps[payload.step] = this.ensureAudioStep(payload.step).concat(payload.ops);
  }

  applyAudioUpdate(op: AudioOp): void {
    debugState.push("runtime.apply_audio_update", {
      op: op.op,
      name: op.name,
      step: Math.floor(this.currentStep),
    });
    this.audio.applyLiveOps(Math.floor(this.currentStep), [op]);
  }

  private syncTimestepToServer(step: number, force = false): void {
    if (!this.config.timestepSyncUuid) {
      return;
    }
    const now = performance.now();
    if (!force && step === this.lastSyncedStep) {
      return;
    }
    if (!force && this.playing && now - this.lastSyncSentAt < this.syncIntervalMs) {
      return;
    }
    this.lastSyncedStep = step;
    this.lastSyncSentAt = now;
    this.sendGuiUpdate(this.config.timestepSyncUuid, step);
  }

  private resetTimelineState(): void {
    debugState.push("runtime.reset_timeline_state", {
      currentStep: this.currentStep,
      appliedStep: this.appliedStep,
      playing: this.playing,
    });
    this.pushMessages(
      Array.from(this.timelineNodeNames).map((name) => ({
        type: "RemoveSceneNodeMessage",
        name,
      })),
    );
    for (const [name, messages] of this.baselineByName.entries()) {
      this.timelineNodeNames.add(name);
      this.pushMessages(messages);
    }
    this.audio.resetTimeline();
    this.appliedStep = -1;
  }

  private applyThrough(step: number): void {
    if (step < this.appliedStep) {
      this.resetTimelineState();
    }
    for (let index = this.appliedStep + 1; index <= step; index += 1) {
      const sceneMessages = this.sceneSteps[index];
      if (sceneMessages?.length) {
        this.pushMessages(sceneMessages);
      }
      const audioOps = this.audioSteps[index];
      if (audioOps?.length) {
        this.audio.applyTimelineOps(index, audioOps);
      }
    }
    this.appliedStep = step;
  }

  seek(payload: { step: number }): void {
    const step = Math.max(0, Math.min(this.config.numSteps - 1, payload.step));
    this.currentStep = step;
    if (this.playing) {
      this.anchorTransport(step);
    }
    this.applyThrough(step);
    this.audio.seek(step, this.config.fps, this.playing);
    this.syncTimestepToServer(step, true);
  }

  private tick(timestamp: number): void {
    if (!this.playing) {
      return;
    }
    const next = this.getTransportStep(timestamp);
    if (next >= this.config.numSteps) {
      if (!this.config.loop) {
        this.currentStep = this.config.numSteps - 1;
        this.playing = false;
        this.audio.pause(this.currentStep, this.config.fps);
        this.syncTimestepToServer(Math.floor(this.currentStep), true);
        return;
      }
      this.anchorTransport(0, timestamp);
      this.resetTimelineState();
      this.applyThrough(0);
      this.audio.play(0, this.config.fps);
    } else {
      this.currentStep = next;
      this.applyThrough(Math.floor(this.currentStep));
    }
    this.syncTimestepToServer(Math.floor(this.currentStep));
    this.rafId = getWindow().requestAnimationFrame((nextTimestamp) => this.tick(nextTimestamp));
  }

  play(payload: { fps: number; loop: boolean }): void {
    const step = this.getTransportStep();
    this.config.fps = payload.fps;
    this.config.loop = payload.loop;
    this.playing = true;
    this.anchorTransport(step);
    this.audio.play(step, this.config.fps);
    if (this.rafId !== null) {
      getWindow().cancelAnimationFrame(this.rafId);
    }
    this.rafId = getWindow().requestAnimationFrame((timestamp) => this.tick(timestamp));
  }

  setFps(payload: { fps: number; loop: boolean }): void {
    const step = this.getTransportStep();
    this.config.fps = payload.fps;
    this.config.loop = payload.loop;
    this.anchorTransport(step);
    this.audio.setFps(step, this.config.fps, this.playing);
  }

  pause(): void {
    const step = this.getTransportStep();
    this.currentStep = step;
    this.playing = false;
    if (this.rafId !== null) {
      getWindow().cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this.audio.pause(step, this.config.fps);
    this.syncTimestepToServer(Math.floor(this.currentStep), true);
  }
}
