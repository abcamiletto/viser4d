// The browser mirror of the canonical keyed-scene model: block decode, the
// target-state fold, and the rev-diff that turns a target map into the minimal
// list of viser messages. Pure data — no viser coupling, no audio scheduling.

import type { ScenePayload, WaveformPayload } from "./binary";
import type { TimelineBlockMessage } from "./protocol.gen";

export type SceneEntry = {
  key: string;
  rev: number;
  name: string | null;
  message: ScenePayload;
};

export type AudioEvent = { rev: number; message: ScenePayload };

export type StepDelta = {
  puts: SceneEntry[];
  deleteNodes: string[];
  audio: AudioEvent[];
};

export type AudioTrackSnapshot = {
  name: string;
  rev: number;
  sampleRate: number;
  startStep: number;
  volume: number;
  waveform: WaveformPayload;
};

export type LoadedBlock = {
  index: number;
  checkpointScene: Map<string, SceneEntry>;
  checkpointAudio: AudioTrackSnapshot[];
  deltas: StepDelta[];
};

const CREATE_PREFIX = "create:";

function isCreateKey(key: string): boolean {
  return key.startsWith(CREATE_PREFIX);
}

export function isDescendant(name: string | null, ancestor: string): boolean {
  return name === ancestor || (name !== null && name.startsWith(ancestor + "/"));
}

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

/** Topmost ancestors of a name list (drop any name whose ancestor is also present). */
function topmost(names: readonly string[]): string[] {
  const set = new Set(names);
  return names.filter((name) => !hasAncestorInSet(name, set));
}

function compareNodeNames(left: string, right: string): number {
  const depth = left.split("/").length - right.split("/").length;
  return depth !== 0 ? depth : left.localeCompare(right);
}

/**
 * Order scene entries for viser: globals (nameless) first, then nodes
 * parent-before-child, each node's `create` entry ahead of its properties.
 */
function orderEntries(entries: Iterable<SceneEntry>): ScenePayload[] {
  const globals: ScenePayload[] = [];
  const byNode = new Map<string, SceneEntry[]>();
  for (const entry of entries) {
    if (entry.name === null) {
      globals.push(entry.message);
      continue;
    }
    const bucket = byNode.get(entry.name);
    if (bucket) {
      bucket.push(entry);
    } else {
      byNode.set(entry.name, [entry]);
    }
  }
  const out = [...globals];
  for (const name of [...byNode.keys()].sort(compareNodeNames)) {
    const bucket = byNode.get(name)!;
    for (const entry of bucket) {
      if (isCreateKey(entry.key)) {
        out.push(entry.message);
      }
    }
    for (const entry of bucket) {
      if (!isCreateKey(entry.key)) {
        out.push(entry.message);
      }
    }
  }
  return out;
}

// --- block decode -----------------------------------------------------------

export function decodeBlock(message: TimelineBlockMessage): LoadedBlock {
  const checkpointScene = new Map<string, SceneEntry>();
  for (const entry of message.checkpointScene) {
    checkpointScene.set(entry.key, entry);
  }
  return {
    index: message.index,
    checkpointScene,
    checkpointAudio: message.checkpointAudio.map((track) => ({
      name: track.name,
      rev: track.rev,
      sampleRate: track.sampleRate,
      startStep: track.startStep,
      volume: track.volume,
      waveform: track.waveform,
    })),
    deltas: message.deltas.map((delta) => ({
      puts: delta.puts,
      deleteNodes: delta.deleteNodes,
      audio: delta.audio,
    })),
  };
}

// --- fold -------------------------------------------------------------------

function deleteNodeFromState(state: Map<string, SceneEntry>, name: string): void {
  for (const [key, entry] of state) {
    if (isDescendant(entry.name, name)) {
      state.delete(key);
    }
  }
}

function putEntryIntoState(state: Map<string, SceneEntry>, entry: SceneEntry): void {
  if (isCreateKey(entry.key) && entry.name !== null) {
    // Re-creating a node resets its own (non-create) properties; descendants
    // keep their keys (they carry different names).
    for (const [key, existing] of state) {
      if (key !== entry.key && existing.name === entry.name) {
        state.delete(key);
      }
    }
  }
  state.set(entry.key, entry);
}

function applyDelta(state: Map<string, SceneEntry>, delta: StepDelta): void {
  for (const name of delta.deleteNodes) {
    deleteNodeFromState(state, name);
  }
  for (const put of delta.puts) {
    putEntryIntoState(state, put);
  }
}

export function isTombstone(entry: SceneEntry): boolean {
  return entry.message.type === "RemoveSceneNodeMessage" && entry.name !== null;
}

/**
 * Fold one override entry into the overlay. A tombstone shadows everything it
 * covers (entries for the same node or descendants) and persists so later
 * seeks keep the node deleted; any other entry is an ordinary upsert.
 */
export function applyOverrideEntry(
  overlay: Map<string, SceneEntry>,
  entry: SceneEntry,
): void {
  if (isTombstone(entry)) {
    for (const [key, existing] of overlay) {
      if (isDescendant(existing.name, entry.name!)) {
        overlay.delete(key);
      }
    }
  }
  overlay.set(entry.key, entry);
}

function applyOverlay(
  state: Map<string, SceneEntry>,
  overlay: ReadonlyMap<string, SceneEntry>,
): void {
  // Insertion order matters: a tombstone shadows earlier puts for its subtree.
  for (const entry of overlay.values()) {
    if (isTombstone(entry)) {
      deleteNodeFromState(state, entry.name!);
      continue;
    }
    // A put override applies only where its node exists (globals apply always).
    if (entry.name === null || state.has(CREATE_PREFIX + entry.name)) {
      state.set(entry.key, entry);
    }
  }
}

/**
 * Target scene state at block offset `offset`: the checkpoint is the folded
 * state *before* the block's first delta, so fold `deltas[0..offset]` inclusive,
 * then apply the override overlay on top.
 */
export function foldTarget(
  block: LoadedBlock,
  offset: number,
  overlay: ReadonlyMap<string, SceneEntry>,
): Map<string, SceneEntry> {
  const state = new Map(block.checkpointScene);
  for (let i = 0; i <= offset; i += 1) {
    const delta = block.deltas[i];
    if (delta) {
      applyDelta(state, delta);
    }
  }
  applyOverlay(state, overlay);
  return state;
}

// --- applied mirror + rev-diff ----------------------------------------------

type AppliedEntry = { rev: number; name: string | null; isCreate: boolean };

const removeMessage = (name: string): ScenePayload => ({
  type: "RemoveSceneNodeMessage",
  name,
});

export class SceneMirror {
  private applied = new Map<string, AppliedEntry>();

  reset(): void {
    this.applied.clear();
  }

  private existingNodes(): Set<string> {
    const names = new Set<string>();
    for (const entry of this.applied.values()) {
      if (entry.isCreate && entry.name !== null) {
        names.add(entry.name);
      }
    }
    return names;
  }

  private dropSubtree(name: string): void {
    for (const [key, entry] of this.applied) {
      if (isDescendant(entry.name, name)) {
        this.applied.delete(key);
      }
    }
  }

  private setApplied(entry: SceneEntry): void {
    this.applied.set(entry.key, {
      rev: entry.rev,
      name: entry.name,
      isCreate: isCreateKey(entry.key),
    });
  }

  private applyCreateToApplied(entry: SceneEntry): void {
    if (entry.name !== null) {
      for (const [key, existing] of this.applied) {
        if (key !== entry.key && existing.name === entry.name) {
          this.applied.delete(key);
        }
      }
    }
    this.applied.set(entry.key, { rev: entry.rev, name: entry.name, isCreate: true });
  }

  /** Fast path: apply one forward delta directly (the delta *is* the diff). */
  advance(delta: StepDelta): ScenePayload[] {
    const doomed = delta.deleteNodes.filter((name) =>
      this.applied.has(CREATE_PREFIX + name),
    );
    const out: ScenePayload[] = topmost(doomed).map(removeMessage);
    for (const name of doomed) {
      this.dropSubtree(name);
    }
    out.push(...orderEntries(delta.puts));
    for (const put of delta.puts) {
      if (isCreateKey(put.key)) {
        this.applyCreateToApplied(put);
      }
    }
    for (const put of delta.puts) {
      if (!isCreateKey(put.key)) {
        this.setApplied(put);
      }
    }
    return out;
  }

  /**
   * Re-apply the override overlay after a forward advance. Tombstones emit a
   * remove only while the applied scene still holds the node; puts emit only
   * when their rev differs from what is applied. Nearly free in steady state.
   */
  reapplyOverrides(overlay: ReadonlyMap<string, SceneEntry>): ScenePayload[] {
    const removes: ScenePayload[] = [];
    const pushes: SceneEntry[] = [];
    for (const entry of overlay.values()) {
      if (isTombstone(entry)) {
        removes.push(...this.removeNode(entry.name!));
        continue;
      }
      if (entry.name !== null && !this.applied.has(CREATE_PREFIX + entry.name)) {
        continue;
      }
      const applied = this.applied.get(entry.key);
      if (!applied || applied.rev !== entry.rev) {
        pushes.push(entry);
        this.setApplied(entry);
      }
    }
    return [...removes, ...orderEntries(pushes)];
  }

  /** An override RemoveSceneNodeMessage: delete the node from the applied scene. */
  removeNode(name: string): ScenePayload[] {
    let held = false;
    for (const entry of this.applied.values()) {
      if (isDescendant(entry.name, name)) {
        held = true;
        break;
      }
    }
    this.dropSubtree(name);
    return held ? [removeMessage(name)] : [];
  }

  /** Full rev-diff from the applied state to `target` (ARCHITECTURE "Client diff"). */
  rebuild(target: ReadonlyMap<string, SceneEntry>): ScenePayload[] {
    const targetNodes = new Set<string>();
    const targetByNode = new Map<string, SceneEntry[]>();
    const targetGlobals: SceneEntry[] = [];
    for (const [key, entry] of target) {
      if (entry.name === null) {
        targetGlobals.push(entry);
        continue;
      }
      if (isCreateKey(key)) {
        targetNodes.add(entry.name);
      }
      const bucket = targetByNode.get(entry.name);
      if (bucket) {
        bucket.push(entry);
      } else {
        targetByNode.set(entry.name, [entry]);
      }
    }

    const appliedByNode = new Map<string, string[]>();
    for (const [key, entry] of this.applied) {
      if (entry.name === null) {
        continue;
      }
      const bucket = appliedByNode.get(entry.name);
      if (bucket) {
        bucket.push(key);
      } else {
        appliedByNode.set(entry.name, [key]);
      }
    }

    // 1. Nodes we hold that vanished from the target -> remove (topmost only).
    const appliedNodes = this.existingNodes();
    const vanished = [...appliedNodes].filter((name) => !targetNodes.has(name));
    const removes = topmost(vanished).map(removeMessage);

    // 2/3. Nodes present in the target.
    const pushes: SceneEntry[] = [];
    for (const [name, entries] of targetByNode) {
      const isNew = !appliedNodes.has(name);
      let stale = false;
      if (!isNew) {
        const targetCreate = target.get(CREATE_PREFIX + name);
        const appliedCreate = this.applied.get(CREATE_PREFIX + name);
        if (!targetCreate || !appliedCreate || appliedCreate.rev !== targetCreate.rev) {
          stale = true;
        } else {
          for (const key of appliedByNode.get(name) ?? []) {
            if (!target.has(key)) {
              stale = true;
              break;
            }
          }
        }
      }
      if (isNew || stale) {
        // Re-push create + every target entry: viser's upsert resets props.
        pushes.push(...entries);
      } else {
        for (const entry of entries) {
          const applied = this.applied.get(entry.key);
          if (!applied || applied.rev !== entry.rev) {
            pushes.push(entry);
          }
        }
      }
    }

    // Globals: push on rev change (a vanished global key cannot be unset).
    for (const entry of targetGlobals) {
      const applied = this.applied.get(entry.key);
      if (!applied || applied.rev !== entry.rev) {
        pushes.push(entry);
      }
    }

    // Applied becomes the target, retaining un-settable vanished globals.
    const next = new Map<string, AppliedEntry>();
    for (const [key, entry] of target) {
      next.set(key, { rev: entry.rev, name: entry.name, isCreate: isCreateKey(key) });
    }
    for (const [key, entry] of this.applied) {
      if (entry.name === null && !target.has(key)) {
        next.set(key, entry);
      }
    }
    this.applied = next;

    return [...removes, ...orderEntries(pushes)];
  }
}
