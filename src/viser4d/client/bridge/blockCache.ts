import type { RuntimeMessage } from "../binary";
import {
  makeLoadedBlock,
  patchLoadedBlock,
  type KeyedRuntimeMessage,
  type LoadedBlock,
  type RuntimeStatePatch,
  type StepPatchUpdate,
} from "./blockState";
import {
  planPreload,
  type BlockManifest,
} from "./preloadPlanner";

export type BlockCacheIO = {
  requestBlock(blockIndex: number): void;
  discardBlock(blockIndex: number): void;
};

export class BlockCache {
  blockSize = 32;
  pendingStep: number | null = null;
  private budgetBytes = 0;
  private manifests: readonly BlockManifest[] = [];
  private blocks = new Map<number, LoadedBlock>();
  private pendingRequests = new Set<number>();

  constructor(private io: BlockCacheIO) {}

  getBlock(step: number): LoadedBlock | null {
    return this.blocks.get(this.blockIndexOf(step)) ?? null;
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
    this.pendingRequests.delete(blockIndex);
    this.blocks.set(blockIndex, makeLoadedBlock(block));
  }

  restoreBlock(blockIndex: number, block: LoadedBlock): void {
    this.pendingRequests.delete(blockIndex);
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

  reset(): void {
    this.pendingStep = null;
    this.blocks.clear();
    this.pendingRequests.clear();
  }

  setManifests(manifests: readonly BlockManifest[]): void {
    this.manifests = manifests;
  }

  setBudgetBytes(bytes: number): void {
    this.budgetBytes = bytes;
  }

  syncCurrentBlock(currentBlock: number): void {
    if (this.manifests.length === 0 || this.budgetBytes <= 0) {
      return;
    }
    const plan = planPreload(
      currentBlock,
      this.manifests,
      this.budgetBytes,
      this.blocks,
    );

    for (const blockIndex of plan.evictions) {
      this.blocks.delete(blockIndex);
      this.pendingRequests.delete(blockIndex);
      this.io.discardBlock(blockIndex);
    }

    let requiredInFlight = false;
    for (const blockIndex of plan.required) {
      if (this.blocks.has(blockIndex)) {
        continue;
      }
      requiredInFlight = true;
      if (!this.pendingRequests.has(blockIndex)) {
        this.issueRequest(blockIndex);
      }
    }

    // At most one speculative request in flight, and never ahead of a required
    // one. syncCurrentBlock re-fires when a block arrives, so the next
    // speculative request issues naturally once the previous completes.
    if (requiredInFlight) {
      return;
    }
    for (const blockIndex of plan.speculative) {
      if (this.blocks.has(blockIndex)) {
        continue;
      }
      if (!this.pendingRequests.has(blockIndex)) {
        this.issueRequest(blockIndex);
      }
      return;
    }
  }

  ensureStepLoaded(step: number): boolean {
    const blockIndex = this.blockIndexOf(step);
    if (this.blocks.has(blockIndex)) {
      return true;
    }
    this.pendingStep = step;
    if (!this.pendingRequests.has(blockIndex)) {
      this.issueRequest(blockIndex);
    }
    return false;
  }

  private issueRequest(blockIndex: number): void {
    this.pendingRequests.add(blockIndex);
    this.io.requestBlock(blockIndex);
  }
}
