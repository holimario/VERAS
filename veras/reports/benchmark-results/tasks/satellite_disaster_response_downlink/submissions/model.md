# Satellite Disaster-Response Downlink Model

## Modeling choice

This instance is best represented as a two-stage stochastic mixed-integer program in extensive form.

- First stage: choose a nonanticipative observation plan, meaning which observation window to use for each request before the disruption scenario is known.
- Second stage: for each public scenario, choose a scenario-specific downlink plan for the observed images.

An extensive-form MIP is appropriate here because the public instance is moderate in size: 48 requests, 144 observation windows, 66 downlink contacts, and 4 scenarios. That size is small enough to model all scenarios directly while keeping the stronger nonanticipativity, memory, energy, thermal, and service-floor constraints in one model.

## Sets and indices

- `S`: scenarios.
- `K`: satellites.
- `T = {1, ..., H}`: planning slots, where `H = 24`.
- `G`: ground stations.
- `R`: requests.
- `W`: listed observation windows.
- `C`: listed downlink contacts.
- `P`: stereo pairs with nonblank `stereo_pair_id`.
- `U`: disaster regions.

Useful subsets:

- `W_r`: observation windows for request `r`.
- `W_{k,t}`: observation windows for satellite `k` in slot `t`.
- `W^H_{k,t}`: high-slew observation windows for satellite `k` in slot `t`.
- `C_{k,t}`: downlink contacts for satellite `k` in slot `t`.
- `C_{g,t}`: downlink contacts using ground station `g` in slot `t`.
- `R_u`: requests from region `u`.
- `R_p`: requests in stereo pair `p`.

For each request-contact combination, a downlink variable is created only when the contact belongs to a satellite that could observe that request and the contact slot is strictly after at least one feasible observation slot and no later than the request deadline.

## Parameters

From the public data:

- `prob_s`: scenario probability.
- `target_s`: scenario target profit used in the tail-risk term.
- `size_r`: request data volume in GB.
- `value_r`: base mission value.
- `deadline_r`: deadline slot.
- `crit_r in {0,1}`: whether request `r` is critical.
- `usable_{s,r} in {0,1}`: whether request `r` is usable in scenario `s`.
- `mult_{s,r}`: scenario value multiplier.
- `cap_c`: nominal contact capacity in GB.
- `multdl_{s,g,t}`: scenario-specific station-slot capacity multiplier.
- `ingest_{s,u,t}`: region ingest capacity in GB.
- `obsEnergy_w`, `obsTherm_w`, `sat_w`, `req_w`, `slot_w`, `slew_w`: observation-window data.
- `memCap_k`, `mem0_k`, `energyCap_k`, `energy0_k`, `solar_k`, `dlEnergy_k`, `dlTherm_k`, `thermLim_k`: satellite data.
- `ant_g`: number of antennas at station `g`.
- `obsCost`, `dlCost`, `critPenalty`, `stereoBonus`, `alpha`, `tailWeight`, `minCritFrac`, `minValueFrac`, `minRegions`, `maxObsPerSat`, `reserveEnergy`, `rollWindow`: constants from `config.json`.

Derived values:

- Scenario delivered value coefficient:
  - `val_{s,r} = value_r * mult_{s,r}`.
- Scenario contact capacity:
  - `scCap_{s,c} = cap_c * multdl_{s,station(c),slot(c)}`.
- Usable critical count:
  - `UC_s = sum_{r in R} crit_r * usable_{s,r}`.
- Total usable delivered-value denominator:
  - `UV_s = sum_{r in R} val_{s,r} * usable_{s,r}`.

## Decision variables

First-stage variables:

- `x_w in {0,1}`: 1 if observation window `w` is selected.

Second-stage scenario variables:

- `y_{s,c,r} >= 0`: GB of request `r` downlinked on contact `c` in scenario `s`.
- `z_{s,c} in {0,1}`: 1 if contact `c` is used in scenario `s`.
- `d_{s,r} in {0,1}`: 1 if request `r` is fully delivered by its deadline in scenario `s`.
- `b_{s,p} in {0,1}`: 1 if every request in stereo pair `p` is delivered in scenario `s`.
- `q_{s,u} in {0,1}`: 1 if region `u` receives at least one delivered request in scenario `s`.
- `mem_{s,k,t} >= 0`: onboard memory after slot `t`.
- `energy_{s,k,t} >= 0`: battery energy after slot `t`.
- `recharge_{s,k,t} >= 0`: chosen solar recharge in slot `t`, bounded by the public per-slot recharge limit.

Risk variables:

- `loss_s >= 0`: shortfall of scenario profit below the public target.
- `eta` free: VaR-like threshold for CVaR.
- `excess_s >= 0`: excess loss above `eta`.

## Objective

Let scenario operating profit be

`profit_s = sum_r val_{s,r} d_{s,r}
          + sum_p stereoBonus b_{s,p}
          - sum_{c,r} dlCost y_{s,c,r}
          - sum_r critPenalty * crit_r * usable_{s,r} * (1 - d_{s,r})`.

The first-stage observation cost is

`ObsCost = obsCost * sum_w x_w`.

The public tail-risk term is modeled as CVaR of target shortfall:

- `loss_s >= target_s - profit_s`
- `loss_s >= 0`
- `excess_s >= loss_s - eta`
- `excess_s >= 0`
- `CVaR = eta + (1 / (1 - alpha)) * sum_s prob_s * excess_s`

The objective is

`maximize sum_s prob_s * profit_s - ObsCost - tailWeight * CVaR`.

This follows the public statement: maximize expected scenario profit, subtract first-stage observation cost, and subtract a weighted tail-risk loss.

## Constraints

### 1. Observation selection and nonanticipativity

- Each request is observed at most once:
  - `sum_{w in W_r} x_w <= 1` for all `r`.
- Each satellite performs at most one observation per slot:
  - `sum_{w in W_{k,t}} x_w <= 1` for all `k,t`.
- Each satellite respects the horizon observation count limit:
  - `sum_{w: sat_w = k} x_w <= maxObsPerSat` for all `k`.

Because `x` is common to all scenarios, first-stage nonanticipativity is enforced directly.

### 2. High-slew recovery

Let

- `obs_{k,t} = sum_{w in W_{k,t}} x_w`
- `high_{k,t} = sum_{w in W^H_{k,t}} x_w`

Then consecutive observations are forbidden when either one is high-slew:

- `high_{k,t} + obs_{k,t+1} <= 1` for all `k, t = 1,...,H-1`
- `obs_{k,t} + high_{k,t+1} <= 1` for all `k, t = 1,...,H-1`

This exactly matches the public rule.

### 3. Contact usage and station limits

For every scenario and contact:

- Link downlink quantity to contact activation:
  - `sum_r y_{s,c,r} <= scCap_{s,c} z_{s,c}`.
- Ground-station antenna limit:
  - `sum_{c in C_{g,t}} z_{s,c} <= ant_g` for all `s,g,t`.
- A satellite cannot observe and downlink in the same slot:
  - `sum_{w in W_{k,t}} x_w + sum_{c in C_{k,t}} z_{s,c} <= 1` for all `s,k,t`.

### 4. Downlink-request feasibility

Only the observing satellite may downlink a request, and only after observation.

For every created variable `y_{s,c,r}`, let `A(c,r)` be the set of observation windows for request `r` on the same satellite as contact `c` with `slot_w < slot_c`. Then:

- `y_{s,c,r} <= size_r * sum_{w in A(c,r)} x_w`.

This simultaneously enforces satellite assignment and temporal precedence.

Each delivered request must have its complete volume downlinked by its deadline:

- `sum_c y_{s,c,r} = size_r * d_{s,r}` for all `s,r`.
- `d_{s,r} <= usable_{s,r}` for all `s,r`.

Because variables `y_{s,c,r}` are created only on contacts with `slot_c <= deadline_r`, the equality forces full on-time delivery whenever `d_{s,r} = 1`.

### 5. Region ingest limits

For every scenario, region, and slot:

- `sum_{c: slot_c=t} sum_{r in R_u} y_{s,c,r} <= ingest_{s,u,t}`.

### 6. Memory dynamics

For each scenario and satellite, memory evolves by slot:

- `mem_{s,k,0} = mem0_k`.
- `mem_{s,k,t} = mem_{s,k,t-1}
               + sum_{w in W_{k,t}} size_{req_w} x_w
               - sum_{c in C_{k,t}} sum_r y_{s,c,r}`.
- `0 <= mem_{s,k,t} <= memCap_k`.

Initial memory is treated as occupied carry-in storage that reduces available capacity throughout the horizon unless new request data is downlinked away.

### 7. Energy dynamics

For each scenario and satellite:

- `energy_{s,k,0} = energy0_k`.
- `energy_{s,k,t} = energy_{s,k,t-1}
                  - sum_{w in W_{k,t}} obsEnergy_w x_w
                  - dlEnergy_k * sum_{c in C_{k,t}} sum_r y_{s,c,r}
                  + recharge_{s,k,t}`.
- `0 <= recharge_{s,k,t} <= solar_k`.
- `reserveEnergy <= energy_{s,k,t} <= energyCap_k`.

This models solar recharge as a controllable amount up to the public per-slot recharge limit and battery capacity.

### 8. Rolling thermal envelope

For each scenario, satellite, and slot `t`, over the public rolling window length `rollWindow`:

`sum_{tau=max(1,t-rollWindow+1)}^t
  [sum_{w in W_{k,tau}} obsTherm_w x_w
   + dlTherm_k * sum_{c in C_{k,tau}} sum_r y_{s,c,r}]
 <= thermLim_k`

### 9. Scenario service floors

Critical-delivery floor:

- `sum_r crit_r * usable_{s,r} * d_{s,r} >= minCritFrac * UC_s` for all `s` with `UC_s > 0`.

Delivered-value floor over usable requests:

- `sum_r val_{s,r} d_{s,r} >= minValueFrac * UV_s` for all `s`.

Regional coverage floor:

- `sum_{r in R_u} d_{s,r} >= q_{s,u}` for all `s,u`.
- `sum_u q_{s,u} >= minRegions` for all `s`.

### 10. Stereo-pair bonuses

For each scenario and stereo pair:

- `b_{s,p} <= d_{s,r}` for all `r in R_p`.
- `b_{s,p} >= sum_{r in R_p} d_{s,r} - |R_p| + 1`.

This makes `b_{s,p} = 1` exactly when every request in the pair is delivered in that scenario.

## Public-data assumptions

Two public fields are not operationally defined by the problem statement:

- `quality_score` in `observation_windows.csv`
- `late_penalty_per_slot` in `config.json`

The public rules never state that value depends on `quality_score`, and they state that a request receives credit only if its full data volume is downlinked by its deadline. Therefore the implemented optimization uses the literal public scoring rules:

- delivered request value depends on `mission_value` and the scenario multiplier only;
- no value is granted for incomplete or late delivery;
- no extra late-delivery penalty term is applied because the public statement does not define a valid late-delivery reward/penalty mechanism.

These assumptions are documented explicitly so the solver remains faithful to the public benchmark text instead of inventing unannounced economics.

## Why this formulation is strong

- First-stage nonanticipativity is exact because observations are represented only once.
- Full-delivery binaries `d_{s,r}` avoid weak partial-credit behavior.
- Contact-use binaries `z_{s,c}` make antenna and observe/downlink exclusion constraints tight.
- Memory, energy, ingest, and rolling thermal constraints are modeled directly in slot time, which is compact here because the horizon has only 24 slots.
- Sparse downlink variables are created only for satellite-request-contact combinations that are temporally and physically possible.
