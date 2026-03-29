import { AudioRuntime } from "../audio/runtime";
import { BlockCache } from "./blockCache";
import { SceneApplicator } from "./sceneApplicator";
import type { RuntimeConfig } from "./protocol";
import { getWindow } from "./protocol";

export type PlaybackCallbacks = {
  syncAdvancedTimesteps(
    previousStep: number,
    nextStep: number,
    forceFinal?: boolean,
  ): void;
  syncTimestepToServer(step: number, force?: boolean): void;
  syncPlaybackButtons(): void;
  sendPlaybackStateToServer(isPlaying: boolean): void;
  sendSpeedToServer(speed: number): void;
};

export class PlaybackEngine {
  playing = false;
  currentStep = 0;
  private playStartStep = 0;
  private playStartPerfTime = 0;
  private rafId: number | null = null;

  constructor(
    private config: RuntimeConfig,
    private scene: SceneApplicator,
    private blocks: BlockCache,
    private audio: AudioRuntime,
    private callbacks: PlaybackCallbacks,
  ) {}

  updateConfig(config: RuntimeConfig): void {
    this.config = config;
  }

  getTransportStep(timestamp = performance.now()): number {
    if (!this.playing) {
      return this.currentStep;
    }
    return (
      this.playStartStep +
      ((timestamp - this.playStartPerfTime) / 1000) * this.getPlaybackFps()
    );
  }

  play(payload: { speed: number; loop: boolean }): void {
    const step = this.getTransportStep();
    this.config.speed = payload.speed;
    this.config.loop = payload.loop;
    this.playing = true;
    this.anchorTransport(step);
    this.audio.play(step, this.getPlaybackFps());
    if (this.rafId !== null) {
      getWindow().cancelAnimationFrame(this.rafId);
    }
    this.callbacks.sendSpeedToServer(payload.speed);
    this.callbacks.sendPlaybackStateToServer(true);
    this.callbacks.syncPlaybackButtons();
    this.rafId = getWindow().requestAnimationFrame((ts) => this.tick(ts));
  }

  pause(): void {
    const step = this.getTransportStep();
    this.currentStep = step;
    this.playing = false;
    if (this.rafId !== null) {
      getWindow().cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this.audio.pause(step, this.getPlaybackFps());
    this.callbacks.sendPlaybackStateToServer(false);
    this.callbacks.syncTimestepToServer(Math.floor(this.currentStep), true);
    this.callbacks.syncPlaybackButtons();
  }

  seek(payload: { step: number }): void {
    const step = Math.max(
      0,
      Math.min(this.config.numSteps - 1, payload.step),
    );
    this.currentStep = step;
    if (this.playing) {
      this.anchorTransport(step);
    }
    if (!this.blocks.ensureStepLoaded(step)) {
      return;
    }
    this.scene.applyThrough(step);
    this.audio.seek(step, this.getPlaybackFps(), this.playing);
    this.callbacks.syncTimestepToServer(step, true);
  }

  refresh(): void {
    this.scene.rebuildThrough(Math.floor(this.currentStep));
  }

  setSpeed(payload: { speed: number; loop: boolean }): void {
    const step = this.getTransportStep();
    this.config.speed = payload.speed;
    this.config.loop = payload.loop;
    this.anchorTransport(step);
    this.audio.setFps(step, this.getPlaybackFps(), this.playing);
    this.callbacks.sendSpeedToServer(payload.speed);
  }

  syncAudioTransport(): void {
    this.audio.seek(this.currentStep, this.getPlaybackFps(), this.playing);
  }

  private getPlaybackFps(): number {
    return this.config.timelineFps * this.config.speed;
  }

  private anchorTransport(step: number, timestamp = performance.now()): void {
    this.currentStep = step;
    this.playStartStep = step;
    this.playStartPerfTime = timestamp;
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
        this.audio.pause(this.currentStep, this.getPlaybackFps());
        this.callbacks.syncAdvancedTimesteps(
          previousStep,
          this.currentStep,
          true,
        );
        this.callbacks.sendPlaybackStateToServer(false);
        this.callbacks.syncPlaybackButtons();
        return;
      }
      this.anchorTransport(0, timestamp);
      this.scene.rebuildThrough(0);
      this.audio.play(0, this.getPlaybackFps());
      this.callbacks.syncAdvancedTimesteps(previousStep, 0, true);
    } else {
      this.currentStep = next;
      this.scene.advanceThrough(Math.floor(this.currentStep));
      this.callbacks.syncAdvancedTimesteps(previousStep, this.currentStep);
    }
    this.rafId = getWindow().requestAnimationFrame((ts) => this.tick(ts));
  }
}
