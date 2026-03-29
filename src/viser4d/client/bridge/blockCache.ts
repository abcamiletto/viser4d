/**
 * Block loading, eviction, and request tracking for the timeline runtime.
 */

import type { RuntimeMessage, RuntimeValue } from "../binary";

export type LoadedBlock = {
  checkpointMessages: RuntimeMessage[];
  stepMessages: RuntimeMessage[][];
};

export class BlockCache {
  private blocks = new Map<number, LoadedBlock>();
  private requestedBlocks = new Set<number>();
  private pendingStep: number | null = null;

  constructor(
    private sendGuiUpdate: (uuid: string, value: RuntimeValue) => void,
  ) {}

  getBlock(step: number, blockSize: number): LoadedBlock | null {
    return this.blocks.get(this.getBlockIndex(step, blockSize)) ?? null;
  }

  getBlockIndex(step: number, blockSize: number): number {
    return Math.floor(step / blockSize);
  }

  getBlockStartStep(blockIndex: number, blockSize: number): number {
    return blockIndex * blockSize;
  }

  loadBlock(blockIndex: number, block: LoadedBlock): void {
    this.requestedBlocks.delete(blockIndex);
    this.blocks.set(blockIndex, block);
  }

  evictBlock(blockIndex: number, appliedBlock: number): void {
    if (blockIndex === appliedBlock) {
      return;
    }
    this.blocks.delete(blockIndex);
    this.requestedBlocks.delete(blockIndex);
  }

  /**
   * Returns true if the step's block is loaded. If not, records it as pending
   * and sends a request to the server.
   */
  ensureStepLoaded(
    step: number,
    blockSize: number,
    blockRequestSyncUuid: string | null,
  ): boolean {
    if (this.getBlock(step, blockSize)) {
      return true;
    }
    this.pendingStep = step;
    const blockIndex = this.getBlockIndex(step, blockSize);
    if (!this.requestedBlocks.has(blockIndex) && blockRequestSyncUuid) {
      this.requestedBlocks.add(blockIndex);
      this.sendGuiUpdate(blockRequestSyncUuid, step);
    }
    return false;
  }

  getPendingStep(): number | null {
    return this.pendingStep;
  }

  clearPendingStep(): void {
    this.pendingStep = null;
  }
}
