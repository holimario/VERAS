# Step 2 Replan Model

This stage-2 model replans from the approved [`initial_plan.csv`](/app/submissions/initial_plan.csv) using the newly visible wave-2 bookings, the disruption notice in [`EVENT_NOTICE.md`](/app/EVENT_NOTICE.md), and all current tables under [`/app/data`](/app/data). The solver preserves any frozen commitments implied by `freeze_until_day`, enforces the new protected-slot and transfer-cap tables as hard constraints, and charges replanning penalties against prior accepted commitments when their decision or full path changes.

## Formulation

Sets and indices:

- `b in B`: all currently visible bookings.
- `p in P_b`: enumerated time-feasible loaded-leg paths for booking `b`.
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
- `z[b] in {0,1}` for prior accepted bookings: 1 if the stage-2 plan differs from the approved step-1 path for booking `b`.

Path generation:

- Paths use the public leg network only.
- First departure must satisfy `earliest_load_day`; final arrival must satisfy `due_arrival_day`.
- Transshipment count is `number_of_loaded_legs - 1`.
- Paths respect `max_transship_count`, `requires_direct`, and allowed services from `booking_path_rules.csv`.
- Customs events are counted at export, each intermediate transfer departure port, and import.

Objective:

Maximize

- accepted booking revenue
- minus rejection penalties
- minus empty repositioning cost and carbon cost
- minus lease cost
- minus customs inspection cost
- minus empty holding cost
- minus stage-2 change penalties on prior accepted bookings whose path/decision changes.

Hard constraints:

- Exactly one accept/reject decision per visible booking.
- Mandatory medical-reefer acceptance if such bookings appear.
- Priority-group minimum acceptance floors from `service_rules.csv`.
- Vessel TEU, reefer-plug, and DG1 segregation limits.
- Empty inventory balance, nonnegativity, yard capacity, reefer-yard capacity, and gate limits.
- Lease upper bounds.
- Customs capacity and medical cold-chain sublimits.
- Protected-slot floors from `protected_slots.csv`.
- Reefer and DG1 transfer caps from `transfer_limits.csv`.
- Frozen commitments from the approved initial plan:
  accepted booking prefixes whose departures fall inside the frozen window,
  empty moves on frozen-departure legs,
  leases on frozen days.

## Stage-2 Notes

- `current_step.json` still labels the instance as `initial`, but the visible data clearly represent the step-2 replan state. The solver therefore applies the stage-2 rules from the user instruction and event notice while taking `freeze_until_day = 0` from the file.
- No `special_booking_rules.csv` is currently present, so no extra booking-level disruption mandates beyond the public booking-path rules were activated.
- Change penalties were modeled only for prior accepted commitments, consistent with the public statement wording.

## Solved Outcome

The final audited artifact [`step2_replan.csv`](/app/submissions/step2_replan.csv) is feasible with zero independent hard-constraint residual and audited objective `22693.30000000`.

- Accepted bookings: `83`
- Rejected bookings: `B001`, `B005`, `B008`, `B034`, `B070`
- Protected-slot usage: `L09/contract = 8`, `L11/premium = 5`, `L16/contract = 7`
- Empty moves used: `EM_L03_REEFER = 1`, `EM_L03_STD = 9`, `EM_L07_STD = 22`
- Leases used: none
- Replanning change penalty paid: `0`
