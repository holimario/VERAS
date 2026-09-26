# Medical Supply DRO Response Model

## Formulation choice

This instance is best modeled as a two-stage extensive-form mixed-integer
inventory-flow network with shared first-stage design variables and
scenario-specific recourse. A pure assignment or network-flow model would miss
the supplier line-start logic, warehouse opening decisions, and dynamic
inventory/release effects. A time-indexed inventory-flow formulation is strong
here because:

- the horizon is only 5 days;
- first-stage decisions are common to all 8 scenarios;
- each scenario has dense demand and lane data;
- the only essential recourse binaries are supplier day-item run indicators.

The resulting model keeps the combinatorial part small and uses tight
data-derived bounds on all continuous flows.

## Sets

- `W`: warehouses.
- `S`: suppliers.
- `C`: clinics.
- `I`: items.
- `T = {1,...,H}`: response days in chronological order.
- `Omega`: disruption scenarios.
- `A_sw`: allowed supplier-to-warehouse lanes.
- `A_wc`: allowed warehouse-to-clinic lanes.
- `F`: fleet types.
- `F_wcsi(omega,t,w,c,i)`: fleets allowed on `(w,c)` for item `i` in scenario
  `omega`, day `t`.
- `I_s`: items with positive compatible reserved capacity at supplier `s`.
- `ColdI`: cold-chain items.
- `CriticalC`: critical clinics.
- `NonCriticalC`: non-critical clinics.

## Main parameters

All data come from `/app/data`.

- `p_omega`: empirical scenario probability.
- `rho`: ambiguity radius.
- `alpha`: CVaR tail level.
- `lambda_tail`: CVaR weight.
- `Budget`: planning budget.
- `fair_gap`: final-fill fairness gap.
- `ret_frac`, `ret_lag`: oxygen cylinder return fraction and lag.
- `vol_i`, `load_i`: unit storage volume and transport load factor.
- `c_stock_i`, `c_prod_i`, `c_res_i`, `c_exp_i`, `c_short_i`, `c_hold_i`:
  item costs.
- `lag_i`: warehouse release lag for item `i`.
- `cold_i`, `return_i`: cold-chain / returnable flags.
- `stockcap_i`: per-warehouse stock cap for item `i`.
- `open_cost_w`, `storcap_w`, `outcap_w`, `cold_w`: warehouse data.
- `act_cost_s`, `basecap_s`, `hours_s`, `setupmax_s`, `cert_s`: supplier data.
- `resmax_si`, `hourper_si`, `batch_si`, `setuph_si`, `setupcost_si`:
  supplier-item data.
- `laneCapSW_sw`, `laneCostSW_sw`, `laneColdSW_sw`: supplier-warehouse lane
  data.
- `laneCapWC_wc`, `laneCostWC_wc`, `roadclass_wc`, `baseTime_wc`:
  warehouse-clinic lane data.
- `fleetTrips_wtf`: fleet trips available at warehouse `w`, day `t`, fleet `f`.
- `fleetCap_f`, `fleetCold_f`, `fleetRough_f`, `fleetAir_f`, `fleetTripCost_f`,
  `fleetMaxTime_f`: fleet parameters.
- `recvcap_c`, `coldcap_c`, `localfrac_c`, `priority_c`: clinic parameters.
- `floor_ci`: final service-floor fraction for clinic `c`, item `i`.
- `target_pit`: cumulative fraction of the final floor required by day `t` for
  priority class `p`, item `i`.
- `demand_omega,t,c,i`: scenario demand.
- `supMult_omega,t,s`: supplier availability multiplier.
- `swCapMult_omega,t,s,w`, `swCostMult_omega,t,s,w`: supplier-lane impacts.
- `whStorMult_omega,t,w`, `whOutMult_omega,t,w`: warehouse availability
  multipliers.
- `roadCapMult_omega,t,w,c`, `roadExtra_omega,t,w,c`,
  `roadTime_omega,t,w,c`: road impacts.
- `expCap_omega,i`: scenario-wide emergency-purchase cap by item.
- `delayMult_t`: period delay multiplier.
- `lateCost`: late-response penalty per load-hour.

Derived constants:

- `TotalDem_omega,c,i = sum_t demand_omega,t,c,i`.
- `FloorReq_omega,c,i = floor_ci * TotalDem_omega,c,i`.
- `CumDem_omega,t,c,i = sum_{tau<=t} demand_omega,tau,c,i`.
- `CumFloor_omega,t,c,i = target_{priority_c,i,t} * floor_ci * CumDem_omega,t,c,i`.
- `Affected_omega,t,w,c = 1` if the public road-impact row differs from the base
  lane on capacity, extra cost, or travel time, otherwise `0`.

## Decision variables

### First stage

- `y_w in {0,1}`: 1 if warehouse `w` is opened.
- `a_s in {0,1}`: 1 if supplier `s` is active.
- `stock_wi >= 0`: starting stock at warehouse `w` for item `i`.
- `res_si >= 0`: reserved daily surge capacity at supplier `s` for item `i`.

### Recourse for each scenario `omega`

- `run_omega,t,s,i in {0,1}`: supplier `s` runs item line `i` on day `t`.
- `prod_omega,t,s,i >= 0`: production quantity.
- `in_omega,t,s,w,i >= 0`: inbound quantity from supplier `s` to warehouse `w`.
- `disp_omega,t,w,c,i,f >= 0`: quantity dispatched from warehouse `w` to clinic
  `c` using fleet `f`.
- `exp_omega,t,c,i >= 0`: local emergency purchase.
- `unmet_omega,t,c,i >= 0`: unmet demand.
- `inv_omega,t,w,i >= 0`: end-of-day released inventory available at warehouse
  `w`.
- `opcost_omega`: total operating cost in scenario `omega`.

### Risk variables

- `eta`: CVaR threshold.
- `excess_omega >= 0`: CVaR excess above `eta`.
- `lam`: free dual variable for the ambiguity premium.
- `gamma >= 0`, `alphaAmb_omega >= 0`, `betaAmb_omega >= 0`: dual variables for
  worst-case expectation over the public `L1` probability ball.

## Objective

Minimize

`FirstStageCost + WorstCaseExpectedOperatingCost + lambda_tail * CVaR_alpha(opcost)`

where

- `FirstStageCost`
  `= sum_w open_cost_w y_w`
  `+ sum_s act_cost_s a_s`
  `+ sum_wi c_stock_i stock_wi`
  `+ sum_si c_res_i res_si`.

- `WorstCaseExpectedOperatingCost` is the exact linear dual of
  `max_q sum_omega q_omega opcost_omega` subject to
  `sum_omega q_omega = 1`, `q >= 0`, and
  `sum_omega |q_omega - p_omega| <= rho`:

  `lam + sum_omega p_omega (alphaAmb_omega - betaAmb_omega) + rho * gamma`

  with dual constraints

  `lam + alphaAmb_omega - betaAmb_omega >= opcost_omega`

  `gamma >= alphaAmb_omega + betaAmb_omega`.

- `CVaR_alpha(opcost)`
  `= eta + (1 / (1 - alpha)) * sum_omega p_omega excess_omega`

  with

  `excess_omega >= opcost_omega - eta`.

## Constraints

### 1. First-stage design and budget

- Warehouse count:
  `sum_w y_w <= max_open_warehouses`.
- Cold-chain open minimum:
  `sum_{w: cold_w=1} y_w >= min_open_cold_chain_warehouses`.
- Active supplier minimum:
  `sum_s a_s >= min_active_suppliers`.
- First-stage budget:
  `FirstStageCost <= Budget`.

### 2. Initial stock and reserve bounds

For all feasible `(w,i)`:

- `stock_wi <= stockcap_i * y_w`.
- If `cold_i = 1` and `cold_w = 0`, then `stock_wi = 0`.

For all supplier-item pairs:

- `res_si <= resmax_si * a_s`.
- If the pair is incompatible, `res_si = 0`.
- If `cold_i = 1` and `cert_s = 0`, then `res_si = 0`.

Supplier reservation tightening:

- `sum_i res_si <= basecap_s * a_s`.
- `sum_i hourper_si * res_si <= hours_s * a_s`.

### 3. Production and supplier line logic

For each scenario `omega`, day `t`, supplier `s`, compatible item `i`:

- Reserve-to-production link:
  `prod_omega,t,s,i <= supMult_omega,t,s * res_si`.
- Activation link:
  `run_omega,t,s,i <= a_s`.
- Minimum batch if a line runs:
  `prod_omega,t,s,i >= batch_si * run_omega,t,s,i`.
- Tight upper bound:
  `prod_omega,t,s,i <= supMult_omega,t,s * resmax_si * run_omega,t,s,i`.

For each `omega,t,s`:

- Base unit capacity:
  `sum_i prod_omega,t,s,i <= basecap_s * supMult_omega,t,s * a_s`.
- Line-hour pool:
  `sum_i hourper_si * prod_omega,t,s,i + sum_i setuph_si * run_omega,t,s,i`
  `<= hours_s * supMult_omega,t,s * a_s`.
- Max active item lines:
  `sum_i run_omega,t,s,i <= setupmax_s`.

### 4. Supplier-to-warehouse flow

For each `omega,t,s,i`:

- Flow conservation at supplier:
  `sum_{w:(s,w) in A_sw} in_omega,t,s,w,i = prod_omega,t,s,i`.

For each `omega,t,s,w` lane:

- Lane capacity in load-equivalent units:
  `sum_i load_i * in_omega,t,s,w,i`
  `<= laneCapSW_sw * swCapMult_omega,t,s,w`.

For cold-chain items, inbound is allowed only if the lane supports cold chain
and the destination warehouse is cold-chain capable.

### 5. Warehouse inventory, release lag, returns, storage, and outbound handling

Let `ReleasedInbound_omega,t,w,i` denote:

- `sum_s in_omega,t-lag_i,s,w,i` if `t - lag_i >= 1`,
- `0` otherwise.

Let `Return_omega,t,w,i` be:

- `ret_frac * sum_{c,f} disp_omega,t-ret_lag,w,c,oxygen,f` if
  `i = oxygen` and `t - ret_lag >= 1`,
- `0` otherwise.

Inventory balance for all `omega,t,w,i`:

- If `t = 1`:
  `inv_omega,1,w,i = stock_wi + ReleasedInbound_omega,1,w,i + Return_omega,1,w,i`
  `- sum_{c,f} disp_omega,1,w,c,i,f`.
- If `t > 1`:
  `inv_omega,t,w,i = inv_omega,t-1,w,i + ReleasedInbound_omega,t,w,i`
  `+ Return_omega,t,w,i - sum_{c,f} disp_omega,t,w,c,i,f`.

Closed-warehouse enforcement is already tight through stock, storage, and
outbound capacity, but we also use:

- `sum_{c,i,f} load_i * disp_omega,t,w,c,i,f`
  `<= outcap_w * whOutMult_omega,t,w * y_w`.

Physical stock occupying storage includes unreleased receipts. For all
`omega,t,w`:

- `sum_i vol_i * (inv_omega,t,w,i + Quarantine_omega,t,w,i)`
  `<= storcap_w * whStorMult_omega,t,w * y_w`

where

- `Quarantine_omega,t,w,i = sum_{tau=max(1,t-lag_i+1)}^t sum_s in_omega,tau,s,w,i`.

For cold-chain items in non-cold warehouses, all corresponding inventory and
flows are fixed to zero.

### 6. Warehouse-to-clinic dispatch and fleet limits

For each scenario-day-lane `(omega,t,w,c)`:

- Road lane capacity:
  `sum_{i,f} load_i * disp_omega,t,w,c,i,f`
  `<= laneCapWC_wc * roadCapMult_omega,t,w,c`.

For each warehouse-day-fleet `(w,t,f)`:

- Fleet trip availability:
  `sum_{c,i} (load_i / fleetCap_f) * disp_omega,t,w,c,i,f`
  `<= fleetTrips_wtf * y_w`.

Fleet eligibility used in `F_wcsi(omega,t,w,c,i)`:

- the lane `(w,c)` must exist;
- if `cold_i = 1`, require `fleetCold_f = 1`;
- if `fleetAir_f = 0`, require `roadTime_omega,t,w,c <= fleetMaxTime_f`;
- conservative public-data interpretation for disrupted non-urban roads:
  if `Affected_omega,t,w,c = 1` and `roadclass_wc != urban`, require
  `fleetRough_f = 1`.

### 7. Demand balance, emergency purchases, and clinic receiving limits

For every `omega,t,c,i`:

- Full demand accounting:
  `sum_{w,f} disp_omega,t,w,c,i,f + exp_omega,t,c,i + unmet_omega,t,c,i`
  `= demand_omega,t,c,i`.

Emergency-purchase limits:

- `exp_omega,t,c,i <= localfrac_c * demand_omega,t,c,i`.
- `sum_{t,c} exp_omega,t,c,i <= expCap_omega,i`.

Clinic receiving capacity by day (volume-based):

- `sum_i vol_i * (sum_{w,f} disp_omega,t,w,c,i,f + exp_omega,t,c,i)`
  `<= recvcap_c`.

Clinic cold-storage capacity by day (cold-chain arrivals only):

- `sum_{i in ColdI} vol_i * (sum_{w,f} disp_omega,t,w,c,i,f + exp_omega,t,c,i)`
  `<= coldcap_c`.

### 8. Service floors and fairness

For every `omega,t,c,i`:

- `sum_{tau<=t} (sum_{w,f} disp_omega,tau,w,c,i,f + exp_omega,tau,c,i)`
  `>= CumFloor_omega,t,c,i`.

For every scenario `omega`, item `i`, critical clinic `c`, and non-critical
clinic `n`:

- `TotalDem_omega,n,i * Served_omega,c,i - TotalDem_omega,c,i * Served_omega,n,i`
  `>= -fair_gap * TotalDem_omega,c,i * TotalDem_omega,n,i`

where

- `Served_omega,c,i = sum_t (sum_{w,f} disp_omega,t,w,c,i,f + exp_omega,t,c,i)`.

This is exactly the final-fill-rate condition
`Served_critical / Demand_critical >= Served_noncritical / Demand_noncritical - fair_gap`.

### 9. Scenario operating cost definition

For each scenario `omega`, `opcost_omega` equals the sum over all days of:

- production cost:
  `sum_{t,s,i} c_prod_i * prod_omega,t,s,i`;
- setup cost:
  `sum_{t,s,i} setupcost_si * run_omega,t,s,i`;
- inbound transport cost:
  `sum_{t,s,w,i} laneCostSW_sw * swCostMult_omega,t,s,w * load_i * in_omega,t,s,w,i`;
- outbound transport load cost:
  `sum_{t,w,c,i,f} (laneCostWC_wc + roadExtra_omega,t,w,c) * load_i * disp_omega,t,w,c,i,f`;
- outbound fleet trip cost:
  `sum_{t,w,c,i,f} fleetTripCost_f * (load_i / fleetCap_f) * disp_omega,t,w,c,i,f`;
- late-response penalty:
  `sum_{t,w,c,i,f} lateCost * delayMult_t * roadTime_omega,t,w,c * load_i * disp_omega,t,w,c,i,f`;
- emergency-purchase cost:
  `sum_{t,c,i} c_exp_i * exp_omega,t,c,i`;
- shortage penalty:
  `sum_{t,c,i} c_short_i * unmet_omega,t,c,i`;
- holding cost on physical end-of-day warehouse stock:
  `sum_{t,w,i} c_hold_i * (inv_omega,t,w,i + Quarantine_omega,t,w,i)`.

### 10. CVaR linearization

For each scenario `omega`:

- `excess_omega >= opcost_omega - eta`.
- `excess_omega >= 0`.

### 11. Ambiguity-premium dual constraints

For each scenario `omega`:

- `lam + alphaAmb_omega - betaAmb_omega >= opcost_omega`.
- `gamma >= alphaAmb_omega + betaAmb_omega`.

## Output mapping

The exported CSV uses:

- `warehouse`, `supplier`, `stock`, `reserve` rows from first-stage variables;
- `produce`, `inbound`, `dispatch`, `expedite`, `unmet` rows from the chosen
  scenario recourse.

`expedite` and `unmet` rows are emitted for every scenario-day-clinic-item
combination. `produce`, `inbound`, and `dispatch` rows are emitted only for
positive quantities.

## Public-data assumptions made explicit

These choices are not spelled out numerically in the statement, so they are
documented and implemented consistently:

1. `clinics.csv` service-floor columns are interpreted as final fill-rate
   floors against total scenario demand for that clinic-item over the full
   5-day horizon.
2. `service_targets.csv` scales that fill-rate floor against cumulative demand
   observed up to each day, not against full-horizon demand on day 1.
3. The distributional-stress premium is modeled as the exact worst-case
   expectation over the empirical scenario distribution inside the public
   `L1` ball of radius `ambiguity_radius`.
4. Clinic receiving and cold-storage capacities are enforced in item-volume
   units.
5. On a disrupted non-urban road row, only rough-road-capable fleets are
   allowed; airlift remains allowed through the fleet data as well.
