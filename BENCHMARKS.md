# BENCHMARKS

- Generated: 2026-02-23 09:33:48 UTC
- Python: `3.13.11`
- Platform: `macOS-26.2-arm64-arm-64bit-Mach-O`
- Repeats: `20`
- Warmup runs: `1`

All numbers are in seconds (lower is better).

| Benchmark | Profile | Mean | Median | P95 | Min | Max | StdDev |
|---|---:|---:|---:|---:|---:|---:|---:|
| `build_timeline` | `heavy` | 0.2226 | 0.2225 | 0.2235 | 0.2211 | 0.2236 | 0.0007 |
| `build_timeline` | `very_heavy` | 0.5894 | 0.5886 | 0.5949 | 0.5852 | 0.5958 | 0.0027 |
| `seek_forward_all_steps` | `heavy` | 1.3028 | 1.3021 | 1.3108 | 1.2924 | 1.3167 | 0.0064 |
| `seek_scrub_random` | `heavy` | 1.4703 | 1.4667 | 1.4844 | 1.4541 | 1.5117 | 0.0124 |
| `serialize_full_range` | `heavy` | 1.7241 | 1.7268 | 1.7400 | 1.7013 | 1.7430 | 0.0123 |
| `seek_forward_all_steps` | `very_heavy` | 3.6756 | 3.6748 | 3.6992 | 3.6430 | 3.7127 | 0.0164 |
