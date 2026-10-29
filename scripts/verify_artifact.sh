#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

expected_benchmark_root="${ORAGENTBENCH_ROOT:-$(cd "$root/.." && pwd)/ORAgentBench}"
if [[ -f "$expected_benchmark_root/local_runner/harness_benchmark.py" ]]; then
  (
    export ORAGENTBENCH_ROOT="$expected_benchmark_root"
    "$root/veras/bin/verify.sh"
  )
else
  (
    export PYTHONPATH="$root/veras/src${PYTHONPATH:+:$PYTHONPATH}"
    python -B -m unittest discover -s "$root/veras/tests" -p 'test_observers.py' -q
  )
  echo "ORAgentBench runtime not installed at $expected_benchmark_root; import-dependent harness tests skipped"
fi

python -B "$root/scripts/verify_controller_profiles.py"
"$root/scripts/check_release_content.sh"

echo "artifact verification passed"
