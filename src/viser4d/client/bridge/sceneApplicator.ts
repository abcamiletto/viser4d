import type { RuntimeMessage } from "../binary";
import { AudioRuntime } from "../audio/runtime";
import { isAudioMessage, type AudioMessage } from "../audio/messages";
import { BlockCache } from "./blockCache";

type RenderedTimelineNodes = Map<string, string>;

export class SceneApplicator {
  appliedStep = -1;
  appliedBlock = -1;
  private overrides = new Map<string, RuntimeMessage>();

  constructor(
    private pushMessages: (messages: RuntimeMessage[]) => void,
    private audio: AudioRuntime,
    private blocks: BlockCache,
    private renderedTimelineNodes: RenderedTimelineNodes,
  ) {}

  applyStepMessages(
    step: number,
    messages: RuntimeMessage[],
    preservedNodes: ReadonlySet<string> = _NO_PRESERVED_NODES,
  ): void {
    const sceneMessages: RuntimeMessage[] = [];
    const audioMessages: AudioMessage[] = [];
    for (const message of messages) {
      if (isAudioMessage(message)) {
        audioMessages.push(message);
      } else {
        const normalized = this.normalizeSceneMessage(message, preservedNodes);
        if (!normalized) {
          continue;
        }
        this.trackTimelineNode(normalized);
        sceneMessages.push(normalized);
      }
    }
    if (sceneMessages.length) {
      this.pushMessages(sceneMessages);
    }
    if (audioMessages.length) {
      this.audio.applyTimelineMessages(step, audioMessages);
    }
  }

  applyOverride(message: RuntimeMessage): void {
    const key = overrideKey(message);
    if (message.type === "RemoveSceneNodeMessage") {
      this.removeOverridesForSubtree(typeof message.name === "string" ? message.name : "");
    }
    this.overrides.set(key, message);
    this.pushMessages([message]);
  }

  resetState(): void {
    this.removeRenderedTimelineNodes();
    this.audio.resetTimeline();
    this.overrides.clear();
    this.appliedStep = -1;
    this.appliedBlock = -1;
  }

  rebuildThrough(step: number): void {
    if (!this.blocks.ensureStepLoaded(step)) {
      return;
    }
    const preservedNodes = this.prepareForRebuild(step);
    this.audio.resetTimeline();
    this.appliedStep = -1;
    this.appliedBlock = -1;
    this.advanceThrough(step, preservedNodes);
  }

  applyThrough(
    step: number,
    preservedNodes: ReadonlySet<string> = _NO_PRESERVED_NODES,
  ): void {
    const blockIndex = this.blocks.blockIndexOf(step);
    if (
      this.appliedStep >= 0 &&
      (blockIndex !== this.appliedBlock || step < this.appliedStep)
    ) {
      this.rebuildThrough(step);
      return;
    }
    this.advanceThrough(step, preservedNodes);
  }

  advanceThrough(
    step: number,
    preservedNodes: ReadonlySet<string> = _NO_PRESERVED_NODES,
  ): void {
    if (!this.blocks.ensureStepLoaded(step)) {
      return;
    }
    if (this.appliedStep < 0) {
      const block = this.blocks.getBlock(step);
      if (!block) {
        return;
      }
      const blockIndex = this.blocks.blockIndexOf(step);
      if (block.checkpointMessages.length) {
        this.applyStepMessages(
          this.blocks.blockStartStep(blockIndex),
          block.checkpointMessages,
          preservedNodes,
        );
      }
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
          this.applyStepMessages(index, messages, preservedNodes);
        }
      }
      this.appliedBlock = blockIndex;
      this.appliedStep = blockEnd;
      nextStep = blockEnd + 1;
    }
    this.reapplyOverrides();
  }

  private reapplyOverrides(): void {
    if (!this.overrides.size) {
      return;
    }
    this.pushMessages(Array.from(this.overrides.values()));
  }

  private removeOverridesForSubtree(name: string): void {
    if (!name) {
      return;
    }
    const prefix = `${name}/`;
    for (const key of Array.from(this.overrides.keys())) {
      const existingName = this.overrides.get(key)?.name;
      if (
        typeof existingName === "string" &&
        (existingName === name || existingName.startsWith(prefix))
      ) {
        this.overrides.delete(key);
      }
    }
  }

  private removeRenderedTimelineNodes(): void {
    this.removeTrackedTimelineNodes(new Set(this.renderedTimelineNodes.keys()));
  }

  private prepareForRebuild(step: number): ReadonlySet<string> {
    const targetNodes = this.collectTargetTimelineNodes(step);
    const preservedNodes = new Set<string>();
    const nodesToRemove = new Set<string>();

    for (const [name, currentType] of this.renderedTimelineNodes) {
      if (targetNodes.get(name) === currentType) {
        preservedNodes.add(name);
        continue;
      }
      nodesToRemove.add(name);
    }

    this.removeTrackedTimelineNodes(nodesToRemove);
    return preservedNodes;
  }

  private collectTargetTimelineNodes(step: number): RenderedTimelineNodes {
    const block = this.blocks.getBlock(step);
    if (!block) {
      return new Map<string, string>();
    }
    const targetNodes: RenderedTimelineNodes = new Map();
    for (const message of block.checkpointMessages) {
      if (!isAudioMessage(message)) {
        this.trackTimelineNode(message, targetNodes);
      }
    }
    const blockIndex = this.blocks.blockIndexOf(step);
    const blockStart = this.blocks.blockStartStep(blockIndex);
    for (let index = blockStart; index <= step; index += 1) {
      const messages = block.stepMessages[index - blockStart] ?? [];
      for (const message of messages) {
        if (!isAudioMessage(message)) {
          this.trackTimelineNode(message, targetNodes);
        }
      }
    }
    return targetNodes;
  }

  private removeTrackedTimelineNodes(nodesToRemove: ReadonlySet<string>): void {
    if (!nodesToRemove.size) {
      return;
    }
    const minimalRemovals = Array.from(nodesToRemove).filter(
      (name) => !hasAncestorInSet(name, nodesToRemove),
    );
    this.pushMessages(
      minimalRemovals.map((name) => ({
        type: "RemoveSceneNodeMessage",
        name,
      })),
    );
    for (const name of Array.from(this.renderedTimelineNodes.keys())) {
      if (hasAncestorInSet(name, nodesToRemove) || nodesToRemove.has(name)) {
        this.renderedTimelineNodes.delete(name);
      }
    }
  }

  private normalizeSceneMessage(
    message: RuntimeMessage,
    preservedNodes: ReadonlySet<string>,
  ): RuntimeMessage | null {
    const name = typeof message.name === "string" ? message.name : null;
    if (!name) {
      return message;
    }
    if (message.type === "RemoveSceneNodeMessage") {
      for (const preservedName of preservedNodes) {
        if (
          preservedName === name ||
          preservedName.startsWith(`${name}/`)
        ) {
          return null;
        }
      }
      return message;
    }
    if (preservedNodes.has(name) && isCreateSceneNodeMessage(message)) {
      return {
        type: "SceneNodeUpdateMessage",
        name,
        updates: message.props as RuntimeMessage["updates"],
      };
    }
    return message;
  }

  private trackTimelineNode(
    message: RuntimeMessage,
    renderedTimelineNodes: RenderedTimelineNodes = this.renderedTimelineNodes,
  ): void {
    const name = typeof message.name === "string" ? message.name : null;
    if (!name) {
      return;
    }
    if (message.type === "RemoveSceneNodeMessage") {
      for (const nodeName of Array.from(renderedTimelineNodes.keys())) {
        if (nodeName === name || nodeName.startsWith(`${name}/`)) {
          renderedTimelineNodes.delete(nodeName);
        }
      }
      return;
    }
    if (isCreateSceneNodeMessage(message)) {
      renderedTimelineNodes.set(name, message.type);
    }
  }
}

const _NO_PRESERVED_NODES = new Set<string>();

function hasAncestorInSet(name: string, names: ReadonlySet<string>): boolean {
  let slash = name.lastIndexOf("/");
  while (slash > 0) {
    const parent = name.slice(0, slash);
    if (names.has(parent)) {
      return true;
    }
    slash = parent.lastIndexOf("/");
  }
  return false;
}

function isCreateSceneNodeMessage(message: RuntimeMessage): boolean {
  return "props" in message;
}

function overrideKey(message: RuntimeMessage): string {
  const name = typeof message.name === "string" ? message.name : "";
  return `${message.type}_${name}`;
}
