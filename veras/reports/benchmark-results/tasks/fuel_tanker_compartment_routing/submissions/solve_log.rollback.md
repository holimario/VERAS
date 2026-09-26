# Solve Log

## Command

`PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Preprocessing

- Station patterns: 2207
- Route patterns: 6635
- Slot-specific candidates: 35503
- Restoration bundles: 17
- Restoration operator: exact repair MIP on a fixed public-data bundle basis, then exact bay assignment.

## Solve Attempts

### Restricted Exact Repair
- Candidate universe: 985
- Assignment status: `optimal`
- Assignment objective: 21597.15028686995
- Assignment dual bound: 21597.15028686995
- Assignment gap: 0.0
- Selected trips: 17
- Bay status: `optimal`
- Bay objective: 152.0

## Validation

- Feasible: `True`
- Total objective: 21597.15028697202
- Fixed trip cost: 9126.0
- Distance cost: 4567.087604553296
- Hazmat cost: 4255.578133618726
- Lateness cost: 2571.4845487999983
- Slot cost: 925.0
- Bay cost: 152.0
- Cleanout cost: 0.0
- Shortfill cost: 0.0
- Checks: schema, slot capacity, vehicle turnaround, work limits, access, compartment capacity, product compatibility, precedence, time propagation, bay cleanout, station split rules, pairing rules, exact order coverage bounds.

## Independent Audit

- Command: standalone `PYTHONPATH=/opt/or-deps python` replay over `/app/data/*` and `/app/submissions/solution.json`.
- Feasible: `True`
- Error count: 0
- Trip count: 17
