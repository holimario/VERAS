# Solve Log

## Commands

1. `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
2. `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Incumbent Audit

- Source artifact: `/app/submissions/solution.csv`
- Header exact-match: `True`
- Incumbent objective: `6646.35920000`
- Incumbent expected profit: `7248.35920000`
- Incumbent CVaR loss: `0.00000000`
- Incumbent audit violations: `0`
- Incumbent max normalized residual: `0.00000000`

## Performance Attempts

### `fixed_observation_recourse`

- Time limit: `82.60` seconds
- Build time: `0.11` seconds
- Solve time: `0.73` seconds
- MIP start stored: `True`
- SCIP status: `gaplimit`
- Incumbent count: `3`
- Candidate objective: `6822.55760000`
- Candidate best bound: `6823.07528327`
- Candidate gap: `0.00007588`
- Recomputed candidate objective: `6822.55760000`
- Candidate audit violations: `0`
- Candidate max normalized residual: `0.00000000`

### `local_branching_radius_4`

- Time limit: `150.00` seconds
- Build time: `0.10` seconds
- Solve time: `150.00` seconds
- MIP start stored: `True`
- SCIP status: `timelimit`
- Incumbent count: `2`
- Candidate objective: `6822.55760000`
- Candidate best bound: `7174.74180252`
- Candidate gap: `0.05162055`
- Recomputed candidate objective: `6822.55760000`
- Candidate audit violations: `0`
- Candidate max normalized residual: `0.00000000`

## Final Artifact

- Published improved artifact: `True`
- Final objective on disk: `6822.55760000`
- Final audit violations: `0`
- Final max normalized residual: `0.00000000`
- Total elapsed: `151.08` seconds
- Final scenario profit `S_CLEAR`: `8131.02000000`
- Final scenario profit `S_TROPICAL_CLOUDS`: `5928.85000000`
- Final scenario profit `S_WILDFIRE_SMOKE`: `8222.16000000`
- Final scenario profit `S_STATION_OUTAGE`: `7245.52000000`
