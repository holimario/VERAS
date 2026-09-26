# Railway Disruption Recovery Model

## Formulation choice

This instance is best modeled as a **continuous-time event-timing MIP with endpoint-choice binaries and resource-path assignment layers**.

- A pure assignment model is too weak because the main difficulty is the interaction between continuous event times, a blocked segment, headway rules, and throat separations.
- A pure network-flow timetable model is incomplete because rolling stock and crews must form physically chainable paths whose feasibility depends on the chosen event times and short-turn endpoints.
- A time-indexed model would be unnecessarily large for a 360-minute horizon with minute-level times and only 14 trains.

The implemented solution therefore uses:

1. A main MIP for train disposition, event times, segment usage, disruption avoidance, headway, throat separation, rolling-stock assignment, crew assignment, and passenger-connection protection.
2. A per-station exact platform-assignment subproblem after the timetable is fixed. With fixed arrival and departure times, each station becomes an interval-coloring problem on identical platforms, which is solved exactly by greedy coloring on interval graphs and then validated.

## Sets

- `T`: trains.
- `R`: rolling-stock units.
- `C`: crews.
- `S`: stations ordered west to east.
- `G`: physical adjacent segments.
- `W_g`: disruption windows on segment `g`.
- `K_t`: feasible terminal actions for train `t`.
  - original destination `dest(t)` is always included,
  - each authorized turnback station before `dest(t)` is included if `allow_short_turn=1`,
  - a special action `cancel` is included if `allow_cancel=1`.
- `P_t = (s_1, ..., s_m)`: ordered station path of train `t` from origin to original destination.
- `A_t`: adjacent segment positions on `P_t`.
- `Conn`: protected passenger connections.
- `E_q`: throat events that use route-separation resource `q = (station, side)`.

## Parameters

- `H`: planning horizon.
- `pd_t`: planned departure time of train `t` at its origin.
- `dwell_t`: minimum dwell for train `t`.
- `run_a`: running time on adjacent segment position `a`.
- `head_g`: headway on physical segment `g`.
- `routegap_q`: minimum separation on throat resource `q`.
- `plat_s`: number of platforms at station `s`.
- `platgap_s`: minimum same-platform separation at station `s`.
- `dem_t`: passenger demand on train `t`.
- `prio_t`: priority weight of train `t`.
- `cap_r`: rolling-stock capacity of unit `r`.
- `turn_r`: rolling-stock turn time of unit `r`.
- `ready_r`, `loc_r`: initial ready time and initial station of unit `r`.
- `rest_c`, `ready_c`, `loc_c`, `duty_c`: crew rest, initial ready time, initial station, and single-train duty limit.
- `qual_{c,t}`: 1 if crew `c` is qualified for train `t`'s direction.
- `prefR_t`, `prefC_t`: preferred rolling stock and crew for train `t`.
- `tau_x`, `pass_x`: required transfer time and passengers for connection `x`.
- Objective weights from `config.json`:
  - `alpha_delay`
  - `alpha_cancel`
  - `alpha_short`
  - `alpha_conn`
  - `alpha_rs`
  - `alpha_crew`

Derived planned event times are computed from `pd_t`, public running times, and minimum dwell. For a feasible endpoint `k in K_t \ {cancel}`, `planArr_{t,k}` is the published arrival time at station `k`.

## Decision variables

### Disposition and path

- `f_{t,k} in {0,1}`: 1 if train `t` ends at station `k in K_t \ {cancel}`.
- `z_t in {0,1}`: 1 if train `t` is cancelled.
- `run_t in {0,1}`: 1 if train `t` runs to its original destination.
- `short_t in {0,1}`: 1 if train `t` short-turns.
- `act_t in {0,1}`: 1 if train `t` is operated (`run_t + short_t`).
- `visit_{t,s} in {0,1}`: 1 if train `t` visits station `s`.
- `cont_{t,s} in {0,1}`: 1 if train `t` continues beyond station `s`.
- `use_{t,g} in {0,1}`: 1 if train `t` traverses physical segment `g`.

### Event times

For every train `t` and station `s` on its original path:

- `arr_{t,s} >= 0`: arrival time at `s`.
- `dep_{t,s} >= 0`: departure time at `s`.

Also:

- `finish_t >= 0`: arrival time at the realized terminal station.
- `delay_t >= 0`: terminal delay relative to the published terminal time of the realized disposition.

### Disruption and sequencing

- `b_{t,w} in {0,1}`: for a train using a blocked segment/window, 1 if it traverses before window `w`, 0 if after it.
- `ordSeg_{t,u,g} in {0,1}`: ordering binary for trains `t,u` using the same physical segment `g` in opposite directions.
- `ordThr_{e,e'} in {0,1}`: ordering binary for two throat events on the same route-separation resource.
- `miss_x in {0,1}`: 1 if protected connection `x` is not held when both trains visit the transfer station.

### Rolling stock

- `x_{r,t} in {0,1}`: 1 if rolling-stock unit `r` operates train `t`.
- `startR_{r,t} in {0,1}`: 1 if `t` is the first train of unit `r`.
- `arcR_{r,i,j} in {0,1}`: 1 if unit `r` operates train `j` immediately after train `i`.

### Crew

- `y_{c,t} in {0,1}`: 1 if crew `c` operates train `t`.
- `startC_{c,t} in {0,1}`: 1 if `t` is the first train of crew `c`.
- `arcC_{c,i,j} in {0,1}`: 1 if crew `c` operates train `j` immediately after train `i`.

## Objective

Minimize

`alpha_delay * sum_t prio_t * delay_t`
`+ alpha_cancel * sum_t dem_t * z_t`
`+ alpha_short * sum_t dem_t * short_t`
`+ alpha_conn * sum_x pass_x * miss_x`
`+ alpha_rs * sum_t (act_t - x_{prefR_t,t})`
`+ alpha_crew * sum_t (act_t - y_{prefC_t,t})`

Assumption used in the implementation:

- delay is charged on the realized terminal arrival only,
- a short-turn penalty applies to the full train demand because no OD split is provided,
- a missed connection penalty is binary per protected connection when both trains serve the transfer station but the transfer time is not satisfied.

## Constraints

### 1. One disposition per train

For every train `t`:

- `sum_{k in K_t \ {cancel}} f_{t,k} + z_t = 1`
- `run_t = f_{t,dest(t)}`
- `short_t = sum_{k in K_t \ {cancel, dest(t)}} f_{t,k}`
- `act_t = run_t + short_t`

### 2. Visit and continuation logic

For every train `t` and station `s` on its path:

- `visit_{t,s} = sum_{k in K_t \ {cancel}: s is on path from origin(t) to k} f_{t,k}`
- `cont_{t,s} = sum_{k in K_t \ {cancel}: s is strictly before k on the path} f_{t,k}`

For every segment `g` on train `t`'s path:

- `use_{t,g} = sum_{k in K_t \ {cancel}: g is on path from origin(t) to k} f_{t,k}`

### 3. Event activation and local timing

For every train `t` and station `s` on its path:

- `0 <= arr_{t,s} <= H * visit_{t,s}`
- `0 <= dep_{t,s} <= H * visit_{t,s}`

At the origin `o(t)`:

- `arr_{t,o(t)} = dep_{t,o(t)}`
- `dep_{t,o(t)} >= pd_t * act_t`

At any non-origin station `s` on the path:

- `dep_{t,s} >= arr_{t,s}`
- `dep_{t,s} >= arr_{t,s} + dwell_t * cont_{t,s}`
- `dep_{t,s} <= arr_{t,s} + H * cont_{t,s}`

Hence:

- if the train continues beyond `s`, dwell is at least `dwell_t`,
- if `s` is the realized terminal station, then `dep_{t,s} = arr_{t,s}`.

### 4. Running-time propagation

For every adjacent path position `(u,v)` of train `t` with physical segment `g`:

- `arr_{t,v} >= dep_{t,u} + run_g - H * (1 - use_{t,g})`
- `arr_{t,v} <= dep_{t,u} + run_g + H * (1 - use_{t,g})`

Thus public running time is exact whenever the segment is used.

### 5. Realized terminal time and delay

For every feasible terminal station `k in K_t \ {cancel}`:

- `finish_t >= arr_{t,k} - H * (1 - f_{t,k})`
- `finish_t <= arr_{t,k} + H * (1 - f_{t,k})`

and

- `delay_t >= finish_t - sum_{k in K_t \ {cancel}} planArr_{t,k} * f_{t,k}`
- `delay_t <= H * act_t`

### 6. Disruption-window avoidance

For every train `t`, blocked segment/window `w` on segment `g`, if `g` can be used by `t`:

- `arrExit_{t,g} <= start_w + H * (1 - b_{t,w}) + H * (1 - use_{t,g})`
- `depEntry_{t,g} >= end_w - H * b_{t,w} - H * (1 - use_{t,g})`

where `depEntry_{t,g}` is the departure time from the segment's entry station in `t`'s direction and `arrExit_{t,g}` is the arrival time at its exit station.

If `use_{t,g}=1`, the train must traverse either fully before or fully after the blocked window.

### 7. Segment headway

For trains using the same physical segment in the same direction, order is fixed by the published order, so no binary is needed. For every ordered pair `(t,u)` with the same direction and both potentially using segment `g`:

- `entry_{u,g} >= entry_{t,g} + head_g - H * (2 - use_{t,g} - use_{u,g})`

For opposite-direction pairs on the same physical segment:

- `entry_{u,g} >= entry_{t,g} + head_g - H * (1 - ordSeg_{t,u,g}) - H * (2 - use_{t,g} - use_{u,g})`
- `entry_{t,g} >= entry_{u,g} + head_g - H * ordSeg_{t,u,g} - H * (2 - use_{t,g} - use_{u,g})`

### 8. Throat-route separation

Each relevant arrival or departure through a station side is a throat event `e` with event time `time_e` and activation binary `active_e`.

For every pair of throat events `e,e'` using the same resource `q`:

- `time_{e'} >= time_e + routegap_q - H * (1 - ordThr_{e,e'}) - H * (2 - active_e - active_{e'})`
- `time_e >= time_{e'} + routegap_q - H * ordThr_{e,e'} - H * (2 - active_e - active_{e'})`

### 9. Protected passenger connections

For each connection `x = (i, j, s)`:

- `miss_x <= visit_{i,s}`
- `miss_x <= visit_{j,s}`
- `dep_{j,s} >= arr_{i,s} + tau_x - H * miss_x - H * (2 - visit_{i,s} - visit_{j,s})`

If both trains serve station `s`, the transfer is either protected or the model pays a missed-connection penalty.

### 10. Rolling-stock assignment

Eligibility:

- `x_{r,t} = 0` if `cap_r < dem_t`.

Exactly one unit for every operated train:

- `sum_r x_{r,t} = act_t`

For each unit `r`:

- `sum_t startR_{r,t} <= 1`
- `startR_{r,t} + sum_i arcR_{r,i,t} = x_{r,t}` for all trains `t`
- `sum_j arcR_{r,t,j} <= x_{r,t}` for all trains `t`

Source feasibility:

- `startR_{r,t}` is only created if `loc_r = origin(t)`
- `dep_{t,origin(t)} >= ready_r - H * (1 - startR_{r,t})`

Arc feasibility from train `i` to train `j` for unit `r`:

- the arc exists only if train `i` can end at `origin(j)`,
- `arcR_{r,i,j} <= f_{i,origin(j)}`
- `dep_{j,origin(j)} >= arr_{i,origin(j)} + turn_r - H * (1 - arcR_{r,i,j})`

This forces each unit to follow one physically chainable path.

### 11. Crew assignment

Exactly one crew for every operated train:

- `sum_c y_{c,t} = act_t`

Qualification:

- `y_{c,t} = 0` if `qual_{c,t} = 0`

Path structure for each crew `c`:

- `sum_t startC_{c,t} <= 1`
- `startC_{c,t} + sum_i arcC_{c,i,t} = y_{c,t}` for all trains `t`
- `sum_j arcC_{c,t,j} <= y_{c,t}` for all trains `t`

Source feasibility:

- `startC_{c,t}` is only created if `loc_c = origin(t)`
- `dep_{t,origin(t)} >= ready_c - H * (1 - startC_{c,t})`

Arc feasibility from train `i` to train `j` for crew `c`:

- the arc exists only if train `i` can end at `origin(j)`,
- `arcC_{c,i,j} <= f_{i,origin(j)}`
- `dep_{j,origin(j)} >= arr_{i,origin(j)} + rest_c - H * (1 - arcC_{c,i,j})`

Single-train duty limit:

- `finish_t - dep_{t,origin(t)} <= duty_c + H * (1 - y_{c,t})`

### 12. Platform assignment subproblem after the timetable

For each fixed station `s`, let `I_s` be the set of operated train events at `s` with intervals `[arr_{t,s}, dep_{t,s}]`.

An exact platform assignment can be written as:

- `u_{t,s,p} in {0,1}`: event `(t,s)` uses platform `p = 1, ..., plat_s`
- `sum_p u_{t,s,p} = visit_{t,s}`

For any two events `(t,s)` and `(u,s)` and platform `p`:

- `arr_{u,s} >= dep_{t,s} + platgap_s - M * (1 - q_{t,u,s,p}) - M * (2 - u_{t,s,p} - u_{u,s,p})`
- `arr_{t,s} >= dep_{u,s} + platgap_s - M * q_{t,u,s,p} - M * (2 - u_{t,s,p} - u_{u,s,p})`

The implementation solves this stage exactly by greedy interval coloring because interval graphs are perfect, then validates the resulting platform numbers against the same inequalities.

## Output

The exported `solution.json` contains, for each train:

- `status` in `{run, short_turn, cancel}`,
- one assigned `rolling_stock_id` and `crew_id` if operated,
- the ordered station events from origin to realized terminal station,
- exact minute values with up to `1e-8` decimal precision,
- an empty `events` list if cancelled.
