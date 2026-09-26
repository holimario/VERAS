# Week 4 Recovery Model

## Scope

This PySCIPOpt mixed-integer model replans weeks 4-10 from the executed week-3 state.
The solve uses `week4_state.json` and independently checks that it matches the accepted `step2_replan.csv` week-3 terminal inventory.
It preserves the approved week-2 baseline where the frozen policy requires it, treats pipeline receipts as mandatory inbound quantities, and prices absolute changes from the approved plan.

## Decision Variables

- `setup[t,p,k] in {0,1}` for feasible plant-product runs.
- `produce_regular[t,p,k] >= 0` and `produce_overtime[t,p,k] >= 0`.
- `ship_plant_dc[t,p,d,k] >= 0`, `ship_dc_customer[t,d,c,k] >= 0`, and `transfer_dc[t,d1,d2,k] >= 0` on listed lanes only.
- `inventory[t,d,k] >= 0`, `shortage[t,c,k] >= 0`, and `emergency_material[t,p,m] >= 0`.
- Auxiliary absolute-deviation variables for production, plant-to-DC shipments, DC-to-customer shipments, and DC transfers.

## Objective

Minimize operating cost plus replanning disruption cost:

- setup, regular production, overtime production, emergency material, transportation, handling, transfer, holding, and shortage costs
- strategic shortage penalties for all public-health customers and for hospital demand of `V_MRNA` and `V_PED` during the priority window
- replan penalty `14.0` per unit of absolute deviation from `/app/submissions/step2_replan.csv` for production totals, plant-to-DC shipments, DC-to-customer shipments, and DC transfers

## Hard Constraints

- setup activation with min/max run sizes
- plant regular hours, overtime hours, and setup-team hours
- material balance with the week-4 robustness discount, emergency purchases, and committed pipeline material receipts
- same-week plant shipment balance
- plant-to-DC, DC-to-customer, and DC-transfer lane capacities
- exact demand balance with per-row service floors
- DC inventory balance from the executed week-3 state, including committed product pipeline receipts
- warehouse freezer capacity, including the reduced west-freezer capacity, and safety-stock plus next-week regional reserve
- frozen commitment bounds from `frozen_commitment_policy.csv` on the full public decision domain, using approved baseline quantities including zero-baseline keys, and including frozen transfers

## Assumptions

- `customer_filter` values such as `hospital` are interpreted as customer-segment filters, not literal customer IDs.
- Replanning cost for production is measured on total weekly plant-product output (`regular + overtime`) versus the approved baseline output.
- Pipeline receipts are committed state transitions, not optional decision rows, and therefore do not appear in the exported CSV.
- The week-4 priority window is modeled by applying the strategic shortage penalty to hospital demand for `V_MRNA` and `V_PED`; `MOBILE_FIELD` already receives the strategic public-health penalty.

## Final Incumbent

- Published artifact result: `rollback incumbent kept; no strict audited improvement`
- Rollback incumbent objective: `136018.36645997`
- Candidate objective: `138063.39866252`
- Rollback incumbent public audit pass: `no`
- Candidate public audit pass: `yes`
- Published artifact public audit pass: `no`
- Solve status: `optimal`
- Solver incumbent count: `6`
- Audited total objective: `136018.36645997`
- Max normalized hard residual theta: `2.25189500e+01`
- Violated hard groups: `2`
- Relative gap at stop: `0.00000000`
