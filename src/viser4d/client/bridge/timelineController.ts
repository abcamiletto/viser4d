import type { RuntimeMessage, RuntimeValue } from "../binary";
import { AudioRuntime } from "../audio/runtime";
import { isAudioMessage } from "../audio/messages";
import { BlockCache } from "./blockCache";
import { PlaybackEngine } from "./playbackEngine";
import { SceneApplicator } from "./sceneApplicator";
import { type GuiUpdateMessage, type RuntimeConfig } from "./protocol";

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

type TimelineControllerIO = {
  pushMessages(messages: RuntimeMessage[]): void;
  sendRuntimeEvent(message: RuntimeEventMessage): void;
  updateGuiVisible(uuid: string, visible: boolean): void;
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

export class TimelineController {
  readonly debug = debugState;

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
    (messages) => this.io.pushMessages(messages),
    this.audio,
    this.blocks,
    new Map<string, string>(),
  );
  private readonly engine: PlaybackEngine = new PlaybackEngine(
    this.config,
    this.scene,
    this.blocks,
    this.audio,
    {
      syncAdvancedTimesteps: (previousStep, nextStep, forceFinal) =>
        this.syncAdvancedTimesteps(previousStep, nextStep, forceFinal),
      syncTimestepToServer: (step, force) =>
        this.syncTimestepToServer(step, force),
      syncPlaybackButtons: () => this.syncPlaybackButtons(),
      sendPlaybackStateToServer: (isPlaying) =>
        this.sendPlaybackStateToServer(isPlaying),
      sendSpeedToServer: (speed) => this.sendSpeedToServer(speed),
    },
  );

  constructor(private io: TimelineControllerIO) {}

  dispose(): void {
    this.engine.dispose();
    this.audio.reset();
  }

  onViewerReady(isWebsocket: boolean): void {
    if (!isWebsocket) {
      return;
    }
    this.sendRuntimeEvent({ type: "Viser4dRuntimeEventMessage", event: "ready" });
  }

  handleQueuedMessage(message: RuntimeMessage): boolean {
    if (message.type !== "Viser4dRuntimeMessage") {
      return false;
    }
    this.handleRuntimeMessage(message as RuntimeControlMessage);
    return true;
  }

  handleGuiMessage(message: GuiUpdateMessage): boolean {
    const { uuid } = message;
    const value = message.updates.value;
    if (uuid === this.config.timelineSliderUuid) {
      const step = Number(value);
      if (!Number.isFinite(step)) {
        return true;
      }
      this.engine.seek({ step });
      return true;
    }
    if (uuid === this.config.speedSliderUuid) {
      const speed = Number(value);
      if (!Number.isFinite(speed)) {
        return true;
      }
      this.engine.setSpeed({ speed, loop: this.config.loop });
      return true;
    }
    if (uuid === this.config.stepButtonsUuid) {
      const currentStep = this.currentStep();
      if (value === "Prev") {
        this.engine.seek({ step: currentStep - 1 });
      } else if (value === "Next") {
        this.engine.seek({ step: currentStep + 1 });
      }
      return true;
    }
    if (uuid === this.config.playButtonUuid) {
      const isAtEnd = this.currentStep() === this.config.numSteps - 1;
      if (!this.config.loop && isAtEnd) {
        this.engine.seek({ step: 0 });
      }
      this.engine.play({ speed: this.config.speed, loop: this.config.loop });
      return true;
    }
    if (uuid === this.config.pauseButtonUuid) {
      this.engine.pause();
      return true;
    }
    return false;
  }

  private configure(config: Partial<RuntimeConfig>): void {
    this.config = { ...this.config, ...config };
    this.blocks.blockSize = this.config.blockSize;
    this.engine.updateConfig(this.config);
    this.audio.setStepRate(this.config.timelineFps);
    debugState.push("runtime.configure", this.config);
    this.engine.syncAudioTransport();
    this.syncPlaybackButtons();
  }

  private clear(): void {
    this.engine.dispose();
    this.blocks.reset();
    this.scene.resetState();
    this.audio.reset();
    this.lastLocalSliderStep = -1;
    this.lastSyncedStep = -1;
    debugState.push("runtime.clear", null);
  }

  private loadBlock(payload: {
    block: number;
    checkpointMessages: RuntimeMessage[];
    stepMessages: RuntimeMessage[][];
  }): void {
    const currentStep = this.currentStep();
    this.blocks.loadBlock(payload.block, {
      checkpointMessages: payload.checkpointMessages,
      stepMessages: payload.stepMessages,
    });
    const activeBlock = this.blocks.blockIndexOf(currentStep);
    const shouldRebuildCurrentBlock =
      payload.block === activeBlock && this.scene.appliedBlock === activeBlock;
    if (shouldRebuildCurrentBlock) {
      this.scene.rebuildThrough(currentStep);
      return;
    }
    const pendingStep = this.blocks.pendingStep;
    if (pendingStep === null) {
      return;
    }
    const pendingBlock = this.blocks.getBlock(pendingStep);
    if (!pendingBlock) {
      return;
    }
    this.blocks.pendingStep = null;
    this.engine.seek({ step: pendingStep });
  }

  private evictBlock(payload: { block: number }): void {
    this.blocks.evictBlock(payload.block, this.scene.appliedBlock);
  }

  private seek(payload: { step: number }): void {
    this.engine.seek(payload);
  }

  private refresh(): void {
    this.engine.refresh();
  }

  private play(payload: { speed: number; loop: boolean }): void {
    this.engine.play(payload);
  }

  private pause(): void {
    this.engine.pause();
  }

  private setSpeed(payload: { speed: number; loop: boolean }): void {
    this.engine.setSpeed(payload);
  }

  private applyMessageUpdate(message: RuntimeMessage): void {
    const currentStep = this.currentStep();
    const name = typeof message.name === "string" ? message.name : null;
    debugState.push("runtime.apply_message_update", {
      type: message.type,
      name,
      step: currentStep,
    });
    if (isAudioMessage(message)) {
      this.audio.applyLiveMessages(currentStep, [message]);
      return;
    }
    this.io.pushMessages([message]);
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

  private sendRuntimeEvent(message: RuntimeEventMessage): void {
    this.io.sendRuntimeEvent(message);
  }

  private syncPlaybackButtons(): void {
    const sync = (uuid: string | null, visible: boolean): void => {
      if (!uuid) {
        return;
      }
      this.io.updateGuiVisible(uuid, visible);
    };
    sync(this.config.playButtonUuid, !this.engine.playing);
    sync(this.config.pauseButtonUuid, this.engine.playing);
  }

  private syncTimelineSlider(step: number, force = false): void {
    const sliderUuid = this.config.timelineSliderUuid;
    if (!sliderUuid) {
      return;
    }
    const clampedStep = this.clampStep(step);
    const stepChanged = clampedStep !== this.lastLocalSliderStep;
    if (!force && !stepChanged) {
      return;
    }
    this.lastLocalSliderStep = clampedStep;
    this.io.pushMessages([
      {
        type: "GuiUpdateMessage",
        uuid: sliderUuid,
        updates: { value: clampedStep },
      },
    ]);
  }

  private sendTimestepToServer(step: number, force = false): void {
    const clampedStep = this.clampStep(step);
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
    const numSteps = this.config.numSteps;
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
    for (let step = previousDiscrete + 1; step < numSteps; step += 1) {
      this.sendTimestepToServer(step);
    }
    for (let step = 0; step <= nextDiscrete; step += 1) {
      this.sendTimestepToServer(step);
    }
  }

  private clampStep(step: number): number {
    return Math.max(0, Math.min(this.config.numSteps - 1, step));
  }

  private currentStep(): number {
    return Math.floor(this.engine.currentStep);
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
