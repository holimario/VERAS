# Petroleum Terminal DRO Inventory Model

## Formulation choice

This instance is best modeled as a two-stage, scenario-expanded, multi-commodity
inventory-flow MIP with fixed-charge stop activations.

- It is **not** a routing or VRP model: there is no vehicle sequencing, travel
  time, or route ordering decision. The outbound complexity is a fixed charge
  and stop-count structure on depot-station-day movements.
- It is **not** an assignment or set-partitioning model: product can flow from
  multiple depots, station and depot inventories evolve over time, and scenario
  recourse is inventory-coupled across days.
- A direct extensive-form MIP is tractable here because the scenario-expanded
  model is moderate in size once inactive station-product pairs are removed and
  scenario-specific terminal loading is compressed into robust deterministic
  bounds.

## Sets

- `T`: terminals.
- `D`: depots.
- `J`: stations.
- `P`: products.
- `H`: planning periods/days in order.
- `S`: public scenarios.
- `L_in ⊆ T x D x P`: feasible inbound terminal-to-depot lanes.
- `L_out ⊆ D x J x P`: feasible outbound depot-to-station lanes.
- `A ⊆ J x P`: active station-product pairs with positive tank capacity and
  public demand rows.
- `R ⊆ D x J`: depot-station pairs that have at least one outbound product lane.

## Parameters

- `rho_s`: public probability of scenario `s` from `scenarios.csv`.
- `eps`: ambiguity worst-case weight from `config.json`.
- `U_stop`: max kiloliters per active stop from `config.json`.
- `C_stop`: active stop fixed cost from `config.json`.
- `cap_in[t,d,p]`: inbound lane daily capacity from `terminal_depot_lanes.csv`.
- `cost_in[t,d,p]`, `co2_in[t,d,p]`: inbound unit transport cost and carbon kg.
- `commit[h,t,d,p]`: minimum take-or-pay inbound commitment; zero if absent.
- `cap_term_nom[h,t,p]`: nominal terminal loading capacity.
- `mult_term[s,h,t,p]`: scenario terminal multiplier.
- `cap_term_rob[h,t,p] = cap_term_nom[h,t,p] * min_s mult_term[s,h,t,p]`.
  Because inbound decisions are scenario-independent and must be loadable in
  every scenario, this compression is exact.
- `cap_out[d,j,p]`: outbound lane daily capacity from `depot_station_lanes.csv`.
- `cost_out[d,j,p]`, `co2_out[d,j,p]`: outbound transport cost and carbon kg.
- `mult_out[s,h,d,j,p]`: scenario route-capacity multiplier.
- `cap_out_eff[s,h,d,j,p] = cap_out[d,j,p] * mult_out[s,h,d,j,p]`.
- `fleet_cap[h,d]`: depot total outbound daily KL.
- `fleet_stops[h,d]`: depot daily active stop limit.
- `shift_cap[h,d,p]`: depot daily product-specific loading cap.
- `depot_init[d,p]`, `depot_cap[d,p]`, `depot_res[d,p]`: depot initial stock,
  tank capacity, and required end-of-day reserve.
- `st_init[j,p]`, `st_cap[j,p]`, `st_res[j,p]`: station initial stock, tank
  capacity, and required end-of-day reserve.
- `floor[j,p]`: minimum served fraction of daily demand.
- `dem[s,h,j,p]`: scenario demand.
- `em_cap[s,p]`: scenario emergency cap by product across the whole horizon.
- `c_em[p]`: emergency unit cost.
- `c_short[p]`: shortage penalty.
- `h_dep[p]`, `h_st[p]`: depot and station holding costs.
- `c_co2[p]`: product-specific carbon cost per kg from `products.csv`.

## Decision variables

First stage:

- `x[h,t,d,p] >= 0`: regular inbound shipment on lane `(t,d,p)` in period `h`.

Second stage, for each scenario `s`:

- `q[s,h,d,j,p] >= 0`: depot-to-station delivery.
- `e[s,h,j,p] >= 0`: emergency delivery to station.
- `u[s,h,j,p] >= 0`: shortage.
- `I[s,h,d,p] >= 0`: depot end-of-day inventory.
- `V[s,h,j,p] >= 0`: station end-of-day inventory.
- `y[s,h,d,j] in {0,1}`: depot `d` makes an active stop at station `j` on day
  `h` in scenario `s`.
- `b[s,h,j,p] in {0,1}`: station `(j,p)` ends day `h` exactly at reserve in
  scenario `s`, which is the only case where shortage can be positive.
- `c_s`: total recourse cost in scenario `s`.
- `z`: worst observed scenario cost.

## Objective

Minimize

```text
InboundCost + (1 - eps) * sum_s rho_s * c_s + eps * z
```

where

```text
InboundCost =
  sum_{h,(t,d,p) in L_in}
    (cost_in[t,d,p] + c_co2[p] * co2_in[t,d,p]) * x[h,t,d,p]
```

and each `c_s` equals

```text
sum_{h,(d,j,p) in L_out}
  (cost_out[d,j,p] + c_co2[p] * co2_out[d,j,p]) * q[s,h,d,j,p]
+ sum_{h,(d,j) in R} C_stop * y[s,h,d,j]
+ sum_{h,(j,p) in A} c_em[p] * e[s,h,j,p]
+ sum_{h,(j,p) in A} c_short[p] * u[s,h,j,p]
+ sum_{h,d,p} h_dep[p] * I[s,h,d,p]
+ sum_{h,(j,p) in A} h_st[p] * V[s,h,j,p]
```

with `z >= c_s` for all scenarios.

## Constraints

### 1. Inbound lane and terminal feasibility

For all `h,(t,d,p) in L_in`:

```text
commit[h,t,d,p] <= x[h,t,d,p] <= cap_in[t,d,p]
```

For all `h,t,p`:

```text
sum_{d : (t,d,p) in L_in} x[h,t,d,p] <= cap_term_rob[h,t,p]
```

### 2. Depot inventory balance and bounds

For each scenario `s`, day `h`, depot `d`, product `p`:

```text
I[s,h,d,p] =
  prevDepot[s,h,d,p]
  + sum_{t : (t,d,p) in L_in} x[h,t,d,p]
  - sum_{j : (d,j,p) in L_out} q[s,h,d,j,p]
```

where `prevDepot` is `depot_init[d,p]` on day 1 and `I[s,h-1,d,p]` otherwise.

For all `s,h,d,p`:

```text
depot_res[d,p] <= I[s,h,d,p] <= depot_cap[d,p]
```

### 3. Station inventory balance and bounds

For each scenario `s`, day `h`, active pair `(j,p) in A`:

```text
V[s,h,j,p] =
  prevStation[s,h,j,p]
  + sum_{d : (d,j,p) in L_out} q[s,h,d,j,p]
  + e[s,h,j,p]
  - dem[s,h,j,p]
  + u[s,h,j,p]
```

where `prevStation` is `st_init[j,p]` on day 1 and `V[s,h-1,j,p]` otherwise.

For all `s,h,(j,p) in A`:

```text
st_res[j,p] <= V[s,h,j,p] <= st_cap[j,p]
0 <= u[s,h,j,p] <= (1 - floor[j,p]) * dem[s,h,j,p]
```

To prevent a time-limited incumbent from using voluntary shortage to create
inventory that is not encoded in the exported shipment file, the model forces
the station recursion into one of two exact branches:

```text
u[s,h,j,p] <= (1 - floor[j,p]) * dem[s,h,j,p] * b[s,h,j,p]
V[s,h,j,p] <= st_res[j,p] + (st_cap[j,p] - st_res[j,p]) * (1 - b[s,h,j,p])
```

Interpretation:

- If `b=1`, then `V=st_res` and any positive shortage is explicitly tied to the
  service-floor limit.
- If `b=0`, then `u=0`, so the station cannot hold back demand artificially.

This makes the shipment export induce a unique, checker-consistent station
inventory trajectory.

### 4. Outbound lane, stop, and depot loading limits

For all `s,h,(d,j,p) in L_out`:

```text
q[s,h,d,j,p] <= cap_out_eff[s,h,d,j,p] * y[s,h,d,j]
```

For all `s,h,(d,j) in R`:

```text
sum_{p : (d,j,p) in L_out} q[s,h,d,j,p] <= U_stop * y[s,h,d,j]
```

For all `s,h,d`:

```text
sum_{j,p : (d,j,p) in L_out} q[s,h,d,j,p] <= fleet_cap[h,d]
sum_{j : (d,j) in R} y[s,h,d,j] <= fleet_stops[h,d]
```

For all `s,h,d,p`:

```text
sum_{j : (d,j,p) in L_out} q[s,h,d,j,p] <= shift_cap[h,d,p]
```

### 5. Emergency capacity

For all `s,p`:

```text
sum_{h,(j,p) in A} e[s,h,j,p] <= em_cap[s,p]
```

## Main assumptions implemented

- Station emergency reserves are enforced as **daily end-of-day lower bounds**,
  analogous to the explicitly stated depot reserve rule.
- Emergency capacity is horizon-wide by `(scenario, product)` because
  `scenario_emergency_caps.csv` has no period index.
- Carbon cost uses `products.csv::carbon_cost_per_kg`, which is product
  specific. The global `config.json::carbon_price_per_kg` is treated as legacy
  metadata and is not multiplied again.
- Public scenario probabilities are used as provided in the CSV. Their text
  values sum to `0.99999` because of rounding; this is immaterial operationally.
- Descriptive metadata fields such as names, regions, notes, route classes, and
  station priority labels are not direct decision parameters because their
  operational effect is already encoded by capacities, costs, reserves, and
  service floors.
