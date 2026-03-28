"""Benchmark: seek (block_payload) latency and memory across a heavy timeline.

Simulates a client seeking to various positions in a large recorded sequence.
block_payload(N) is what the server calls when a client requests block N.

Usage:
    uv run --group dev python benchmarks/seek_performance.py
"""

from __future__ import annotations

import gc
import time
import tracemalloc
from dataclasses import dataclass

import numpy as np

import viser4d
from viser4d.timeline._store import TimelineStore


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NUM_OBJECTS = 200       # scene objects per step (frames with position/rotation)
NUM_STEPS = 512         # total timeline steps
BLOCK_SIZE = 64         # default in TimelineStore
FPS = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class SeekResult:
    block_index: int
    step: int
    elapsed_ms: float
    peak_memory_kb: float


def record_heavy_scene(num_steps: int, num_objects: int) -> viser4d.Viser4dServer:
    server = viser4d.Viser4dServer(num_steps=num_steps, fps=FPS, port=0, verbose=False)
    rng = np.random.default_rng(42)
    base = rng.uniform(-2.5, 2.5, size=(num_objects, 3)).astype(np.float32)
    phase = rng.uniform(0.0, 2.0 * np.pi, size=num_objects).astype(np.float32)
    speed = rng.uniform(0.4, 1.8, size=num_objects).astype(np.float32)
    amp = rng.uniform(0.05, 0.35, size=num_objects).astype(np.float32)

    handles = []
    with server.at(0):
        for i in range(num_objects):
            h = server.scene.add_frame(f"/obj/{i}", axes_length=0.05)
            h.position = base[i]
            handles.append(h)

    for step in range(num_steps):
        t = (step / num_steps) * 2.0 * np.pi
        with server.at(step):
            for i, h in enumerate(handles):
                theta = t * speed[i] + phase[i]
                h.position = (
                    base[i, 0] + amp[i] * np.cos(theta),
                    base[i, 1] + amp[i] * np.sin(1.7 * theta),
                    base[i, 2] + 0.25 * amp[i] * np.sin(2.3 * theta),
                )
                h.wxyz = (np.cos(theta * 0.5), 0.0, np.sin(theta * 0.5), 0.0)

    return server


def flush_all_blocks(store: TimelineStore) -> None:
    """Force all in-memory blocks to disk and clear caches, simulating a cold seek."""
    with store._lock:
        for block_index, block in list(store._loaded_blocks.items()):
            store._flush_block(block_index, block)
        for block_index in list(store._pending_flushes):
            store._wait_for_pending_flush(block_index)
        store._loaded_blocks.clear()
        store._checkpoint_cache.clear()
    gc.collect()


def time_seek(store: TimelineStore, block_index: int) -> SeekResult:
    """Time a single block_payload call with a clean cache."""
    flush_all_blocks(store)
    gc.collect()

    tracemalloc.start()
    t0 = time.perf_counter()
    store.block_payload(block_index)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    step = block_index * store.block_size
    return SeekResult(
        block_index=block_index,
        step=step,
        elapsed_ms=elapsed_ms,
        peak_memory_kb=peak / 1024,
    )


def disk_usage_kb(store: TimelineStore) -> tuple[float, float]:
    """Return (block_files_kb, checkpoint_files_kb) on disk."""
    block_kb = sum(
        p.stat().st_size for p in store._block_dir.glob("????????.msgpack.zst")
    ) / 1024
    ckpt_kb = sum(
        p.stat().st_size for p in store._block_dir.glob("checkpoint_*.msgpack.zst")
    ) / 1024
    return block_kb, ckpt_kb


def run_benchmark(num_steps: int = NUM_STEPS, num_objects: int = NUM_OBJECTS) -> list[SeekResult]:
    print(f"Recording {num_steps} steps × {num_objects} objects …", flush=True)
    server = record_heavy_scene(num_steps, num_objects)
    store: TimelineStore = server._timeline

    # Wait for all background flushes to complete.
    flush_all_blocks(store)

    block_count = store.block_count
    block_kb, ckpt_kb = disk_usage_kb(store)
    print(f"Timeline: {num_steps} steps, {block_count} blocks of {BLOCK_SIZE} steps each")
    print(f"Disk: {block_kb:.0f} KB blocks  +  {ckpt_kb:.0f} KB checkpoints")

    # Sample: first, quarter, half, three-quarters, last block
    sample_indices = sorted(set([
        0,
        block_count // 4,
        block_count // 2,
        3 * block_count // 4,
        block_count - 1,
    ]))
    print(f"Seeking to blocks: {sample_indices}\n")

    results = []
    for block_index in sample_indices:
        result = time_seek(store, block_index)
        results.append(result)
        print(
            f"  block {block_index:3d} (step {result.step:4d}): "
            f"{result.elapsed_ms:7.1f} ms   peak {result.peak_memory_kb:8.1f} KB"
        )

    server.stop()
    return results


if __name__ == "__main__":
    print("=" * 60)
    print("viser4d seek benchmark")
    print("=" * 60)
    run_benchmark()
