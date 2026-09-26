#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ORAGENTBENCH_ROOT=${ORAGENTBENCH_ROOT:-"$HERE/../../ORAgentBench"}
SPLIT=${1:-all}
RUN_ID=${RUN_ID:-$(date +%Y%m%d-%H%M%S)}
OUTPUT=${OUTPUT:-"$HERE/runs/veras-$SPLIT-$RUN_ID"}
CONCURRENCY=${CONCURRENCY:-2}
TIMEOUT_SCALE=${TIMEOUT_SCALE:-1.0}

if [[ -z "${VERAS_MODEL:-}" || -z "${VERAS_BASE_URL:-}" ]]; then
  echo "Set VERAS_MODEL and VERAS_BASE_URL before running VERAS." >&2
  exit 2
fi

case "$SPLIT" in
  all) BANDS="easy medium hard" ;;
  easy-medium) BANDS="easy medium" ;;
  hard) BANDS="hard" ;;
  *) echo "Usage: $0 [all|easy-medium|hard]" >&2; exit 2 ;;
esac

mapfile -t TASKS < <(
  python - "$ORAGENTBENCH_ROOT/difficulty.json" $BANDS <<'PY'
import json
import sys
from pathlib import Path

tasks = json.loads(Path(sys.argv[1]).read_text())["tasks"]
bands = set(sys.argv[2:])
for name in sorted(name for name, metadata in tasks.items() if metadata.get("band") in bands):
    print(name)
PY
)

export ORAGENTBENCH_ROOT
export PYTHONPATH="$HERE/src:$ORAGENTBENCH_ROOT/local_runner${PYTHONPATH:+:$PYTHONPATH}"

"$HERE/bin/verify.sh"
python -m veras_harness.cli \
  --output "$OUTPUT" \
  --tasks "${TASKS[@]}" \
  --concurrency "$CONCURRENCY" \
  --timeout-scale "$TIMEOUT_SCALE"
python -m veras_harness.reporting.scores "$OUTPUT"
python -m veras_harness.reporting.components "$OUTPUT"
