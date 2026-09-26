# Week 2 Recovery Model

## Scope

This PySCIPOpt mixed-integer model replans weeks 2-10 from the executed week-1 state.
The solve uses the provided week-2 state for planning, and the independent audit reconstructs the same week-2 starting inventory from public inputs plus the accepted `initial_plan.csv` week-1 suffix.
It preserves the approved baseline where the frozen policy requires it, treats pipeline receipts as mandatory inbound quantities, and prices absolute changes from the approved plan.

## Decision Variables

- `setup[t,p,k] in {0,1}` for feasible plant-product runs.
- `produce_regular[t,p,k] >= 0` and `produce_overtime[t,p,k] >= 0`.
- `ship_plant_dc[t,p,d,k] >= 0`, `ship_dc_customer[t,d,c,k] >= 0`, and `transfer_dc[t,d1,d2,k] >= 0` on listed lanes only.
- `inventory[t,d,k] >= 0`, `shortage[t,c,k] >= 0`, and `emergency_material[t,p,m] >= 0`.
- Auxiliary absolute-deviation variables for production, plant-to-DC shipments, DC-to-customer shipments, and DC transfers.

## Objective

Minimize operating cost plus replanning disruption cost:

- setup, regular production, overtime production, emergency material, transportation, handling, transfer, holding, and shortage costs
- replan penalty `11.5` per unit of absolute deviation from `/app/submissions/initial_plan.csv` for production totals, plant-to-DC shipments, DC-to-customer shipments, and DC transfers

## Hard Constraints

- setup activation with min/max run sizes
- plant regular hours, overtime hours, and setup-team hours
- material balance with the week-2 robustness discount, emergency purchases, and committed pipeline material receipts
- same-week plant shipment balance
- plant-to-DC, DC-to-customer, and DC-transfer lane capacities
- exact demand balance with per-row service floors
- DC inventory balance from the provided week-1 executed state, including committed product pipeline receipts
- warehouse freezer capacity and safety-stock plus next-week regional reserve
- frozen commitment bounds from `frozen_commitment_policy.csv` on the approved baseline rows only

## Assumptions

- Frozen commitment bounds apply only to baseline rows with positive approved quantity. Baseline zeros do not forbid new recovery decisions.
- Replanning cost for production is measured on total weekly plant-product output (`regular + overtime`) versus the approved baseline output.
- Pipeline receipts are committed state transitions, not optional decision rows, and therefore do not appear in the exported CSV.

## Final Incumbent

- Published artifact result: `rollback incumbent kept; no strict audited improvement`
- Rollback incumbent objective: `169893.73380179`
- Candidate objective: `169893.73380179`
- Solve status: `optimal`
- Solver incumbent count: `6`
- Audited total objective: `169893.73380179`
- Max normalized hard residual theta: `2.73171967e-10`
- Violated hard groups: `0`
- Relative gap at stop: `0.00000000`
