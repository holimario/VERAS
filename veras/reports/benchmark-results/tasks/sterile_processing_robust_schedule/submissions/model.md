# Sterile Processing Robust Schedule Model

## Modeling Choice

The instance is a two-stage robust scheduling problem:

- First stage: choose one operating-room schedule that is shared by all scenarios.
- Second stage: for each public scenario, build a feasible sterile-processing and cart-delivery recourse plan for that fixed schedule.

An exact monolithic formulation would couple room sequencing, tray circulation, three-stage SPD processing, loaners, emergency cycles, and scenario-specific elevator windows. That is possible in principle, but it is much larger than needed for this public instance. The solver therefore uses:

1. a strong time-indexed master MIP for case selection, room assignment, and case start time;
2. conservative scenario-specific tray-prefix constraints in the master so the room schedule does not create obviously impossible early tray demand;
3. exact per-scenario recourse construction for carts and tray sourcing, with independent validation.

## Sets

- `C`: cases.
- `R`: operating rooms.
- `G = {0, 5, 10, ..., H}`: 5-minute start grid.
- `S`: scenarios.
- `T`: tray types.
- `V_t`: vendors that can provide tray type `t`.
- `W`: washers.
- `A`: assembly stations.
- `Z`: sterilizers.
- `E_s`: elevator windows in scenario `s`.
- `K_c`: tray types required by case `c`.

## Main Parameters

- `rel_c`, `tar_c`, `dur_c`, `turn_c`: release, target start, duration, turnover for case `c`.
- `cancel_c`, `delay_c`: cancellation penalty and delay penalty per minute.
- `mand_c`, `robot_c`, `implant_c`: mandatory, robotic, implant indicators.
- `open_r`, `close_r`: room open and close times.
- `buf_r`: room turnover buffer.
- `otmax_room`: maximum room overtime.
- `q_ct`: quantity of tray type `t` required by case `c`.
- `route_r`: unique delivery route for room `r`.
- `travel_r`, `settle_r`, `setup_r`, `lead_r`, `cartcap_r`: route travel, room-settle, setup buffer, max staging lead, and tray-cart capacity.
- `prob_s`: scenario probability.
- `mw_s`, `ma_s`, `mz_s`: wash, assembly, and sterilizer multipliers.
- `initmult_s`: initial sterile multiplier.
- `loanerdelay_s`: scenario delay applied to loaner arrivals.
- `emax_s`: scenario-wide emergency-cycle limit.
- `staff_s`: staff-capacity factor.
- `initster_t`, `initdirty_t`: initial sterile and dirty sets.
- `loanmax_tv`, `loanarr_tv`, `loancost_tv`, `checkin_tv`: vendor quantity, arrival, direct cost, and check-in minutes.
- `weight_t`, `cycle_t`, `impl_t`, `robotpack_t`, `emer_t`: tray weight, sterilizer cycle type, implant sensitivity, robotic packing eligibility, emergency eligibility.
- `skill_t`: required assembly skill group for tray type `t`.
- `pcase_spec`: maximum emergency cases by specialty.
- `lateTrayPenalty`, `roomOTWeight`, `spdOTWeight`, `emPenalty`, `implEmPenalty`, `loanerPenalty`, `tailWeight`, `cartPenalty`, `cartDwellPenalty`: objective weights from `config.json`.

Derived parameters used in the master:

- `elig_{c,r} in {0,1}`: room `r` is compatible with case `c`.
- `occ_{c,r} = dur_c + max(turn_c, buf_r)`: conservative occupied time for case `c` in room `r`.
- `depLatest_{c,r,g} = g - ceil(travel_r + settle_r + setup_r)`: latest tray-ready / cart-departure minute if case `c` starts in room `r` at `g`.
- `end_{c,g} = g + dur_c`: surgery end minute.
- `proc_{t,s}`: conservative regular reprocessing time for one set of tray type `t` in scenario `s`, equal to the shortest compatible washer duration plus the shortest compatible assembly duration plus the shortest compatible sterilizer-plus-postprocessing duration.
- `InitSter_{t,s} = floor(initster_t * initmult_s)`.
- `LoanAvail_{t,s,τ}`: total vendor quantity for tray `t` arriving by minute `τ` under scenario `s`.
- `InitDirtyReady_{t,s,τ} = initdirty_t` if `τ >= proc_{t,s}`, else `0`.

## First-Stage Decision Variables

- `z_{c,r,g} in {0,1}`: case `c` starts in room `r` at time `g`.
- `y_c in {0,1}`: case `c` is scheduled.
- `ot_r >= 0`: overtime used in room `r`.

## Master Objective

The master minimizes first-stage room cost plus cancellation and delay cost:

```text
min
sum_c cancel_c (1 - y_c)
+ sum_{c,r,g} delay_c * max(0, g - tar_c) * z_{c,r,g}
+ roomOTWeight * sum_r ot_r
```

The expected scenario recourse cost and tail-risk term are evaluated after the master solve by the scenario recourse builder and are used for repair and local improvement. The master itself carries the dominant hard feasibility structure.

## Master Constraints

### 1. One schedule decision per case

```text
sum_{r,g} z_{c,r,g} = y_c                      for all c in C
```

Only compatible `(r,g)` pairs are created:

- `elig_{c,r} = 1`;
- `g >= max(rel_c, open_r)`;
- `g + dur_c <= close_r + otmax_room`.

### 2. No room overlap

For each room `r` and each 5-minute time point `τ`,

```text
sum_{c,g : τ in [g, g + occ_{c,r})} z_{c,r,g} <= 1
```

This is a conservative time-indexed non-overlap model that embeds turnover.

### 3. Room overtime

For every candidate `(c,r,g)`,

```text
ot_r >= g + occ_{c,r} - close_r - otmax_room * (1 - z_{c,r,g})
```

with

```text
0 <= ot_r <= otmax_room
```

### 4. Scenario-specific tray-prefix feasibility cuts

For each tray type `t`, scenario `s`, and audit minute `τ`, cumulative tray demand that must be ready by `τ` cannot exceed the initial sterile supply, loaners already arrived by `τ`, initial dirty trays that could have been reprocessed by `τ`, and trays from earlier scheduled cases whose dirty sets could have completed regular reprocessing by `τ`:

```text
sum_{c,r,g : depLatest_{c,r,g} <= τ} q_ct z_{c,r,g}
<= InitSter_{t,s}
 + LoanAvail_{t,s,τ}
 + InitDirtyReady_{t,s,τ}
 + sum_{c,r,g : end_{c,g} + proc_{t,s} <= τ} q_ct z_{c,r,g}
```

for all `(t,s,τ)` with at least one case requiring tray type `t`.

These are necessary conditions, not a full SPD resource model, but they are strong enough to prevent schedules that ask for more early tray sets than the public scenario data can plausibly supply.

## Second-Stage Scenario Recourse

After the common room schedule is fixed, each scenario `s` solves the following recourse tasks.

### 2A. Cart-window assignment

For each scheduled case `c`, let `r(c)` be its assigned room and `Q_c = sum_t q_ct` the total tray sets carried on the case cart.

Feasible departure interval:

```text
earliestDep_{c,s} = start_c - lead_{r(c)}
latestDep_c       = start_c - ceil(travel_{r(c)} + settle_{r(c)} + setup_{r(c)})
```

Binary variables:

- `x_{c,e} in {0,1}`: case `c` uses elevator window `e in E_s` that belongs to its route elevator and intersects `[earliestDep_{c,s}, latestDep_c]`.

Constraints:

```text
sum_{e eligible for c} x_{c,e} = 1                           for all scheduled c
sum_{c} x_{c,e} <= maxTrips_e                                for all e in E_s
sum_{c} Q_c x_{c,e} <= maxTraySets_e                         for all e in E_s
```

The realized departure time is the latest minute inside the chosen window and the feasible interval:

```text
dep_{c,s} = min(latestDep_c, windowEnd_e)    where x_{c,e} = 1
```

This minimizes avoidable early cart dwell.

### 2B. Tray sourcing and regular reprocessing

For each scenario `s`, case `c`, and required tray type `t`, the recourse must assign exactly `q_ct` sets from:

- initial sterile inventory,
- regular reprocessed inventory,
- loaners,
- emergency cycles.

Conceptually, for each demand unit `u` of tray type `t` for case `c`, let:

- `a_u^init, a_u^repr, a_u^loan, a_u^emer in {0,1}` indicate its source,
- with

```text
a_u^init + a_u^repr + a_u^loan + a_u^emer = 1
```

Regular reprocessing jobs are unit jobs on a 3-stage flow:

- washer stage on a compatible washer,
- assembly stage on a compatible assembly station,
- sterilizer stage on a compatible sterilizer.

For each regular job `j` of tray type `t`, sourced either from `INIT` dirty stock or a previously completed case `src(j)`, variables are:

- `u_{j,w} in {0,1}`: washer choice,
- `v_{j,a} in {0,1}`: assembly-station choice,
- `h_{j,z} in {0,1}`: sterilizer choice,
- `sw_j, sa_j, sz_j >= 0`: stage start times,
- `fin_j >= 0`: ready time after sterilization, drying, inspection, and quarantine.

Compatibility:

```text
sum_w u_{j,w} = 1,  sum_a v_{j,a} = 1,  sum_z h_{j,z} = 1
```

Timing:

```text
sw_j >= dirtyAvail_{src(j)}
sa_j >= sw_j + washDur_{t,s,w}             when u_{j,w}=1
sz_j >= sa_j + asmDur_{t,s,a}              when v_{j,a}=1
fin_j = sz_j + sterDur_{t,s,z}             when h_{j,z}=1
fin_j <= dep_{c,s}                         if job j feeds case c
```

Washer, assembly, and sterilizer capacities are enforced by resource event sweeps in the implemented constructor:

- at most one overlapping washer job per washer because each unit job is below the machine weight capacity;
- at most `parallelTechs_a` simultaneous assembly jobs on station `a`;
- heavy-tray count on station `a` does not exceed `heavyLimit_a`;
- at most one overlapping sterilizer job per sterilizer because each unit job is below the machine weight capacity.

The public instance is small enough that unit-job scheduling is tractable and keeps the exported batch rows unambiguous.

### 2C. Loaner limits

For each tray type `t`, vendor `v`, and scenario `s`,

```text
sum_{u using vendor v for tray t} a_u^loan <= loanmax_tv
```

and loaner use is allowed only if

```text
loanarr_tv + loanerdelay_s <= dep_{c,s}
```

### 2D. Emergency limits

For each scenario `s`,

```text
sum_u a_u^emer <= emax_s
```

and for each specialty `p`,

```text
sum_{c in specialty p} emergencyCase_{c,s} <= maxEmergencyCases_p
```

where `emergencyCase_{c,s}` is activated if any demand unit of case `c` in scenario `s` uses emergency supply.

### 2E. Demand balance

For every scheduled case `c`, tray type `t`, and scenario `s`,

```text
initial_{c,t,s} + reprocessed_{c,t,s} + loaner_{c,t,s} + emergency_{c,t,s} = q_ct
```

### 2F. Objective evaluated in recourse

For a fixed room schedule, scenario cost is

```text
RecourseCost_s =
  sum_{scheduled c} delay_c * max(0, start_c - tar_c)
+ roomOTWeight * sum_r overtime_r
+ spdOTWeight * SPDOvertime_s
+ sum_{loaner units} (loancost_tv + loanerPenalty * checkin_tv)
+ emPenalty * (# emergency units)
+ implEmPenalty * (# implant-sensitive emergency units)
+ cartDwellPenalty * sum_c Q_c * (latestDep_c - dep_{c,s})
+ cartPenalty * (# missing carts, forbidden in the final solution)
```

The solver reports expected recourse cost plus the prompt’s tail-risk proxy:

```text
Expected = sum_s prob_s * RecourseCost_s
Tail = tailWeight * max_s RecourseCost_s
```

## What The Implemented Solver Solves Exactly

- The common case schedule is solved exactly by the master MIP.
- Cart-window assignment is solved exactly per scenario by a small MIP.
- Tray-source construction uses exact accounting and exact public compatibility rules, then schedules regular reprocessing jobs greedily at the earliest feasible times on compatible SPD resources.
- The exported solution is accepted only if an independent validator confirms schema, room feasibility, tray balance, cart-window capacity, source timing, and SPD resource feasibility on the artifact itself.
