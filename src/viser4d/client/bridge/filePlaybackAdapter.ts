import type { RuntimeValue } from "../binary";
import { AudioRuntime } from "../audio/runtime";
import type { AudioMessage } from "../audio/messages";
import { getWindow } from "./protocol";

type DebugPush = (event: string, payload: RuntimeValue) => void;

export class FilePlaybackAdapter {
  private readonly audio: AudioRuntime;
  private disposed = false;
  private playbackTime = 0;
  private playbackPlaying = false;
  private playbackObserved = false;
  private pendingMessages: AudioMessage[] = [];
  private syncScheduled = false;

  constructor(
    private readPlaybackTime: () => number | null,
    debugPush: DebugPush,
  ) {
    this.audio = new AudioRuntime(() => this.playbackTime, debugPush);
    this.audio.setStepRate(1);
  }

  dispose(): void {
    this.disposed = true;
    this.audio.reset();
    this.playbackTime = 0;
    this.playbackPlaying = false;
    this.playbackObserved = false;
    this.pendingMessages = [];
    this.syncScheduled = false;
  }

  enqueue(message: AudioMessage): void {
    if (this.disposed) {
      return;
    }
    this.pendingMessages.push(message);
    this.scheduleSync();
  }

  sync(): void {
    if (this.disposed) {
      return;
    }
    const nextTime = this.readPlaybackTime();
    if (nextTime === null) {
      return;
    }
    const delta = nextTime - this.playbackTime;
    const jumped = Math.abs(delta) > 0.2;
    const playing = delta > 1e-4;
    const playbackStateChanged = playing !== this.playbackPlaying;
    const pausedButMoved = !playing && Math.abs(delta) > 1e-4;
    if (!this.playbackObserved) {
      this.playbackObserved = true;
      this.playbackTime = nextTime;
      this.flushPendingMessages(nextTime);
      this.audio.seek(nextTime, 1, false);
      return;
    }
    if (delta < -1e-4) {
      this.audio.resetTimeline();
    }
    if (jumped) {
      this.audio.seek(nextTime, 1, playing);
    } else if (playbackStateChanged) {
      if (playing) {
        this.audio.play(nextTime, 1);
      } else {
        this.audio.pause(nextTime, 1);
      }
    } else if (pausedButMoved) {
      this.audio.seek(nextTime, 1, false);
    }
    this.playbackTime = nextTime;
    this.playbackPlaying = playing;
    this.flushPendingMessages(nextTime);
  }

  private flushPendingMessages(playbackTime: number): void {
    if (this.pendingMessages.length === 0) {
      return;
    }
    this.audio.applyLiveMessages(playbackTime, this.pendingMessages);
    this.pendingMessages = [];
  }

  private scheduleSync(): void {
    if (this.syncScheduled) {
      return;
    }
    this.syncScheduled = true;
    getWindow().requestAnimationFrame(() => {
      if (this.disposed) {
        return;
      }
      this.syncScheduled = false;
      this.sync();
    });
  }
}
