// One Web Audio engine, shared by live playback and file playback. It folds
// audio events into per-track states, reconciles scheduled buffer sources by
// content, and is driven entirely by an injected transport clock.

import { waveformFloats } from "./binary";
import type { AudioTrackSnapshot } from "./state";
import { runtimeWindow } from "./viser";

export type AudioTransport = {
  getStep(): number;
  isPlaying(): boolean;
  speed(): number;
};

type RawWaveform = { numChannels: number; numFrames: number; data: Uint8Array };

type AudioEventLike = { type: string; name: string; [key: string]: unknown };

type TrackState = {
  sampleRate: number;
  startStep: number;
  volume: number;
  numChannels: number;
  numFrames: number;
  samples: Float32Array; // flat, frame-major: samples[frame * numChannels + ch]
  removed: boolean;
};

type RuntimeTrack = {
  source: AudioBufferSourceNode | null;
  gain: GainNode | null;
  buffer: AudioBuffer | null;
  bufferSamples: Float32Array | null;
  token: number;
};

function readWaveform(raw: RawWaveform): {
  samples: Float32Array;
  numChannels: number;
  numFrames: number;
} {
  return {
    samples: waveformFloats(raw.data),
    numChannels: raw.numChannels,
    numFrames: raw.numFrames,
  };
}

export class AudioEngine {
  private ctx: AudioContext | null = null;
  private tracks = new Map<string, TrackState>();
  private runtime = new Map<string, RuntimeTrack>();
  private nextToken = 1;

  constructor(
    private readonly transport: AudioTransport,
    private stepRate: number,
  ) {}

  setStepRate(rate: number): void {
    this.stepRate = rate > 0 ? rate : this.stepRate;
  }

  /** Replace the timeline track states from a block checkpoint. */
  loadCheckpoint(tracks: AudioTrackSnapshot[]): void {
    this.tracks.clear();
    for (const track of tracks) {
      const { samples, numChannels, numFrames } = readWaveform(track.waveform);
      this.tracks.set(track.name, {
        sampleRate: track.sampleRate,
        startStep: track.startStep,
        volume: track.volume,
        numChannels,
        numFrames,
        samples,
        removed: false,
      });
    }
  }

  /** Fold audio events recorded at `step` into the track states. */
  applyEvents(step: number, events: { message: unknown }[]): void {
    for (const event of events) {
      this.foldEvent(step, event.message as AudioEventLike);
    }
  }

  /** Stop and re-schedule every track for the current transport position. */
  reschedule(): void {
    for (const [name, runtime] of this.runtime) {
      if (!this.tracks.has(name)) {
        this.stopRuntime(runtime); // a track that vanished (e.g. after a seek)
      }
    }
    for (const name of this.tracks.keys()) {
      this.reconcileTrack(name);
    }
  }

  reset(): void {
    for (const runtime of this.runtime.values()) {
      this.stopRuntime(runtime);
    }
    this.tracks.clear();
    this.runtime.clear();
    this.nextToken = 1;
  }

  private foldEvent(step: number, message: AudioEventLike): void {
    const name = message.name;
    const existing = this.tracks.get(name);
    switch (message.type) {
      case "AddAudioMessage": {
        const { samples, numChannels, numFrames } = readWaveform(
          message.waveform as RawWaveform,
        );
        this.tracks.set(name, {
          sampleRate: message.sampleRate as number,
          startStep: step,
          volume: message.volume as number,
          numChannels,
          numFrames,
          samples,
          removed: false,
        });
        this.reconcileTrack(name);
        break;
      }
      case "SetAudioWaveformMessage": {
        if (!existing) {
          break;
        }
        const { samples, numChannels, numFrames } = readWaveform(
          message.waveform as RawWaveform,
        );
        existing.samples = samples;
        existing.numChannels = numChannels;
        existing.numFrames = numFrames;
        existing.removed = false;
        this.reconcileTrack(name);
        break;
      }
      case "AppendAudioMessage": {
        if (!existing) {
          break;
        }
        const { samples, numFrames } = readWaveform(message.waveform as RawWaveform);
        const merged = new Float32Array(existing.samples.length + samples.length);
        merged.set(existing.samples, 0);
        merged.set(samples, existing.samples.length);
        existing.samples = merged;
        existing.numFrames += numFrames;
        this.reconcileTrack(name);
        break;
      }
      case "SetAudioVolumeMessage": {
        if (!existing) {
          break;
        }
        existing.volume = message.volume as number;
        this.updateGain(name);
        break;
      }
      case "RemoveAudioMessage": {
        if (!existing) {
          break;
        }
        existing.removed = true;
        this.reconcileTrack(name);
        break;
      }
    }
  }

  private context(): AudioContext | null {
    if (!this.ctx) {
      const Ctor = runtimeWindow().AudioContext || runtimeWindow().webkitAudioContext;
      this.ctx = Ctor ? new Ctor() : null;
    }
    return this.ctx;
  }

  private clipDurationSteps(track: TrackState): number {
    return (track.numFrames / track.sampleRate) * this.stepRate;
  }

  private buildBuffer(track: TrackState, runtime: RuntimeTrack, ctx: AudioContext): AudioBuffer {
    if (runtime.buffer && runtime.bufferSamples === track.samples) {
      return runtime.buffer;
    }
    const buffer = ctx.createBuffer(track.numChannels, track.numFrames, track.sampleRate);
    for (let ch = 0; ch < track.numChannels; ch += 1) {
      const channel = new Float32Array(track.numFrames);
      for (let frame = 0; frame < track.numFrames; frame += 1) {
        channel[frame] = track.samples[frame * track.numChannels + ch] ?? 0;
      }
      buffer.copyToChannel(channel, ch);
    }
    runtime.buffer = buffer;
    runtime.bufferSamples = track.samples;
    return buffer;
  }

  private getRuntime(name: string): RuntimeTrack {
    let runtime = this.runtime.get(name);
    if (!runtime) {
      runtime = { source: null, gain: null, buffer: null, bufferSamples: null, token: 0 };
      this.runtime.set(name, runtime);
    }
    return runtime;
  }

  private stopRuntime(runtime: RuntimeTrack): void {
    runtime.token += 1;
    if (runtime.source) {
      runtime.source.onended = null;
      runtime.source.stop();
      runtime.source.disconnect();
      runtime.source = null;
    }
    if (runtime.gain) {
      runtime.gain.disconnect();
      runtime.gain = null;
    }
  }

  private updateGain(name: string): void {
    const runtime = this.runtime.get(name);
    const track = this.tracks.get(name);
    if (runtime?.gain && track) {
      runtime.gain.gain.value = track.volume;
    }
  }

  private reconcileTrack(name: string): void {
    const ctx = this.context();
    if (!ctx) {
      return;
    }
    const runtime = this.getRuntime(name);
    this.stopRuntime(runtime);
    if (!this.transport.isPlaying()) {
      return;
    }
    if (ctx.state === "suspended") {
      void ctx.resume();
    }
    const track = this.tracks.get(name);
    if (!track || track.removed || track.numFrames === 0) {
      return;
    }
    const step = this.transport.getStep();
    if (step >= track.startStep + this.clipDurationSteps(track)) {
      return; // clip already finished at this position
    }
    const speed = this.transport.speed() || 1;
    const source = ctx.createBufferSource();
    const gain = ctx.createGain();
    source.buffer = this.buildBuffer(track, runtime, ctx);
    gain.gain.value = track.volume;
    source.playbackRate.value = speed;
    source.connect(gain);
    gain.connect(ctx.destination);
    const token = ++this.nextToken;
    runtime.token = token;
    source.onended = () => {
      if (runtime.token !== token) {
        return;
      }
      runtime.source = null;
      runtime.gain?.disconnect();
      runtime.gain = null;
    };
    const when = ctx.currentTime + Math.max(0, (track.startStep - step) / (this.stepRate * speed));
    const offset = Math.max(0, (step - track.startStep) / this.stepRate);
    source.start(when, offset);
    runtime.source = source;
    runtime.gain = gain;
  }
}
