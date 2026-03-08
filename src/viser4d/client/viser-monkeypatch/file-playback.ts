// __VISER4D_AUDIO_PLAYBACK__
type Viser4dAudioMessage = Message & {
  name: string;
  sampleRate?: number;
  waveform?: {
    data: string;
    numChannels: number;
    numFrames: number;
  };
  volume?: number;
  __viserPlaybackTime?: number;
};

type AudioContextWindow = Window & {
  webkitAudioContext?: typeof AudioContext;
};

interface FileAudioTrack {
  sampleRate: number;
  waveform: Float32Array[];
  volume: number;
  startTime: number;
  removed: boolean;
  source: AudioBufferSourceNode | null;
  gain: GainNode | null;
}

function isViser4dAudioMessage(
  message: Message | Record<string, unknown>,
): message is Viser4dAudioMessage {
  const type = (message as { type?: string }).type;
  return (
    type === "AddAudioMessage" ||
    type === "SetAudioVolumeMessage" ||
    type === "SetAudioWaveformMessage" ||
    type === "AppendAudioMessage" ||
    type === "RemoveAudioMessage"
  );
}

function getAudioContextClass(): typeof AudioContext | undefined {
  const audioWindow = window as AudioContextWindow;
  return audioWindow.AudioContext || audioWindow.webkitAudioContext;
}

function ensureViser4dFileAudioRuntime() {
  const windowRef = window as Window & { __VISER4D_FILE_AUDIO__?: any };
  if (windowRef.__VISER4D_FILE_AUDIO__) {
    return windowRef.__VISER4D_FILE_AUDIO__;
  }

  const decodeWaveform = (
    waveform: NonNullable<Viser4dAudioMessage["waveform"]>,
  ): Float32Array[] => {
    const binary = atob(waveform.data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    const flat = new Float32Array(bytes.buffer);
    return Array.from({ length: waveform.numChannels }, (_, channel) => {
      const out = new Float32Array(waveform.numFrames);
      for (let frame = 0; frame < waveform.numFrames; frame += 1) {
        out[frame] = flat[frame * waveform.numChannels + channel] ?? 0;
      }
      return out;
    });
  };

  const appendWaveforms = (head: Float32Array[], tail: Float32Array[]) =>
    head.map((samples, channel) => {
      const merged = new Float32Array(samples.length + (tail[channel]?.length ?? 0));
      merged.set(samples, 0);
      merged.set(tail[channel] ?? new Float32Array(0), samples.length);
      return merged;
    });

  class FileAudioRuntime {
    ctx: AudioContext | null = null;
    tracks = new Map<string, FileAudioTrack>();
    currentTime = 0;
    playing = false;

    ensureContext() {
      if (!this.ctx) {
        const AudioContextClass = getAudioContextClass();
        this.ctx = AudioContextClass ? new AudioContextClass() : null;
      }
      return this.ctx;
    }

    stopTrack(track: FileAudioTrack) {
      if (track.source) {
        try {
          track.source.stop();
        } catch {}
        track.source.disconnect();
        track.source = null;
      }
      if (track.gain) {
        track.gain.disconnect();
        track.gain = null;
      }
    }

    reconcile(name: string) {
      const ctx = this.ensureContext();
      if (!ctx) return;
      const track = this.tracks.get(name);
      if (!track) return;
      this.stopTrack(track);
      if (!this.playing || track.removed || !track.waveform?.[0]?.length) return;
      const elapsed = Math.max(0, this.currentTime - track.startTime);
      const duration = (track.waveform[0]?.length ?? 0) / track.sampleRate;
      if (elapsed >= duration) return;
      if (ctx.state === "suspended") {
        ctx.resume().catch(() => {});
      }
      const source = ctx.createBufferSource();
      const gain = ctx.createGain();
      const buffer = ctx.createBuffer(
        track.waveform.length,
        track.waveform[0]?.length ?? 0,
        track.sampleRate,
      );
      for (let channel = 0; channel < track.waveform.length; channel += 1) {
        buffer.copyToChannel(track.waveform[channel], channel);
      }
      source.buffer = buffer;
      gain.gain.value = track.volume;
      source.connect(gain);
      gain.connect(ctx.destination);
      source.start(0, elapsed);
      track.source = source;
      track.gain = gain;
    }

    apply(message: Viser4dAudioMessage) {
      const track = this.tracks.get(message.name) ?? {
        sampleRate: 44100,
        waveform: [new Float32Array(0)],
        volume: 1,
        startTime: message.__viserPlaybackTime ?? this.currentTime,
        removed: false,
        source: null,
        gain: null,
      };
      switch (message.type) {
        case "AddAudioMessage":
          track.sampleRate = message.sampleRate ?? track.sampleRate;
          track.waveform = message.waveform
            ? decodeWaveform(message.waveform)
            : track.waveform;
          track.volume = message.volume ?? track.volume;
          track.startTime = message.__viserPlaybackTime ?? this.currentTime;
          track.removed = false;
          break;
        case "SetAudioVolumeMessage":
          track.volume = message.volume ?? track.volume;
          break;
        case "SetAudioWaveformMessage":
          track.waveform = message.waveform
            ? decodeWaveform(message.waveform)
            : track.waveform;
          track.removed = false;
          break;
        case "AppendAudioMessage":
          track.waveform = message.waveform
            ? appendWaveforms(track.waveform, decodeWaveform(message.waveform))
            : track.waveform;
          break;
        case "RemoveAudioMessage":
          track.removed = true;
          break;
      }
      this.tracks.set(message.name, track);
      this.reconcile(message.name);
    }

    seek(time: number, playing: boolean) {
      this.currentTime = time;
      this.playing = playing;
      for (const name of this.tracks.keys()) {
        this.reconcile(name);
      }
    }

    resetAll() {
      for (const track of this.tracks.values()) {
        this.stopTrack(track);
      }
      this.tracks.clear();
      this.currentTime = 0;
      this.playing = false;
    }
  }

  windowRef.__VISER4D_FILE_AUDIO__ = new FileAudioRuntime();
  return windowRef.__VISER4D_FILE_AUDIO__;
}

ensureViser4dFileAudioRuntime();
