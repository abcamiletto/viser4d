"""Benchmark: seek (block_message) latency and memory across a heavy timeline.

Simulates a client seeking to various positions in a large recorded sequence.
block_message(N) is what the server calls when a client requests block N.

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
from viser4d._timeline import Timeline

NUM_OBJECTS = 200  # scene objects per step (frames with position/rotation)
NUM_STEPS = 512  # total timeline steps
FPS = 30.0


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
    with server.at(0) as timeline:
        for i in range(num_objects):
            h = timeline.scene.add_frame(f"/obj/{i}", axes_length=0.05)
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


def flush_all_blocks(timeline: Timeline) -> None:
    """Force all in-memory blocks to disk and clear caches, simulating a cold seek."""
    with timeline._lock:
        for index, block in list(timeline._loaded.items()):
            timeline._flush(index, block)
        timeline._wait_all_flushes()
        timeline._loaded.clear()
        timeline._checkpoints.clear()
    gc.collect()


def time_seek(timeline: Timeline, block_index: int) -> SeekResult:
    """Time a single block_message call with a clean cache."""
    flush_all_blocks(timeline)

    tracemalloc.start()
    t0 = time.perf_counter()
    timeline.block_message(block_index)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return SeekResult(
        block_index=block_index,
        step=block_index * timeline.block_size,
        elapsed_ms=elapsed_ms,
        peak_memory_kb=peak / 1024,
    )


def run_benchmark(num_steps: int = NUM_STEPS, num_objects: int = NUM_OBJECTS) -> None:
    print(f"Recording {num_steps} steps x {num_objects} objects ...", flush=True)
    server = record_heavy_scene(num_steps, num_objects)
    timeline = server._timeline

    flush_all_blocks(timeline)
    block_count = timeline.block_count
    disk_kb = sum(p.stat().st_size for p in timeline._dir.glob("*.msgpack.zst")) / 1024
    print(
        f"Timeline: {num_steps} steps, {block_count} blocks "
        f"of {timeline.block_size} steps each"
    )
    print(f"Disk: {disk_kb:.0f} KB block files")

    sample_indices = sorted(
        {0, block_count // 4, block_count // 2, 3 * block_count // 4, block_count - 1}
    )
    print(f"Seeking to blocks: {sample_indices}\n")

    for block_index in sample_indices:
        result = time_seek(timeline, block_index)
        print(
            f"  block {result.block_index:3d} (step {result.step:4d}): "
            f"{result.elapsed_ms:7.1f} ms   peak {result.peak_memory_kb:8.1f} KB"
        )

    server.stop()


if __name__ == "__main__":
    print("=" * 60)
    print("viser4d seek benchmark")
    print("=" * 60)
    run_benchmark()
