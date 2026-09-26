#!/usr/bin/env bash
set -euo pipefail

CANDIDATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT_ROOT="$(cd "${CANDIDATE_ROOT}/.." && pwd)"

mkdir -p "${CANDIDATE_ROOT}/results" "${CANDIDATE_ROOT}/logs"
rm -f \
  "${CANDIDATE_ROOT}/results/solution.npz" \
  "${CANDIDATE_ROOT}/results/internal_start.npz" \
  "${CANDIDATE_ROOT}/results/solver_summary.json" \
  "${CANDIDATE_ROOT}/audit_report.json" \
  "${CANDIDATE_ROOT}/manifest.json" \
  "${CANDIDATE_ROOT}/logs/mosek.log" \
  "${CANDIDATE_ROOT}/logs/mosek_relax.log" \
  "${CANDIDATE_ROOT}/logs/mosek_planning_screen.log" \
  "${CANDIDATE_ROOT}/logs/mosek_integer_screen.log" \
  "${CANDIDATE_ROOT}/logs/runner.log" \
  "${CANDIDATE_ROOT}/logs/audit.log" \
  "${CANDIDATE_ROOT}/logs/finalize.log"

python "${CANDIDATE_ROOT}/src/staged_solve.py" \
  --input-root "${INPUT_ROOT}" \
  --output-root "${CANDIDATE_ROOT}" \
  --threads 4 \
  --relax-time 12 \
  --planning-screen-time 25 \
  --integer-screen-time 25 \
  --final-time 150 2>&1 | tee "${CANDIDATE_ROOT}/logs/runner.log"

python "${CANDIDATE_ROOT}/src/audit.py" \
  --input-root "${INPUT_ROOT}" \
  --candidate-root "${CANDIDATE_ROOT}" 2>&1 | tee "${CANDIDATE_ROOT}/logs/audit.log"

python "${CANDIDATE_ROOT}/src/finalize.py" \
  --input-root "${INPUT_ROOT}" \
  --candidate-root "${CANDIDATE_ROOT}" 2>&1 | tee "${CANDIDATE_ROOT}/logs/finalize.log"
