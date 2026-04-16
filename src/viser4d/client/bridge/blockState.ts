import type { RuntimeMessage } from "../binary";
import { isAudioMessage, type AudioMessage } from "../audio/messages";

export type KeyedRuntimeMessage = {
  key: string;
  message: RuntimeMessage;
};

export type RuntimeStatePatch = {
  scenePuts: KeyedRuntimeMessage[];
  sceneDeleteNodes: string[];
  audioMessages: RuntimeMessage[];
};

export type StepPatchUpdate = {
  stepOffset: number;
  patch: RuntimeStatePatch;
};

export type LoadedCheckpoint = {
  sceneEntries: Map<string, RuntimeMessage>;
  audioEntries: Map<string, AudioMessage>;
};

export type LoadedStatePatch = {
  scenePuts: Map<string, RuntimeMessage>;
  sceneDeleteNodes: string[];
  audioMessages: AudioMessage[];
};

export type LoadedBlock = {
  checkpoint: LoadedCheckpoint;
  stepPatches: LoadedStatePatch[];
};

export function makeLoadedBlock(payload: {
  checkpointSceneEntries: KeyedRuntimeMessage[];
  checkpointAudioMessages: RuntimeMessage[];
  stepPatches: RuntimeStatePatch[];
}): LoadedBlock {
  return {
    checkpoint: {
      sceneEntries: new Map(
        payload.checkpointSceneEntries.map((entry) => [entry.key, entry.message]),
      ),
      audioEntries: audioMessageMap(payload.checkpointAudioMessages),
    },
    stepPatches: payload.stepPatches.map(makeLoadedStatePatch),
  };
}

export function patchLoadedBlock(
  block: LoadedBlock,
  patch: {
    checkpointScenePuts: KeyedRuntimeMessage[];
    checkpointSceneDeletes: string[];
    checkpointAudioPuts: RuntimeMessage[];
    checkpointAudioDeletes: string[];
    stepPatchUpdates: StepPatchUpdate[];
  },
): void {
  for (const key of patch.checkpointSceneDeletes) {
    block.checkpoint.sceneEntries.delete(key);
  }
  for (const entry of patch.checkpointScenePuts) {
    block.checkpoint.sceneEntries.set(entry.key, entry.message);
  }
  for (const name of patch.checkpointAudioDeletes) {
    block.checkpoint.audioEntries.delete(name);
  }
  for (const [name, message] of audioMessageMap(patch.checkpointAudioPuts)) {
    block.checkpoint.audioEntries.set(name, message);
  }
  for (const update of patch.stepPatchUpdates) {
    block.stepPatches[update.stepOffset] = makeLoadedStatePatch(update.patch);
  }
}

export function materializeCheckpointMessages(
  checkpoint: LoadedCheckpoint,
): RuntimeMessage[] {
  return [
    ...materializeSceneEntries(checkpoint.sceneEntries.values()),
    ...checkpoint.audioEntries.values(),
  ];
}

export function materializeStatePatchMessages(
  patch: LoadedStatePatch,
): RuntimeMessage[] {
  const sceneDeletes = patch.sceneDeleteNodes.map((name) => ({
    type: "RemoveSceneNodeMessage" as const,
    name,
  }));
  return [
    ...sceneDeletes,
    ...materializeSceneEntries(patch.scenePuts.values()),
    ...patch.audioMessages,
  ];
}

function makeLoadedStatePatch(patch: RuntimeStatePatch): LoadedStatePatch {
  return {
    scenePuts: new Map(patch.scenePuts.map((entry) => [entry.key, entry.message])),
    sceneDeleteNodes: [...patch.sceneDeleteNodes],
    audioMessages: patch.audioMessages.filter(isAudioMessage),
  };
}

function audioMessageMap(messages: RuntimeMessage[]): Map<string, AudioMessage> {
  const audioEntries = new Map<string, AudioMessage>();
  for (const message of messages) {
    if (!isAudioMessage(message)) {
      continue;
    }
    audioEntries.set(message.name, message);
  }
  return audioEntries;
}

function materializeSceneEntries(
  messages: Iterable<RuntimeMessage>,
): RuntimeMessage[] {
  const unnamedMessages: RuntimeMessage[] = [];
  const nodeMessages = new Map<string, RuntimeMessage[]>();
  for (const message of messages) {
    const name = extractMessageName(message);
    if (name === null) {
      unnamedMessages.push(message);
      continue;
    }
    const sceneMessages = nodeMessages.get(name) ?? [];
    sceneMessages.push(message);
    nodeMessages.set(name, sceneMessages);
  }

  const sceneMessages = [...unnamedMessages];
  const sortedNodeNames = Array.from(nodeMessages.keys()).sort(compareSceneNodeNames);
  for (const name of sortedNodeNames) {
    const messagesForNode = nodeMessages.get(name) ?? [];
    sceneMessages.push(...messagesForNode.filter(isCreateSceneNodeMessage));
    sceneMessages.push(
      ...messagesForNode.filter((message) => !isCreateSceneNodeMessage(message)),
    );
  }

  return sceneMessages;
}

function compareSceneNodeNames(left: string, right: string): number {
  const depthDifference = left.split("/").length - right.split("/").length;
  if (depthDifference !== 0) {
    return depthDifference;
  }
  return left.localeCompare(right);
}

function extractMessageName(message: RuntimeMessage): string | null {
  return typeof message.name === "string" && message.name.length > 0
    ? message.name
    : null;
}

function isCreateSceneNodeMessage(message: RuntimeMessage): boolean {
  return "props" in message;
}
