// __VISER4D_AUDIO_MESSAGES__
      case "AddAudioMessage":
      case "SetAudioVolumeMessage":
      case "SetAudioWaveformMessage":
      case "AppendAudioMessage":
      case "RemoveAudioMessage": {
        (window as Window & {
          __VISER4D_FILE_AUDIO__?: { apply: (message: Message) => void };
        }).__VISER4D_FILE_AUDIO__?.apply(message);
        return;
      }
