import {
  reviveMessage,
  type RuntimeMessage,
  type RuntimeValue,
} from "./binary";
import { AudioRuntime } from "./audio-runtime";
import {
  findViewer,
  getWindow,
  isAudioMessage,
  type RuntimeConfig,
  type ViewerLike,
} from "./protocol";

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

export class TimelineRuntime {
  stepMessages: RuntimeMessage[][] = [];
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
  private readonly audio = new AudioRuntime(
    () => this.getTransportStep(),
    (event, payload) => debugState.push(event, payload),
  );

  private getViewer(): ViewerLike {
    if (!this.viewer) {
      this.viewer = findViewer();
    }
    return this.viewer;
  }

  private pushMessages(messages: RuntimeMessage[]): void {
    this.getViewer().mutable.current.messageQueue.push(...messages);
  }

  private sendGuiUpdate(uuid: string, value: number): void {
    this.getViewer().mutable.current.sendMessage({
      type: "GuiUpdateMessage",
      uuid,
      updates: { value },
    });
  }

  private ensureStep(step: number): RuntimeMessage[] {
    const bucket = this.stepMessages[step];
    if (bucket) {
      return bucket;
    }
    const created: RuntimeMessage[] = [];
    this.stepMessages[step] = created;
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

  private applyStepMessages(step: number, messages: RuntimeMessage[]): void {
    const sceneMessages: RuntimeMessage[] = [];
    const audioMessages = [];
    for (const message of messages) {
      if (isAudioMessage(message)) {
        audioMessages.push(message);
      } else {
        sceneMessages.push(message);
      }
    }
    if (sceneMessages.length) {
      this.pushMessages(sceneMessages);
    }
    if (audioMessages.length) {
      this.audio.applyTimelineMessages(step, audioMessages);
    }
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
    while (this.stepMessages.length < this.config.numSteps) {
      this.stepMessages.push([]);
    }
    debugState.push("runtime.configure", this.config);
    this.syncAudioTransport();
  }

  setBaseline(payload: { name: string; messages: RuntimeMessage[] }): void {
    this.baselineByName.set(payload.name, payload.messages.map(reviveMessage));
    this.timelineNodeNames.add(payload.name);
  }

  preloadStep(payload: {
    step: number;
    messages: RuntimeMessage[];
    nodeNames?: string[];
  }): void {
    this.stepMessages[payload.step] = this.ensureStep(payload.step).concat(
      payload.messages.map(reviveMessage),
    );
    for (const name of payload.nodeNames || []) {
      this.timelineNodeNames.add(name);
    }
  }

  applyMessageUpdate(message: RuntimeMessage): void {
    const revived = reviveMessage(message);
    debugState.push("runtime.apply_message_update", {
      type: revived.type,
      name: revived.name ?? null,
      step: Math.floor(this.currentStep),
    });
    if (isAudioMessage(revived)) {
      this.audio.applyLiveMessages(Math.floor(this.currentStep), [revived]);
      return;
    }
    this.pushMessages([revived]);
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
      const messages = this.stepMessages[index];
      if (messages?.length) {
        this.applyStepMessages(index, messages);
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
    this.rafId = getWindow().requestAnimationFrame((nextTimestamp) =>
      this.tick(nextTimestamp),
    );
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
    this.rafId = getWindow().requestAnimationFrame((timestamp) =>
      this.tick(timestamp),
    );
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
