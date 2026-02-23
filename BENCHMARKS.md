# BENCHMARKS

- Generated: 2026-02-23 08:30:57 UTC
- Python: `3.13.11`
- Platform: `macOS-26.2-arm64-arm-64bit-Mach-O`
- Repeats: `9`
- Warmup runs: `1`

All numbers are in seconds (lower is better).

| Benchmark | Profile | Mean | Median | P95 | Min | Max | StdDev |
|---|---:|---:|---:|---:|---:|---:|---:|
| `build_timeline` | `heavy` | 0.2205 | 0.2203 | 0.2222 | 0.2187 | 0.2224 | 0.0012 |
| `build_timeline` | `very_heavy` | 0.5829 | 0.5819 | 0.5895 | 0.5765 | 0.5915 | 0.0045 |
| `seek_forward_all_steps` | `heavy` | 1.2947 | 1.2937 | 1.3055 | 1.2870 | 1.3090 | 0.0072 |
| `seek_scrub_random` | `heavy` | 1.4528 | 1.4505 | 1.4677 | 1.4418 | 1.4696 | 0.0088 |
| `serialize_full_range` | `heavy` | 1.6960 | 1.6961 | 1.7050 | 1.6871 | 1.7084 | 0.0064 |
| `seek_forward_all_steps` | `very_heavy` | 3.6241 | 3.6230 | 3.6413 | 3.6057 | 3.6421 | 0.0131 |
