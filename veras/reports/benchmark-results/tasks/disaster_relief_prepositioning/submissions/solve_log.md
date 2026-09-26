## Solve Log

### Environment checks

Command:

```bash
PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"
```

Result:

- `pyscipopt` version `5.6.0`

Command:

```bash
PYTHONPATH=/opt/or-deps python -m py_compile /app/submissions/solve.py
```

Result:

- Syntax check passed.

### Solve attempts

Command:

```bash
ORCLAW_SOLVE_TIME_LIMIT_SECONDS=20 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py
```

Result:

- Solver status: `timelimit`
- Solve mode: `main_mip`
- Incumbents found: `1`
- Objective from SCIP: `903285.3871593194`
- Independent audit objective: `903285.387160049`
- Independent audit `theta`: `2.7236640979984364e-10`
- Violated hard-constraint groups: `0`

Command:

```bash
ORCLAW_SOLVE_TIME_LIMIT_SECONDS=60 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py
```

Result:

- Solver status: `optimal`
- Solve mode: `main_mip_plus_fixed_open_lp`
- Incumbents found: `1`
- Objective from SCIP: `878079.3607283123`
- Dual bound: `878079.3607283123`
- Relative gap: `0.0`
- Independent audit objective: `878079.3607290736`
- Independent audit `theta`: `9.3985036962698e-11`
- Violated hard-constraint groups: `0`

Final rerun from scratch:

```bash
ORCLAW_SOLVE_TIME_LIMIT_SECONDS=60 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py
```

- Solver status: `optimal`
- Solve mode: `main_mip_plus_fixed_open_lp`
- Objective from SCIP: `878079.3607283123`
- Independent audit objective: `878079.3607290736`
- Independent audit `theta`: `9.3985036962698e-11`
- Violated hard-constraint groups: `0`

### Final artifact summary

- Final open warehouses: `W_NORTH`, `W_WEST`, `W_CENTRAL`, `W_PORT`, `W_AIRBASE`, `W_EAST`
- Closed warehouses: `W_SOUTH`, `W_HIGHLAND`, `W_RESERVE`
- `solution.csv` row counts:
  - `warehouse`: `9`
  - `inventory`: `27`
  - `ship`: `1230`
  - `procure`: `2016`
  - `unmet`: `2016`
- Objective breakdown from independent audit:
  - First-stage cost: `543097.099161334`
  - Expected operating cost: `214403.75433458114`
  - CVaR term before weight: `344510.0206661671`
  - Total risk-adjusted objective: `878079.3607290736`

### Validation checks

- Output schema matches the required 9-column CSV format.
- One `warehouse` row exists for every candidate warehouse.
- One `inventory` row exists for every warehouse-item pair.
- One `procure` row and one `unmet` row exist for every scenario-period-zone-item combination.
- All warehouse-opening, budget, storage, stock-limit, cold-chain, service-floor, fairness, procurement-cap, stock-usability, lane-capacity, outbound-capacity, and fleet-capacity checks passed in the independent audit.
- The exported artifact was regenerated from scratch in the final run before submission.

## Guarded Repair And Performance Phase

### Challenger issue check

- Reparsed the exact disk artifact `submissions/solution.csv` independently of solver state.
- The challenger’s concrete suspicion did not hold for the current artifact: open-warehouse count is `6`, not `9`.
- Open warehouses on disk: `W_NORTH`, `W_WEST`, `W_CENTRAL`, `W_PORT`, `W_AIRBASE`, `W_EAST`

### Independent disk audit before optimization

Command:

```bash
PYTHONPATH=/opt/or-deps python - <<'PY'
from submissions.solve import load_data, audit_solution_csv
data = load_data()
audit = audit_solution_csv('/app/submissions/solution.csv', data)
print(audit['objective'], audit['theta'], audit['violated_groups'], audit['warehouse_open_count'])
PY
```

Result:

- Recomputed objective from exported artifact: `878079.3607290736`
- Maximum normalized hard-constraint residual `theta`: `9.3985036962698e-11`
- Violated hard-constraint groups: `0`
- Warehouse-open count: `6`
- Schema/blank-field/domain diagnostics: `0`

### Guarded started solve

Rollback copies saved before the run:

- `submissions/solution.rollback.csv`
- `submissions/solve_summary.rollback.json`

Command:

```bash
ORCLAW_SOLVE_TIME_LIMIT_SECONDS=60 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py
```

Result:

- Incumbent objective from disk audit: `878079.3607290736`
- Incumbent loaded as SCIP MIP start: `true`
- Candidate solve mode: `main_mip_plus_fixed_open_lp`
- Exact MIP dual bound: `878079.3607283155`
- Exact MIP relative gap: `0.0`
- Candidate objective from exported candidate artifact: `878079.3607290736`
- Candidate audit `theta`: `9.3985036962698e-11`
- Candidate violated hard-constraint groups: `0`
- Publish decision: `false`

Decision:

- The candidate did not strictly improve the incumbent objective.
- `submissions/solution.csv` was left untouched and remains the incumbent.
