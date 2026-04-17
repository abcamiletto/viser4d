export type BlockManifest = {
  blockIndex: number;
  stepStart: number;
  stepStop: number;
  payloadByteSize: number | null;
};

export type PreloadPlan = {
  desired: number[];
  required: number[];
  speculative: number[];
  evictions: number[];
};

type LoadedBlocks = {
  has(block: number): boolean;
  keys(): Iterable<number>;
};

/**
 * Decide which timeline blocks the client should hold for a given focus block.
 *
 * Required blocks are the current block plus its predecessor; speculative
 * blocks are filled forward up to the byte budget. Unknown-size manifests
 * claim one speculative slot so the first fetch can populate their size.
 * Anything outside the desired set is an eviction candidate.
 */
export function planPreload(
  currentBlock: number,
  manifests: readonly BlockManifest[],
  budgetBytes: number,
  loaded: LoadedBlocks,
): PreloadPlan {
  const blockCount = manifests.length;
  if (blockCount === 0) {
    return { desired: [], required: [], speculative: [], evictions: [] };
  }

  const focus = Math.max(0, Math.min(blockCount - 1, currentBlock));
  const required: number[] = [focus];
  let used = manifests[focus].payloadByteSize ?? 0;

  const previous = (focus - 1 + blockCount) % blockCount;
  if (previous !== focus) {
    required.push(previous);
    used += manifests[previous].payloadByteSize ?? 0;
  }

  const desired = new Set<number>(required);
  const speculative: number[] = [];
  for (let offset = 1; offset < blockCount; offset += 1) {
    if (used >= budgetBytes) {
      break;
    }
    const index = (focus + offset) % blockCount;
    if (desired.has(index)) {
      continue;
    }
    const size = manifests[index].payloadByteSize;
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
  for (const block of loaded.keys()) {
    if (!desired.has(block)) {
      evictions.push(block);
    }
  }
  evictions.sort((a, b) => a - b);

  return {
    desired: [...required, ...speculative],
    required,
    speculative,
    evictions,
  };
}
