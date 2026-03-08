from __future__ import annotations

from pathlib import Path

import viser
from viser import _client_autobuild

_MESSAGE_HANDLER_MARKER = "// __VISER4D_AUDIO_MESSAGES__"
_FILE_PLAYBACK_MARKER = "// __VISER4D_AUDIO_PLAYBACK__"

_MESSAGE_HANDLER_PATCH = r"""
// __VISER4D_AUDIO_MESSAGES__
type Viser4dAudioMessage = {
  type:
    | "AddAudioMessage"
    | "SetAudioVolumeMessage"
    | "SetAudioWaveformMessage"
    | "AppendAudioMessage"
    | "RemoveAudioMessage";
  name: string;
  sampleRate?: number;
  waveform?: {
    dtype: string;
    numChannels: number;
    numFrames: number;
    data: string;
  };
  volume?: number;
  __viserPlaybackTime?: number;
};

function getViser4dAudioManager() {
  const windowRef = window as Window & { __VISER4D_AUDIO__?: any };
  if (windowRef.__VISER4D_AUDIO__) return windowRef.__VISER4D_AUDIO__;

  const decodeBase64Bytes = (base64Text: string) => {
    const binary = atob(base64Text);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  };

  const decodeWaveform = (payload: NonNullable<Viser4dAudioMessage["waveform"]>) => {
    const bytes = decodeBase64Bytes(payload.data);
    const samples =
      payload.dtype === "float32"
        ? new Float32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4)
        : new Float32Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
    const out = Array.from({ length: payload.numChannels }, () => new Float32Array(payload.numFrames));
    for (let frame = 0; frame < payload.numFrames; frame += 1) {
      for (let channel = 0; channel < payload.numChannels; channel += 1) {
        out[channel]![frame] = samples[frame * payload.numChannels + channel] ?? 0;
      }
    }
    return out;
  };

  const appendWaveforms = (head: Float32Array[], tail: Float32Array[]) =>
    head.map((samples, channel) => {
      const merged = new Float32Array(samples.length + (tail[channel]?.length ?? 0));
      merged.set(samples, 0);
      merged.set(tail[channel] ?? new Float32Array(0), samples.length);
      return merged;
    });

  class Viser4dAudioManager {
    ctx: AudioContext | null = null;
    tracks = new Map<string, any>();
    currentTime = 0;
    playing = true;

    ensureContext() {
      if (!this.ctx) {
        const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext;
        this.ctx = AudioContextClass ? new AudioContextClass() : null;
      }
      return this.ctx;
    }

    stopTrack(track: any) {
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
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
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
      const startTime = message.__viserPlaybackTime ?? this.currentTime;
      const track =
        this.tracks.get(message.name) ??
        { sampleRate: 44100, waveform: [new Float32Array(0)], volume: 1.0, startTime, removed: false, source: null, gain: null };
      switch (message.type) {
        case "AddAudioMessage":
          track.sampleRate = message.sampleRate ?? track.sampleRate;
          track.waveform = message.waveform ? decodeWaveform(message.waveform) : track.waveform;
          track.volume = message.volume ?? track.volume;
          track.startTime = startTime;
          track.removed = false;
          break;
        case "SetAudioVolumeMessage":
          track.volume = message.volume ?? track.volume;
          break;
        case "SetAudioWaveformMessage":
          track.waveform = message.waveform ? decodeWaveform(message.waveform) : track.waveform;
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
      for (const name of this.tracks.keys()) this.reconcile(name);
    }

    resetAll() {
      for (const track of this.tracks.values()) this.stopTrack(track);
      this.tracks.clear();
    }
  }

  windowRef.__VISER4D_AUDIO__ = new Viser4dAudioManager();
  return windowRef.__VISER4D_AUDIO__;
}
"""

_MESSAGE_HANDLER_CASES = r"""
      case "AddAudioMessage":
      case "SetAudioVolumeMessage":
      case "SetAudioWaveformMessage":
      case "AppendAudioMessage":
      case "RemoveAudioMessage": {
        getViser4dAudioManager().apply(message as unknown as Viser4dAudioMessage);
        return;
      }
"""

_FILE_PLAYBACK_PATCH = r"""
// __VISER4D_AUDIO_PLAYBACK__
function isViser4dAudioMessage(message: Message | Record<string, unknown>): boolean {
  return [
    "AddAudioMessage",
    "SetAudioVolumeMessage",
    "SetAudioWaveformMessage",
    "AppendAudioMessage",
    "RemoveAudioMessage",
  ].includes((message as { type?: string }).type ?? "");
}
"""


def _patch_message_handler(text: str) -> str:
    if _MESSAGE_HANDLER_MARKER not in text:
        text = text.replace(
            '/** Returns a handler for all incoming messages. */',
            _MESSAGE_HANDLER_PATCH + '\n/** Returns a handler for all incoming messages. */',
        )
    if _MESSAGE_HANDLER_CASES not in text:
        text = text.replace(
            '      // Add a notification.\n',
            _MESSAGE_HANDLER_CASES + '      // Add a notification.\n',
        )
    return text


def _patch_file_playback(text: str) -> str:
    if _FILE_PLAYBACK_MARKER not in text:
        text = text.replace(
            'export interface SerializedMessages {\n',
            _FILE_PLAYBACK_PATCH + '\nexport interface SerializedMessages {\n',
        )
    text = text.replace(
        "    // Instead of removing all of the existing scene nodes, we're just going to hide them.\n",
        "    // Instead of removing all of the existing scene nodes, we're just going to hide them.\n"
        "    (window as Window & { __VISER4D_AUDIO__?: { resetAll?: () => void } }).__VISER4D_AUDIO__?.resetAll?.();\n",
        1,
    )
    text = text.replace(
        "      const message = recording.messages[mutable.currentIndex][1];\n      viewerMutable.messageQueue.push(message);\n",
        "      const message = recording.messages[mutable.currentIndex][1];\n"
        "      if (isViser4dAudioMessage(message)) {\n"
        "        (message as Message & { __viserPlaybackTime?: number }).__viserPlaybackTime =\n"
        "          recording.messages[mutable.currentIndex][0];\n"
        "      }\n"
        "      viewerMutable.messageQueue.push(message);\n",
        1,
    )
    text = text.replace(
        "    setCurrentTime(mutable.currentTime);\n  }, [recording]);\n",
        "    setCurrentTime(mutable.currentTime);\n"
        "    (window as Window & { __VISER4D_AUDIO__?: { seek?: (time: number, playing: boolean) => void } }).__VISER4D_AUDIO__?.seek?.(mutable.currentTime, !paused);\n"
        "  }, [recording, paused]);\n",
        1,
    )
    return text


def ensure_viser_audio_patch() -> None:
    client_src = Path(viser.__file__).resolve().parent / "client" / "src"
    message_handler_path = client_src / "MessageHandler.tsx"
    file_playback_path = client_src / "FilePlayback.tsx"

    original_message_handler = message_handler_path.read_text()
    original_file_playback = file_playback_path.read_text()
    patched_message_handler = _patch_message_handler(original_message_handler)
    patched_file_playback = _patch_file_playback(original_file_playback)

    if (
        patched_message_handler == original_message_handler
        and patched_file_playback == original_file_playback
    ):
        return

    message_handler_path.write_text(patched_message_handler)
    file_playback_path.write_text(patched_file_playback)
    _client_autobuild._build_viser_client(_client_autobuild.build_dir, cached=False)
