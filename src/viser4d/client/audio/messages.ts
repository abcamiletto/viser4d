import type { AudioArrayPayload, RuntimeMessage } from "../binary";

export type AudioMessage =
  | {
      type: "AddAudioMessage";
      name: string;
      sampleRate: number;
      waveform: AudioArrayPayload;
      volume: number;
    }
  | {
      type: "SetAudioVolumeMessage";
      name: string;
      volume: number;
    }
  | {
      type: "SetAudioWaveformMessage";
      name: string;
      waveform: AudioArrayPayload;
    }
  | {
      type: "AppendAudioMessage";
      name: string;
      waveform: AudioArrayPayload;
    }
  | {
      type: "RemoveAudioMessage";
      name: string;
    };

export function isAudioMessage(message: RuntimeMessage): message is AudioMessage {
  return (
    message.type === "AddAudioMessage" ||
    message.type === "SetAudioVolumeMessage" ||
    message.type === "SetAudioWaveformMessage" ||
    message.type === "AppendAudioMessage" ||
    message.type === "RemoveAudioMessage"
  );
}
