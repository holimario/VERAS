# Solve Log

## Environment

- PySCIPOpt import check:
  `PYTHONPATH=/opt/or-deps python -c "import pyscipopt; print(pyscipopt.__version__)"`
  -> `5.6.0`

## Commands And Outcomes

1. Initial compile check
   Command:
   `PYTHONPATH=/opt/or-deps python -m py_compile /app/submissions/solve.py`
   Result:
   script compiled successfully.

2. First short solve on the original formulation
   Command:
   `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=5 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   Result:
   `SCIP status: infeasible`
   Diagnosis:
   the original day-by-day service-floor interpretation was too strict. On day 1,
   some required service floors exceeded cumulative day-1 demand, so the model
   was corrected to apply service-target fractions to cumulative demand-to-date,
   not full-horizon demand on day 1.

3. Baseline feasibility test with production disabled
   Command:
   `PYTHONPATH=/opt/or-deps python - <<'PY' ... fix reserve/run/produce/inbound upper bounds to 0 ... PY`
   Result:
   `status optimal nsols 2`
   `obj 3522231.046901719`
   Use:
   confirmed feasibility and provided the restoration path used later as a warm
   start for the full model.

4. Warm-started full model, 30-second run
   Command:
   `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=30 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   Result:
   baseline incumbent exported and audited feasible.
   `Chosen objective (SCIP): 3522231.0469017192`
   `Audit objective: 3522231.0469017369`

5. Warm-started full model, 120-second run
   Command:
   `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=120 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   Result:
   improved incumbent.
   `Chosen objective (SCIP): 3031792.7085011564`
   `Chosen bound: 3022501.7009012331`
   `Chosen relative gap: 0.0030739462`
   `Audit feasible: True`

6. Final warm-started full model, 300-second run
   Command:
   `ORCLAW_SOLVE_TIME_LIMIT_SECONDS=300 PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`
   Result:
   final submission artifact written to `/app/submissions/solution.csv`.
   `Baseline status: optimal`
   `Warm start stored: True`
   `Chosen status: timelimit`
   `Full-model status: timelimit`
   `Full-model incumbents: 25`
   `Chosen objective (SCIP): 3029411.9243150321`
   `Chosen bound: 3026402.7752828994`
   `Chosen relative gap: 0.0009942989`
   `Audit objective: 3029411.9243151932`
   `Elapsed seconds: 300.59`

## Validation Checks

- Export schema:
  header matched the required schema exactly.
- Artifact presence:
  `/app/submissions/solution.csv` exists.
- Independent audit result:
  `feasible = True`
- Maximum normalized hard-constraint residual:
  `0.0`
- Violated constraint groups:
  `0`
- Final row count:
  `5112` data rows (`5113` lines including header)
- Row-type counts:
  `warehouse=6`, `supplier=4`, `stock=24`, `reserve=16`,
  `produce=50`, `inbound=100`, `dispatch=1712`,
  `expedite=1600`, `unmet=1600`

## Final Files

- Model: `/app/submissions/model.md`
- Solver: `/app/submissions/solve.py`
- Solution: `/app/submissions/solution.csv`

## Guarded Repair And Performance Phase

Public challenger report:

- No explicit infeasibility finding was supplied.
- Action taken anyway:
  reparsed the exact `solution.csv` from disk, reran the independent hard-check
  audit, and added a stricter row-semantics/schema pass before doing any
  performance work.

Exact incumbent audit from disk before optimization:

1. Independent audit
   Command:
   `PYTHONPATH=/opt/or-deps python - <<'PY' ... mod.validate_solution(mod.load_data(), '/app/submissions/solution.csv') ... PY`
   Result:
   `feasible True`
   `objective 3029411.924315193`
   `issues 0`
   `violations 0`

2. Strict CSV row-semantics audit
   Command:
   `python - <<'PY' ... check blank/nonblank columns and decision domains for every row_type ... PY`
   Result:
   `schema_errors 0`

Bounded performance searches run after the exact-disk audit passed:

1. Exact neighborhood solve with incumbent first-stage policy fixed
   Policy fixed:
   warehouse open/close, supplier active/inactive, starting stock, and reserved
   capacity all fixed to the incumbent on disk.
   Command:
   `PYTHONPATH=/opt/or-deps python - <<'PY' ... build_model(...,120); fix first-stage to parsed incumbent; optimize; write solution_candidate.csv; audit it ... PY`
   Result:
   `status gaplimit nsols 3`
   `incumbent objective 3029411.924315193`
   `candidate objective 3029166.0131246373`
   `bound 3029136.1949262572`
   `gap 9.843795855508927e-06`
   `audit feasible True`
   `max residual 0.0`
   Decision:
   accepted because the audited objective strictly improved.

2. Broader activation-pattern exact solve
   Policy fixed:
   warehouse open/close and supplier activation only; stock and reserve freed.
   Command:
   `PYTHONPATH=/opt/or-deps python - <<'PY' ... build_model(...,120); fix y_open/y_active to incumbent; optimize; audit solution_candidate2.csv ... PY`
   Result:
   `status timelimit nsols 25`
   `candidate objective 3029177.1683245334`
   `bound 3027270.421872884`
   `gap 0.0006298566648518419`
   `audit feasible True`
   Decision:
   rejected because it was worse than the accepted candidate.

Artifact update:

- Previous incumbent preserved at
  `/app/submissions/solution_rollback_before_repair.csv`
- Improved candidate promoted to `/app/submissions/solution.csv`

Post-promotion exact audit from disk:

- `feasible True`
- `objective 3029166.0131246373`
- `max normalized residual 0.0`
- `violated groups 0`
- `records 5121`
- Row-type counts:
  `warehouse=6`, `supplier=4`, `stock=24`, `reserve=16`,
  `produce=51`, `inbound=111`, `dispatch=1709`,
  `expedite=1600`, `unmet=1600`
