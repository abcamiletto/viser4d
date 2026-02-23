from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import gc
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Callable

from workloads import (
    HEAVY,
    VERY_HEAVY,
    BuiltScene,
    SceneProfile,
    bench_build_only,
    bench_seek_forward,
    bench_seek_scrub,
    bench_serialize,
    build_heavy_scene,
    teardown_scene,
)


@dataclass(frozen=True)
class BenchSpec:
    name: str
    profile: SceneProfile
    build_once: bool
    run: Callable[[BuiltScene], None] | None
    run_no_setup: Callable[[SceneProfile], None] | None
    description: str


@dataclass(frozen=True)
class BenchResult:
    name: str
    profile: str
    repeats: int
    warmup: int
    mean_s: float
    median_s: float
    p95_s: float
    min_s: float
    max_s: float
    stdev_s: float


def _percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("Cannot compute percentile of empty input.")
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _time_run(fn: Callable[[], None]) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def run_spec(spec: BenchSpec, repeats: int, warmup: int) -> BenchResult:
    for _ in range(warmup):
        gc.collect()
        if spec.build_once:
            scene = build_heavy_scene(spec.profile)
            try:
                assert spec.run is not None
                spec.run(scene)
            finally:
                teardown_scene(scene)
        else:
            assert spec.run_no_setup is not None
            spec.run_no_setup(spec.profile)

    samples: list[float] = []
    for _ in range(repeats):
        gc.collect()
        if spec.build_once:
            scene = build_heavy_scene(spec.profile)
            try:
                assert spec.run is not None
                elapsed = _time_run(lambda: spec.run(scene))
            finally:
                teardown_scene(scene)
        else:
            assert spec.run_no_setup is not None
            elapsed = _time_run(lambda: spec.run_no_setup(spec.profile))
        samples.append(elapsed)

    return BenchResult(
        name=spec.name,
        profile=spec.profile.name,
        repeats=repeats,
        warmup=warmup,
        mean_s=statistics.fmean(samples),
        median_s=statistics.median(samples),
        p95_s=_percentile(samples, 0.95),
        min_s=min(samples),
        max_s=max(samples),
        stdev_s=statistics.stdev(samples) if len(samples) > 1 else 0.0,
    )


def default_specs() -> list[BenchSpec]:
    return [
        BenchSpec(
            name="build_timeline",
            profile=HEAVY,
            build_once=False,
            run=None,
            run_no_setup=bench_build_only,
            description="Create heavy scene timeline by recording all steps.",
        ),
        BenchSpec(
            name="build_timeline",
            profile=VERY_HEAVY,
            build_once=False,
            run=None,
            run_no_setup=bench_build_only,
            description="Create very-heavy scene timeline by recording all steps.",
        ),
        BenchSpec(
            name="seek_forward_all_steps",
            profile=HEAVY,
            build_once=True,
            run=bench_seek_forward,
            run_no_setup=None,
            description="Seek sequentially from first to last step with blocking seeks.",
        ),
        BenchSpec(
            name="seek_scrub_random",
            profile=HEAVY,
            build_once=True,
            run=bench_seek_scrub,
            run_no_setup=None,
            description="Scrub timeline with 200 random blocking seeks.",
        ),
        BenchSpec(
            name="serialize_full_range",
            profile=HEAVY,
            build_once=True,
            run=bench_serialize,
            run_no_setup=None,
            description="Serialize full timeline to .viser file.",
        ),
        BenchSpec(
            name="seek_forward_all_steps",
            profile=VERY_HEAVY,
            build_once=True,
            run=bench_seek_forward,
            run_no_setup=None,
            description="Seek sequentially from first to last step with blocking seeks.",
        ),
    ]


def _render_markdown(results: list[BenchResult], repeats: int, warmup: int) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# BENCHMARKS",
        "",
        f"- Generated: {now}",
        f"- Python: `{sys.version.split()[0]}`",
        f"- Platform: `{platform.platform()}`",
        f"- Repeats: `{repeats}`",
        f"- Warmup runs: `{warmup}`",
        "",
        "All numbers are in seconds (lower is better).",
        "",
        "| Benchmark | Profile | Mean | Median | P95 | Min | Max | StdDev |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| `{r.name}` | `{r.profile}` | {r.mean_s:.4f} | {r.median_s:.4f} | "
            f"{r.p95_s:.4f} | {r.min_s:.4f} | {r.max_s:.4f} | {r.stdev_s:.4f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run viser4d performance benchmarks.")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--update-md", type=Path, default=Path("BENCHMARKS.md"))
    args = parser.parse_args()

    if args.repeats < 1:
        raise ValueError("--repeats must be >= 1")
    if args.warmup < 0:
        raise ValueError("--warmup must be >= 0")

    specs = default_specs()
    results: list[BenchResult] = []

    print(f"Running {len(specs)} benchmark specs...")
    for i, spec in enumerate(specs, start=1):
        print(f"[{i}/{len(specs)}] {spec.name} ({spec.profile.name})")
        result = run_spec(spec, repeats=args.repeats, warmup=args.warmup)
        results.append(result)
        print(
            f"  mean={result.mean_s:.4f}s median={result.median_s:.4f}s "
            f"p95={result.p95_s:.4f}s"
        )

    markdown = _render_markdown(results, args.repeats, args.warmup)
    args.update_md.write_text(markdown)
    print(f"Wrote {args.update_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
