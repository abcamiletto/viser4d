import {
  type RuntimeMessage,
  type RuntimeValue,
} from "../binary";
import { AudioRuntime } from "../audio/runtime";
import { isAudioMessage, type AudioMessage } from "../audio/messages";
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

type RuntimeControlMessage =
  | {
      type: "Viser4dRuntimeMessage";
      method: "clear";
      payload: null;
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "configure";
      payload: Partial<RuntimeConfig>;
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "loadBlock";
      payload: {
        block: number;
        checkpointMessages: RuntimeMessage[];
        stepMessages: RuntimeMessage[][];
      };
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "evictBlock";
      payload: { block: number };
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "seek";
      payload: { step: number };
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "refresh";
      payload: null;
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "play";
      payload: { speed: number; loop: boolean };
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "pause";
      payload: null;
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "setSpeed";
      payload: { speed: number; loop: boolean };
    }
  | {
      type: "Viser4dRuntimeMessage";
      method: "applyMessageUpdate";
      payload: RuntimeMessage;
    };

type RuntimeEventMessage =
  | {
      type: "Viser4dRuntimeEventMessage";
      event: "blockRequest";
      step: number;
    }
  | {
      type: "Viser4dRuntimeEventMessage";
      event: "timestep";
      step: number;
    }
  | {
      type: "Viser4dRuntimeEventMessage";
      event: "speed";
      speed: number;
    }
  | {
      type: "Viser4dRuntimeEventMessage";
      event: "playbackState";
      isPlaying: boolean;
    }
  | {
      type: "Viser4dRuntimeEventMessage";
      event: "ready";
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
    timelineSliderUuid: null,
    speedSliderUuid: null,
    stepButtonsUuid: null,
    playButtonUuid: null,
    pauseButtonUuid: null,
  };

  private lastLocalSliderStep = -1;
  private lastSyncedStep = -1;

  private readonly audio: AudioRuntime = new AudioRuntime(
    () => this.engine.getTransportStep(),
    (event, payload) => debugState.push(event, payload),
  );
  private readonly blocks: BlockCache = new BlockCache((step) =>
    this.sendRuntimeEvent({
      type: "Viser4dRuntimeEventMessage",
      event: "blockRequest",
      step,
    }),
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
  private pendingPlaybackAudioMessages: AudioMessage[] = [];
  private playbackSyncScheduled = false;

  // Browser integration state
  private disposed = false;
  private undoQueueIngress: (() => void) | null = null;
  private undoGuiMessageInterceptor: (() => void) | null = null;
  private queuePush: ((...messages: RuntimeMessage[]) => number) | null = null;
  private playbackMonitor: MutationObserver | null = null;

  constructor() {
    this.playbackAudio.setStepRate(1);
    this.installWhenReady();
  }

  get appliedStep(): number {
    return this.scene.appliedStep;
  }

  dispose(): void {
    this.disposed = true;
    this.undoQueueIngress?.();
    this.undoQueueIngress = null;
    this.undoGuiMessageInterceptor?.();
    this.undoGuiMessageInterceptor = null;
    this.playbackMonitor?.disconnect();
    this.playbackMonitor = null;
    this.engine.dispose();
    this.audio.reset();
    this.playbackAudio.reset();
    this.viewer = null;
    this.playbackTimeSlider = null;
    this.queuePush = null;
  }

  configure(config: Partial<RuntimeConfig>): void {
    this.config = { ...this.config, ...config };
    this.blocks.blockSize = this.config.blockSize;
    this.engine.updateConfig(this.config);
    this.audio.setStepRate(this.config.timelineFps);
    debugState.push("runtime.configure", this.config);
    this.engine.syncAudioTransport();
    this.syncPlaybackButtons();
  }

  clear(): void {
    this.engine.dispose();
    this.blocks.reset();
    this.scene.resetState();
    this.audio.reset();
    this.lastLocalSliderStep = -1;
    this.lastSyncedStep = -1;
    debugState.push("runtime.clear", null);
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
    if (this.disposed) {
      return;
    }
    try {
      this.configureQueueIngress();
      this.installGuiMessageInterceptor();
      if (this.getViewer().messageSource !== "websocket") {
        this.installPlaybackMonitor();
      }
      this.announceRuntimeReady();
    } catch {
      if (!this.disposed) {
        getWindow().requestAnimationFrame(() => this.installWhenReady());
      }
    }
  }

  private configureQueueIngress(): void {
    if (this.undoQueueIngress) {
      return;
    }
    const queue = this.getViewer().mutable.current.messageQueue;
    const originalPush = queue.push.bind(queue);
    const wrappedPush = (...messages: RuntimeMessage[]): number => {
      const forwarded: RuntimeMessage[] = [];
      for (const message of messages) {
        if (this.handleQueuedMessage(message)) {
          continue;
        }
        forwarded.push(message);
      }
      return originalPush(...forwarded);
    };
    this.queuePush = originalPush;
    queue.push = wrappedPush;
    this.undoQueueIngress = () => {
      if (queue.push === wrappedPush) {
        queue.push = originalPush;
      }
    };
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
    if (this.undoGuiMessageInterceptor) {
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
    this.undoGuiMessageInterceptor = () => {
      Object.defineProperty(mutable, "sendMessage", {
        configurable: true,
        enumerable: true,
        writable: true,
        value: rawSendMessage,
      });
    };
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
    if (message.type === "Viser4dRuntimeMessage") {
      this.handleRuntimeMessage(message as RuntimeControlMessage);
      return true;
    }
    if (!isAudioMessage(message)) {
      return false;
    }
    if (this.getViewer().messageSource === "websocket") {
      return false;
    }
    this.pendingPlaybackAudioMessages.push(message);
    this.schedulePlaybackSync();
    return true;
  }

  private handleRuntimeMessage(message: RuntimeControlMessage): void {
    switch (message.method) {
      case "configure":
        this.configure(message.payload);
        return;
      case "clear":
        this.clear();
        return;
      case "loadBlock":
        this.loadBlock(message.payload);
        return;
      case "evictBlock":
        this.evictBlock(message.payload);
        return;
      case "seek":
        this.seek(message.payload);
        return;
      case "refresh":
        this.refresh();
        return;
      case "play":
        this.play(message.payload);
        return;
      case "pause":
        this.pause();
        return;
      case "setSpeed":
        this.setSpeed(message.payload);
        return;
      case "applyMessageUpdate":
        this.applyMessageUpdate(message.payload);
        return;
    }
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
      this.flushPendingPlaybackAudioMessages(nextTime);
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
    this.flushPendingPlaybackAudioMessages(nextTime);
  }

  private resetPlaybackAudio(): void {
    this.playbackAudio.resetTimeline();
  }

  private flushPendingPlaybackAudioMessages(playbackTime: number): void {
    if (this.pendingPlaybackAudioMessages.length === 0) {
      return;
    }
    this.playbackAudio.applyLiveMessages(
      playbackTime,
      this.pendingPlaybackAudioMessages,
    );
    this.pendingPlaybackAudioMessages = [];
  }

  private schedulePlaybackSync(): void {
    if (this.playbackSyncScheduled) {
      return;
    }
    this.playbackSyncScheduled = true;
    getWindow().requestAnimationFrame(() => {
      this.playbackSyncScheduled = false;
      this.syncPlaybackState();
    });
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

  private announceRuntimeReady(): void {
    if (this.getViewer().messageSource !== "websocket") {
      return;
    }
    this.sendRuntimeEvent({ type: "Viser4dRuntimeEventMessage", event: "ready" });
  }

  private pushMessages(messages: RuntimeMessage[]): void {
    if (this.queuePush) {
      this.queuePush(...messages);
      return;
    }
    this.getViewer().mutable.current.messageQueue.push(...messages);
  }

  private sendRuntimeEvent(message: RuntimeEventMessage): void {
    this.getViewer().mutable.current.sendMessage(message);
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
    if (!force && clampedStep === this.lastSyncedStep) {
      return;
    }
    this.lastSyncedStep = clampedStep;
    this.sendRuntimeEvent({
      type: "Viser4dRuntimeEventMessage",
      event: "timestep",
      step: clampedStep,
    });
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
    this.sendRuntimeEvent({
      type: "Viser4dRuntimeEventMessage",
      event: "speed",
      speed,
    });
  }

  private sendPlaybackStateToServer(isPlaying: boolean): void {
    this.sendRuntimeEvent({
      type: "Viser4dRuntimeEventMessage",
      event: "playbackState",
      isPlaying,
    });
  }
}
