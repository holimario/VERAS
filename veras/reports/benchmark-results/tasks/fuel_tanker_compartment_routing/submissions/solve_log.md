# Solve Log

## Command

`PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Preprocessing

- Station patterns: 2846
- Route patterns: 6383
- Slot-specific candidates: 34001
- Restoration bundles: 17
- Restoration operator: exact repair MIP on a fixed public-data bundle basis, then exact bay assignment.

## Solve Attempts

### Incumbent Audit
- Incumbent objective: 21704.72650378299
- Internal validation feasible: `True`
- Independent audit feasible: `True`
- Independent audit errors: 0

### Restricted Exact Repair
- Candidate universe: 940
- Assignment status: `optimal`
- Assignment objective: 21704.72650368092
- Assignment dual bound: 21704.72650368092
- Assignment gap: 0.0
- Selected trips: 17
- Bay status: `optimal`
- Bay objective: 156.0

## Repair Audit

- Repair objective: 21704.72650378299
- Repair internal feasible: `True`
- Repair independent feasible: `True`
- Repair independent errors: 0

## Performance Attempt

- Status: `timelimit`
- Candidate objective: 21704.72650368092
- Dual bound: -1e+20
- Gap: 1e+20
- Bay feasible: `True`
- Audit feasible: `True`
- Strict improvement over repaired incumbent: `False`

## Published Artifact

- Source: `incumbent`
- Published objective: 21704.72650378299

## Validation

- Feasible: `True`
- Total objective: 21704.72650378299
- Fixed trip cost: 9202.0
- Distance cost: 4594.6638213642655
- Hazmat cost: 4255.578133618726
- Lateness cost: 2571.4845487999983
- Slot cost: 925.0
- Bay cost: 156.0
- Cleanout cost: 0.0
- Shortfill cost: 0.0
- Checks: schema, slot capacity, vehicle turnaround, work limits, access, compartment single-order rule, compartment capacity, product compatibility, precedence, time propagation, bay cleanout, station split rules, pairing rules, exact order coverage bounds.

## Independent Audit

- Command: standalone `PYTHONPATH=/opt/or-deps python` replay over `/app/data/*` and the exported solution artifact.
- Feasible: `True`
- Error count: 0
- Trip count: 17
