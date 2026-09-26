# Wildfire Bus Evacuation Planning Model

## Modeling choice

This instance is best handled as a two-stage stochastic evacuation problem with:

- first-stage activation decisions for shelters, bus staging, and corridor contraflow
- second-stage scenario-specific bus trip sequencing, pickup-to-shelter assignment, passenger quantities, and timing

For this public instance, the shelter and corridor first-stage choices are effectively dominated:

- In scenarios with `min_shelter_smoke_rating = 4`, only `S1,S2,S3,S5` are usable.
- `max_open_shelters = 4`, and those four shelters already cover the worst-case severe-smoke capacity, wheelchair, pet, medical-bed, and backup-power needs much better than any set containing `S4` or `S6`.
- With those shelters open, activating all five corridors remains inside the planning budget and only improves travel time, closure time, and corridor trip capacity.

So the implemented solver fixes:

- `open_s = 1` for `s in {S1,S2,S3,S5}`, `0` otherwise
- `active_c = 1` for all corridors `c`

and optimizes bus staging plus the exact scenario recourse. This keeps the solve budget focused on the real bottleneck: scenario routing and timing.

## Implementation strategy

The benchmark objective and constraints are modeled below in full. The actual `solve.py` implementation uses a decomposition tailored to the short runtime budget:

- solve a small PySCIPOpt staging assignment MIP
- start from the current audited artifact when present, then run bounded first-stage local search over bus staging and small shelter/corridor subset neighborhoods
- construct each scenario route plan with an exact simulator that respects bus timing, pickup boarding-bay limits, corridor trip limits, shelter capacities, service floors, fairness, and shift limits
- recompute the true benchmark score from the exported artifact with an independent audit

This is intentional: on this public instance, the first-stage decisions are nearly forced, while scenario routing is easier to make reliable with direct constructive scheduling than with a large monolithic MIP under a 5-minute budget.

To keep the scenario MIP tight and robust, boarding at each pickup is serialized. This is a conservative strengthening of the public rule because every pickup has at least one boarding bay. Any solution feasible for the implemented model is therefore feasible for the benchmark rules.

## Sets and indices

- `B`: buses, index `b`
- `D`: depots, index `d`
- `P`: pickup sites, index `p`
- `S`: shelters, index `s`
- `C`: corridors, index `c`
- `W`: scenarios, index `w`
- `K = {1,...,Kmax}`: trip positions on each bus route
- `J = {(b,k): b in B, k in K}`: bus-trip slots

Here `Kmax = 4`, which is enough for this instance because the fleet-wide service floors can be met with far fewer than `|B| * 4` trips and the shift/closure limits make longer per-bus schedules unattractive.

## Parameters

Input parameters come directly from the CSV/JSON files. The main derived parameters are:

- `dem[w,p]`, `wc_dem[w,p]`, `pet_dem[w,p]`
- `prob[w]`
- `floor[p]`: minimum served fraction
- `sip_lim[p]`: shelter-in-place limit fraction
- `prio[p] in {high, standard, remote, medical}`
- `corr[p] in C`: corridor assigned to pickup `p`
- `cap_bus[b]`, `wc_cap_bus[b]`, `shift[b]`, `op_cost[b]`
- `stage_cap[d]`, `stage_cost[d]`, `release[d]`
- `cap_shel[s]`, `wc_cap_shel[s]`, `pet_cap_shel[s]`, `med_cap_shel[s]`
- `backup[s]`, `smoke_rating[s]`
- `travel_mult[w,c]`, `cap_trip[w,c]`, `smoke_mult[w,c]`, `latest[w,c]`
- `bonus_cap[c]`, `bonus_close[c]`, `active_time_factor[c]`
- `warn[w]`, `global_mult[w]`, `min_smoke[w]`

Distances use Euclidean geometry from the provided coordinates.

For any leg from location `u` to pickup `p`, with `u` a depot or shelter:

`base_leg_time[u,p,w] = (base_intersection_delay + minutes_per_distance_unit * dist(u,p)) * global_mult[w] * travel_mult[w,corr[p]] * active_time_factor[corr[p]]`

For the loaded leg from pickup `p` to shelter `s`:

`loaded_time[p,s,w] = (base_intersection_delay + minutes_per_distance_unit * dist(p,s)) * global_mult[w] * travel_mult[w,corr[p]] * active_time_factor[corr[p]]`

Boarding and unloading times:

- `board(j) = load_time_per_person * evac_j + wheelchair_extra_board_min * wc_j`
- `unload(j) = unload_time_per_person * evac_j`

## First-stage staging model

Decision variable:

- `z[b,d] in {0,1}`: bus `b` is staged at depot `d`

Constraints:

1. Each bus is staged exactly once:
   `sum_d z[b,d] = 1`
2. Depot staging capacity:
   `sum_b z[b,d] <= stage_cap[d]`
3. Planning budget:
   `sum_b sum_d stage_cost[d] z[b,d] + fixed_open_cost + fixed_corridor_cost <= planning_budget`

The staging objective uses a demand-weighted proxy for first-leg access:

`min sum_b sum_d (stage_cost[d] + proxy[b,d]) z[b,d]`

where `proxy[b,d]` is the expected first-trip deadhead time from depot `d`, weighted by scenario probabilities, pickup demands, and bus operating cost.

## Second-stage scenario routing model

For each scenario `w`, after fixing the staging depot of each bus, solve the following MIP.

### Decision variables

- `x[w,b,k,p,s] in {0,1}`: trip slot `(b,k)` serves pickup `p` and ends at shelter `s`
- `u[w,b,k] in {0,1}`: trip slot `(b,k)` is used
- `g[w,b,k,s,p] in {0,1}`: trip `k` on bus `b` ends at shelter `s` and trip `k+1` starts at pickup `p`
- `start[w,b,k] >= 0`: boarding start time at the pickup
- `depart[w,b,k] >= 0`: departure time from the pickup after boarding
- `e[w,b,k,p,s] in Z_+`: evacuees carried on that trip
- `q[w,b,k,p,s] in Z_+`: wheelchair evacuees on that trip
- `r[w,b,k,p,s] in Z_+`: pets on that trip
- `unserved_e[w,p], unserved_q[w,p], unserved_r[w,p] >= 0`
- `sip_e[w,p], sip_q[w,p], sip_r[w,p] >= 0`

Convenience expressions:

- `pickup_used[w,b,k,p] = sum_s x[w,b,k,p,s]`
- `shel_used[w,b,k,s] = sum_p x[w,b,k,p,s]`
- `served_e[w,p] = sum_b sum_k sum_s e[w,b,k,p,s] + sip_e[w,p]`

### Objective for scenario `w`

Minimize

`scenario_cost[w] =`

- `sum_b sum_k op_cost[b] * trip_completion_time[w,b,k]`
- `+ sum_b sum_k sum_p sum_s late_pickup_penalty_per_person_min * depart[w,b,k] * e[w,b,k,p,s]`
- `+ sum_b sum_k sum_p sum_s smoke_exposure_cost_per_person * smoke_severity[w] * smoke_mult[w,corr[p]] * e[w,b,k,p,s]`
- `+ sum_p unserved_penalty_per_person * (unserved_e[w,p] + (wheelchair_unserved_multiplier-1) * unserved_q[w,p] + pet_unserved_multiplier * unserved_r[w,p])`
- `+ sum_p shelter_in_place_penalty_per_person * (sip_e[w,p] + (wheelchair_unserved_multiplier-1) * sip_q[w,p] + pet_unserved_multiplier * sip_r[w,p])`

where

`trip_completion_time[w,b,k] = depart[w,b,k] + sum_p sum_s loaded_time[p,s,w] x[w,b,k,p,s] + unload_time_per_person * sum_p sum_s e[w,b,k,p,s]`

Since the first stage is fixed, minimizing each `scenario_cost[w]` independently also minimizes the total monotone risk measure.

### Constraints

#### Trip structure

1. One pickup-shelter pair per used slot:
   `sum_p sum_s x[w,b,k,p,s] = u[w,b,k]`
2. No gaps in a bus route:
   `u[w,b,k+1] <= u[w,b,k]`
3. Quantities only on active assignments:
   `e[w,b,k,p,s] <= cap_bus[b] * x[w,b,k,p,s]`
   `q[w,b,k,p,s] <= wc_cap_bus[b] * x[w,b,k,p,s]`
   `r[w,b,k,p,s] <= cap_bus[b] * x[w,b,k,p,s]`
4. Bus capacities:
   `sum_p sum_s e[w,b,k,p,s] <= cap_bus[b]`
   `sum_p sum_s q[w,b,k,p,s] <= wc_cap_bus[b]`
   `sum_p sum_s r[w,b,k,p,s] <= sum_p sum_s e[w,b,k,p,s]`
   `sum_p sum_s q[w,b,k,p,s] <= sum_p sum_s e[w,b,k,p,s]`

#### Demand accounting

For every scenario `w` and pickup `p`:

5. Evacuees:
   `sum_b sum_k sum_s e[w,b,k,p,s] + unserved_e[w,p] + sip_e[w,p] = dem[w,p]`
6. Wheelchairs:
   `sum_b sum_k sum_s q[w,b,k,p,s] + unserved_q[w,p] + sip_q[w,p] = wc_dem[w,p]`
7. Pets:
   `sum_b sum_k sum_s r[w,b,k,p,s] + unserved_r[w,p] + sip_r[w,p] = pet_dem[w,p]`
8. Subset consistency:
   `unserved_q[w,p] <= unserved_e[w,p]`
   `unserved_r[w,p] <= unserved_e[w,p]`
   `sip_q[w,p] <= sip_e[w,p]`
   `sip_r[w,p] <= sip_e[w,p]`

#### Service floor and fairness

9. Service floor:
   `served_e[w,p] >= floor[p] * dem[w,p]`
10. Shelter-in-place limit:
   `sip_e[w,p] <= sip_lim[p] * dem[w,p]`
11. Fairness:
   for every `p` in `{high, medical}` and `p'` in `{standard, remote}`,
   `dem[w,p'] * served_e[w,p] + fairness_gap * dem[w,p] * dem[w,p'] >= dem[w,p] * served_e[w,p']`

#### Shelter feasibility

For each scenario `w` and shelter `s`:

12. Smoke usability:
   if `smoke_rating[s] < min_smoke[w]`, then no assignment to `s`
13. Total shelter capacity:
   `sum_b sum_k sum_p e[w,b,k,p,s] <= cap_shel[s] * open_s`
14. Wheelchair capacity:
   `sum_b sum_k sum_p q[w,b,k,p,s] <= wc_cap_shel[s] * open_s`
15. Pet capacity:
   `sum_b sum_k sum_p r[w,b,k,p,s] <= pet_cap_shel[s] * open_s`
16. Medical beds:
   `sum_b sum_k sum_p medical_share[p] * e[w,b,k,p,s] <= med_cap_shel[s] * open_s`
17. Backup power for medical pickups:
   if `priority_class[p] = medical` and `backup[s] = 0`, then `x[w,b,k,p,s] = 0`

#### Corridor capacity and closure

For each scenario `w` and corridor `c`:

18. Trip count:
   `sum_b sum_k sum_{p: corr[p]=c} sum_s x[w,b,k,p,s] <= cap_trip[w,c] + bonus_cap[c] * active_c`
19. Safe departure:
   if trip `(b,k)` serves pickup `p`,
   `depart[w,b,k] <= latest[w,corr[p]] + bonus_close[corr[p]] * active_{corr[p]}`

#### Bus timing

20. Boarding duration:
   `depart[w,b,k] = start[w,b,k] + load_time_per_person * sum_p sum_s e[w,b,k,p,s] + wheelchair_extra_board_min * sum_p sum_s q[w,b,k,p,s]`
21. First trip earliest boarding start:
   `start[w,b,1] >= warn[w] + release[staged_depot[b]] + sum_p sum_s base_leg_time[staged_depot[b],p,w] * x[w,b,1,p,s]`
22. Inter-trip continuity:
   for `k = 1,...,Kmax-1`,
   `start[w,b,k+1] >= depart[w,b,k]`
   `+ sum_p sum_s loaded_time[p,s,w] * x[w,b,k,p,s]`
   `+ unload_time_per_person * sum_p sum_s e[w,b,k,p,s]`
   `+ sum_s sum_p base_leg_time[s,p,w] * g[w,b,k,s,p]`
23. Transition linearization:
   `g[w,b,k,s,p] <= shel_used[w,b,k,s]`
   `g[w,b,k,s,p] <= pickup_used[w,b,k+1,p]`
   `g[w,b,k,s,p] >= shel_used[w,b,k,s] + pickup_used[w,b,k+1,p] - 1`
24. Shift completion:
   `depart[w,b,k] + sum_p sum_s loaded_time[p,s,w] * x[w,b,k,p,s] + unload_time_per_person * sum_p sum_s e[w,b,k,p,s] <= warn[w] + shift[b] + M * (1-u[w,b,k])`

#### Conservative pickup serialization

25. For any two distinct trip slots `j` and `j'` in the same scenario and any pickup `p`, if both slots serve `p`, their boarding intervals may not overlap.

With binary order variable `ord[w,p,j,j']`, enforce:

- `depart[w,j] <= start[w,j'] + M * (1 - ord[w,p,j,j'] + 2 - pickup_used[w,j,p] - pickup_used[w,j',p])`
- `depart[w,j'] <= start[w,j] + M * (ord[w,p,j,j'] + 2 - pickup_used[w,j,p] - pickup_used[w,j',p])`

This is a valid strengthening because it uses one boarding bay even when the public instance offers two.

## Overall reported objective

After all scenario MIPs are solved, the reported total score is

`fixed_first_stage_cost + sum_w prob[w] * scenario_cost[w] + tail_risk_weight * CVaR_alpha({scenario_cost[w]})`

with `alpha = tail_risk_alpha` and the standard linear CVaR formula:

- variable `eta`
- variables `xi[w] >= 0`
- `xi[w] >= scenario_cost[w] - eta`
- `CVaR_alpha = eta + (1 / (1-alpha)) * sum_w prob[w] * xi[w]`

In the implementation, the final score is always recomputed from the exported `solution.json` by independent audit. The constructive scenario scheduler therefore optimizes feasibility first and true score second, while the mathematical model above defines the benchmark objective being audited.
