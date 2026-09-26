# Mathematical Model

## Formulation Choice

A naive model with integer trip variables alone is weak because the objective is
the probability that at least 5 relays are restored, not a linear sum of trip
benefits. The instance is small enough to use a stronger exact formulation:

- a set-partitioning pattern model for each relay, where each pattern fixes how
  many drops of each payload are sent to that relay in each period, subject to
  the relay's own accessibility and safety limits;
- explicit aircraft-assignment variables to realize those aggregate drops while
  enforcing aircraft, period, fuel, crew, and payload-stock constraints;
- a scenario-specific dynamic-programming extended formulation that computes the
  exact probability that at least `required_restored_relays = 5` relays are
  restored.

This avoids nonlinear products in the MIP and gives a much tighter model than a
drop-level big-M approximation.

## Sets and Indices

- `R`: relays, indexed by `r`
- `P`: periods, indexed by `p`, ordered by increasing sequence
- `A`: aircraft, indexed by `a`
- `K`: payload types, indexed by `k`
- `W`: weather scenarios, indexed by `w`
- `D`: districts, indexed by `d`
- `R_d ⊆ R`: relays in district `d`
- `E ⊆ P × A × R × K`: feasible trip-assignment arcs `(p,a,r,k)` such that
  the aircraft is available in period `p`, aircraft `a` is compatible with
  payload `k`, relay `r` is accessible in period `p`, and payload `k` is
  eligible for relay `r`
- `J_r`: feasible aggregate drop patterns for relay `r`

Each relay pattern `j ∈ J_r` is an integer vector
`n_{rjpk} ≥ 0` for `(p,k)` pairs available to relay `r`, satisfying:

- `n_{rjpk} = 0` if relay `r` is inaccessible in period `p` or payload `k` is
  ineligible at relay `r`
- `∑_k n_{rjpk} ≤ max_period_drops_{pr}` for every period `p`
- `∑_{p,k} n_{rjpk} ≤ max_total_drops_r`

## Parameters

- `Kreq`: required number of restored relays (`5`)
- `prob_w`: probability weight of scenario `w`
- `minsucc_w`: minimum allowed mission-success probability in scenario `w`
- `stock_k`: available units of payload `k`
- `tripcap_p`, `fuelcap_p`, `prepcap_p`: total period limits
- `fuelbudget`, `prepbudget`: mission-wide operating budgets
- `avail_pa ∈ {0,1}`: aircraft `a` available in period `p`
- `tripcap_pa`, `fuelcap_pa`, `prepcap_pa`: aircraft-period limits
- `tripcap_a`: mission-wide sortie limit for aircraft `a`
- `compat_ak ∈ {0,1}`: aircraft-payload compatibility
- `eligible_rk ∈ {0,1}`: relay-payload eligibility
- `accessible_pr ∈ {0,1}`: relay accessibility in period `p`
- `maxpdrops_pr`: relay-period drop cap
- `maxtdrops_r`: relay-wide total drop cap
- `district_min_drops_d`, `district_min_active_d`: district-wide coverage rules
- `cumdrop_target_pd`, `cumactive_target_pd`: cumulative district targets
- `baseeff_rk`: base per-drop restore probability
- `peff_pr`: period access effectiveness multiplier
- `seff_wpr`: scenario-period-relay effectiveness multiplier
- `dist_r`: one-way distance to relay `r`
- `loaded_k`, `empty_k`, `fixedfuel_k`: payload fuel coefficients
- `preptrip_k`: payload preparation hours per trip
- `fuelmult_a`, `prepmult_a`: aircraft multipliers

Derived trip resource coefficients for each feasible arc `(p,a,r,k) ∈ E`:

- `fuel_{park} = fuelmult_a * (dist_r / loaded_k + dist_r / empty_k + fixedfuel_k)`
- `prep_{ak} = prepmult_a * preptrip_k`

Derived pattern summaries:

- `N_{rj} = ∑_{p,k} n_{rjpk}`: total drops in pattern `j`
- `N_{rjt}^{cum} = ∑_{p: seq(p) ≤ seq(t)} ∑_k n_{rjpk}`: cumulative drops by end
  of period `t`
- `Act_{rj} = 1` if `N_{rj} ≥ 1`, else `0`
- `Act_{rjt}^{cum} = 1` if `N_{rjt}^{cum} ≥ 1`, else `0`

For scenario `w`, the per-drop success probability of a drop to relay `r` in
period `p` with payload `k` is

`rho_{wprk} = baseeff_rk * peff_pr * seff_wpr`

and the resulting restoration probability of relay `r` under pattern `j` is

`q_{wrj} = 1 - ∏_{p,k} (1 - rho_{wprk})^{n_{rjpk}}`.

This is exact because each drop is an independent restoration attempt.

## Decision Variables

- `x_{park} ∈ Z_+` for `(p,a,r,k) ∈ E`:
  number of flights assigned on arc `(p,a,r,k)`
- `lambda_{rj} ∈ {0,1}` for `r ∈ R, j ∈ J_r`:
  1 if relay `r` uses aggregate pattern `j`

For the exact mission-success probability recursion, order relays as
`r_1, ..., r_|R|`. Because the mission only cares whether at least `Kreq = 5`
relays are restored, the recursion uses a capped state space
`m = 0, ..., Kreq`, where state `Kreq` means "at least `Kreq` restored." This
is exact and smaller than tracking all counts `0, ..., |R|`.

- `alpha_{wim} ∈ [0,1]`:
  probability that, after processing the first `i` ordered relays in scenario
  `w`, the capped restored-relay count equals `m`; for `m < Kreq` this means
  exactly `m` relays are restored, and for `m = Kreq` this means at least
  `Kreq` relays are restored
- `u_{wijm} ∈ [0,1]`:
  linearization variable for
  `lambda_{r_i j} * alpha_{w,i-1,m}`
- `MissionProb_w ∈ [0,1]`:
  probability that at least `Kreq` relays are restored in scenario `w`

## Objective

Maximize the scenario-weighted mission success probability:

`max ∑_{w ∈ W} prob_w * MissionProb_w`

## Constraints

### 1. One aggregate pattern per relay

For every relay `r ∈ R`:

`∑_{j ∈ J_r} lambda_{rj} = 1`

### 2. Aggregate drop counts must equal assigned aircraft trips

For every relay `r ∈ R`, period `p ∈ P`, payload `k ∈ K`:

`∑_{a : (p,a,r,k) ∈ E} x_{park} = ∑_{j ∈ J_r} n_{rjpk} * lambda_{rj}`

If `(p,r,k)` is not feasible for relay `r`, then `n_{rjpk} = 0` for all
patterns and the equality forces zero trips.

### 3. Payload stock limits

For every payload `k ∈ K`:

`∑_{(p,a,r,k) ∈ E} x_{park} ≤ stock_k`

### 4. Mission-wide fuel and preparation budgets

- `∑_{(p,a,r,k) ∈ E} fuel_{park} * x_{park} ≤ fuelbudget`
- `∑_{(p,a,r,k) ∈ E} prep_{ak} * x_{park} ≤ prepbudget`

### 5. Period-wide operating limits

For every period `p ∈ P`:

- `∑_{(p,a,r,k) ∈ E} x_{park} ≤ tripcap_p`
- `∑_{(p,a,r,k) ∈ E} fuel_{park} * x_{park} ≤ fuelcap_p`
- `∑_{(p,a,r,k) ∈ E} prep_{ak} * x_{park} ≤ prepcap_p`

### 6. Aircraft mission-wide sortie limits

For every aircraft `a ∈ A`:

`∑_{(p,a,r,k) ∈ E} x_{park} ≤ tripcap_a`

### 7. Aircraft-period operating limits

For every period `p ∈ P`, aircraft `a ∈ A`:

- `∑_{(p,a,r,k) ∈ E} x_{park} ≤ tripcap_pa`
- `∑_{(p,a,r,k) ∈ E} fuel_{park} * x_{park} ≤ fuelcap_pa`
- `∑_{(p,a,r,k) ∈ E} prep_{ak} * x_{park} ≤ prepcap_pa`

If `avail_pa = 0`, then these bounds are zero and force no use of that
aircraft-period pair.

### 8. District-wide coverage requirements

For every district `d ∈ D`:

- `∑_{r ∈ R_d} ∑_{j ∈ J_r} N_{rj} * lambda_{rj} ≥ district_min_drops_d`
- `∑_{r ∈ R_d} ∑_{j ∈ J_r} Act_{rj} * lambda_{rj} ≥ district_min_active_d`

### 9. Cumulative district targets

For every district `d ∈ D` and period `t ∈ P`:

- `∑_{r ∈ R_d} ∑_{j ∈ J_r} N_{rjt}^{cum} * lambda_{rj} ≥ cumdrop_target_td`
- `∑_{r ∈ R_d} ∑_{j ∈ J_r} Act_{rjt}^{cum} * lambda_{rj} ≥ cumactive_target_td`

### 10. Exact probability recursion for restored relays

Initialization for each scenario `w ∈ W`:

- `alpha_{w,0,0} = 1`
- `alpha_{w,0,m} = 0` for `m = 1, ..., Kreq`

Linearization of `u_{wijm} = lambda_{r_i j} * alpha_{w,i-1,m}` for
`i = 1, ..., |R|`, `j ∈ J_{r_i}`, `m = 0, ..., Kreq`:

- `u_{wijm} ≤ lambda_{r_i j}`
- `u_{wijm} ≤ alpha_{w,i-1,m}`
- `u_{wijm} ≥ alpha_{w,i-1,m} - (1 - lambda_{r_i j})`
- `u_{wijm} ≥ 0`

The pattern weights must partition the previous-stage probability mass:

For every `w`, `i = 1, ..., |R|`, and `m = 0, ..., Kreq`:

`∑_{j ∈ J_{r_i}} u_{wijm} = alpha_{w,i-1,m}`

State transition for each scenario `w`, relay position `i = 1, ..., |R|`, and
state `m = 0, ..., Kreq - 1`:

`alpha_{wim} = ∑_{j ∈ J_{r_i}} ((1 - q_{wr_i j}) * u_{wijm} + q_{wr_i j} * u_{wij,m-1})`

with the convention `u_{wij,-1} = 0`.

For the capped success state:

`alpha_{w,i,Kreq} = ∑_{j ∈ J_{r_i}} (u_{wij,Kreq} + q_{wr_i j} * u_{wij,Kreq-1})`

The term `u_{wij,Kreq}` keeps all probability mass that already had at least
`Kreq` restored relays, while `q_{wr_i j} * u_{wij,Kreq-1}` is the probability
of reaching the threshold at relay `i`. This is the usual Poisson-binomial
recursion, made linear by conditioning on the selected relay pattern.

### 11. Scenario mission-success probability and minimum thresholds

For every scenario `w ∈ W`:

- `MissionProb_w = alpha_{w,|R|,Kreq}`
- `MissionProb_w ≥ minsucc_w`

## Variable Domains

- `x_{park}` integer and nonnegative
- `lambda_{rj}` binary
- `alpha_{wim}, u_{wijm}, MissionProb_w` continuous in `[0,1]`

## Output Mapping

For every feasible arc `(p,a,r,k) ∈ E` with `x_{park} > 0`, write one row to
`solution.csv` with

- `variable = "drop"`
- `period = p`
- `aircraft = a`
- `relay = r`
- `payload_type = k`
- `trips = x_{park}`

Values are exported as integers in the CSV while preserving exact solver values
internally to at least `1e-8` precision for auditing.
