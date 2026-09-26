# Step 2 Replan Model at Minute 570

The accepted step-1 plan is state for this online stage. The depot is now at minute `570` with `freeze_until_min = 690`, six newly visible trips, two charger-outage windows, two temporary charger-session safety limits, and three bus-level derating notices.

## State And Freeze

- Visible trips: all rows in `data/trips.csv` with `reveal_step <= 2`.
- Prior accepted artifact: [`initial_plan.csv`](/app/submissions/initial_plan.csv).
- Frozen trip commitments: every trip row already accepted in step 1 whose `depart_min < 690`.
- Frozen charge commitments: any accepted charge row with `start_min < 690`.
- Current-stage interpretation: newly revealed trips inside the freeze window, `T201` and `T202`, are not frozen because they did not exist in the accepted step-1 artifact.

For this packet the accepted step-1 plan contains no charge rows, so the only frozen commitments are the 29 previously accepted trip assignments before minute `690`.

## Decision Structure

The public objective is nonnegative in every visible term: cancellation penalties, electricity cost, carbon cost, battery-stress cost, and non-frozen change penalties. That means a feasible stage-2 plan with:

- no cancellations,
- no charge rows,
- no changes to any previously accepted non-frozen trip,

has certified public objective `0`.

The step-2 solver therefore uses a finite-horizon restoration-first strategy:

1. Carry forward every previously accepted trip exactly as state.
2. Add the six newly visible trips with tentative `block_hint` buses.
3. Independently audit the exported artifact against the public hard rules, including:
   - schema and nullability,
   - frozen commitments,
   - compatibility,
   - deadhead timing,
   - SOC propagation with derates,
   - outages and temporary charger-session rules,
   - reserve checkpoints.
4. Because only the newly revealed trips are free decisions, exhaustively enumerate zero-charge assignments for those six trips while keeping the accepted legacy work fixed.
5. Publish only a candidate whose exact re-audit reaches `theta = 0`.

## Exact Search Variables

For each newly visible trip `t in {T201, ..., T206}` and each compatible bus `b` that can be inserted around the fixed legacy timeline by timing alone, the solver considers the assignment `assign(b, t) = 1`.

There are no charge decision variables in the published incumbent because the exact search finds a feasible zero-charge suffix. Charge rows remain part of the audit universe, so any future packet can still use the same checker.

## Hard Rules In The Audit

- Every visible trip has exactly one `trip` row.
- Critical trips cannot be canceled.
- A bus assignment must satisfy class and low-floor rules.
- Each bus timeline must respect deadhead travel and chronological order.
- SOC must satisfy trip start reserve, bus minimum SOC, bus maximum SOC, and derated trip energy.
- Chargers, if used, must respect compatibility, setup gaps, outages, feeder caps, site caps, and temporary session limits.
- Reserve checkpoints must count buses that are idle at the depot and above the required SOC threshold.
- Frozen commitments from the accepted prior artifact must be preserved exactly.

## Resulting Exact Incumbent

The carry-forward incumbent fails only on:

- `B06` running out of usable energy before new critical trip `T205`,
- the `720` reserve checkpoint missing one qualifying depot bus.

The exact suffix search repairs those violations without changing any previously accepted trip:

- `T202` moves from `B17` to `B08`,
- `T205` moves from `B06` to unused bus `B18`,
- `T206` stays on its hinted bus `B17`,
- every other trip keeps its accepted or hinted bus,
- no charging is needed.

That yields `theta = 0` and certified public objective `0`.

## Guarded Performance Phase

After the repaired stage-2 artifact exists, the continuation phase treats the on-disk [`step2_replan.csv`](/app/submissions/step2_replan.csv) as the rollback incumbent and reparses it directly from disk before any optimization.

The guarded performance model then uses a bounded exact SCIP solve over only the six newly revealed trips, with the rollback incumbent supplied as the MIP start. This restriction has a valid dominance certificate:

- the rollback incumbent already serves all visible trips,
- it uses no charge rows,
- it changes no previously accepted non-frozen trip,
- every omitted public objective term is nonnegative.

Therefore any charged or legacy-changing alternative cannot strictly improve on the incumbent public objective `0`. The exact suffix solve can still certify a candidate bound and gap, but publication is allowed only for a strict public-objective improvement after a fresh independent disk audit.
