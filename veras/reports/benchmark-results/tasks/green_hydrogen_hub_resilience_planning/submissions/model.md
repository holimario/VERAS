# Green Hydrogen Hub Resilience Planning Model

## Formulation choice

This instance is best modeled as a two-stage stochastic mixed-integer program in
extensive form.

- First-stage variables choose the common infrastructure posture for each site:
  whether the site is active, how many electrolyzer modules are installed, how
  many tank modules are installed, and whether polishing capability is enabled.
- Second-stage variables are scenario-specific hourly operating decisions:
  online modules, startups, production, energy use, polishing, inventory,
  deliveries, trips, venting, and shortfalls.

This is stronger and simpler than a decomposition-first approach for the public
instance because the instance is small:

- 4 production sites
- 6 customers
- 24 site-customer lanes
- 6 scenarios
- 12 hours

The only cross-scenario coupling is in the first-stage infrastructure, so the
extensive form remains tractable while preserving exact nonanticipativity.

## Sets and indices

- Sites `s in S`
- Customers `c in C`
- Delivery lanes `l in L`
- Scenarios `w in W`
- Hours `t in T = {0, ..., 11}`

Derived subsets:

- `L(s)` = lanes whose origin is site `s`
- `L(c)` = lanes whose customer is `c`
- `C_hi` = customers requiring polished hydrogen
- `C_lo` = customers that can use unpolished hydrogen

## Public-data assumptions fixed for the model

The brief leaves a few items implicit. I use the least risky interpretation that
matches the supplied fields:

1. Customers with `purity_required_pct >= 99.9` are treated as high-purity and
   must be served from polished hydrogen. The only public customer below that
   threshold is `STEEL_PILOT` at `99.7`, so it may be served by unpolished
   hydrogen.
2. Initial inventory is treated as unpolished hydrogen because the public data
   does not provide any initial polished-stock split.
3. `water_limit_multiplier` scales the site's daily water permit from
   `sites.csv`, because the base permit is given in `m3/day`.
4. `travel_time_multiplier` scales driver-hours consumption but not trip cost,
   because the brief says scenario lane rows tighten movement and travel burden;
   it does not say trip cost itself changes.
5. A site with `active = 0` cannot operate, deliver, or polish. Its inventory
   may remain idle, but no operating variables can be positive.
6. Tail risk is modeled as a CVaR surcharge at confidence level
   `alpha = 0.8`, weighted by `tail_risk_weight = 0.35`.

## Parameters

From `sites.csv`:

- `M_s^max` = max electrolyzer modules
- `cap_s` = kg/h per module
- `rho_s^min` = minimum load fraction
- `fmod_s` = daily fixed cost per selected electrolyzer module
- `vprod_s` = variable production cost per kg
- `B_s` = base storage kg
- `K_s` = storage kg per tank module
- `N_s^max` = max tank modules
- `ftank_s` = daily fixed cost per selected tank module
- `I_s^0` = initial inventory kg
- `I_s^F` = minimum final inventory kg
- `W_s` = daily water limit m3/day
- `U_s` = startup limit over the day
- `vpol_s` = polishing cost per kg
- `fpol_s` = polishing fixed daily cost

From `customers.csv`:

- `floor_c` = minimum daily service fraction
- `hcap_c` = maximum hourly shortfall fraction
- `vshort_c` = shortfall penalty per kg
- `prio_c` = priority weight

From `lanes.csv`:

- `orig_l` = origin site
- `cust_l` = destination customer
- `Q_l` = kg per trip
- `Trips_l^base` = normal hourly trip cap
- `dh_l` = driver-hours per trip
- `ctrip_l` = cost per trip

From `scenarios.csv`:

- `p_w` = scenario probability
- `multprice_w` = grid-price multiplier
- `Ecap_w` = emissions cap
- `Dcap_w` = hourly driver-hours cap

From `hourly_forecast.csv`:

- `R_{s,t}` = base renewable MWh
- `gprice_{s,t}` = base grid price per MWh
- `ge_{s,t}` = grid emissions kg per MWh

From `scenario_site_hourly.csv`:

- `rmult_{w,s,t}` = renewable multiplier
- `cmult_{w,s,t}` = module capacity multiplier
- `wmult_{w,s,t}` = water multiplier
- `Gcap_{w,s,t}` = grid MWh cap

From `scenario_customer_hourly_demand.csv`:

- `dem_{w,c,t}` = realized hourly demand

From `scenario_lane_hourly.csv`:

- `Trips_{w,l,t}` = scenario hourly lane trip cap
- `tmult_{w,l,t}` = driver-hours multiplier
- `closed_{w,l,t}` = closure flag

Global constants from `config.json`:

- `e_prod` = electrolysis MWh per kg H2
- `e_pol` = polishing MWh per kg
- `w_prod` = water m3 per kg H2
- `c_water` = water cost per m3
- `c_start` = startup cost per module start
- `c_emit` = emissions cost per kg
- `c_vent` = vent penalty per kg
- `Reserve` = minimum network reserve at the end of every hour
- `alpha` = tail-risk confidence level
- `lambda` = tail-risk weight

Useful derived constants:

- `StoreCap_s(y) = B_s + K_s * n_s`
- `Cap_{w,s,t}(m) = cap_s * cmult_{w,s,t} * m`
- `RenCap_{w,s,t} = R_{s,t} * rmult_{w,s,t}`
- Effective lane closure bound:
  `Trips_{w,l,t} = 0` whenever `closed_{w,l,t} = 1`

## Decision variables

### First stage

For each site `s`:

- `a_s in {0,1}`: site active flag
- `m_s in Z_+`: selected electrolyzer modules
- `n_s in Z_+`: selected tank modules
- `p_s in {0,1}`: polishing active flag

### Second stage, for each scenario `w`, hour `t`, site `s`

- `on_{w,s,t} in Z_+`: online modules
- `start_{w,s,t} in Z_+`: module starts
- `prod_{w,s,t} >= 0`: produced hydrogen kg
- `ren_{w,s,t} >= 0`: renewable MWh used
- `grid_{w,s,t} >= 0`: grid MWh used
- `pol_{w,s,t} >= 0`: hydrogen polished kg
- `uinv_{w,s,t} >= 0`: unpolished inventory at end of hour
- `pinv_{w,s,t} >= 0`: polished inventory at end of hour
- `vent_u_{w,s,t} >= 0`, `vent_p_{w,s,t} >= 0`: vented kg by purity class

For each scenario `w`, hour `t`, lane `l`

- `trip_{w,l,t} in Z_+`: trips
- `del_{w,l,t} >= 0`: delivered kg

For each scenario `w`, hour `t`, customer `c`

- `short_{w,c,t} >= 0`: shortfall kg

Tail-risk variables:

- `eta` free: VaR anchor
- `xi_w >= 0`: positive excess of scenario operating cost above `eta`

## Objective

Minimize

`InfraCost + ExpectedOpCost + lambda * CVaRalpha(OpCost_w)`

where

- `InfraCost =
  sum_s (fmod_s * m_s + ftank_s * n_s + fpol_s * p_s)`

- `OpCost_w =
  sum_{s,t} [vprod_s * prod_{w,s,t}
           + c_water * w_prod * prod_{w,s,t}
           + c_start * start_{w,s,t}
           + vpol_s * pol_{w,s,t}
           + c_vent * (vent_u_{w,s,t} + vent_p_{w,s,t})
           + multprice_w * gprice_{s,t} * grid_{w,s,t}
           + c_emit * ge_{s,t} * grid_{w,s,t}]
  + sum_{l,t} ctrip_l * trip_{w,l,t}
  + sum_{c,t} (vshort_c * prio_c) * short_{w,c,t}`

- `ExpectedOpCost = sum_w p_w * OpCost_w`

- `CVaRalpha(OpCost_w) = eta + (1 / (1 - alpha)) * sum_w p_w * xi_w`

with

- `xi_w >= OpCost_w - eta`
- `xi_w >= 0`

## Constraints

### 1. Infrastructure selection

For all sites `s`:

- `0 <= m_s <= M_s^max * a_s`
- `0 <= n_s <= N_s^max * a_s`
- `0 <= p_s <= a_s`

These are the nonanticipative first-stage decisions and are shared by all
scenarios.

### 2. Online modules and starts

For all `w,s,t`:

- `0 <= on_{w,s,t} <= m_s`
- `start_{w,s,0} >= on_{w,s,0}`
- `start_{w,s,t} >= on_{w,s,t} - on_{w,s,t-1}` for `t >= 1`
- `sum_t start_{w,s,t} <= U_s`

Because starts have positive cost and a hard daily cap, the model will set them
to the true positive changes in online count.

### 3. Production bounds

For all `w,s,t`:

- `prod_{w,s,t} <= cap_s * cmult_{w,s,t} * on_{w,s,t}`
- `prod_{w,s,t} >= rho_s^min * cap_s * cmult_{w,s,t} * on_{w,s,t}`

This enforces the minimum stable load whenever online modules are positive.

### 4. Energy balance and limits

For all `w,s,t`:

- `e_prod * prod_{w,s,t} + e_pol * pol_{w,s,t} <= ren_{w,s,t} + grid_{w,s,t}`
- `0 <= ren_{w,s,t} <= RenCap_{w,s,t}`
- `0 <= grid_{w,s,t} <= Gcap_{w,s,t}`

### 5. Water permits

For all `w,s`:

- `sum_t w_prod * prod_{w,s,t} <= W_s * wmult_{w,s,0}`

The multiplier is constant across hours within each scenario-site in the public
instance, so using any hour index is equivalent.

### 6. Purity-tracked inventory balance

Let `LowDel_{w,s,t} = sum_{l in L(s): cust_l in C_lo} del_{w,l,t}` and
`HighDel_{w,s,t} = sum_{l in L(s): cust_l in C_hi} del_{w,l,t}`.

For all `w,s,t = 0`:

- `uinv_{w,s,0} = I_s^0 + prod_{w,s,0} - pol_{w,s,0} - LowDel_{w,s,0} - vent_u_{w,s,0}`
- `pinv_{w,s,0} = pol_{w,s,0} - HighDel_{w,s,0} - vent_p_{w,s,0}`

For all `w,s,t >= 1`:

- `uinv_{w,s,t} = uinv_{w,s,t-1} + prod_{w,s,t} - pol_{w,s,t} - LowDel_{w,s,t} - vent_u_{w,s,t}`
- `pinv_{w,s,t} = pinv_{w,s,t-1} + pol_{w,s,t} - HighDel_{w,s,t} - vent_p_{w,s,t}`

Storage capacity and final-state rules for all `w,s,t`:

- `uinv_{w,s,t} + pinv_{w,s,t} <= B_s + K_s * n_s`
- `uinv_{w,s,11} + pinv_{w,s,11} >= I_s^F`

Polishing can only occur if polishing is activated:

- `pol_{w,s,t} <= Mpol_s * p_s`

where a safe tight choice is
`Mpol_s = I_s^0 + 12 * cap_s * M_s^max`.

### 7. Network reserve

For all `w,t`:

- `sum_s (uinv_{w,s,t} + pinv_{w,s,t}) >= Reserve`

### 8. Delivery feasibility

For all `w,l,t`:

- `0 <= trip_{w,l,t} <= Trips_{w,l,t}`
- `del_{w,l,t} <= Q_l * trip_{w,l,t}`

Because `Trips_{w,l,t}` already incorporates hazmat-window closures and
scenario closures in the provided data, this single bound is sufficient.

Hourly driver-hours budget for each `w,t`:

- `sum_l dh_l * tmult_{w,l,t} * trip_{w,l,t} <= Dcap_w`

### 9. Demand satisfaction and shortfall controls

For all `w,c,t`:

- `sum_{l in L(c)} del_{w,l,t} + short_{w,c,t} = dem_{w,c,t}`
- `short_{w,c,t} <= hcap_c * dem_{w,c,t}`

Daily service-floor constraints for all `w,c`:

- `sum_t short_{w,c,t} <= (1 - floor_c) * sum_t dem_{w,c,t}`

### 10. Scenario emissions cap

For all `w`:

- `sum_{s,t} ge_{s,t} * grid_{w,s,t} <= Ecap_w`

## Export mapping

The exported JSON fields are taken from the solved variables as follows:

- `infrastructure.active = (a_s = 1)`
- `infrastructure.electrolyzer_modules = m_s`
- `infrastructure.tank_modules = n_s`
- `infrastructure.polishing_active = (p_s = 1)`
- `operations.storage_end_kg = uinv_{w,s,t} + pinv_{w,s,t}`
- `operations.vent_kg = vent_u_{w,s,t} + vent_p_{w,s,t}`
- `operations.water_m3 = w_prod * prod_{w,s,t}`
- `deliveries.kg = del_{w,l,t}`
- `deliveries.trips = trip_{w,l,t}`
- `shortfalls.kg = short_{w,c,t}`

All numeric exports are nonnegative, integer fields are emitted as integers, and
continuous values are rounded only to 8 decimal places for stable JSON output.

## Why this formulation is strong for the instance

- Nonanticipativity is exact because only infrastructure variables are shared.
- Online-module counts avoid extra on/off binaries while still enforcing integer
  production blocks and startup counting.
- Purity-tracked inventory prevents illegal use of unpolished hydrogen for
  high-purity customers without introducing lane-site purity flow binaries.
- Delivery integrality is placed only on trip counts; delivered kg remains
  continuous up to payload, which matches the schema and strengthens the model
  versus binary trip-use formulations.
- The CVaR linearization captures the stated tail-risk surcharge without adding
  nonlinear terms.
