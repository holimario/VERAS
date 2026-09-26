# Robust Microgrid Islanding Dispatch Model

## Why this formulation

This instance is small enough for an exact mixed-integer linear model, but the
structure matters more than raw size. The key coupling is:

- first-stage generator commitment shared by all scenarios,
- scenario-dependent dispatch and reserve recourse,
- hourly unit-commitment logic,
- storage and EV state transitions,
- demand-response rebound across hours,
- islanding-only stability rules,
- expected-cost plus tail-risk objective.

Because of that, the strongest practical representation here is a two-stage
scenario MILP rather than a heuristic, time-indexed decomposition, or set
partitioning reformulation.

## Sets and indices

- `G`: dispatchable generators.
- `B`: stationary batteries.
- `E`: EV clusters.
- `R = {pv, wind}`: renewable assets.
- `S`: public scenarios.
- `T = {0, ..., 23}`: hourly periods.
- `D`: demand-response eligible hours from `demand_response.csv`.

All hours are treated as 1-hour intervals, so kW and kWh use the same numeric
multiplier in hourly balance and cost terms.

## Main parameters

From the data files:

- base load, critical load, and import tariff by hour,
- generator min/max output, ramp limits, startup cost, no-load cost, marginal
  cost, emissions, minimum up/down time, and inertia score,
- battery min/max/initial state of charge, charge and discharge limits,
  efficiencies, cycling cost, grid-forming flag, and inertia coefficient,
- EV arrival/departure window, SOC limits, target SOC, power limits,
  efficiencies, degradation cost, and V2G flag,
- PV and wind hourly forecast,
- base grid import limit,
- DR curtailment limit, rebound hour, rebound fraction, and discomfort cost,
- scenario probability and hourly stress multipliers.

## Decision variables

### First-stage variables

These are shared across all scenarios:

- `u[g,t] in {0,1}`: generator commitment.
- `su[g,t] in {0,1}`: startup indicator.
- `sd[g,t] in {0,1}`: shutdown indicator.

### Scenario recourse variables

For each scenario `s` and hour `t`:

- `pg[s,g,t] >= 0`: generator dispatch.
- `rg[s,g,t] >= 0`: generator upward reserve.
- `ch_b[s,b,t], dis_b[s,b,t] >= 0`: battery charge/discharge.
- `soc_b[s,b,t]`: battery end-of-hour SOC.
- `rb[s,b,t] >= 0`: battery reserve contribution.
- `z_b[s,b,t] in {0,1}`: battery mode binary, used to forbid simultaneous
  charge and discharge.
- `ch_e[s,e,t], dis_e[s,e,t] >= 0`: EV charge/discharge.
- `soc_e[s,e,t]`: EV end-of-hour SOC.
- `re[s,e,t] >= 0`: EV reserve contribution.
- `z_e[s,e,t] in {0,1}`: EV mode binary.
- `grid[s,t] >= 0`: grid import.
- `pren[s,r,t] >= 0`: renewable power used.
- `curt[s,r,t] >= 0`: renewable curtailment.
- `dr[s,t] >= 0`: flexible-load curtailment.
- `y_dr[s,t] in {0,1}`: DR activation binary for consecutive-hour logic.
- `shed[s,t] >= 0`: emergency critical-load shedding.
- `cost_s[s]`: total operating cost in scenario `s`.

### Tail-risk variables

- `eta`: VaR-like threshold for scenario daily cost.
- `xi[s] >= 0`: excess above `eta` for scenario `s`.

## Objective

Minimize

`expected_cost + tail_risk_weight * CVaR_alpha(cost_s)`

with

- `expected_cost = sum_s prob[s] * cost_s[s]`
- `CVaR_alpha = eta + (1 / (1 - alpha)) * sum_s prob[s] * xi[s]`
- `xi[s] >= cost_s[s] - eta`

This matches the statement that the operator minimizes expected operating cost
 plus a tail-risk surcharge on expensive scenarios.

## Constraints

### 1. Commitment transition and minimum up/down rules

For each generator and hour:

- `u[g,t] - u[g,t-1] = su[g,t] - sd[g,t]` with the given initial commitment used
  as `u[g,-1]`.
- minimum up-time and minimum down-time are enforced with the standard summed
  startup/shutdown inequalities over rolling windows.

Assumption: the public data gives the initial on/off state but not residual time
already spent on or off before hour 0, so the horizon starts from the provided
state without extra carry-in minimum up/down obligations.

### 2. Generator operating limits

For every scenario, generator, and hour:

- `min_kw[g] * u[g,t] <= pg[s,g,t] <= max_kw[g] * u[g,t]`
- upward reserve is limited by both headroom and ramp-up capability:
  - `rg[s,g,t] <= max_kw[g] * u[g,t] - pg[s,g,t]`
  - `rg[s,g,t] <= ramp_up_kw[g]`
- dispatch follows scenario-wise ramp limits between consecutive hours.

For hour 0, dispatch is ramped from a conservative previous output equal to
`min_kw[g]` if the initial commitment is on, otherwise 0.

### 3. Battery charge/discharge and SOC dynamics

For each scenario, battery, and hour:

- charge/discharge mutual exclusivity:
  - `ch_b <= max_charge * z_b`
  - `dis_b <= max_discharge * (1 - z_b)`
- SOC recursion:
  - `soc_b[t] = soc_b[t-1] + eta_c * ch_b[t] - dis_b[t] / eta_d`
  - the initial SOC uses `initial_soc_kwh`
- SOC bounds:
  - `min_soc <= soc_b[t] <= max_soc`
- terminal condition:
  - `soc_b[23] >= initial_soc`

Battery reserve is modeled as extra upward net injection available above the
scheduled operating point, with both power and one-hour energy limits:

- `dis_b + rb <= max_discharge + ch_b`
- `dis_b + rb <= eta_d * (start_soc_b - min_soc) + ch_b`

where `start_soc_b` is the battery SOC at the start of hour `t` (initial SOC at
`t=0`, previous end-of-hour SOC otherwise).

This lets a charging battery provide reserve by backing off charge, and lets an
idle/discharging battery provide additional discharge if energy is available.

### 4. EV charge/discharge and SOC dynamics

The EV formulation mirrors the battery formulation, but only during connected
hours and with scenario-specific availability derating.

Assumption: an EV cluster is connected on hours
`arrival_hour <= t < departure_hour`, and the target SOC must hold at the end
of hour `departure_hour - 1`. Outside that window, charge, discharge, and
reserve are forced to zero and SOC stays constant.

For connected hours:

- `ch_e <= availability[s,t] * max_charge * z_e`
- `dis_e <= availability[s,t] * max_discharge * (1 - z_e)`
- SOC recursion uses the EV efficiencies and start-of-hour SOC
- `min_soc <= soc_e[t] <= max_soc`
- `soc_e[departure_hour - 1] >= target_soc`

EV reserve uses the same net-injection logic as storage:

- `dis_e + re <= availability * max_discharge + ch_e`
- `dis_e + re <= eta_d * (start_soc_e - min_soc) + ch_e`

### 5. Renewable availability and curtailment

For each scenario, renewable type, and hour:

- `pren[s,r,t] + curt[s,r,t] = available_renewable[s,r,t]`
- both terms are nonnegative.

Available renewable energy is the hourly forecast multiplied by the
scenario-specific PV or wind multiplier.

### 6. Grid imports

For each scenario and hour:

- `0 <= grid[s,t] <= base_grid_cap[t] * grid_import_multiplier[s,t]`
- during islanded hours this upper bound is zero.

No grid export variable is included because the submission schema and prompt do
not allow export.

### 7. Demand response and rebound

For each scenario and eligible hour:

- `0 <= dr[s,t] <= max_curtail_kw[t]`
- `dr[s,t] <= max_curtail_kw[t] * y_dr[s,t]`
- in every rolling 3-hour window, `sum y_dr <= 2`, implementing the
  max-consecutive-DR-hours limit of 2
- `sum_t dr[s,t] <= max_daily_dr_kwh`

Rebound is added back to the load in the specified rebound hour:

- `rebound[s,h] = sum_{t in D: rebound_hour[t] = h} rebound_fraction[t] * dr[s,t]`

### 8. Power balance

For every scenario and hour:

`sum_g pg + grid + sum_r pren + sum_b dis_b + sum_e dis_e + dr + shed`
`= stressed_load + rebound + sum_b ch_b + sum_e ch_e`

where `stressed_load = base_load[t] * load_multiplier[s,t]`.

This exactly keeps curtailment, charging, and emergency shedding on the correct
sides of the balance.

### 9. Reserve requirements

For every scenario and hour:

- `sum_g rg + sum_b rb + sum_e re >= reserve_requirement[s,t]`

Additional islanding constraints:

- synchronous generator reserve floor:
  - `sum_g rg >= island_min_synchronous_reserve_kw`
- grid-forming battery reserve floor:
  - `sum_{b grid_forming} rb >= grid_forming_storage_reserve_fraction * reserve_requirement[s,t]`
- inertia/stability floor:
  - `sum_g inertia_score[g] * u[g,t] + sum_{b grid_forming} inertia_coeff[b] * soc_b[s,b,t] / 100 >= island_min_inertia_score`

The battery reserve fraction assumption follows the parameter name
`grid_forming_storage_reserve_fraction` and is a conservative interpretation of
the requirement that islanded reserve support must come from grid-forming
batteries as well as synchronous machines.

### 10. Emergency shedding cap

For each scenario and hour:

- `0 <= shed[s,t] <= max_critical_shed_kw[s,t]`

The penalty is set high enough that the optimizer only uses shedding when the
public data makes it necessary.

### 11. Daily emissions cap

For each scenario:

- `sum_{g,t} emission_rate[g] * pg[s,g,t] <= scenario_emissions_cap_kg`

### 12. Scenario operating cost definition

For each scenario:

- generator startup cost: `sum_{g,t} startup_cost[g] * su[g,t]`
- no-load cost: `sum_{g,t} no_load_cost[g] * u[g,t]`
- fuel cost: `sum_{g,t} marginal_cost[g] * pg[s,g,t]`
- grid import cost: `sum_t tariff[t] * grid[s,t]`
- storage cycling cost: `sum_{b,t} cycle_cost[b] * (ch_b + dis_b)`
- EV degradation cost: `sum_{e,t} cycle_cost[e] * (ch_e + dis_e)`
- DR discomfort cost: `sum_t discomfort_cost[t] * dr[s,t]`
- renewable curtailment cost: `sum_{r,t} curtailment_cost * curt[s,r,t]`
- emergency shedding penalty: `sum_t load_shed_penalty * shed[s,t]`

`cost_s[s]` is constrained to equal the sum of these terms.

## Output mapping

The final `solution.csv` exports one row for every required
`(scenario, hour, asset)` combination:

- `generator`: `commit`, `power_kw`, `reserve_kw`
- `storage`: `charge_kw`, `discharge_kw`, `soc_kwh`, `reserve_kw`
- `ev_cluster`: `charge_kw`, `discharge_kw`, `soc_kwh`, `reserve_kw`
- `grid`: `power_kw`
- `renewable` (`pv`, `wind`): `power_kw`, `curtail_kw`
- `demand_response` (`flex_load`): `curtail_kw`
- `load_shed` (`critical_load`): `shed_kw`

Unused numeric columns are exported as zero, and non-generator `commit` is left
blank.
