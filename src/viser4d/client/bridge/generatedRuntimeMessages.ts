// AUTOMATICALLY GENERATED message interfaces, from Python dataclass definitions.
// This file should not be manually modified.
export interface RuntimeClearMessage {
  type: "RuntimeClearMessage";
}
export interface RuntimeConfigureMessage {
  type: "RuntimeConfigureMessage";
  numSteps: number;
  blockSize: number;
  timelineFps: number;
  speed: number;
  loop: boolean;
  timelineSliderUuid: string;
  speedSliderUuid: string;
  stepButtonsUuid: string;
  playButtonUuid: string;
  pauseButtonUuid: string;
}
export interface RuntimeLoadBlockMessage {
  type: "RuntimeLoadBlockMessage";
  block: number;
  checkpointMessages: import("../binary").RuntimeMessage[];
  stepMessages: import("../binary").RuntimeMessage[][];
}
export interface RuntimeEvictBlockMessage {
  type: "RuntimeEvictBlockMessage";
  block: number;
}
export interface RuntimeSeekMessage {
  type: "RuntimeSeekMessage";
  step: number;
}
export interface RuntimeRefreshMessage {
  type: "RuntimeRefreshMessage";
}
export interface RuntimePlayMessage {
  type: "RuntimePlayMessage";
  speed: number;
  loop: boolean;
}
export interface RuntimePauseMessage {
  type: "RuntimePauseMessage";
}
export interface RuntimeSetSpeedMessage {
  type: "RuntimeSetSpeedMessage";
  speed: number;
  loop: boolean;
}
export interface RuntimeApplyMessageUpdateMessage {
  type: "RuntimeApplyMessageUpdateMessage";
  message: import("../binary").RuntimeMessage;
}
export interface RuntimeBlockRequestMessage {
  type: "RuntimeBlockRequestMessage";
  step: number;
}
export interface RuntimeTimestepMessage {
  type: "RuntimeTimestepMessage";
  step: number;
}
export interface RuntimeSpeedMessage {
  type: "RuntimeSpeedMessage";
  speed: number;
}
export interface RuntimePlaybackStateMessage {
  type: "RuntimePlaybackStateMessage";
  isPlaying: boolean;
}
export interface RuntimeReadyMessage {
  type: "RuntimeReadyMessage";
}

export type Message = 
  | RuntimeClearMessage
  | RuntimeConfigureMessage
  | RuntimeLoadBlockMessage
  | RuntimeEvictBlockMessage
  | RuntimeSeekMessage
  | RuntimeRefreshMessage
  | RuntimePlayMessage
  | RuntimePauseMessage
  | RuntimeSetSpeedMessage
  | RuntimeApplyMessageUpdateMessage
  | RuntimeBlockRequestMessage
  | RuntimeTimestepMessage
  | RuntimeSpeedMessage
  | RuntimePlaybackStateMessage
  | RuntimeReadyMessage;
export type RuntimeControlMessage = 
  | RuntimeClearMessage
  | RuntimeConfigureMessage
  | RuntimeLoadBlockMessage
  | RuntimeEvictBlockMessage
  | RuntimeSeekMessage
  | RuntimeRefreshMessage
  | RuntimePlayMessage
  | RuntimePauseMessage
  | RuntimeSetSpeedMessage
  | RuntimeApplyMessageUpdateMessage;
export type RuntimeEventMessage = 
  | RuntimeBlockRequestMessage
  | RuntimeTimestepMessage
  | RuntimeSpeedMessage
  | RuntimePlaybackStateMessage
  | RuntimeReadyMessage;
const typeSetRuntimeControlMessage = new Set(['RuntimeClearMessage', 'RuntimeConfigureMessage', 'RuntimeLoadBlockMessage', 'RuntimeEvictBlockMessage', 'RuntimeSeekMessage', 'RuntimeRefreshMessage', 'RuntimePlayMessage', 'RuntimePauseMessage', 'RuntimeSetSpeedMessage', 'RuntimeApplyMessageUpdateMessage']);
export function isRuntimeControlMessage(message: { type: string }): message is RuntimeControlMessage {
  return typeSetRuntimeControlMessage.has(message.type);
}
const typeSetRuntimeEventMessage = new Set(['RuntimeBlockRequestMessage', 'RuntimeTimestepMessage', 'RuntimeSpeedMessage', 'RuntimePlaybackStateMessage', 'RuntimeReadyMessage']);
export function isRuntimeEventMessage(message: { type: string }): message is RuntimeEventMessage {
  return typeSetRuntimeEventMessage.has(message.type);
}
