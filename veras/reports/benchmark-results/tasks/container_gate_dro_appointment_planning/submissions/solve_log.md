# Solve Log

## Commands

- `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Solver Settings

- Relative gap: `0.0005`
- Time limit used: `295.00` seconds

## Solve Attempts

- Mode: `exact_improvement`
  Status: `timelimit`
  Elapsed seconds: `120.1174`
  Incumbent count: `5`
  Objective: `53841.03522400`
  Dual bound: `49256.20840486`
  Gap: `0.09308120`
  Note: `mip_start_loaded=True`
  Note: `incumbent_objective=53792.71764400`
  Note: `candidate_audited_objective=53841.03522400`
  Note: `candidate_audit_valid=True`
  Note: `published=False`
- Mode: `local_branching`
  Status: `timelimit`
  Elapsed seconds: `175.0297`
  Incumbent count: `4`
  Objective: `52073.57360400`
  Dual bound: `49504.13029316`
  Gap: `0.05190361`
  Note: `mip_start_loaded=True`
  Note: `local_branch_radius=18`
  Note: `incumbent_objective=53792.71764400`
  Note: `candidate_audited_objective=52073.57360400`
  Note: `candidate_audit_valid=True`
  Note: `published=True`

## Performance Summary

- Rollback incumbent objective: `53792.71764400`
- Best candidate objective considered: `52073.57360400`
- Published artifact objective: `52073.57360400`
- Published strict improvement: `True`
- Rollback audit valid: `True`
- Published audit valid: `True`

## Independent Audit

- Artifact: `/app/submissions/solution.csv`
- Exists: `True`
- Valid: `True`
- Record count: `1855`
- Max normalized residual theta: `0.00000000`
- Violated constraint groups: `0`
- Recomputed score proxy: `52073.57360400`
- Violations: none
