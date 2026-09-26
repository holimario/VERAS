#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
data_dir="${BANDWIDTH_DATA_DIR:-/work/app/data}"
export POLARS_MAX_THREADS="${POLARS_MAX_THREADS:-4}"
python "$script_dir/algorithm.py" \
  --data-dir "$data_dir" \
  --output "$script_dir/results/solution.parquet" \
  --runtime-file "$script_dir/results/dispatch_seconds.txt"
