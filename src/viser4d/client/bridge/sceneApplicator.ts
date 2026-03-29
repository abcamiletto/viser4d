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
    private blocks: BlockCache,
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

  resetState(): void {
    this.removeRenderedTimelineNodes();
    this.audio.resetTimeline();
    this.appliedStep = -1;
    this.appliedBlock = -1;
  }

  rebuildThrough(step: number): void {
    if (!this.blocks.ensureStepLoaded(step)) {
      return;
    }
    this.resetState();
    this.applyThrough(step);
  }

  applyThrough(step: number): void {
    const blockIndex = this.blocks.blockIndexOf(step);
    if (
      this.appliedStep >= 0 &&
      (blockIndex !== this.appliedBlock || step < this.appliedStep)
    ) {
      this.rebuildThrough(step);
      return;
    }
    this.advanceThrough(step);
  }

  advanceThrough(step: number): void {
    if (!this.blocks.ensureStepLoaded(step)) {
      return;
    }
    if (this.appliedStep < 0) {
      const block = this.blocks.getBlock(step);
      if (!block) {
        return;
      }
      const blockIndex = this.blocks.blockIndexOf(step);
      this.applyBlockCheckpoint(blockIndex, block);
      this.appliedBlock = blockIndex;
      this.appliedStep = this.blocks.blockStartStep(blockIndex) - 1;
    }
    let nextStep = this.appliedStep + 1;
    while (nextStep <= step) {
      if (!this.blocks.ensureStepLoaded(nextStep)) {
        return;
      }
      const block = this.blocks.getBlock(nextStep);
      if (!block) {
        return;
      }
      const blockIndex = this.blocks.blockIndexOf(nextStep);
      const blockStart = this.blocks.blockStartStep(blockIndex);
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

  private applyBlockCheckpoint(
    blockIndex: number,
    block: LoadedBlock,
  ): void {
    if (block.checkpointMessages.length) {
      this.applyStepMessages(
        this.blocks.blockStartStep(blockIndex),
        block.checkpointMessages,
      );
    }
  }

  private removeRenderedTimelineNodes(): void {
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
