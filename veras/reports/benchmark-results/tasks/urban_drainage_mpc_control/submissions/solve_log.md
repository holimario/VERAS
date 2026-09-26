# Solve Log

## Environment

- Working directory: `/app`
- Solver command prefix: `PYTHONPATH=/opt/or-deps python`
- PySCIPOpt import check:
  - Command: `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
  - Result: `5.6.0`

## Modeling choice

The instance was modeled as a compact extensive-form multi-scenario basin inventory/flow MIP with:

- explicit basin storage and overflow states by scenario and slot;
- delayed inter-basin transfer arrivals;
- binary control activation and start variables;
- group activity, group start, rolling-duty, WWTP, and outfall limits;
- locked-prefix nonanticipativity on movement decisions;
- expected-value district overflow penalties;
- a linear CVaR tail-risk term on scenario costs.

This is stronger and simpler than decomposition or route-based formulations for the public instance size: 5 scenarios, 24 slots, 12 basins, 14 controls.

## Commands Run

1. `PYTHONPATH=/opt/or-deps python -m py_compile /app/submissions/solve.py`
   - Result: success

2. `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=10 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   - Purpose: diagnostic solve and audit

3. `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   - Purpose: first full-budget run

4. Edited `/app/submissions/solve.py`
   - Change: suppress exported near-zero flows that are far below any legitimate activity threshold, to avoid numerical dust appearing as real activations in the sparse CSV artifact

5. `PYTHONPATH=/opt/or-deps python -m py_compile /app/submissions/solve.py`
   - Result: success

6. `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=10 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   - Purpose: confirm export repair on a short run

7. `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   - Purpose: final full-budget run used for the submitted artifact

8. Independent audit and negative check:

```bash
PYTHONPATH=/opt/or-deps python - <<'PY'
import csv, importlib.util, os
spec = importlib.util.spec_from_file_location('solve_mod', '/app/submissions/solve.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
data = mod.load_instance()
res = mod.validate_solution(data, '/app/submissions/solution.csv')
print('FINAL_FEASIBLE', res['feasible'])
print('FINAL_MAX_RESIDUAL', res['max_normalized_residual'])
print('FINAL_OBJECTIVE_ESTIMATE', f"{res['objective_estimate']:.6f}")
print('FINAL_ROW_COUNT', res['row_count'])
corrupt = '/app/submissions/_tmp_corrupt_solution.csv'
with open('/app/submissions/solution.csv', newline='') as f:
    rows = list(csv.DictReader(f))
for row in rows:
    if row['scenario_id'] != 'S_base' and row['slot'] == '0':
        row['volume_ml'] = f"{float(row['volume_ml']) + 0.12345678:.8f}"
        break
with open(corrupt, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['scenario_id', 'slot', 'control_id', 'volume_ml'])
    w.writeheader()
    w.writerows(rows)
neg = mod.validate_solution(data, corrupt)
print('NEGATIVE_FEASIBLE', neg['feasible'])
print('NEGATIVE_FAMILIES', ','.join(neg['families_violated']))
os.remove(corrupt)
PY
```

## Solve Attempts

### Attempt 1: diagnostic, 10 seconds

- Status: `timelimit`
- Incumbents: `2`
- SCIP objective: `1595690.9454257314`
- Best bound: `1369140.3059679582`
- Relative gap: `0.16546926452333593`
- Elapsed seconds: `10.3214430809021`
- Validation:
  - Feasible: `true`
  - Violated constraint groups: `0`
  - Max normalized residual: `0.0`
  - Exported row count: `1187`
  - Objective recomputed from exported artifact: `1595690.9319865443`

### Attempt 2: full-budget run before export repair

- Status: `timelimit`
- Incumbents: `2`
- SCIP objective: `1595690.6354257218`
- Best bound: `1369140.3059679582`
- Relative gap: `0.1654690381038753`
- Elapsed seconds: `300.32782459259033`
- Validation:
  - Feasible: `false`
  - Violated constraint groups: `2`
  - Families: `group_active`, `min_positive`
  - Cause: sparse export retained numerical dust from near-zero control flows; those tiny positives were interpreted as active controls by the independent audit
- Action taken: patch exporter to drop values far below legitimate activity thresholds

### Attempt 3: diagnostic after export repair, 10 seconds

- Status: `timelimit`
- Incumbents: `2`
- SCIP objective: `1595690.9454257314`
- Best bound: `1369140.3059679582`
- Relative gap: `0.16546926452333593`
- Elapsed seconds: `10.298743486404419`
- Validation:
  - Feasible: `true`
  - Violated constraint groups: `0`
  - Max normalized residual: `0.0`
  - Exported row count: `1187`
  - Objective recomputed from exported artifact: `1595690.9319865443`

### Attempt 4: final full-budget run after export repair

- Status: `timelimit`
- Incumbents: `2`
- SCIP objective: `1595690.6354257218`
- Best bound: `1369140.3059679582`
- Relative gap: `0.1654690381038753`
- Elapsed seconds: `300.3322865962982`
- Integrality residual on binary variables in best SCIP solution: `2.041492459212992e-06`
- Validation:
  - Feasible: `true`
  - Violated constraint groups: `0`
  - Max normalized residual: `0.0`
  - Exported row count: `1187`
  - Objective recomputed from exported artifact: `1595690.9555668163`
  - Expected-cost component: `656175.9526400086`
  - District penalty component: `328791.09855683387`
  - CVaR component: `1357164.231933275`

## Final Validation Checks

- Final artifact: `/app/submissions/solution.csv`
- Header check: passed
- Domain check on scenario IDs, control IDs, and slots: passed
- Locked-prefix nonanticipativity check: passed
- Control capacity and unavailability check: passed
- Minimum positive movement check: passed
- Ramp check: passed
- Per-control start budgets: passed
- Group simultaneous-activity limits: passed
- Group start limits: passed
- Rolling duty limits: passed
- WWTP capacity limits: passed
- Outfall release limits: passed
- Basin source-availability and delayed-transfer dynamics: passed
- Overflow and storage propagation recomputation: passed
- Independent negative check on corrupted copy: passed
  - Corrupted artifact was rejected with violation families `duty`, `locked_prefix`, `ramp`

## Guarded Repair And Performance Phase

The public challenger report did not identify a concrete hard-constraint violation. I therefore treated the repair phase as a disk-based re-audit of the incumbent artifact before any further optimization.

### Incumbent re-audit from disk

- Artifact audited: `/app/submissions/solution.csv`
- Public hard-constraint result: feasible
- Violated constraint groups: `0`
- Max normalized residual: `0.0`
- Recomputed incumbent objective: `1595690.9555668163`

### Guarded optimization commands

1. `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=20 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   - Warm-started exact pass only
   - Candidate objective: `1595690.9555668163`
   - Result: no strict improvement, incumbent kept

2. `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=90 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   - Warm-started exact pass plus local branching radius `48`
   - Exact phase:
     - Bound `1369140.3059679586`, gap `0.16546927193023558`
     - Candidate objective `1595690.9555668163`
   - Local branching radius `48`:
     - Bound `1401138.8566326967`, gap `0.13885283247491947`
     - Candidate objective `1595690.9555668163`
   - Result: no strict improvement, incumbent kept

3. `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   - Warm-started exact pass plus local branching radii `48` and `96`
   - Exact phase:
     - Bound `1369140.3059679586`, gap `0.16546927193023558`
     - Candidate objective `1595690.9555668163`
   - Local branching radius `48`:
     - Bound `1401138.8566326967`, gap `0.13885283247491947`
     - Candidate objective `1595690.9555668163`
   - Local branching radius `96`:
     - SCIP objective `1450911.3605326558`
     - Best bound `1400424.3414363614`
     - Relative gap `0.03605122933275483`
     - Candidate objective recomputed from exported artifact `1450911.3492370672`
     - Audit result: feasible, `0` violated constraint groups, max normalized residual `0.0`
   - Result: strict improvement achieved, candidate published

### Independent post-publication audit

- Command:

```bash
PYTHONPATH=/opt/or-deps python - <<'PY'
import importlib.util, json
spec = importlib.util.spec_from_file_location('solve_mod', '/app/submissions/solve.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
data = mod.load_instance()
res = mod.validate_solution(data, '/app/submissions/solution.csv')
print(json.dumps(res, indent=2, sort_keys=True))
PY
```

- Result:
  - Feasible: `true`
  - Violated constraint groups: `0`
  - Max normalized residual: `0.0`
  - Objective estimate: `1450911.3492370672`
  - Row count: `1239`

## Final Artifact Summary

- Submitted file: `/app/submissions/solution.csv`
- Nonzero policy rows: `1239`
- Public-audit feasibility: `true`
- Public-audit objective estimate: `1450911.3492370672`
