#!/usr/bin/env bash
set -euo pipefail

REPEATS="${REPEATS:-20}"
WARMUP="${WARMUP:-1}"
OUT="${OUT:-BENCHMARKS.md}"

uv run python bench/run.py --repeats "${REPEATS}" --warmup "${WARMUP}" --update-md "${OUT}"
