# Step 3 Final Replan Model

This stage-3 model replans from the approved `step2_replan.csv` using the final visible booking packet, the final event notice, and all current tables under `/app/data`. It preserves every frozen commitment implied by `freeze_until_day`, enforces the updated protected-slot, transfer-cap, and special-booking tables as hard constraints, and charges change penalties against prior accepted step-2 commitments whose decision or full path changes outside the frozen window.

## Formulation

Sets and indices:

- `b in B`: all currently visible bookings.
- `p in P_b`: enumerated time-feasible loaded-leg paths for booking `b` under booking-path and special-booking limits.
- `l in L`: vessel legs.
- `n in N`: ports.
- `d in D`: days in the finite horizon.
- `c in {STD, REEFER}`: empty-container types.

Decision variables:

- `x[b,p] in {0,1}`: booking `b` is accepted on path `p`.
- `r[b] in {0,1}`: booking `b` is rejected.
- `m[l,c] in Z_+`: empty repositioning TEU on leg `l`.
- `u[n,d,c] in Z_+`: leased empties at port `n`, day `d`.
- `s[n,d,c] >= 0`: end-of-day empty stock.
- `z[b] in {0,1}` for prior accepted bookings: 1 if the stage-3 plan differs from the approved step-2 path for booking `b`.

Path generation:

- Paths use only public vessel legs and must satisfy booking time windows.
- Transshipment count is `number_of_loaded_legs - 1`.
- Paths respect `max_transship_count`, `requires_direct`, allowed services, and any `special_booking_rules.csv` maximum-leg override.
- Customs events are counted at export, each intermediate transfer departure port, and import.

Objective:

Maximize accepted booking revenue minus rejection penalties, empty reposition and carbon costs, lease cost, customs inspection cost, empty holding cost, and step-3 change penalties against the accepted step-2 plan.

Hard constraints:

- Exactly one accept/reject decision per visible booking.
- Mandatory acceptance for visible medical reefer bookings and any `special_booking_rules.csv` `require_accept=1` booking.
- Priority-group minimum acceptance floors from `service_rules.csv`.
- Vessel TEU, reefer-plug, and DG1 segregation limits.
- Empty inventory balance, nonnegativity, yard capacity, reefer-yard capacity, and gate limits.
- Lease upper bounds.
- Customs capacity and medical cold-chain sublimits.
- Protected-slot floors from `protected_slots.csv`.
- Reefer and DG1 transfer caps from `transfer_limits.csv`.
- Frozen prefixes, empty moves, and leases inherited from the approved step-2 plan inside the public frozen window.

## Stage-3 Notes

- `current_step.json` still reports `current_step=initial` and `freeze_until_day=0`, but the visible data clearly represent `step3`; the solver keeps the file's numeric frozen window as the hard boundary and treats the visible step-3 tables as active rules.
- Active special-booking rules: `2`. Mandatory special accepts: `B126, B127`.
- Frozen accepted booking prefixes implied by the public window: `0`.

## Solved Outcome

The final audited artifact `step3_replan.csv` is feasible with zero independent hard-constraint residual and audited objective `46805.30000000`.

- Accepted bookings: `122`
- Rejected bookings: `B001, B005, B008, B034, B061, B070`
- Enumerated feasible booking paths: `133`
- Bookings with no feasible path: `B001, B034`
- Prior accepted step-2 commitments changed: `2`
- Frozen-window violations among those changes under public `freeze_until_day=0`: `0`
- Protected-slot usage `L09/contract`: `8`
- Protected-slot usage `L11/premium`: `5`
- Protected-slot usage `L16/contract`: `15`
- Protected-slot usage `L20/premium`: `24`
