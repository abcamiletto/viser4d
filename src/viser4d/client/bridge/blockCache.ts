import type { RuntimeMessage } from "../binary";

export type LoadedBlock = {
  checkpointMessages: RuntimeMessage[];
  stepMessages: RuntimeMessage[][];
};

export class BlockCache {
  blockSize = 64;
  pendingStep: number | null = null;
  private blocks = new Map<number, LoadedBlock>();
  private requestedBlocks = new Set<number>();

  constructor(private requestStep: (step: number) => void) {}

  getBlock(step: number): LoadedBlock | null {
    return this.blocks.get(Math.floor(step / this.blockSize)) ?? null;
  }

  blockIndexOf(step: number): number {
    return Math.floor(step / this.blockSize);
  }

  blockStartStep(blockIndex: number): number {
    return blockIndex * this.blockSize;
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
