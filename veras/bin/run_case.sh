#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PROFILE=${1:-}
case "$PROFILE" in
  datacenter|power-grid) ;;
  *) echo "Usage: $0 [datacenter|power-grid] [additional CLI arguments]" >&2; exit 2 ;;
esac
: "${ORAGENTBENCH_ROOT:?Set ORAGENTBENCH_ROOT to an authorized checkout with the case task packet}"
RUN_ID=${RUN_ID:-$(date +%Y%m%d-%H%M%S)}
OUTPUT=${OUTPUT:-"$HERE/runs/veras-$PROFILE-$RUN_ID"}
export PYTHONPATH="$HERE/src:$ORAGENTBENCH_ROOT/local_runner${PYTHONPATH:+:$PYTHONPATH}"

# CLI checks the pinned task and runner hashes before loading credentials or running.
python -m veras_harness.cli "${@:2}" \
  --profile "$PROFILE" --benchmark-root "$ORAGENTBENCH_ROOT" --output "$OUTPUT"
