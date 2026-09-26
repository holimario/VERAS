#!/usr/bin/env bash
set -euo pipefail

candidate_dir="$(cd "$(dirname "$0")" && pwd)"
export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-4}"
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

exec python "$candidate_dir/algorithm.py" \
  --data-dir "$candidate_dir/../data" \
  --output "$candidate_dir/results/solution.parquet"
