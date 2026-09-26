# Week 6 Recovery Model

## Scope

This PySCIPOpt mixed-integer model replans weeks 6-10 from the executed week-5 state.
The solve uses `week6_state.json` and independently checks that it matches the accepted `step3_replan.csv` week-5 terminal inventory.
It preserves the approved step-3 suffix where the week-6 frozen policy requires it, treats pipeline receipts as mandatory inbound quantities, and prices absolute changes from the approved plan.

## Decision Variables

- `setup[t,p,k] in {0,1}` for feasible plant-product runs.
- `produce_regular[t,p,k] >= 0` and `produce_overtime[t,p,k] >= 0`.
- `ship_plant_dc[t,p,d,k] >= 0`, `ship_dc_customer[t,d,c,k] >= 0`, and `transfer_dc[t,d1,d2,k] >= 0` on listed lanes only.
- `validated_shipper[t,d,c,k] in Z_+` for covered week-6 outbreak shipments, bounded by policy box availability.
- `inventory[t,d,k] >= 0`, `shortage[t,c,k] >= 0`, and `emergency_material[t,p,m] >= 0`.
- Auxiliary absolute-deviation variables for production totals, plant-to-DC shipments, DC-to-customer shipments, and DC transfers.

## Objective

Minimize operating cost plus replanning disruption cost:

- setup, regular production, overtime production, emergency material, transportation, validated-shipper box, handling, transfer, holding, and shortage costs
- strategic shortage penalties for public-health demand
- replan penalty `18.0` per unit of absolute deviation from `/app/submissions/step3_replan.csv` for production totals, plant-to-DC shipments, DC-to-customer shipments, and DC transfers

## Hard Constraints

- setup activation with min/max run sizes
- plant regular hours, overtime hours, and setup-team hours
- material balance with the week-6 robustness discount, emergency purchases, and committed pipeline material receipts
- same-week plant shipment balance
- plant-to-DC, DC-to-customer, and DC-transfer lane capacities
- validated shipper coverage `shipment <= box_capacity * boxes` plus weekly box availability by DC
- exact demand balance with per-row service floors
- DC inventory balance from the executed week-5 state, including committed product pipeline receipts
- warehouse freezer capacity and safety-stock plus next-week regional reserve
- frozen commitment bounds from `frozen_commitment_policy.csv` on the full public decision domain, including `produce_regular` week-6 bounds and zero-baseline keys

## Assumptions

- `customer_filter=non_priority` is interpreted as the non-priority retail and rural segments; week-6 hospital and public-health shipments remain flexible for the recovery response.
- Replanning cost for production is measured on total weekly plant-product output (`regular + overtime`) versus the approved baseline output.
- Pipeline receipts are committed state transitions, not optional decision rows, and therefore do not appear in the exported CSV.
- The current shipper policy data contains one covered customer-product row per DC, so the DC-level box limit is identical to the row-level limit in this instance.

## Final Incumbent

- Published artifact result: `rollback incumbent kept; no strict audited improvement`
- Rollback incumbent objective: `119716.58727080`
- Candidate objective: `119716.58727080`
- Rollback incumbent public audit pass: `yes`
- Candidate public audit pass: `yes`
- Published artifact public audit pass: `yes`
- Solve status: `optimal`
- Solver incumbent count: `100`
- Audited total objective: `119716.58727080`
- Max normalized hard residual theta: `6.31874562e-08`
- Violated hard groups: `0`
- Relative gap at stop: `0.00000000`
