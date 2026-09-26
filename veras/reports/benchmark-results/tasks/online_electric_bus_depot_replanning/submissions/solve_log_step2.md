# Step 2 Replan Solve Log

## Environment

- `pyscipopt` version import validated: `5.6.0`
- current minute: `570`
- freeze-until minute: `690`
- visible trips: `48`
- prior accepted artifact: `/app/submissions/initial_plan.csv`
- rollback incumbent artifact: `/app/submissions/step2_replan.csv`

## Method

- The on-disk `step2_replan.csv` is reparsed first and treated as the rollback incumbent.
- The independent disk audit replays frozen-trip preservation, trip timing/location continuity, SOC propagation with derates, reserve checkpoints, outages, and session limits directly from the exported CSV.
- Restoration is allowed only if the rollback artifact violates a hard rule; otherwise the rollback incumbent is preserved.
- Guarded performance uses a bounded exact SCIP solve over the free stage-2 suffix with the rollback incumbent as a MIP start.
- Coverage certificate: charge decisions and legacy trip changes are excluded from the performance model because the rollback incumbent already has certified public objective `0` and every omitted public objective term is nonnegative.

## Concrete Issue Response

- Challenger concern `frozen-trip preservation`: replayed directly from disk against the accepted step-1 artifact.
- Challenger concern `trip timing/location continuity`: replayed from exported bus chains using public deadhead times.
- Challenger concern `battery feasibility`: replayed from exported bus chains using public SOC limits, trip start reserves, and derating multipliers.

## Rollback Disk Audit

- theta: `0`
- violated groups: `0` -> `[]`
- rollback incumbent objective: `0`

## Restoration

- invoked: `False`
- incumbent after restoration theta: `0`
- not needed because the rollback incumbent already had `theta = 0`.

## Guarded Performance

- exact suffix universe: newly visible stage-2 trips only, with all legacy trip rows fixed to the rollback incumbent.
- pattern count: `80`
- new-trip ids: `['T201', 'T202', 'T203', 'T204', 'T205', 'T206']`
- mip invoked: `True`
- mip status: `optimal`
- mip incumbents: `1`
- dominance certificate: `Excluded charge decisions and legacy trip changes are dominated because the rollback incumbent already serves all visible trips with zero charge rows, zero cancellations, and zero non-frozen changes, while all omitted public objective terms are nonnegative.`
- incumbent objective: `0`
- candidate objective: `0`
- best bound: `0`
- relative gap: `0`
- candidate hard-audit passed: `True`
- published improved artifact: `False`
- decision: `retained rollback incumbent because the exact performance candidate did not strictly improve the independently recomputed public objective`

## Final State Vector

- artifact present: `True`
- schema exact: `True`
- incumbent count: `1`
- theta: `0`
- violated groups: `0` -> `[]`
- integrality residual: `0`
- decision-universe coverage: `1`
- operated trip rows: `48`
- canceled trip rows: `0`
- charge rows: `0`
- changed non-frozen prior trips: `0` -> `[]`
- elapsed time: `0.01429653` seconds

## Objective

- cancellation penalties: `0`
- electricity purchases: `0`
- carbon metric on charge rows: `0`
- battery-stress cost term: `0`
- change-penalty term: `0`
- total public objective: `0`

## Reserve Checkpoints

- `480`: `['B01', 'B02', 'B03', 'B04', 'B08', 'B09', 'B11', 'B13', 'B14', 'B15', 'B16', 'B17', 'B18']`
- `720`: `['B17', 'B18']`
- `1020`: `['B15', 'B17', 'B18']`

## Frozen Commitments

- frozen prior trip rows preserved: `29`
- frozen prior charge rows preserved: `0`
- newly revealed trips inside the freeze window (`T201`, `T202`) were treated as current-stage decisions because they were not present in the accepted step-1 artifact.

## Negative Audit Check

- corrupted-copy rejection check passed: `True`

## Notes

- No charging is needed in the retained artifact even after applying the midday derates, outage windows, and temporary charger-session restrictions.
- Because the public objective is bounded below by `0` and the rollback incumbent already attains `0`, the performance phase can only tie, not strictly improve.
