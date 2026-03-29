/**
 * Applies timeline messages to the scene and tracks rendered timeline nodes.
 */

import type { RuntimeMessage } from "../binary";
import { AudioRuntime } from "../audio/runtime";
import { isAudioMessage, type AudioMessage } from "../audio/messages";
import { BlockCache, type LoadedBlock } from "./blockCache";

export class SceneApplicator {
  appliedStep = -1;
  appliedBlock = -1;

  constructor(
    private pushMessages: (messages: RuntimeMessage[]) => void,
    private audio: AudioRuntime,
    private renderedTimelineNodes: Set<string>,
  ) {}

  applyStepMessages(step: number, messages: RuntimeMessage[]): void {
    const sceneMessages: RuntimeMessage[] = [];
    const audioMessages: AudioMessage[] = [];
    for (const message of messages) {
      if (isAudioMessage(message)) {
        audioMessages.push(message);
      } else {
        this.trackTimelineNode(message);
        sceneMessages.push(message);
      }
    }
    if (sceneMessages.length) {
      this.pushMessages(sceneMessages);
    }
    if (audioMessages.length) {
      this.audio.applyTimelineMessages(step, audioMessages);
    }
  }

  removeRenderedTimelineNodes(): void {
    if (!this.renderedTimelineNodes.size) {
      return;
    }
    this.pushMessages(
      Array.from(this.renderedTimelineNodes).map((name) => ({
        type: "RemoveSceneNodeMessage",
        name,
      })),
    );
    this.renderedTimelineNodes.clear();
  }

  applyBlockCheckpoint(
    blockIndex: number,
    block: LoadedBlock,
    blockSize: number,
  ): void {
    if (block.checkpointMessages.length) {
      this.applyStepMessages(blockIndex * blockSize, block.checkpointMessages);
    }
  }

  resetState(): void {
    this.removeRenderedTimelineNodes();
    this.audio.resetTimeline();
    this.appliedStep = -1;
    this.appliedBlock = -1;
  }

  /**
   * Apply steps forward from the current applied position to `step`.
   * Assumes checkpoint is already applied if appliedStep < 0.
   */
  advanceThrough(
    step: number,
    blockCache: BlockCache,
    blockSize: number,
    blockRequestSyncUuid: string | null,
  ): void {
    if (!blockCache.ensureStepLoaded(step, blockSize, blockRequestSyncUuid)) {
      return;
    }
    if (this.appliedStep < 0) {
      const block = blockCache.getBlock(step, blockSize);
      if (!block) {
        return;
      }
      const blockIndex = blockCache.getBlockIndex(step, blockSize);
      this.applyBlockCheckpoint(blockIndex, block, blockSize);
      this.appliedBlock = blockIndex;
      this.appliedStep = blockCache.getBlockStartStep(blockIndex, blockSize) - 1;
    }
    let nextStep = this.appliedStep + 1;
    while (nextStep <= step) {
      if (
        !blockCache.ensureStepLoaded(nextStep, blockSize, blockRequestSyncUuid)
      ) {
        return;
      }
      const block = blockCache.getBlock(nextStep, blockSize);
      if (!block) {
        return;
      }
      const blockIndex = blockCache.getBlockIndex(nextStep, blockSize);
      const blockStart = blockCache.getBlockStartStep(blockIndex, blockSize);
      const blockEnd = Math.min(
        step,
        blockStart + block.stepMessages.length - 1,
      );
      for (let index = nextStep; index <= blockEnd; index += 1) {
        const messages = block.stepMessages[index - blockStart] ?? [];
        if (messages.length) {
          this.applyStepMessages(index, messages);
        }
      }
      this.appliedBlock = blockIndex;
      this.appliedStep = blockEnd;
      nextStep = blockEnd + 1;
    }
  }

  /**
   * Apply from checkpoint through `step`. Rebuilds if the target is in a
   * different block or before the current position.
   */
  applyThrough(
    step: number,
    blockCache: BlockCache,
    blockSize: number,
    blockRequestSyncUuid: string | null,
  ): void {
    const blockIndex = blockCache.getBlockIndex(step, blockSize);
    if (
      this.appliedStep >= 0 &&
      (blockIndex !== this.appliedBlock || step < this.appliedStep)
    ) {
      this.rebuildThrough(step, blockCache, blockSize, blockRequestSyncUuid);
      return;
    }
    this.advanceThrough(step, blockCache, blockSize, blockRequestSyncUuid);
  }

  rebuildThrough(
    step: number,
    blockCache: BlockCache,
    blockSize: number,
    blockRequestSyncUuid: string | null,
  ): void {
    if (!blockCache.ensureStepLoaded(step, blockSize, blockRequestSyncUuid)) {
      return;
    }
    this.resetState();
    this.applyThrough(step, blockCache, blockSize, blockRequestSyncUuid);
  }

  private trackTimelineNode(message: RuntimeMessage): void {
    const name = typeof message.name === "string" ? message.name : null;
    if (!name) {
      return;
    }
    if (message.type === "RemoveSceneNodeMessage") {
      const prefix = `${name}/`;
      for (const nodeName of Array.from(this.renderedTimelineNodes)) {
        if (nodeName === name || nodeName.startsWith(prefix)) {
          this.renderedTimelineNodes.delete(nodeName);
        }
      }
      return;
    }
    this.renderedTimelineNodes.add(name);
  }
}
