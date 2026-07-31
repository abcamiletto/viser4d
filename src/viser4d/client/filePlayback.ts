// Export-mode adapter. In exported HTML viser's native player replays the
// scene; we only keep audio in sync by scraping its player position. This is
// the single DOM scrape in the runtime, deliberately isolated to this module.

import { AudioEngine, type AudioTransport } from "./audio";

const JUMP_STEPS = 0.2;
const EPSILON = 1e-4;

export class FilePlayback implements AudioTransport {
  private readonly audio = new AudioEngine(this, 1);
  private slider: Element | null = null;
  private observer: MutationObserver | null = null;
  private playbackTime = 0;
  private playing = false;
  private observed = false;
  private pending: { message: unknown }[] = [];
  private syncScheduled = false;
  private disposed = false;

  getStep(): number {
    return this.playbackTime;
  }
  isPlaying(): boolean {
    return this.playing;
  }
  speed(): number {
    return 1;
  }

  install(): void {
    this.attach();
  }

  enqueue(message: unknown): void {
    if (this.disposed) {
      return;
    }
    this.pending.push({ message });
    this.scheduleSync();
  }

  dispose(): void {
    this.disposed = true;
    this.observer?.disconnect();
    this.observer = null;
    this.audio.reset();
    this.slider = null;
    this.pending = [];
  }

  private attach(): void {
    if (this.disposed || this.observer) {
      return;
    }
    const slider = this.findSlider();
    if (!slider) {
      requestAnimationFrame(() => this.attach());
      return;
    }
    this.observer = new MutationObserver(() => this.sync());
    this.observer.observe(slider, {
      attributes: true,
      attributeFilter: ["aria-valuenow"],
    });
    this.sync();
  }

  private findSlider(): Element | null {
    if (!this.slider || !this.slider.isConnected) {
      this.slider = document.querySelector("[role='slider'][aria-valuenow]");
    }
    return this.slider;
  }

  private readTime(): number | null {
    const slider = this.findSlider();
    const value = slider?.getAttribute("aria-valuenow");
    if (value == null) {
      return null;
    }
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  private flush(): void {
    if (!this.pending.length) {
      return;
    }
    this.audio.applyEvents(this.playbackTime, this.pending);
    this.pending = [];
  }

  private scheduleSync(): void {
    if (this.syncScheduled) {
      return;
    }
    this.syncScheduled = true;
    requestAnimationFrame(() => {
      this.syncScheduled = false;
      this.sync();
    });
  }

  private sync(): void {
    if (this.disposed) {
      return;
    }
    const next = this.readTime();
    if (next === null) {
      return;
    }
    const delta = next - this.playbackTime;
    const playing = delta > EPSILON;
    if (!this.observed) {
      this.observed = true;
      this.playbackTime = next;
      this.playing = playing;
      this.audio.reschedule();
      this.flush();
      return;
    }
    const jumped = Math.abs(delta) > JUMP_STEPS;
    const stateChanged = playing !== this.playing;
    const pausedButMoved = !playing && Math.abs(delta) > EPSILON;
    const backward = delta < -EPSILON;
    this.playbackTime = next;
    this.playing = playing;
    if (backward) {
      this.audio.reset();
    }
    if (backward || jumped || stateChanged || pausedButMoved) {
      this.audio.reschedule();
    }
    this.flush();
  }
}
