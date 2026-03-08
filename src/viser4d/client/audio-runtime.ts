import { decodeAudioWaveform, type RuntimeValue } from "./binary";
import { getWindow } from "./protocol";
import type { AudioMessage } from "./protocol";

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

type MutableTrackState = TrackState | Partial<TrackState>;

type DebugPush = (event: string, payload: RuntimeValue) => void;

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

function getMessageSampleRate(message: AudioMessage): number | undefined {
  return message.type === "AddAudioMessage" ? message.sampleRate : undefined;
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

export class AudioRuntime {
  private ctx: AudioContext | null = null;
  private timelineTracks = new Map<string, TrackState>();
  private liveOverrides = new Map<string, Partial<TrackState>>();
  private runtimeTracks = new Map<string, RuntimeTrack>();
  private playing = false;
  private currentStep = 0;
  private fps = 30;
  private baseFps = 30;
  private nextSourceToken = 1;

  constructor(
    private getTransportStep: () => number,
    private debugPush: DebugPush,
  ) {}

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
    const timelineTrack = this.timelineTracks.get(name);
    const liveOverride = this.liveOverrides.get(name);
    return mergeTrackState(timelineTrack, liveOverride);
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
    target: MutableTrackState | null,
    step: number,
    message: AudioMessage,
    partial: boolean,
  ): { track: MutableTrackState; effect: "volume" | "reschedule" | "none" } {
    const next =
      target ?? (partial ? {} : makeTrackState(step, getMessageSampleRate(message)));
    let effect: "volume" | "reschedule" | "none" = "none";
    switch (message.type) {
      case "AddAudioMessage":
        next.sampleRate = message.sampleRate;
        next.channels = message.waveform.numChannels;
        next.waveform = decodeAudioWaveform(message.waveform);
        next.volume = message.volume;
        next.startStep = step;
        next.removed = false;
        effect = "reschedule";
        break;
      case "SetAudioVolumeMessage":
        next.volume = message.volume;
        effect = "volume";
        break;
      case "SetAudioWaveformMessage":
        next.channels = message.waveform.numChannels;
        next.waveform = decodeAudioWaveform(message.waveform);
        next.removed = false;
        effect = "reschedule";
        break;
      case "AppendAudioMessage": {
        const tail = decodeAudioWaveform(message.waveform);
        next.channels = message.waveform.numChannels;
        next.waveform = next.waveform ? appendWaveforms(next.waveform, tail) : tail;
        effect = "reschedule";
        break;
      }
      case "RemoveAudioMessage":
        next.removed = true;
        effect = "reschedule";
        break;
    }
    return { track: next, effect };
  }

  private applyOps(
    targetMap: Map<string, MutableTrackState>,
    step: number,
    messages: AudioMessage[],
    eventName: string,
    partial: boolean,
  ): void {
    for (const message of messages) {
      const current = targetMap.get(message.name);
      const hasWaveform = current && "waveform" in current && current.waveform;
      const target =
        partial || hasWaveform
          ? current || null
          : makeTrackState(step, getMessageSampleRate(message));
      const result = this.applyOp(target, step, message, partial);
      targetMap.set(message.name, result.track);
      this.debugPush(eventName, {
        name: message.name,
        step,
        type: message.type,
        effect: result.effect,
      });
      if (result.effect === "volume") {
        this.updateTrackVolume(message.name);
      } else if (result.effect === "reschedule") {
        this.reconcileTrack(message.name);
      }
    }
  }

  applyTimelineMessages(step: number, messages: AudioMessage[]): void {
    this.applyOps(
      this.timelineTracks as Map<string, MutableTrackState>,
      step,
      messages,
      "audio.timeline_message",
      false,
    );
  }

  applyLiveMessages(step: number, messages: AudioMessage[]): void {
    this.applyOps(
      this.liveOverrides as Map<string, MutableTrackState>,
      step,
      messages,
      "audio.live_message",
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
        this.debugPush("audio.stop_error", {
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
