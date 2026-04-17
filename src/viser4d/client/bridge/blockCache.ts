import type { RuntimeMessage } from "../binary";
import {
  makeLoadedBlock,
  patchLoadedBlock,
  type KeyedRuntimeMessage,
  type LoadedBlock,
  type RuntimeStatePatch,
  type StepPatchUpdate,
} from "./blockState";

export class BlockCache {
  blockSize = 32;
  pendingStep: number | null = null;
  private blocks = new Map<number, LoadedBlock>();
  private requestedBlocks = new Set<number>();

  constructor(private requestStep: (step: number) => void) {}

  getBlock(step: number): LoadedBlock | null {
    return this.blocks.get(Math.floor(step / this.blockSize)) ?? null;
  }

  getBlockByIndex(blockIndex: number): LoadedBlock | null {
    return this.blocks.get(blockIndex) ?? null;
  }

  hasBlockIndex(blockIndex: number): boolean {
    return this.blocks.has(blockIndex);
  }

  blockIndexOf(step: number): number {
    return Math.floor(step / this.blockSize);
  }

  blockStartStep(blockIndex: number): number {
    return blockIndex * this.blockSize;
  }

  loadBlock(
    blockIndex: number,
    block: {
      checkpointSceneEntries: KeyedRuntimeMessage[];
      checkpointAudioMessages: RuntimeMessage[];
      stepPatches: RuntimeStatePatch[];
    },
  ): void {
    this.requestedBlocks.delete(blockIndex);
    this.blocks.set(blockIndex, makeLoadedBlock(block));
  }

  restoreBlock(blockIndex: number, block: LoadedBlock): void {
    this.requestedBlocks.delete(blockIndex);
    this.blocks.set(blockIndex, block);
  }

  patchBlock(
    blockIndex: number,
    patch: {
      checkpointScenePuts: KeyedRuntimeMessage[];
      checkpointSceneDeletes: string[];
      checkpointAudioPuts: RuntimeMessage[];
      checkpointAudioDeletes: string[];
      stepPatchUpdates: StepPatchUpdate[];
    },
  ): boolean {
    const block = this.blocks.get(blockIndex);
    if (!block) {
      return false;
    }
    patchLoadedBlock(block, patch);
    return true;
  }

  evictBlock(blockIndex: number, appliedBlock: number): void {
    if (blockIndex === appliedBlock) {
      return;
    }
    this.blocks.delete(blockIndex);
    this.requestedBlocks.delete(blockIndex);
  }

  reset(): void {
    this.pendingStep = null;
    this.blocks.clear();
    this.requestedBlocks.clear();
  }

  ensureStepLoaded(step: number): boolean {
    if (this.getBlock(step)) {
      return true;
    }
    this.pendingStep = step;
    const blockIndex = this.blockIndexOf(step);
    if (!this.requestedBlocks.has(blockIndex)) {
      this.requestedBlocks.add(blockIndex);
      this.requestStep(step);
    }
    return false;
  }
}
