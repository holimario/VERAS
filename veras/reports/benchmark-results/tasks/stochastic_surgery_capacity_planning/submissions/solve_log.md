# Solve Log

## Commands

- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`

## Guarded Repair

- Repair note: disk-based audit reparses `solution.csv` with typed day/night keys before any optimization step.
- Repair note: PACU overflow is recomputed exactly as bucket bed-hours above capacity from interval concurrency, not from total overlap-hours.
- incumbent_disk_audit: `pass`
- incumbent_theta: 0.0
- incumbent_violated_groups: 0
- incumbent_exact_objective: 112673.583200

## Solve Attempts

- `rollback_incumbent`: status `audited`, has_solution=True, time_limit=0.0s, candidate_exact_objective=112673.583200, audit=pass
- `incumbent_retime`: status `optimal`, has_solution=True, time_limit=50.0s, mip_objective=110320.084, dual_bound=110320.084, gap=0.000000, candidate_exact_objective=112273.650200, audit=pass
- `lb_k2_bedsafe`: status `gaplimit`, has_solution=True, time_limit=50.0s, mip_objective=112826.812, dual_bound=112772.557, gap=0.000481, candidate_exact_objective=112678.312700, audit=pass
- `lb_k4_balanced`: status `timelimit`, has_solution=True, time_limit=50.0s, mip_objective=109974.575, dual_bound=109619.103, gap=0.003243, candidate_exact_objective=112185.246800, audit=pass
- `lb_k6_throughput`: status `timelimit`, has_solution=True, time_limit=50.0s, mip_objective=108983.947, dual_bound=107431.703, gap=0.014449, candidate_exact_objective=112292.279300, audit=pass

## Final Artifact

- selected_profile: `lb_k4_balanced`
- solution_file: `/app/submissions/solution.csv`
- scheduled_cases: 34
- deferred_cases: 2
- hard_constraint_groups_violated: 0
- max_normalized_hard_residual_theta: 0.0
- planning_cost: 91220.000
- expected_operating_cost: 13219.632
- tail_cvar: 22130.328
- total_score: 112185.247
- incumbent_exact_objective: 112673.583200
- published_exact_objective: 112185.246800

## Validation Checks

- Schema: exact row count and identifiers checked against `surgery_cases.csv`.
- Hard rules checked from the exported artifact: block compatibility, sequence/start consistency, setup/turnover, infection-final, surgeon availability and overlap, equipment concurrency with turnaround, daily anesthesia caps, and block cardinality/acuity.
- Scenario audit recomputed independently from the exported artifact: overtime, idle time, PACU overflow, ward overflow, ICU overflow, and tail-risk CVaR.
