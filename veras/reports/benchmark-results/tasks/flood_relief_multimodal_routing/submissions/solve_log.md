# Solve Log

## Command

- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Incumbent Audit

- Incumbent objective from disk audit: 17251.44890000
- Incumbent completion time: 824.36000000
- Incumbent weighted lateness: 12168.21400000
- Incumbent repeated activity nodes: {'S08': 2, 'S03': 3, 'S05': 2}
- Best no-repeat repair objective for incumbent pattern set: 19878.03510000

## Preprocessing

- Relevant staging nodes: 13
- Flooded shelters: 16
- Boat trips generated: 212
- Node patterns generated: 234
- Heuristic candidate covers tested: 12
- Best constructive candidate objective: 17123.58230000
- Best constructive repeated activity nodes: {'S08': 2, 'S03': 3, 'S05': 2}

## Solve Attempt

- SCIP status: `timelimit`
- Time limit used: 60.00000000 seconds
- Solve wall time: 60.00034428 seconds
- Warm starts accepted: `[('incumbent', True), ('constructive', True)]`
- Incumbent count: 2
- Solver objective: 17123.58230000
- Best bound: 7.96134993
- Relative gap: 2149.83904710

## Candidate Comparison

- Rollback incumbent objective: 17251.44890000
- Constructive candidate objective: 17123.58230000
- SCIP candidate objective: 17123.58230000
- Publish decision: published `constructive_candidate`

## Validation

- Exact artifact reparsed from disk-independent candidate rows and recomputed against public data.
- Truck route nodes audited: 19
- Activity nodes audited: 11
- Completion time from final audit: 824.36000000
- Weighted lateness from final audit: 12073.49800000
- Objective from final audit: 17123.58230000
- Final repeated activity nodes: {'S08': 2, 'S03': 3, 'S05': 2}
- Hard checks passed: shelter coverage, road adjacency, boat adjacency, boat capacity, boat duration, launch eligibility.

## Runtime

- End-to-end wall time: 111.56662536 seconds
