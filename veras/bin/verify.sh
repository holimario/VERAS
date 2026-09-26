#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BENCHMARK_ROOT=${ORAGENTBENCH_ROOT:-"$HERE/../../ORAgentBench"}
export PYTHONPATH="$HERE/src:$BENCHMARK_ROOT/local_runner${PYTHONPATH:+:$PYTHONPATH}"
python -B -m unittest discover -s "$HERE/tests" -p 'test_*.py' -q
