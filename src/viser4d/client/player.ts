// Wall-clock transport. Owns play/pause/seek/speed/loop and the rAF tick that
// turns elapsed time into discrete step crossings. It computes *when*; the
// controller decides *what* to apply for each step.

export type PlayerListener = {
  /**
   * A discrete step the transport reached. `continuous` means it was crossed by
   * forward playback from the immediately preceding step (the fast delta path);
   * otherwise it is a jump (seek, loop wrap, refresh) that needs a full rebuild.
   */
  step(step: number, continuous: boolean): void;
  /** Playing / speed / loop changed. */
  transport(): void;
};

export class Player {
  private numSteps = 1;
  private fps = 30;
  private _speed = 1;
  private _loop = false;
  private _playing = false;
  private position = 0; // fractional current step
  private anchorStep = 0;
  private anchorTime = 0;
  private emitted = -1; // last integer step handed to the listener
  private rafId: number | null = null;

  constructor(private readonly listener: PlayerListener) {}

  get playing(): boolean {
    return this._playing;
  }
  get speed(): number {
    return this._speed;
  }
  get loop(): boolean {
    return this._loop;
  }
  get currentStep(): number {
    return Math.min(this.numSteps - 1, Math.max(0, Math.floor(this.position)));
  }

  configure(numSteps: number, fps: number, speed: number, loop: boolean): void {
    this.numSteps = Math.max(1, numSteps);
    this.fps = fps > 0 ? fps : this.fps;
    this._speed = speed;
    this._loop = loop;
    this.position = Math.min(this.position, this.numSteps - 1);
    if (this._playing) {
      this.anchor(this.position);
    }
  }

  getTransportStep(now = performance.now()): number {
    if (!this._playing) {
      return this.position;
    }
    return this.anchorStep + ((now - this.anchorTime) / 1000) * this.fps * this._speed;
  }

  play(speed?: number, loop?: boolean): void {
    if (speed !== undefined) {
      this._speed = speed;
    }
    if (loop !== undefined) {
      this._loop = loop;
    }
    if (!this._loop && this.currentStep >= this.numSteps - 1) {
      this.seek(0);
    }
    this._playing = true;
    this.anchor(this.position);
    this.listener.transport();
    this.startRaf();
  }

  pause(): void {
    this.position = this.clamp(this.getTransportStep());
    this._playing = false;
    this.stopRaf();
    // Emit the steps crossed since the last tick so the applied scene lands
    // exactly on the pause step (at speed > 1 several may be outstanding).
    this.emitForward(Math.floor(this.position));
    this.listener.transport();
  }

  seek(step: number): void {
    this.position = this.clamp(step);
    if (this._playing) {
      this.anchor(this.position);
    }
    this.emitted = Math.floor(this.position);
    this.listener.step(this.emitted, false);
  }

  setSpeed(speed: number, loop: boolean): void {
    const step = this.getTransportStep();
    this._speed = speed;
    this._loop = loop;
    if (this._playing) {
      this.anchor(this.clamp(step));
    }
    this.listener.transport();
  }

  refresh(): void {
    this.listener.step(this.currentStep, false);
  }

  dispose(): void {
    this.stopRaf();
    this._playing = false;
    this.position = 0;
    this.emitted = -1;
  }

  private clamp(step: number): number {
    return Math.min(this.numSteps - 1, Math.max(0, step));
  }

  private anchor(step: number, now = performance.now()): void {
    this.position = step;
    this.anchorStep = step;
    this.anchorTime = now;
  }

  private startRaf(): void {
    this.stopRaf();
    this.rafId = requestAnimationFrame((ts) => this.tick(ts));
  }

  private stopRaf(): void {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
  }

  private emitForward(through: number): void {
    for (let step = this.emitted + 1; step <= through; step += 1) {
      this.emitted = step;
      this.listener.step(step, true);
    }
  }

  private tick(timestamp: number): void {
    if (!this._playing) {
      return;
    }
    const next = this.getTransportStep(timestamp);
    if (next >= this.numSteps) {
      this.emitForward(this.numSteps - 1);
      if (this._loop) {
        this.anchor(0, timestamp);
        this.emitted = 0;
        this.listener.step(0, false);
        this.startRaf();
      } else {
        this.position = this.numSteps - 1;
        this._playing = false;
        this.stopRaf();
        this.listener.transport();
      }
      return;
    }
    this.position = next;
    this.emitForward(Math.floor(next));
    this.startRaf();
  }
}
