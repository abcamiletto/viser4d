import {
  decodeHybridPayloadBase64,
  type RuntimeMessage,
  type RuntimeValue,
  normalizeTransportMessage,
} from "../binary";
import { AudioRuntime } from "../audio/runtime";
import { isAudioMessage } from "../audio/messages";
import { BlockCache } from "./blockCache";
import { SceneApplicator } from "./sceneApplicator";
import { PlaybackEngine } from "./playbackEngine";
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
  readonly debug = debugState;

  private viewer: ViewerLike | null = null;
  private playbackTimeSlider: Element | null = null;
  private config: RuntimeConfig = {
    numSteps: 1,
    blockSize: 64,
    timelineFps: 30,
    speed: 1,
    loop: false,
    blockRequestSyncUuid: null,
    timelineSliderUuid: null,
    speedSliderUuid: null,
    stepButtonsUuid: null,
    playButtonUuid: null,
    pauseButtonUuid: null,
    speedSyncUuid: null,
    playbackStateSyncUuid: null,
    timestepSyncUuid: null,
  };

  private lastLocalSliderStep = -1;
  private lastSyncedStep = -1;

  private readonly audio: AudioRuntime = new AudioRuntime(
    () => this.engine.getTransportStep(),
    (event, payload) => debugState.push(event, payload),
  );
  private readonly blocks: BlockCache = new BlockCache((uuid, value) =>
    this.sendGuiUpdate(uuid, value),
  );
  private readonly scene: SceneApplicator = new SceneApplicator(
    (messages) => this.pushMessages(messages),
    this.audio,
    this.blocks,
    new Set<string>(),
  );
  private readonly engine: PlaybackEngine = new PlaybackEngine(
    this.config,
    this.scene,
    this.blocks,
    this.audio,
    {
      syncAdvancedTimesteps: (prev, next, force) =>
        this.syncAdvancedTimesteps(prev, next, force),
      syncTimestepToServer: (step, force) =>
        this.syncTimestepToServer(step, force),
      syncPlaybackButtons: () => this.syncPlaybackButtons(),
      sendPlaybackStateToServer: (p) => this.sendPlaybackStateToServer(p),
      sendSpeedToServer: (s) => this.sendSpeedToServer(s),
    },
  );

  // File/embed playback audio
  private readonly playbackAudio = new AudioRuntime(
    () => this.playbackTime,
    (event, payload) => debugState.push(event, payload),
  );
  private playbackTime = 0;
  private playbackPlaying = false;
  private playbackObserved = false;
  private playbackLastAppliedMessageTime = -1;

  // Browser integration state
  private queueIngressConfigured = false;
  private guiMessageInterceptorInstalled = false;
  private queuePush: ((...messages: RuntimeMessage[]) => number) | null = null;
  private playbackMonitor: MutationObserver | null = null;

  constructor() {
    this.playbackAudio.setStepRate(1);
    this.installWhenReady();
  }

  get appliedStep(): number {
    return this.scene.appliedStep;
  }

  // --- Public API (called from Python runtime messages) ---

  invokeRuntimeCall(method: string, encodedPayload: string): void {
    const target = this[method as keyof TimelineRuntime];
    if (typeof target !== "function") {
      throw new Error(`[viser4d] Unknown runtime method: ${method}`);
    }
    const payload = decodeHybridPayloadBase64<Record<string, RuntimeValue>>(
      encodedPayload,
    );
    (target as (payload: Record<string, RuntimeValue>) => void).call(this, payload);
  }

  configure(config: Partial<RuntimeConfig>): void {
    this.config = { ...this.config, ...config };
    this.blocks.blockSize = this.config.blockSize;
    this.blocks.blockRequestSyncUuid = this.config.blockRequestSyncUuid;
    this.engine.updateConfig(this.config);
    this.audio.setStepRate(this.config.timelineFps);
    debugState.push("runtime.configure", this.config);
    this.engine.syncAudioTransport();
    this.syncPlaybackButtons();
  }

  loadBlock(payload: {
    block: number;
    checkpointMessages: RuntimeMessage[];
    stepMessages: RuntimeMessage[][];
  }): void {
    this.blocks.loadBlock(payload.block, {
      checkpointMessages: payload.checkpointMessages,
      stepMessages: payload.stepMessages,
    });
    const activeBlock = this.blocks.blockIndexOf(
      Math.floor(this.engine.currentStep),
    );
    if (
      payload.block === activeBlock &&
      this.scene.appliedBlock === activeBlock
    ) {
      this.scene.rebuildThrough(Math.floor(this.engine.currentStep));
      return;
    }
    if (
      this.blocks.pendingStep !== null &&
      this.blocks.getBlock(this.blocks.pendingStep)
    ) {
      const step = this.blocks.pendingStep;
      this.blocks.pendingStep = null;
      this.engine.seek({ step });
    }
  }

  evictBlock(payload: { block: number }): void {
    this.blocks.evictBlock(payload.block, this.scene.appliedBlock);
  }

  seek(payload: { step: number }): void {
    this.engine.seek(payload);
  }

  refresh(): void {
    this.engine.refresh();
  }

  play(payload: { speed: number; loop: boolean }): void {
    this.engine.play(payload);
  }

  pause(): void {
    this.engine.pause();
  }

  setSpeed(payload: { speed: number; loop: boolean }): void {
    this.engine.setSpeed(payload);
  }

  applyMessageUpdate(message: RuntimeMessage): void {
    const name = typeof message.name === "string" ? message.name : null;
    debugState.push("runtime.apply_message_update", {
      type: message.type,
      name,
      step: Math.floor(this.engine.currentStep),
    });
    if (isAudioMessage(message)) {
      this.audio.applyLiveMessages(Math.floor(this.engine.currentStep), [
        message,
      ]);
      return;
    }
    this.pushMessages([message]);
  }

  // --- Browser integration ---

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
        this.installPlaybackMonitor();
      }
    } catch {
      getWindow().requestAnimationFrame(() => this.installWhenReady());
    }
  }

  private configureQueueIngress(): void {
    if (this.queueIngressConfigured) {
      return;
    }
    const queue = this.getViewer().mutable.current.messageQueue;
    const originalPush = queue.push.bind(queue);
    this.queuePush = originalPush;
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

  private installPlaybackMonitor(): void {
    if (this.playbackMonitor) {
      return;
    }
    const slider = this.getPlaybackTimeSlider();
    if (!slider) {
      throw new Error("[viser4d] Could not find the playback slider.");
    }
    this.playbackMonitor = new MutationObserver(() => {
      this.syncPlaybackState();
    });
    this.playbackMonitor.observe(slider, {
      attributes: true,
      attributeFilter: ["aria-valuenow"],
    });
    this.syncPlaybackState();
  }

  private installGuiMessageInterceptor(): void {
    if (this.guiMessageInterceptorInstalled) {
      return;
    }
    const mutable = this.getViewer().mutable.current;
    let rawSendMessage = mutable.sendMessage;
    const wrappedSendMessage = (message: ViewerMessage): void => {
      if (
        message.type === "GuiUpdateMessage" &&
        this.handleLocalPlaybackGuiMessage(message as GuiUpdateMessage)
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
      this.engine.seek({ step });
      return true;
    }
    if (message.uuid === this.config.speedSliderUuid) {
      const speed = Number(value);
      if (!Number.isFinite(speed)) {
        return true;
      }
      this.engine.setSpeed({ speed, loop: this.config.loop });
      return true;
    }
    if (message.uuid === this.config.stepButtonsUuid) {
      if (value === "Prev") {
        this.engine.seek({ step: Math.floor(this.engine.currentStep) - 1 });
      } else if (value === "Next") {
        this.engine.seek({ step: Math.floor(this.engine.currentStep) + 1 });
      }
      return true;
    }
    if (message.uuid === this.config.playButtonUuid) {
      this.engine.play({ speed: this.config.speed, loop: this.config.loop });
      return true;
    }
    if (message.uuid === this.config.pauseButtonUuid) {
      this.engine.pause();
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
    if (
      playbackTime !== null &&
      playbackTime < this.playbackLastAppliedMessageTime
    ) {
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

  // --- File/embed playback sync ---

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

  // --- Server sync helpers ---

  private pushMessages(messages: RuntimeMessage[]): void {
    if (this.queuePush) {
      this.queuePush(...messages);
      return;
    }
    this.getViewer().mutable.current.messageQueue.push(...messages);
  }

  private sendGuiUpdate(uuid: string, value: RuntimeValue): void {
    this.getViewer().mutable.current.sendMessage({
      type: "GuiUpdateMessage",
      uuid,
      updates: { value },
    });
  }

  private syncPlaybackButtons(): void {
    const viewer = this.getViewer();
    const sync = (uuid: string | null, visible: boolean): void => {
      if (!uuid || viewer.useGuiConfig.get(uuid) === undefined) {
        return;
      }
      viewer.guiActions.updateGuiProps(uuid, { visible });
    };
    sync(this.config.playButtonUuid, !this.engine.playing);
    sync(this.config.pauseButtonUuid, this.engine.playing);
  }

  private syncTimelineSlider(step: number, force = false): void {
    const clampedStep = Math.max(0, Math.min(this.config.numSteps - 1, step));
    if (
      this.config.timelineSliderUuid &&
      (force || clampedStep !== this.lastLocalSliderStep)
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
      for (
        let step = previousDiscrete + 1;
        step <= nextDiscrete;
        step += 1
      ) {
        this.sendTimestepToServer(step);
      }
      return;
    }
    for (
      let step = previousDiscrete + 1;
      step < this.config.numSteps;
      step += 1
    ) {
      this.sendTimestepToServer(step);
    }
    for (let step = 0; step <= nextDiscrete; step += 1) {
      this.sendTimestepToServer(step);
    }
  }

  private sendSpeedToServer(speed: number): void {
    if (this.config.speedSyncUuid) {
      this.sendGuiUpdate(this.config.speedSyncUuid, speed);
    }
  }

  private sendPlaybackStateToServer(isPlaying: boolean): void {
    if (this.config.playbackStateSyncUuid) {
      this.sendGuiUpdate(this.config.playbackStateSyncUuid, isPlaying);
    }
  }
}

function getPlaybackMessageTime(message: RuntimeMessage): number | null {
  const playbackTime = message.__viserPlaybackTime;
  return typeof playbackTime === "number" ? playbackTime : null;
}
