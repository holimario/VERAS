# Mathematical Model: eVTOL Vertiport Recovery

## 1. Modeling choice

This instance is best modeled as a **two-stage robust time-expanded aircraft-path MIP**:

- First-stage binary variables decide which passenger requests are accepted.
- Second-stage recourse is scenario-specific and builds one feasible time-ordered path for each aircraft across a directed acyclic network of ready states.
- Shared resource constraints are enforced by slot across all aircraft in each scenario.

This is stronger than a naive pairwise no-overlap model because aircraft feasibility is captured by flow on a time-expanded DAG, while scenario coupling is limited to the accept/decline variables.

## 2. Sets and indices

- `S`: scenarios.
- `A`: aircraft.
- `V`: vertiports.
- `T = {0, ..., H-1}`: operating slots, where `H = planning_horizon_slots`.
- `N = {0, ..., H + tau_max}`: ready-state time nodes, where `tau_max = max_a turnaround_slots_a`.
- `R`: passenger requests.
- `G`: request groups.
- `Q_g`: requests in group `g`.
- `K`: directed routes `(i,j)`.
- `K_s^open`: routes open in scenario `s`.
- `P_s(a)`: feasible passenger-flight arcs `(r,t)` for aircraft `a` in scenario `s`.
- `M_s(a)`: feasible reposition arcs `((i,j),t)` for aircraft `a` in scenario `s`.

## 3. Parameters

From the public data:

- `p_r`: passengers in request `r`.
- `fare_r`: fare per passenger for request `r`.
- `pen_r`: decline penalty for request `r`.
- `es_r, ls_r`: earliest and latest departure slots for request `r`.
- `o_r, d_r`: origin and destination of request `r`.
- `cap_a`: seat capacity of aircraft `a`.
- `bcap_a`: battery capacity of aircraft `a`.
- `b0_a`: nominal initial SOC of aircraft `a`.
- `home_a`: home vertiport of aircraft `a`.
- `chgkw_a`: maximum charge power of aircraft `a`.
- `turn_a`: turnaround slots of aircraft `a`.
- `noise_a`: aircraft noise multiplier.
- `dur_ij`, `eng_ij`, `opcost_ij`, `noise_ij`, `corr_ij`: route duration, energy, passenger-flight cost, noise points, corridor.
- `prob_s`: scenario probability.
- `gdelay_s`: global delay slots.
- `chgder_s`: charger derate.
- `socder_s`: scenario SOC derate.
- `target_s`: target profit for scenario `s`.
- `open_sij`, `multdur_sij`, `multeng_sij`: scenario route status and multipliers.
- `release_sa`, `socder_sa`: scenario-aircraft release and SOC derate.
- `price_vt`, `gridmult_vt`: grid price and local grid multiplier.
- `pads_v`, `chargers_v`, `stands_v`, `gridkw_v`: vertiport capacities.
- `quota_day_v`, `quota_eve_v`: departure noise quotas.
- `slot_minutes`, `reserve_soc`, `charge_eff`, `high_frac`, `taper_frac`, `delay_pen`, `rep_cost`, `alpha`, `tail_wt`: config values.
- `origin_floor_v`: accepted-passenger floor by origin.
- `reserve_req_(s,v,t)`, `reserve_soc_(s,v,t)`: emergency reserve requirement.
- `window_(s,a) = (v, ws, we, minsoc)`: service window data.
- `ctrl_(s,c)` gives corridor control intervals and slot cap reductions.
- `period_t in {day, evening}` from `time_slots.csv`.

Derived parameters:

- `slot_hours = slot_minutes / 60`.
- Scenario-adjusted route duration:
  `D_sij = ceil(dur_ij * multdur_sij + gdelay_s)`.
- Scenario-adjusted route energy:
  `E_sij = eng_ij * multeng_sij`.
- Scenario-adjusted initial SOC:
  `B0_sa = b0_a * socder_s * socder_sa`.
- Fast one-slot charge limit:
  `R_sa = chgkw_a * chgder_s * slot_hours`.
- Taper one-slot charge limit above threshold:
  `Rt_sa = taper_frac * R_sa`.
- High-SOC threshold:
  `TH_a = high_frac * bcap_a`.

Only arcs with `open_sij = 1`, enough seats, and `t + D_sij <= H` are generated.

## 4. Decision variables

### First stage

- `x_r in {0,1}`: 1 if request `r` is accepted.

### Second stage, for each scenario `s` and aircraft `a`

Ready-state flow variables on the DAG:

- `idle_savt in {0,1}`: aircraft `a` idles at vertiport `v` during slot `t`, moving from ready node `(v,t)` to `(v,t+1)`.
- `chg_savt in {0,1}`: aircraft `a` charges at vertiport `v` during slot `t`, moving from `(v,t)` to `(v,t+1)`.
- `y_sart in {0,1}`: aircraft `a` serves request `r` departing at slot `t`.
- `z_saijt in {0,1}`: aircraft `a` repositions from `i` to `j` departing at slot `t`.

Continuous variables:

- `e_savt >= 0`: energy charged during slot `t` by aircraft `a` at `v`.
- `b_sat`: battery SOC of aircraft `a` at slot boundary `t`, for `t in {0,...,H}`.
- `dep_sr`: departure slot of served request `r` in scenario `s`.
- `pi_s`: scenario profit.
- `u_s >= 0`: shortfall of scenario profit below target.
- `eta`: CVaR threshold.
- `xi_s >= 0`: excess shortfall for CVaR.

Auxiliary binaries:

- `res_savt in {0,1}`: aircraft `a` counts toward reserve requirement at `(s,v,t)`.
- `h_sat in {0,1}`: indicates `b_sat <= TH_a` for charging taper linearization.

## 5. Objective

Let accepted-request revenue net of decline penalties be

`ACC = sum_r [(p_r * fare_r + pen_r) x_r]`.

For scenario `s`, define

`pi_s = ACC`
`      - sum_(a,r,t) opcost_(o_r,d_r) * y_sart`
`      - sum_(a,(i,j),t) rep_cost * E_sij * z_saijt`
`      - sum_(a,v,t) (price_vt / charge_eff) * e_savt`
`      - sum_r delay_pen * p_r * (dep_sr - es_r)`.

Shortfall relative to the scenario target:

- `u_s >= target_s - pi_s`
- `u_s >= 0`

CVaR tail-risk term at confidence `alpha`:

- `xi_s >= u_s - eta`
- `xi_s >= 0`
- `CVaR = eta + (1 / (1 - alpha)) * sum_s prob_s * xi_s`

Maximize

`sum_s prob_s * pi_s - tail_wt * CVaR`.

## 6. Constraints

### 6.1 First-stage acceptance rules

For every request group `g` and `r, r' in Q_g`:

- `x_r = x_r'`

Global acceptance floors:

- `sum_r p_r x_r >= minimum_accepted_passenger_fraction * sum_r p_r`
- `sum_(r: priority=medical) p_r x_r >= minimum_medical_accepted_fraction * sum_(r: priority=medical) p_r`
- `sum_(r: market=airport) p_r x_r >= minimum_airport_accepted_passengers`
- `sum_(r: priority in {airport, medical}) (1 - x_r) <= maximum_declined_priority_requests`

Origin service floors:

- `sum_(r: o_r = v) p_r x_r >= origin_floor_v` for each origin with a floor.

If request `r` has no feasible service arc in some scenario, fix `x_r = 0`.

### 6.2 Aircraft path flow on the ready-state DAG

For each scenario `s`, aircraft `a`, vertiport `v`, and ready node time `t < H`:

`incoming_savt = outgoing_savt`

where

- `incoming_savt` is the sum of:
  - source supply `1` if `t = release_sa` and `v = home_a`,
  - `idle_sav,t-1 + chg_sav,t-1` if `t > 0`,
  - all passenger/reposition arcs whose ready time is `t` and destination is `v`.
- `outgoing_savt` is:
  - `idle_savt + chg_savt`,
  - all passenger arcs `y_sart` with `o_r = v`,
  - all reposition arcs `z_saijt` with `i = v`.

All arcs that would start before `release_sa` or end after `H` as a scheduled action are omitted.

Aircraft can finish at any ready node time `t >= H` reached by the last flown arc.

### 6.3 Request service in every scenario

For each scenario `s` and request `r`:

- `sum_(a,t) y_sart = x_r`

Only start times `t in [es_r, ls_r]` are generated, and only aircraft with `cap_a >= p_r` are eligible.

Departure-time definition:

- `dep_sr = sum_(a,t) t * y_sart`

### 6.4 Group departure spread

For each group `g`, scenario `s`, and `r, r' in Q_g`:

- `dep_sr - dep_sr' <= spread_g + H * (1 - x_r)`
- `dep_sr' - dep_sr <= spread_g + H * (1 - x_r)`

### 6.5 Battery dynamics

Initial SOC:

- `b_sa0 = B0_sa`

For each `s,a,t in T`:

`b_sa,t+1 = b_sat`
`           + sum_v e_savt`
`           - sum_(r starting at t) E_s,(o_r,d_r) * y_sart`
`           - sum_((i,j) starting at t) E_sij * z_saijt`

Battery bounds:

- `0 <= b_sat <= bcap_a` for all `t`

Reserve after every flight:

- `b_sat - E_s,(o_r,d_r) >= reserve_soc - bcap_a * (1 - y_sart)`
- `b_sat - E_sij >= reserve_soc - bcap_a * (1 - z_saijt)`

### 6.6 Charging power, taper, and battery capacity

For each `s,a,v,t`:

- `0 <= e_savt <= R_sa * chg_savt`
- `e_savt <= bcap_a - b_sat + bcap_a * (1 - chg_savt)`

To encode the high-SOC taper exactly for a one-slot charging action, use auxiliary binary `h_sat` and the linearized below-threshold headroom:

- `head_s_at >= TH_a - b_sat`
- `head_s_at <= TH_a - b_sat + bcap_a * (1 - h_sat)`
- `head_s_at <= bcap_a * h_sat`
- `head_s_at >= 0`
- `e_savt <= Rt_sa * chg_savt + (1 - taper_frac) * head_s_at`

Together with `e_savt <= R_sa * chg_savt`, this gives the exact one-slot charge cap:

- full-rate if SOC is well below the threshold,
- mixed-rate if the slot crosses the threshold,
- tapered-rate if SOC starts above the threshold.

### 6.7 Vertiport chargers and grid headroom

For each scenario `s`, vertiport `v`, and slot `t`:

- `sum_a chg_savt <= chargers_v`
- `sum_a e_savt / charge_eff <= gridkw_v * chgder_s * gridmult_vt * slot_hours`

### 6.8 Pad capacity

For each scenario `s`, vertiport `v`, and slot `t`:

- departures from `v` at slot `t`
- plus arrivals to `v` with `end_slot = t`

must satisfy:

- `sum departing/arriving actions <= pads_v`

Passenger and reposition flights both count.

### 6.9 Corridor capacity and slot controls

For each scenario `s`, corridor `c`, and slot `t`, the number of flights occupying slot `t` on corridor `c` is limited by

- base route corridor capacity, and
- any stricter scenario control in `corridor_slot_controls.csv`.

So:

- `sum_(actions occupying corridor c in slot t) <= cap_sct`

where `cap_sct` is the minimum of the route-level corridor capacity and active control limit for that corridor/slot.

### 6.10 Noise quotas

For each scenario `s`, vertiport `v`, and period `p in {day, evening}`:

- `sum_(departures from v in period p) noise_(route) * noise_a <= quota_(v,p)`

Passenger and reposition departures both count.

### 6.11 Parking stands

For each scenario `s`, vertiport `v`, and slot `t`:

- idle aircraft at `v` in slot `t`,
- charging aircraft at `v` in slot `t`,
- aircraft in turnaround at `v` in slot `t`

must satisfy:

- `sum grounded presence <= stands_v`

### 6.12 Mandatory service windows

For each service window `(s,a,v,ws,we,minsoc)`:

- `idle_savt = 1` for every `t in {ws,...,we-1}`
- `chg_savt = 0` for every `t in {ws,...,we-1}`
- `b_sa,ws >= minsoc`

Since the aircraft path is unique, forcing consecutive idle arcs at the specified vertiport makes the aircraft remain idle there for the whole window.

### 6.13 Emergency reserve checkpoints

For each reserve checkpoint `(s,v,t)`:

- `res_savt <= idle_savt`
- `b_sat >= reserve_soc_(s,v,t) - bcap_a * (1 - res_savt)`
- `sum_a res_savt >= min_idle_aircraft_(s,v,t)`

Only truly idle aircraft count; charging or turnaround aircraft do not.

## 7. Exported solution

The exported CSV uses:

- one `accept` row per request,
- one `flight` row per passenger-flight arc with `decision = serve`,
- one `reposition` row per selected empty flight arc,
- one `charge` row per selected charging slot.

For charge rows:

- `origin` is the charging vertiport,
- `start_slot = t`, `end_slot = t+1`,
- `energy_kwh = e_savt`.

Quantities and energies are written with up to `1e-8` precision.

## 8. Independent validation

After solving, the exported artifact is re-audited from scratch against the public data:

- schema and identifier validity,
- first-stage acceptance rules,
- scenario-by-scenario request coverage,
- aircraft continuity and release times,
- route openness and durations,
- battery and reserve feasibility,
- charger, grid, pad, corridor, parking, and noise limits,
- service windows and emergency reserve constraints,
- objective recomputation and scenario-profit audit.

The solver only exports a schedule when SCIP has an incumbent solution.
