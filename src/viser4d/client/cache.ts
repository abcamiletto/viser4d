// Block cache + preload planner. Holds decoded blocks under a byte budget,
// keeps the current and previous block resident, speculatively fills forward,
// and evicts the rest. One speculative request is in flight at a time.

import type { LoadedBlock } from "./state";

export type CacheIO = {
  requestBlock(index: number): void;
  discardBlock(index: number): void;
};

type PreloadPlan = {
  required: number[];
  speculative: number[];
  evictions: number[];
};

/**
 * Which blocks to hold for a given focus block. Required = focus + predecessor;
 * speculative = forward fill under budget (a block of unknown size claims one
 * slot so its first fetch can populate its size); everything else is evictable.
 */
function planPreload(
  focusBlock: number,
  blockBytes: readonly (number | null)[],
  budgetBytes: number,
  loaded: ReadonlyMap<number, unknown>,
): PreloadPlan {
  const count = blockBytes.length;
  if (count === 0) {
    return { required: [], speculative: [], evictions: [] };
  }
  const focus = Math.max(0, Math.min(count - 1, focusBlock));
  const required = [focus];
  let used = blockBytes[focus] ?? 0;
  if (focus > 0) {
    required.push(focus - 1);
    used += blockBytes[focus - 1] ?? 0;
  }

  const desired = new Set<number>(required);
  const speculative: number[] = [];
  for (let offset = 1; offset < count; offset += 1) {
    if (used >= budgetBytes) {
      break;
    }
    const index = (focus + offset) % count;
    if (desired.has(index)) {
      continue;
    }
    const size = blockBytes[index];
    if (size === null) {
      speculative.push(index);
      desired.add(index);
      break;
    }
    if (used + size > budgetBytes) {
      break;
    }
    speculative.push(index);
    desired.add(index);
    used += size;
  }

  const evictions: number[] = [];
  for (const index of loaded.keys()) {
    if (!desired.has(index)) {
      evictions.push(index);
    }
  }
  evictions.sort((a, b) => a - b);
  return { required, speculative, evictions };
}

export class BlockCache {
  blockSize = 1;
  private budgetBytes = 0;
  private blockBytes: readonly (number | null)[] = [];
  private blocks = new Map<number, LoadedBlock>();
  private pending = new Set<number>();

  constructor(private readonly io: CacheIO) {}

  blockIndexOf(step: number): number {
    return Math.floor(step / this.blockSize);
  }

  blockStartStep(index: number): number {
    return index * this.blockSize;
  }

  getBlock(step: number): LoadedBlock | null {
    return this.blocks.get(this.blockIndexOf(step)) ?? null;
  }

  setBlockBytes(blockBytes: readonly (number | null)[]): void {
    this.blockBytes = blockBytes;
  }

  setBudgetBytes(bytes: number): void {
    this.budgetBytes = bytes;
  }

  loadBlock(block: LoadedBlock): void {
    this.pending.delete(block.index);
    this.blocks.set(block.index, block);
  }

  reset(): void {
    this.blocks.clear();
    this.pending.clear();
  }

  /** Request the block holding `step` unless it is already resident. */
  ensureStepLoaded(step: number): void {
    const index = this.blockIndexOf(step);
    if (!this.blocks.has(index)) {
      this.issue(index);
    }
  }

  /** Re-plan the resident set around `focusBlock`, pinning the applied block. */
  syncFocus(focusBlock: number, appliedBlock: number): void {
    if (this.blockBytes.length === 0 || this.budgetBytes <= 0) {
      return;
    }
    const plan = planPreload(focusBlock, this.blockBytes, this.budgetBytes, this.blocks);
    const desired = new Set<number>([...plan.required, ...plan.speculative]);

    for (const index of [...this.pending]) {
      if (!desired.has(index)) {
        this.pending.delete(index);
        this.io.discardBlock(index);
      }
    }

    for (const index of plan.evictions) {
      if (index === appliedBlock) {
        continue; // evicting the rendered block would blank the scene mid-seek
      }
      this.blocks.delete(index);
      this.pending.delete(index);
      this.io.discardBlock(index);
    }

    let requiredInFlight = false;
    for (const index of plan.required) {
      if (this.blocks.has(index)) {
        continue;
      }
      requiredInFlight = true;
      this.issue(index);
    }
    if (requiredInFlight) {
      return; // never speculate ahead of a required load
    }
    for (const index of plan.speculative) {
      if (!this.blocks.has(index)) {
        this.issue(index);
        return; // one speculative request at a time
      }
    }
  }

  private issue(index: number): void {
    if (this.pending.has(index)) {
      return;
    }
    this.pending.add(index);
    this.io.requestBlock(index);
  }
}
