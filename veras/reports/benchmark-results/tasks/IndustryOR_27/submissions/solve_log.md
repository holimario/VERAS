# Solve Log

## Environment checks

- Command: `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
- Result: `5.6.0`

- Command: `PYTHONPATH=/opt/or-deps python -m py_compile /app/submissions/solve.py`
- Result: syntax check passed

## Solve attempts

### Attempt 1: short diagnostic before validator fix

- Command: `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=30 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
- Solver status: incumbent found, but script exited with validation failure
- Root cause: the independent checker was incorrectly double-counting gross production across A and B stages when recomputing campaign-size and material-usage constraints
- Action taken: fixed the checker so gross production is counted once per product-month

### Attempt 2: short diagnostic after validator fix

- Command: `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=30 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
- Solver status: `optimal`
- Incumbents: `5`
- Objective: `4353767.029006387`
- Dual bound: `4353767.029006387`
- Relative gap: `0.0`
- Solve time: `0.1502218246459961` seconds
- Validation:
  - feasible export: `true`
  - validation objective: `4353767.0290051075`
  - max normalized hard-constraint residual `theta`: `4.300090949982405e-09`
  - violated constraint groups: `0`
  - setup integrality residual: `0.0`

### Attempt 3: final scratch run

- Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
- Solver status: `optimal`
- Incumbents: `5`
- Objective: `4353767.029006387`
- Dual bound: `4353767.029006387`
- Relative gap: `0.0`
- Solve time: `0.1567094326019287` seconds
- Time limit used by script: `295.0` seconds
- Validation:
  - feasible export: `true`
  - validation objective: `4353767.0290051075`
  - max normalized hard-constraint residual `theta`: `4.300090949982405e-09`
  - violated constraint groups: `0`
  - setup integrality residual: `0.0`

## Final artifact checks

- `solution.csv` written to `/app/submissions/solution.csv`
- Row count: `222` decision rows plus header (`223` total lines)
- Coverage:
  - `102` production rows for all eligible month-stage-machine-product routes
  - `90` shipment rows for all month-product-channel demand keys
  - `30` setup rows for all month-product pairs
- Independent audit passed for:
  - schema and key coverage
  - setup binary values
  - stage A/B gross balance
  - campaign min/max production
  - machine regular-plus-overtime capacity
  - material availability and reserve rules
  - released inventory flow and final safety stock
  - unreleased quality-pipeline limits
  - service minima and demand ceilings
  - restart logic, minimum run lengths, and setup-crew capacity
  - family campaign limits and pairwise conflict rules
  - contract commitments
  - channel-mix windows

## Notes

- `quality_context.csv` and `maintenance_notes.csv` were treated as context-only because the problem statement explicitly says their operational effects are already reflected in the numeric input tables.
- The model enforces the minimum run length conservatively at the horizon end by forbidding new starts when the full run window would extend beyond June 2026.

## Guarded repair-and-performance phase

- Public challenger report review: no concrete counterexample rows or numeric violations were provided, so there was no specific exported-row defect to repair before rerunning the independent audit.
- Action taken:
  - reparsed `/app/submissions/solution.csv` from disk
  - recomputed all public hard constraints independently of the optimization model
  - used the audited incumbent as a SCIP MIP start
  - ran a bounded exact re-optimization with the same public objective and gap setting

### Attempt 4: guarded incumbent audit plus MIP-start re-optimization

- Command: `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
- Pre-solve disk audit of incumbent:
  - objective: `4353767.0290051075`
  - max normalized hard-constraint residual `theta`: `4.300090949982405e-09`
  - violated constraint groups: `0`
  - setup integrality residual: `0.0`
  - audit result: pass
- MIP start accepted by SCIP: `true`
- Solver status: `gaplimit`
- Incumbents: `3`
- Candidate solver objective: `4353767.0290051075`
- Candidate audited objective: `4353767.0290051075`
- Dual bound: `4354607.42453407`
- Relative gap: `0.00019302721605522964`
- Candidate audit:
  - feasible export: `true`
  - max normalized hard-constraint residual `theta`: `4.300090949982405e-09`
  - violated constraint groups: `0`
  - setup integrality residual: `0.0`
- Publication rule:
  - strict audited-objective improvement over rollback incumbent required
  - result: no strict improvement, so `/app/submissions/solution.csv` was left unchanged
- Final on-disk artifact after the guarded phase:
  - objective: `4353767.0290051075`
  - audit result: pass
