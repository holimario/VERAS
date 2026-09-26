# Initial Model

This initial-stage model uses only the currently visible packet in `/app/data` and the initial event notice. No future bookings, disruptions, protected-slot tables, transfer-cap tables, or replanning freezes are assumed.

## Formulation

Sets and indices:

- `b in B`: visible bookings.
- `p in P_b`: enumerated time-feasible loaded-leg paths for booking `b`.
- `l in L`: vessel legs.
- `n in N`: ports.
- `d in D`: days `0..14`.
- `c in {STD, REEFER}`: empty-container type.

Decision variables:

- `x[b,p] in {0,1}`: booking `b` is accepted on loaded path `p`.
- `r[b] in {0,1}`: booking `b` is rejected.
- `m[l,c] in Z_+`: empty repositioning TEU of type `c` on leg `l`.
- `u[n,d,c] in Z_+`: emergency leased TEU of type `c` at port `n` on day `d`.
- `s[n,d,c] >= 0`: end-of-day empty inventory of type `c` at port `n`.

Path construction:

- Paths are generated from the scheduled leg graph.
- The first leg must depart on or after the booking `earliest_load_day`.
- The final arrival must be on or before `due_arrival_day`.
- Transshipment count is `number_of_loaded_legs - 1`.
- Paths respect `max_transship_count`, `requires_direct`, and allowed service loops from `booking_path_rules.csv`.
- A port is not revisited inside a path.
- Customs events are attached at export, each intermediate departure port, and import.

Objective:

Maximize

`accepted revenue`
`- rejection penalties`
`- empty repositioning costs`
`- carbon charges`
`- emergency lease costs`
`- customs inspection costs`
`- empty holding costs`.

I used `carbon_kg_per_teu` with unit coefficient `1.0` because no separate carbon-price table is provided in the visible data.

Hard constraints:

- Booking decision completeness: `sum_p x[b,p] + r[b] = 1` for every visible booking.
- Mandatory medical reefer acceptance if such bookings are visible.
- Priority-group service floors from `service_rules.csv`.
- Vessel TEU capacity on every leg: loaded TEU plus empty moves.
- Vessel reefer-plug capacity on every leg: loaded reefers plus reefer empty moves.
- DG1 segregation limit on every leg.
- Customs capacity on every visible `(port, day, customs_group)` window, with medical reefer sublimits where applicable.
- Empty inventory balance by `(port, day, container_type)`:
  previous stock
  `+` booking releases
  `+` empty arrivals
  `+` leases
  `-` accepted booking origin consumption
  `-` empty departures
  `= current stock`.
- Nonnegative empty inventory at all ports and days.
- Yard-capacity limit on total empty stock.
- Reefer-plug yard limit on reefer empty stock.
- Daily gate-out limit on accepted booking origin pickups plus empty departures.
- Daily gate-in limit on empty releases, empty arrivals, and leases.
- Lease availability upper bounds by `(port, day, container_type)`.

Timing assumptions used in the exported plan:

- Standard containers release back into empty stock on the final loaded-leg arrival day.
- Reefer containers release one day after final arrival.
- Accepted bookings consume gate-out on the first loaded-leg departure day at the origin.
- Empty releases, empty reposition arrivals, and leases consume gate-in.
- Every `empty_move` CSV row exports the public leg departure day in the `day` column and is independently checked against `vessel_legs.csv`.

## Solve Method

The solver is a compact path-based MIP in PySCIPOpt/SCIP with:

- exact path enumeration for each visible booking,
- integer empty-move and lease decisions,
- daily inventory-state variables for empties,
- rollback-incumbent warm start from the on-disk accepted artifact,
- guarded candidate publication only on strict audited objective improvement,
- direct export to the required CSV schema,
- independent artifact audit rerun from the written CSV.

SCIP parameters:

- relative gap: `0.0005`
- time limit: `min(60, ORCLAW_SOLVE_TIME_LIMIT_SECONDS)` seconds
- threads: `1`

## Solved Initial Outcome

The solved initial artifact accepts 50 of 54 visible bookings, rejects `B001`, `B005`, `B008`, and `B034`, uses one empty repositioning move `EM_L07_STD = 11`, and uses no leases. The final audited objective is `5157.00000000` with zero audited hard-constraint residual.
