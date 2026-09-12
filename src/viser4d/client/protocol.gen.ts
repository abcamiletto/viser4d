// AUTOMATICALLY GENERATED from viser4d/_protocol.py -- do not edit.
// Regenerate with: python -m viser4d._codegen

export interface TimelineConfigureMessage {
  type: "TimelineConfigureMessage";
  numSteps: number;
  blockSize: number;
  timelineFps: number;
  speed: number;
  loop: boolean;
  cacheBytes: number;
  blockBytes: (number | null)[];
}

/** Encoded size of every block, by index; ``None`` where not yet known. */
export interface TimelineBlockBytesMessage {
  type: "TimelineBlockBytesMessage";
  blockBytes: (number | null)[];
}

/** Full payload for one block; replaces any previously held copy. */
export interface TimelineBlockMessage {
  type: "TimelineBlockMessage";
  index: number;
  checkpointScene: { key: string, rev: number, name: string | null, message: import("./binary").ScenePayload }[];
  checkpointAudio: import("viser-audio/protocol").AudioAddMessage[];
  deltas: { puts: { key: string, rev: number, name: string | null, message: import("./binary").ScenePayload }[], deleteNodes: string[], audio: import("viser-audio/protocol").AudioMessage[] }[];
}

/** One keyed entry of the live override overlay. */
export interface TimelineOverrideMessage {
  type: "TimelineOverrideMessage";
  entry: { key: string, rev: number, name: string | null, message: import("./binary").ScenePayload };
}

export interface TimelineSeekMessage {
  type: "TimelineSeekMessage";
  step: number;
}

export interface TimelinePlayMessage {
  type: "TimelinePlayMessage";
  speed: number;
  loop: boolean;
}

export interface TimelinePauseMessage {
  type: "TimelinePauseMessage";
}

export interface TimelineSetSpeedMessage {
  type: "TimelineSetSpeedMessage";
  speed: number;
  loop: boolean;
}

/** Reset all client timeline state (blocks, overrides, transport). */
export interface TimelineClearMessage {
  type: "TimelineClearMessage";
}

/** Re-apply the current timestep from scratch. */
export interface TimelineRefreshMessage {
  type: "TimelineRefreshMessage";
}

export interface TimelineReadyMessage {
  type: "TimelineReadyMessage";
}

export interface TimelineBlockRequestMessage {
  type: "TimelineBlockRequestMessage";
  index: number;
}

export interface TimelineBlockDiscardMessage {
  type: "TimelineBlockDiscardMessage";
  index: number;
}

export interface TimelineTimestepMessage {
  type: "TimelineTimestepMessage";
  step: number;
}

export interface TimelinePlaybackStateMessage {
  type: "TimelinePlaybackStateMessage";
  isPlaying: boolean;
}

export interface TimelineSpeedMessage {
  type: "TimelineSpeedMessage";
  speed: number;
}

export type TimelineControlMessage =
  | TimelineConfigureMessage
  | TimelineBlockBytesMessage
  | TimelineBlockMessage
  | TimelineOverrideMessage
  | TimelineSeekMessage
  | TimelinePlayMessage
  | TimelinePauseMessage
  | TimelineSetSpeedMessage
  | TimelineClearMessage
  | TimelineRefreshMessage;
const _TimelineControlMessageTypes = new Set(['TimelineBlockBytesMessage', 'TimelineBlockMessage', 'TimelineClearMessage', 'TimelineConfigureMessage', 'TimelineOverrideMessage', 'TimelinePauseMessage', 'TimelinePlayMessage', 'TimelineRefreshMessage', 'TimelineSeekMessage', 'TimelineSetSpeedMessage']);
export function isTimelineControlMessage(message: { type: string }): message is TimelineControlMessage {
  return _TimelineControlMessageTypes.has(message.type);
}

export type TimelineEventMessage =
  | TimelineReadyMessage
  | TimelineBlockRequestMessage
  | TimelineBlockDiscardMessage
  | TimelineTimestepMessage
  | TimelinePlaybackStateMessage
  | TimelineSpeedMessage;
const _TimelineEventMessageTypes = new Set(['TimelineBlockDiscardMessage', 'TimelineBlockRequestMessage', 'TimelinePlaybackStateMessage', 'TimelineReadyMessage', 'TimelineSpeedMessage', 'TimelineTimestepMessage']);
export function isTimelineEventMessage(message: { type: string }): message is TimelineEventMessage {
  return _TimelineEventMessageTypes.has(message.type);
}
