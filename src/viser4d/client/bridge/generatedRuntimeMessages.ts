// AUTOMATICALLY GENERATED message interfaces, from Python dataclass definitions.
// This file should not be manually modified.
/** RuntimeClearMessage()
 *
 * (automatically generated)
 */
export interface RuntimeClearMessage {
  type: "RuntimeClearMessage";
}
/** RuntimeConfigureMessage(numSteps: int, blockSize: int, timelineFps: float, speed: float, loop: bool, timelineSliderUuid: str, speedSliderUuid: str, stepButtonsUuid: str, playButtonUuid: str, pauseButtonUuid: str)
 *
 * (automatically generated)
 */
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
/** RuntimeLoadBlockMessage(block: int, checkpointMessages: list[viser4d._runtime_messages.RuntimeSceneMessage], stepMessages: list[list[viser4d._runtime_messages.RuntimeSceneMessage]])
 *
 * (automatically generated)
 */
export interface RuntimeLoadBlockMessage {
  type: "RuntimeLoadBlockMessage";
  block: number;
  checkpointMessages: import("../binary").RuntimeMessage[];
  stepMessages: import("../binary").RuntimeMessage[][];
}
/** RuntimeEvictBlockMessage(block: int)
 *
 * (automatically generated)
 */
export interface RuntimeEvictBlockMessage {
  type: "RuntimeEvictBlockMessage";
  block: number;
}
/** RuntimeSeekMessage(step: int)
 *
 * (automatically generated)
 */
export interface RuntimeSeekMessage {
  type: "RuntimeSeekMessage";
  step: number;
}
/** RuntimeRefreshMessage()
 *
 * (automatically generated)
 */
export interface RuntimeRefreshMessage {
  type: "RuntimeRefreshMessage";
}
/** RuntimePlayMessage(speed: float, loop: bool)
 *
 * (automatically generated)
 */
export interface RuntimePlayMessage {
  type: "RuntimePlayMessage";
  speed: number;
  loop: boolean;
}
/** RuntimePauseMessage()
 *
 * (automatically generated)
 */
export interface RuntimePauseMessage {
  type: "RuntimePauseMessage";
}
/** RuntimeSetSpeedMessage(speed: float, loop: bool)
 *
 * (automatically generated)
 */
export interface RuntimeSetSpeedMessage {
  type: "RuntimeSetSpeedMessage";
  speed: number;
  loop: boolean;
}
/** RuntimeApplyMessageUpdateMessage(message: viser4d._runtime_messages.RuntimeSceneMessage)
 *
 * (automatically generated)
 */
export interface RuntimeApplyMessageUpdateMessage {
  type: "RuntimeApplyMessageUpdateMessage";
  message: import("../binary").RuntimeMessage;
}
/** RuntimeBlockRequestMessage(step: int)
 *
 * (automatically generated)
 */
export interface RuntimeBlockRequestMessage {
  type: "RuntimeBlockRequestMessage";
  step: number;
}
/** RuntimeTimestepMessage(step: int)
 *
 * (automatically generated)
 */
export interface RuntimeTimestepMessage {
  type: "RuntimeTimestepMessage";
  step: number;
}
/** RuntimeSpeedMessage(speed: float)
 *
 * (automatically generated)
 */
export interface RuntimeSpeedMessage {
  type: "RuntimeSpeedMessage";
  speed: number;
}
/** RuntimePlaybackStateMessage(isPlaying: bool)
 *
 * (automatically generated)
 */
export interface RuntimePlaybackStateMessage {
  type: "RuntimePlaybackStateMessage";
  isPlaying: boolean;
}
/** RuntimeReadyMessage()
 *
 * (automatically generated)
 */
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
