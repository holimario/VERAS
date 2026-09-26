# Solve Log

## Environment checks

- Command: `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
  Result: `5.6.0`
- Command: `PYTHONPATH=/opt/or-deps python -m py_compile /app/submissions/solve.py`
  Result: passed

## Solve attempts

1. Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   Result: model solved far enough to export an incumbent, but the post-solve
   validator crashed on a cancelled-lane capacity edge case:
   `TypeError: 'NoneType' object is not subscriptable`.
   Action: patched the validator to treat cancelled lanes as zero-capacity.

2. Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   Result:
   - SCIP status: `gaplimit`
   - Elapsed: `4.77134037` seconds
   - Time limit used: `300.0` seconds
   - Incumbents found: `5`
   - Primal objective: `129676.65057143`
   - Dual bound: `129657.04855589`
   - Relative gap: `0.00015118`
   - Forced rejects from preprocessing: `O06`, `O07`
   - Exported artifact: `/app/submissions/solution.json`

## Validation

- Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py --validate-only /app/submissions/solution.json`
  Result:
  - Issues: none
  - Maximum normalized hard-constraint residual: `0.00000000`
  - Recomputed objective: `129676.65057143`
  - Accepted orders: `O01 O02 O03 O04 O05 O08 O09 O10 O11 O12 O13 O14 O15 O16 O17 O18`
  - Rejected orders: `O06 O07`
  - Accepted TEU by priority: `critical=17`, `priority=15`, `standard=33`
  - Expected scenario cost component: `46108.87766667`
  - Specialty overflow cost used: `S03=420.0`, `S08=440.0`

- Corrupted-copy audit:
  - Command: create a temporary corrupted copy with one accepted order missing a
    scenario plan, then run
    `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py --validate-only /tmp/solution_corrupt.json`
  - Result: validator flagged the artifact with
    `accepted order O01 does not cover every scenario exactly once`

## Final notes

- Mathematical model written to `/app/submissions/model.md`
- Solver written to `/app/submissions/solve.py`
- Final submission artifact written to `/app/submissions/solution.json`

## Guarded repair and performance phase

- Public challenger follow-up:
  - Strengthened the independent auditor in `solve.py` so validation now
    reparses `solution.json` directly from disk and reconstructs each reserved
    path and scenario path lane-by-lane, instead of relying on pre-enumerated
    path membership alone.
  - Added guarded incumbent handling: the existing `solution.json` is treated as
    the rollback incumbent, loaded as a SCIP MIP start, and only replaced if a
    strictly better audited objective is found.

- Incumbent disk audit:
  - Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py --validate-only /app/submissions/solution.json`
  - Audit result: zero issues, maximum normalized hard-constraint residual
    `0.00000000`
  - Incumbent audited objective: `129676.65057143`

- Guarded improvement solve:
  - Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
  - SCIP status: `gaplimit`
  - MIP start loaded: `true`
  - Elapsed: `10.36934638` seconds
  - Candidate primal objective: `129676.65057143`
  - Dual bound: `129618.84170028`
  - Relative gap: `0.00044599`
  - Candidate audit result: zero issues, maximum normalized hard-constraint
    residual `0.00000000`
  - Publication decision: incumbent retained because the candidate objective did
    not strictly improve

- Final exact-artifact audit after the guarded solve:
  - Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py --validate-only /app/submissions/solution.json`
  - Result: zero issues, maximum normalized hard-constraint residual
    `0.00000000`, audited objective `129676.65057143`
