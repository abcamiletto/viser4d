// Orchestration: dispatch timeline control messages, own state/cache/player/
// audio/ui, and send events back. Full-block replacement + rev-diff live here.

import type { ScenePayload } from "./binary";
import { AudioEngine, type AudioTransport } from "./audio";
import { BlockCache } from "./cache";
import { Player } from "./player";
import {
  applyOverrideEntry,
  decodeBlock,
  foldTarget,
  SceneMirror,
  type LoadedBlock,
  type SceneEntry,
} from "./state";
import { PlaybackBar } from "./ui";
import type {
  TimelineControlMessage,
  TimelineConfigureMessage,
  TimelineBlockMessage,
  TimelineOverrideMessage,
  TimelineEventMessage,
} from "./protocol.gen";

export type ControllerIO = {
  pushMessages(messages: ScenePayload[]): void;
  sendEvent(message: TimelineEventMessage): void;
  isWebsocket(): boolean;
};

export class Controller {
  private readonly scene = new SceneMirror();
  private readonly overlay = new Map<string, SceneEntry>();
  private readonly cache: BlockCache;
  private readonly player: Player;
  private readonly audio: AudioEngine;
  private ui: PlaybackBar | null = null;

  private numSteps = 1;
  private timelineFps = 30;
  private appliedStep = -1;
  private appliedBlock = -1;
  private reportedStep = -1;
  private focusBlock = -1;
  private pendingStep: number | null = null; // step whose block we are waiting for

  constructor(private readonly io: ControllerIO) {
    this.cache = new BlockCache({
      requestBlock: (index) => io.sendEvent({ type: "TimelineBlockRequestMessage", index }),
      discardBlock: (index) => io.sendEvent({ type: "TimelineBlockDiscardMessage", index }),
    });
    this.player = new Player({
      step: (step, continuous) => this.applyStep(step, continuous),
      transport: () => this.onTransport(),
    });
    const transport: AudioTransport = {
      getStep: () => this.player.getTransportStep(),
      isPlaying: () => this.player.playing,
      speed: () => this.player.speed,
    };
    this.audio = new AudioEngine(transport, this.timelineFps);
  }

  /** Called once the viewer is located, in websocket mode. */
  start(): void {
    this.ui = new PlaybackBar({
      play: () => this.player.play(),
      pause: () => this.player.pause(),
      prev: () => this.player.seek(this.player.currentStep - 1),
      next: () => this.player.seek(this.player.currentStep + 1),
      seek: (step) => this.player.seek(step),
      setSpeed: (speed) => this.player.setSpeed(speed, this.player.loop),
      setLoop: (loop) => this.player.setSpeed(this.player.speed, loop),
    });
    this.ui.mount();
    this.updateUi();
    this.io.sendEvent({ type: "TimelineReadyMessage" });
  }

  dispose(): void {
    this.player.dispose();
    this.audio.reset();
    this.ui?.dispose();
    this.ui = null;
  }

  debug(): unknown {
    return {
      numSteps: this.numSteps,
      currentStep: this.player.currentStep,
      appliedStep: this.appliedStep,
      appliedBlock: this.appliedBlock,
      playing: this.player.playing,
    };
  }

  handleControl(message: TimelineControlMessage): void {
    switch (message.type) {
      case "TimelineConfigureMessage":
        return this.configure(message);
      case "TimelineBlockBytesMessage":
        this.cache.setBlockBytes(message.blockBytes);
        return this.refocusPreload(this.player.currentStep, true);
      case "TimelineBlockMessage":
        return this.loadBlock(message);
      case "TimelineOverrideMessage":
        return this.applyOverride(message);
      case "TimelineSeekMessage":
        return this.player.seek(message.step);
      case "TimelinePlayMessage":
        return this.player.play(message.speed, message.loop);
      case "TimelinePauseMessage":
        return this.player.pause();
      case "TimelineSetSpeedMessage":
        return this.player.setSpeed(message.speed, message.loop);
      case "TimelineClearMessage":
        return this.clear();
      case "TimelineRefreshMessage":
        return this.player.refresh();
    }
  }

  private configure(message: TimelineConfigureMessage): void {
    this.numSteps = message.numSteps;
    this.timelineFps = message.timelineFps;
    this.cache.blockSize = message.blockSize;
    this.cache.setBudgetBytes(message.cacheBytes);
    this.cache.setBlockBytes(message.blockBytes);
    this.audio.setStepRate(message.timelineFps);
    this.player.configure(message.numSteps, message.timelineFps, message.speed, message.loop);
    this.updateUi();
    this.refocusPreload(this.player.currentStep, true);
    this.applyStep(this.player.currentStep, false);
  }

  private loadBlock(message: TimelineBlockMessage): void {
    this.cache.loadBlock(decodeBlock(message));
    const step = this.player.currentStep;
    // Re-derive if this block holds the current step (first load or a full-block
    // replacement); otherwise resume the step that was waiting on it.
    const target = this.cache.blockIndexOf(step) === message.index ? step : this.pendingStep;
    if (target !== null && this.cache.blockIndexOf(target) === message.index) {
      this.pendingStep = null;
      this.applyStep(target, false);
    }
    this.refocusPreload(step, true);
  }

  private applyOverride(message: TimelineOverrideMessage): void {
    applyOverrideEntry(this.overlay, message.entry);
    // reapplyOverrides removes tombstoned nodes from the applied scene and
    // pushes rev-changed puts, so receipt takes effect immediately.
    this.io.pushMessages(this.scene.reapplyOverrides(this.overlay));
  }

  private clear(): void {
    this.io.pushMessages(this.scene.rebuild(new Map()));
    this.player.dispose();
    this.cache.reset();
    this.scene.reset();
    this.audio.reset();
    this.overlay.clear();
    this.appliedStep = -1;
    this.appliedBlock = -1;
    this.reportedStep = -1;
    this.focusBlock = -1;
    this.pendingStep = null;
    this.updateUi();
  }

  private applyStep(step: number, continuous: boolean): void {
    const block = this.cache.getBlock(step);
    if (!block) {
      this.pendingStep = step;
      this.cache.ensureStepLoaded(step); // resumes from loadBlock()
      return;
    }
    const offset = step - this.cache.blockStartStep(block.index);
    const delta = block.deltas[offset];
    // The delta path needs the step right after the applied one, reached by
    // playback, within the applied block or at the start of the next one.
    const forward =
      continuous &&
      this.appliedStep >= 0 &&
      step === this.appliedStep + 1 &&
      !!delta &&
      (block.index === this.appliedBlock ||
        (offset === 0 && block.index === this.appliedBlock + 1));

    if (forward) {
      const messages = this.scene.advance(delta);
      messages.push(...this.scene.reapplyOverrides(this.overlay));
      this.io.pushMessages(messages);
      this.audio.applyEvents(step, delta.audio);
    } else {
      this.io.pushMessages(this.scene.rebuild(foldTarget(block, offset, this.overlay)));
      this.loadAudioThrough(block, offset);
      this.audio.reschedule();
    }

    this.appliedStep = step;
    this.appliedBlock = block.index;
    this.reportTimestep(step);
    this.updateUi();
    this.refocusPreload(step, false);
  }

  private loadAudioThrough(block: LoadedBlock, offset: number): void {
    // The checkpoint is the state before the block's first delta, so fold
    // audio events from deltas[0..offset] inclusive.
    this.audio.loadCheckpoint(block.checkpointAudio);
    const start = this.cache.blockStartStep(block.index);
    for (let i = 0; i <= offset; i += 1) {
      const delta = block.deltas[i];
      if (delta) {
        this.audio.applyEvents(start + i, delta.audio);
      }
    }
  }

  private onTransport(): void {
    this.audio.reschedule();
    this.updateUi();
    if (this.io.isWebsocket()) {
      this.io.sendEvent({ type: "TimelinePlaybackStateMessage", isPlaying: this.player.playing });
      this.io.sendEvent({ type: "TimelineSpeedMessage", speed: this.player.speed });
      this.reportTimestep(this.player.currentStep);
    }
  }

  private reportTimestep(step: number): void {
    if (step === this.reportedStep || !this.io.isWebsocket()) {
      return;
    }
    this.reportedStep = step;
    this.io.sendEvent({ type: "TimelineTimestepMessage", step });
  }

  private refocusPreload(step: number, force: boolean): void {
    const block = this.cache.blockIndexOf(step);
    if (!force && block === this.focusBlock) {
      return;
    }
    this.focusBlock = block;
    this.cache.syncFocus(block, this.appliedBlock);
  }

  private updateUi(): void {
    this.ui?.setState({
      playing: this.player.playing,
      step: this.player.currentStep,
      total: this.numSteps,
      speed: this.player.speed,
      loop: this.player.loop,
    });
  }
}
