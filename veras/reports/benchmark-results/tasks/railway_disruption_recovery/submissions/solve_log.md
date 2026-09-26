# Solve Log

## Commands

- `python -m py_compile /app/submissions/solve.py`
- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
- `PYTHONPATH=/opt/or-deps python - <<'PY' ... diagnostic extract-and-audit script ... PY`

## Attempts

1. Initial full solve:
   Status `optimal`, SCIP objective `549.1999999999935`, incumbent count `16`.
   Validation failed in the self-audit because the checker incorrectly enforced minimum dwell at origins and double-counted origin throat departures, which triggered a fallback export.
2. Diagnostic extraction and audit:
   Re-ran the model and audited the pre-fallback incumbent directly.
   Confirmed the optimization model was feasible and the defect was in the checker, not the MIP.
3. Short repaired test:
   Status `timelimit` after a 20-second slice with a feasible incumbent objective `26010.0`.
   The repaired checker accepted the exported incumbent with `theta = 0.0`.
4. Final full solve:
   Status `optimal`, SCIP objective `549.1999999999935`, dual bound `549.1999999999935`, relative gap `0.0`.

## Final Solver Summary

- Time limit seconds: `300.0`
- Elapsed seconds: `36.23080134`
- Incumbent count: `16`
- Platform assignment result: `ok`

## Final Validation

- Artifact path: `/app/submissions/solution.json`
- Feasible after independent audit: `True`
- Max normalized hard-constraint residual theta: `0.0`
- Violated hard-constraint groups: `0`
- Violated groups detail: `none`
- Recomputed total objective: `549.2`
- Objective components: `{"cancel": 0.0, "crew_swap": 24.0, "delay": 489.2, "missed_connection": 0.0, "rolling_swap": 36.0, "short_turn": 0.0}`
- Decision coverage count: `14` trains
- Integrality residual: `0.0`

## Notes

- Final artifact keeps all 14 trains running; there are no cancellations and no short turns.
- The dominant cost is delay around the disrupted river segment and the induced waiting at `C1` and `C2`.

## Repair And Performance Phase

### Commands

- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

### Incumbent Audit From Disk

- Artifact reparsed from disk: `True`
- Incumbent feasible after public-only audit: `True`
- Incumbent objective: `549.2`
- Incumbent theta: `0.0`
- Incumbent violated groups: `none`

### Guarded Improvement Solve

- Search time limit seconds: `120.0`
- SCIP status: `gaplimit`
- MIP start accepted: `True`
- SCIP incumbent count: `3`
- SCIP best objective: `549.2`
- SCIP dual bound: `549.1992774679957`
- SCIP relative gap: `1.3156098965259874e-06`
- Candidate platform assignment: `ok`
- Candidate objective: `549.2`
- Candidate feasible after public-only audit: `True`
- Candidate theta: `0.0`
- Candidate violated groups: `none`
- Strict improvement found: `False`
- Published artifact changed: `False`
- Final kept objective: `549.2`
- Elapsed seconds: `14.76723075`
