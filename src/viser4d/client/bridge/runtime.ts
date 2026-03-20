import {
  type RuntimeMessage,
  type RuntimeValue,
  normalizeTransportMessage,
} from "../binary";
import { AudioRuntime } from "../audio/runtime";
import { isAudioMessage } from "../audio/messages";
import {
  findPlaybackTimeSlider,
  findViewer,
  getWindow,
  type GuiUpdateMessage,
  type RuntimeConfig,
  type ViewerMessage,
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
  private playbackTimeSlider: Element | null = null;
  private config: RuntimeConfig = {
    numSteps: 1,
    fps: 30,
    timelineFps: null,
    loop: false,
    timelineSliderUuid: null,
    fpsSliderUuid: null,
    stepButtonsUuid: null,
    playButtonUuid: null,
    pauseButtonUuid: null,
    timestepSyncUuid: null,
  };
  private timelineNodeNames = new Set<string>();
  private baselineByName = new Map<string, RuntimeMessage[]>();
  private currentStep = 0;
  private playStartStep = 0;
  private playStartPerfTime = 0;
  private playing = false;
  private rafId: number | null = null;
  private lastLocalSliderStep = -1;
  private lastSyncedStep = -1;
  private readonly audio = new AudioRuntime(
    () => this.getTransportStep(),
    (event, payload) => debugState.push(event, payload),
  );
  private readonly playbackAudio = new AudioRuntime(
    () => this.playbackTime,
    (event, payload) => debugState.push(event, payload),
  );
  private playbackTime = 0;
  private playbackPlaying = false;
  private playbackObserved = false;
  private playbackLastAppliedMessageTime = -1;
  private queueIngressConfigured = false;
  private guiMessageInterceptorInstalled = false;
  private playbackMonitorId: number | null = null;
  // Rewinds are staged across frames: remove, rebuild baselines, then replay diffs.
  private resetEpoch = 0;
  private resetTargetStep: number | null = null;

  constructor() {
    this.playbackAudio.setStepRate(1);
    this.installWhenReady();
  }

  private getViewer(): ViewerLike {
    if (!this.viewer) {
      this.viewer = findViewer();
    }
    return this.viewer;
  }

  private getPlaybackTimeSlider(): Element | null {
    if (!this.playbackTimeSlider || !this.playbackTimeSlider.isConnected) {
      this.playbackTimeSlider = findPlaybackTimeSlider();
    }
    return this.playbackTimeSlider;
  }

  private installWhenReady(): void {
    try {
      this.configureQueueIngress();
      this.installGuiMessageInterceptor();
      if (this.getViewer().messageSource !== "websocket") {
        this.startPlaybackMonitor();
      }
    } catch {
      getWindow().requestAnimationFrame(() => this.installWhenReady());
    }
  }

  private configureQueueIngress(): void {
    if (this.queueIngressConfigured) {
      return;
    }
    if (this.getViewer().messageSource === "websocket") {
      this.queueIngressConfigured = true;
      return;
    }
    // File playback and embedded recordings push msgpack-decoded transport
    // messages directly into the viewer queue, so normalize them at ingress.
    const queue = this.getViewer().mutable.current.messageQueue;
    const originalPush = queue.push.bind(queue);
    queue.push = (...messages: RuntimeMessage[]): number => {
      const forwarded: RuntimeMessage[] = [];
      for (const message of messages) {
        const normalized = normalizeTransportMessage(message);
        if (this.handleQueuedMessage(normalized)) {
          continue;
        }
        forwarded.push(normalized);
      }
      return originalPush(...forwarded);
    };
    this.queueIngressConfigured = true;
  }

  private installGuiMessageInterceptor(): void {
    if (this.guiMessageInterceptorInstalled) {
      return;
    }
    const mutable = this.getViewer().mutable.current;
    let rawSendMessage = mutable.sendMessage;
    // Keep the built-in playback controls client-local by consuming their
    // outgoing GUI updates before viser forwards them to Python.
    const wrappedSendMessage = (message: ViewerMessage): void => {
      if (
        message.type === "GuiUpdateMessage"
        && this.handleLocalPlaybackGuiMessage(message as GuiUpdateMessage)
      ) {
        return;
      }
      rawSendMessage(message);
    };
    Object.defineProperty(mutable, "sendMessage", {
      configurable: true,
      enumerable: true,
      get: () => wrappedSendMessage,
      set: (value) => {
        rawSendMessage = value;
      },
    });
    this.guiMessageInterceptorInstalled = true;
  }

  private handleLocalPlaybackGuiMessage(message: GuiUpdateMessage): boolean {
    const value = message.updates.value;
    if (message.uuid === this.config.timelineSliderUuid) {
      const step = Number(value);
      if (!Number.isFinite(step)) {
        return true;
      }
      this.seek({ step });
      return true;
    }
    if (message.uuid === this.config.fpsSliderUuid) {
      const fps = Number(value);
      if (!Number.isFinite(fps)) {
        return true;
      }
      this.setFps({ fps, loop: this.config.loop });
      return true;
    }
    if (message.uuid === this.config.stepButtonsUuid) {
      if (value === "Prev") {
        this.seek({ step: Math.floor(this.currentStep) - 1 });
      } else if (value === "Next") {
        this.seek({ step: Math.floor(this.currentStep) + 1 });
      }
      return true;
    }
    if (message.uuid === this.config.playButtonUuid) {
      this.play({ fps: this.config.fps, loop: this.config.loop });
      return true;
    }
    if (message.uuid === this.config.pauseButtonUuid) {
      this.pause();
      return true;
    }
    return false;
  }

  private handleQueuedMessage(message: RuntimeMessage): boolean {
    if (!isAudioMessage(message)) {
      return false;
    }
    if (this.getViewer().messageSource === "websocket") {
      return false;
    }
    const playbackTime = getPlaybackMessageTime(message);
    if (playbackTime !== null && playbackTime < this.playbackLastAppliedMessageTime) {
      this.resetPlaybackAudio();
    }
    if (playbackTime !== null) {
      this.playbackLastAppliedMessageTime = playbackTime;
      this.playbackAudio.applyLiveMessages(playbackTime, [message]);
    } else {
      this.playbackAudio.applyLiveMessages(this.playbackTime, [message]);
    }
    return true;
  }

  private startPlaybackMonitor(): void {
    if (this.playbackMonitorId !== null) {
      return;
    }
    const tick = (): void => {
      this.syncPlaybackState();
      this.playbackMonitorId = getWindow().requestAnimationFrame(tick);
    };
    this.playbackMonitorId = getWindow().requestAnimationFrame(tick);
  }

  private syncPlaybackState(): void {
    const nextTime = this.readPlaybackTime();
    if (nextTime === null) {
      return;
    }
    const delta = nextTime - this.playbackTime;
    const jumped = Math.abs(delta) > 0.2;
    const playing = delta > 1e-4;
    if (!this.playbackObserved) {
      this.playbackObserved = true;
      this.playbackTime = nextTime;
      this.playbackAudio.seek(nextTime, 1, false);
      return;
    }
    if (delta < -1e-4) {
      this.resetPlaybackAudio();
    }
    if (jumped) {
      this.playbackAudio.seek(nextTime, 1, playing);
    } else if (playing !== this.playbackPlaying) {
      if (playing) {
        this.playbackAudio.play(nextTime, 1);
      } else {
        this.playbackAudio.pause(nextTime, 1);
      }
    } else if (!playing && Math.abs(delta) > 1e-4) {
      this.playbackAudio.seek(nextTime, 1, false);
    }
    this.playbackTime = nextTime;
    this.playbackPlaying = playing;
  }

  private resetPlaybackAudio(): void {
    this.playbackAudio.resetTimeline();
    this.playbackLastAppliedMessageTime = -1;
  }

  private readPlaybackTime(): number | null {
    const slider = this.getPlaybackTimeSlider();
    if (!slider) {
      return null;
    }
    const value = slider.getAttribute("aria-valuenow");
    if (value === null) {
      return null;
    }
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
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

  private syncPlaybackButtons(): void {
    const guiState = this.getViewer().useGui.getState();
    const sync = (uuid: string | null, visible: boolean): void => {
      if (!uuid || guiState.guiConfigFromUuid[uuid] === undefined) {
        return;
      }
      guiState.updateGuiProps(uuid, { visible });
    };
    sync(this.config.playButtonUuid, !this.playing);
    sync(this.config.pauseButtonUuid, this.playing);
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
    if (!this.config.timelineFps) {
      this.config.timelineFps = this.config.fps;
    }
    this.audio.setStepRate(this.config.timelineFps);
    while (this.stepMessages.length < this.config.numSteps) {
      this.stepMessages.push([]);
    }
    debugState.push("runtime.configure", this.config);
    this.syncAudioTransport();
    this.syncPlaybackButtons();
  }

  setBaseline(payload: { name: string; messages: RuntimeMessage[] }): void {
    const messages = payload.messages.map((message) =>
      normalizeTransportMessage(message),
    );
    this.baselineByName.set(payload.name, messages);
    this.timelineNodeNames.add(payload.name);
  }

  preloadStep(payload: {
    step: number;
    messages: RuntimeMessage[];
    nodeNames?: string[];
  }): void {
    const messages = payload.messages.map((message) =>
      normalizeTransportMessage(message),
    );
    this.stepMessages[payload.step] = this.ensureStep(payload.step).concat(
      messages,
    );
    for (const name of payload.nodeNames || []) {
      this.timelineNodeNames.add(name);
    }
  }

  applyMessageUpdate(rawMessage: RuntimeMessage): void {
    const message = normalizeTransportMessage(rawMessage);
    const name = typeof message.name === "string" ? message.name : null;
    debugState.push("runtime.apply_message_update", {
      type: message.type,
      name,
      step: Math.floor(this.currentStep),
    });
    if (isAudioMessage(message)) {
      this.audio.applyLiveMessages(Math.floor(this.currentStep), [message]);
      return;
    }
    this.pushMessages([message]);
  }

  private syncTimelineSlider(step: number, force = false): void {
    const clampedStep = Math.max(0, Math.min(this.config.numSteps - 1, step));
    if (
      this.config.timelineSliderUuid
      && (force || clampedStep !== this.lastLocalSliderStep)
    ) {
      this.lastLocalSliderStep = clampedStep;
      this.pushMessages([
        {
          type: "GuiUpdateMessage",
          uuid: this.config.timelineSliderUuid,
          updates: { value: clampedStep },
        },
      ]);
    }
  }

  private sendTimestepToServer(step: number, force = false): void {
    const clampedStep = Math.max(0, Math.min(this.config.numSteps - 1, step));
    if (!this.config.timestepSyncUuid) {
      return;
    }
    if (!force && clampedStep === this.lastSyncedStep) {
      return;
    }
    this.lastSyncedStep = clampedStep;
    this.sendGuiUpdate(this.config.timestepSyncUuid, clampedStep);
  }

  private syncTimestepToServer(step: number, force = false): void {
    this.syncTimelineSlider(step, force);
    this.sendTimestepToServer(step, force);
  }

  private syncAdvancedTimesteps(
    previousStep: number,
    nextStep: number,
    forceFinal = false,
  ): void {
    const previousDiscrete = Math.floor(previousStep);
    const nextDiscrete = Math.floor(nextStep);
    this.syncTimelineSlider(nextDiscrete, forceFinal);
    if (nextDiscrete === previousDiscrete) {
      if (forceFinal) {
        this.sendTimestepToServer(nextDiscrete, true);
      }
      return;
    }
    if (nextDiscrete > previousDiscrete) {
      for (let step = previousDiscrete + 1; step <= nextDiscrete; step += 1) {
        this.sendTimestepToServer(step);
      }
      return;
    }
    for (let step = previousDiscrete + 1; step < this.config.numSteps; step += 1) {
      this.sendTimestepToServer(step);
    }
    for (let step = 0; step <= nextDiscrete; step += 1) {
      this.sendTimestepToServer(step);
    }
  }

  private resetTimelineState(targetStep = 0): void {
    debugState.push("runtime.reset_timeline_state", {
      currentStep: this.currentStep,
      appliedStep: this.appliedStep,
      playing: this.playing,
    });
    const epoch = ++this.resetEpoch;
    this.resetTargetStep = targetStep;
    this.pushMessages(
      Array.from(this.timelineNodeNames).map((name) => ({
        type: "RemoveSceneNodeMessage",
        name,
      })),
    );
    this.audio.resetTimeline();
    this.appliedStep = -1;
    getWindow().requestAnimationFrame(() => {
      if (epoch !== this.resetEpoch) {
        return;
      }
      // Recreate nodes in a separate frame so reused names remount cleanly.
      for (const [name, messages] of this.baselineByName.entries()) {
        this.timelineNodeNames.add(name);
        this.pushMessages(messages);
      }
      getWindow().requestAnimationFrame(() => {
        if (epoch !== this.resetEpoch) {
          return;
        }
        const targetStep = this.resetTargetStep ?? 0;
        this.resetTargetStep = null;
        if (targetStep >= 0) {
          this.applyThrough(targetStep);
        }
      });
    });
  }

  private applyThrough(step: number): void {
    if (this.resetTargetStep !== null) {
      this.resetTargetStep = step;
      return;
    }
    if (step < this.appliedStep) {
      this.resetTimelineState(step);
      return;
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

  refresh(): void {
    this.resetTimelineState(Math.floor(this.currentStep));
  }

  private tick(timestamp: number): void {
    if (!this.playing) {
      return;
    }
    const previousStep = this.currentStep;
    const next = this.getTransportStep(timestamp);
    if (next >= this.config.numSteps) {
      if (!this.config.loop) {
        this.currentStep = this.config.numSteps - 1;
        this.playing = false;
        this.audio.pause(this.currentStep, this.config.fps);
        this.syncAdvancedTimesteps(previousStep, this.currentStep, true);
        this.syncPlaybackButtons();
        return;
      }
      this.anchorTransport(0, timestamp);
      this.resetTimelineState();
      this.applyThrough(0);
      this.audio.play(0, this.config.fps);
      this.syncAdvancedTimesteps(previousStep, 0, true);
    } else {
      this.currentStep = next;
      this.applyThrough(Math.floor(this.currentStep));
      this.syncAdvancedTimesteps(previousStep, this.currentStep);
    }
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
    this.syncPlaybackButtons();
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
    this.syncPlaybackButtons();
  }
}

function getPlaybackMessageTime(message: RuntimeMessage): number | null {
  const playbackTime = message.__viserPlaybackTime;
  return typeof playbackTime === "number" ? playbackTime : null;
}
