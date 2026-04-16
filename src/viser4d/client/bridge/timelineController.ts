import type { RuntimeMessage, RuntimeValue } from "../binary";
import { AudioRuntime } from "../audio/runtime";
import { isAudioMessage } from "../audio/messages";
import { BlockCache } from "./blockCache";
import {
  isRuntimeControlMessage,
  type RuntimeApplyMessageUpdateMessage,
  type RuntimeConfigureMessage,
  type RuntimeControlMessage,
  type RuntimeEventMessage,
  type RuntimeEvictBlockMessage,
  type RuntimeLoadBlockMessage,
  type RuntimePatchBlockMessage,
  type RuntimePlayMessage,
  type RuntimeSeekMessage,
  type RuntimeSetSpeedMessage,
} from "./generatedRuntimeMessages";
import { PlaybackEngine } from "./playbackEngine";
import { SceneApplicator } from "./sceneApplicator";
import { type GuiUpdateMessage, type RuntimeConfig } from "./protocol";

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
    blockSize: 32,
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
      type: "RuntimeBlockRequestMessage",
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
    this.sendRuntimeEvent({ type: "RuntimeReadyMessage" });
  }

  handleQueuedMessage(message: RuntimeMessage): boolean {
    if (!isRuntimeControlMessage(message)) {
      return false;
    }
    this.handleRuntimeMessage(message);
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

  private configure(message: RuntimeConfigureMessage): void {
    this.config = {
      ...this.config,
      numSteps: message.numSteps,
      blockSize: message.blockSize,
      timelineFps: message.timelineFps,
      speed: message.speed,
      loop: message.loop,
      timelineSliderUuid: message.timelineSliderUuid,
      speedSliderUuid: message.speedSliderUuid,
      stepButtonsUuid: message.stepButtonsUuid,
      playButtonUuid: message.playButtonUuid,
      pauseButtonUuid: message.pauseButtonUuid,
    };
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

  private loadBlock(message: RuntimeLoadBlockMessage): void {
    const currentStep = this.currentStep();
    this.blocks.loadBlock(message.block, {
      checkpointMessages: message.checkpointMessages,
      stepMessages: message.stepMessages,
    });
    const activeBlock = this.blocks.blockIndexOf(currentStep);
    const shouldRebuildCurrentBlock =
      message.block === activeBlock && this.scene.appliedBlock === activeBlock;
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

  private evictBlock(message: RuntimeEvictBlockMessage): void {
    this.blocks.evictBlock(message.block, this.scene.appliedBlock);
  }

  private patchBlock(message: RuntimePatchBlockMessage): void {
    const activeStep = this.currentStep();
    const activeBlock = this.blocks.blockIndexOf(activeStep);
    const shouldRebuildCurrentBlock =
      message.block === activeBlock &&
      this.scene.appliedBlock === activeBlock &&
      this.patchTouchesAppliedState(activeStep, message);
    const patched = this.blocks.patchBlock(message.block, {
      checkpointMessages: message.checkpointMessages,
      stepDeltas: message.stepDeltas,
    });
    if (!patched || !shouldRebuildCurrentBlock) {
      return;
    }
    this.scene.rebuildThrough(activeStep);
  }

  private seek(message: RuntimeSeekMessage): void {
    this.engine.seek({ step: message.step });
  }

  private refresh(): void {
    this.engine.refresh();
  }

  private play(message: RuntimePlayMessage): void {
    this.engine.play({ speed: message.speed, loop: message.loop });
  }

  private pause(): void {
    this.engine.pause();
  }

  private setSpeed(message: RuntimeSetSpeedMessage): void {
    this.engine.setSpeed({ speed: message.speed, loop: message.loop });
  }

  private applyMessageUpdate(message: RuntimeApplyMessageUpdateMessage): void {
    const currentStep = this.currentStep();
    const updatedMessage = message.message;
    const name = typeof updatedMessage.name === "string" ? updatedMessage.name : null;
    debugState.push("runtime.apply_message_update", {
      type: updatedMessage.type,
      name,
      step: currentStep,
    });
    if (isAudioMessage(updatedMessage)) {
      this.audio.applyLiveMessages(currentStep, [updatedMessage]);
      return;
    }
    this.io.pushMessages([updatedMessage]);
  }

  private handleRuntimeMessage(message: RuntimeControlMessage): void {
    switch (message.type) {
      case "RuntimeConfigureMessage":
        this.configure(message);
        return;
      case "RuntimeClearMessage":
        this.clear();
        return;
      case "RuntimeLoadBlockMessage":
        this.loadBlock(message);
        return;
      case "RuntimePatchBlockMessage":
        this.patchBlock(message);
        return;
      case "RuntimeEvictBlockMessage":
        this.evictBlock(message);
        return;
      case "RuntimeSeekMessage":
        this.seek(message);
        return;
      case "RuntimeRefreshMessage":
        this.refresh();
        return;
      case "RuntimePlayMessage":
        this.play(message);
        return;
      case "RuntimePauseMessage":
        this.pause();
        return;
      case "RuntimeSetSpeedMessage":
        this.setSpeed(message);
        return;
      case "RuntimeApplyMessageUpdateMessage":
        this.applyMessageUpdate(message);
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
      type: "RuntimeTimestepMessage",
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

  private patchTouchesAppliedState(
    activeStep: number,
    message: RuntimePatchBlockMessage,
  ): boolean {
    if (message.checkpointMessages !== null) {
      return true;
    }
    const blockStart = this.blocks.blockStartStep(message.block);
    return message.stepDeltas.some(
      (stepDelta) => blockStart + stepDelta.offset <= activeStep,
    );
  }

  private sendSpeedToServer(speed: number): void {
    this.sendRuntimeEvent({
      type: "RuntimeSpeedMessage",
      speed,
    });
  }

  private sendPlaybackStateToServer(isPlaying: boolean): void {
    this.sendRuntimeEvent({
      type: "RuntimePlaybackStateMessage",
      isPlaying,
    });
  }
}
