# Fuel Tanker Compartment Routing Model

## Modeling choice

A monolithic arc-flow VRP with compartment-state, loading-bay sequencing, and heterogeneous fleet logic would be weak and unnecessarily large for this instance. The public instance has 32 stations, 11 vehicles, at most 22 trips, fixed loading-slot start times, and strong per-trip packing structure from tanker compartments.

I therefore use a two-level exact formulation:

1. Precompute feasible vehicle-specific trip patterns.
   Each pattern already fixes:
   - the ordered station sequence on the trip;
   - the exact compartment-to-order assignment;
   - compartment precedence feasibility (`M` before `R` before `F`);
   - vehicle access feasibility;
   - payload and volume feasibility;
   - compatibility with loading-bay hose families.
2. Solve a master set-partitioning / scheduling MIP over trip-slot candidates.
3. Given the selected trip-slot candidates, solve a smaller exact bay-sequencing MIP.

This gives a much tighter model than a naive routing formulation because compartment packing and stop-order feasibility are pushed into preprocessing.

## Sets and indices

- `S`: stations with demand.
- `O_s`: orders at station `s`.
- `V`: vehicles.
- `K_v`: physical compartments of vehicle `v`.
- `L`: loading slots.
- `B`: loading bays.
- `P_{v,s}`: feasible compartment patterns for station `s` on vehicle `v`.
- `R_v`: feasible ordered trip patterns for vehicle `v`, restricted here to one-stop and two-stop trips.
- `C`: feasible trip-slot candidates derived from `R_v` and `L`.
- `T`: selected trip-slot candidates in the bay-assignment subproblem.

## Parameters

From the public data:

- `d_o`, `m_o`: requested and minimum liters for order `o`.
- `pen_o`: short-fill penalty per liter for order `o`.
- `rho_p`: product density.
- `cap_k`: capacity of compartment `k`.
- `compat_{k,p}`: 1 if compartment `k` can carry product `p`.
- `grp_k in {M,R,F}`: unloading precedence group of compartment `k`.
- `Q_v`, `W_v`: vehicle volume and payload limits.
- `ntrip_v`: maximum trips for vehicle `v`.
- `work_v`: vehicle working-time limit.
- `fc_v`, `kc_v`: fixed trip cost and distance cost.
- `slotcap_l`, `slotcost_l`, `start_l`: loading-slot capacity, slot cost, and start time.
- `baycost_b`, `bayclass_b`, `bayfam_b`: bay use cost, maximum vehicle class, and supported hose families.
- `clean_{f,g}^{time}`, `clean_{f,g}^{cost}`: cleanout time and cost from hose family `f` to `g`.
- `a_s`, `pref_s`, `u_s`: station earliest, preferred latest, and hard latest arrival time.
- `svc_s`: station service time using the exact delivered demand in this model:
  `fixed_service_s + sum_{o in O_s} d_o / service_rate`.
- `tau_{i,j}`, `dist_{i,j}`, `haz_{i,j}`: travel time, distance, and hazardous surcharge factor for arc `(i,j)` derived from coordinates, risk-zone factors, and local road adjustments.

## Station-pattern preprocessing

For each `(v,s)`, enumerate compartment subsets `H subseteq K_v`. A subset is feasible if:

1. The station is accessible to vehicle `v` after applying class rules and explicit vehicle-station exceptions.
2. The subset has enough total volume and enough compatible capacity by product.
3. There exists a feasible compartment-to-order assignment:

   - each compartment is assigned to at most one order item on the trip;
   - a compartment may be assigned to order `o` only when it is compatible with the product of `o`;
   - the total assigned capacity to each order is at least `d_o`;
   - actual liters are then allocated within those assigned compartments so each order receives exactly `d_o`.

For each feasible subset, keep the resulting pattern:

- used compartments;
- exact compartment-to-order quantities;
- `gmin` and `gmax`, the minimum and maximum precedence groups used by the pattern;
- the set of hose families loaded on the trip.

Dominated patterns are removed. Pattern `p1` dominates `p2` when:

- `used(p1) subseteq used(p2)`;
- `gmin(p1) >= gmin(p2)`;
- `gmax(p1) <= gmax(p2)`.

## Trip-pattern preprocessing

A one-stop trip pattern is a pair `(s, p)` with `p in P_{v,s}`.

A two-stop trip pattern is `(s1, p1, s2, p2)` with:

- `s1 != s2`;
- `used(p1) intersect used(p2) = emptyset`;
- `gmin(p2) >= gmax(p1)`;
- combined liters do not exceed `Q_v`;
- combined payload does not exceed `W_v`.

This exactly enforces the compartment unloading rule: any `R` delivery occurs no earlier than all `M` deliveries, and any `F` delivery occurs no earlier than all `M` and `R` deliveries.

## Trip-slot candidate preprocessing

For each trip pattern `r` and loading slot `l`, simulate the trip from scratch:

- departure from depot at `start_l + loading_duration`;
- exact arrival propagation using `tau_{i,j}`;
- no hidden waiting is inserted in the exported artifact;
- departure from each station is arrival plus service time.

Candidate `c = (r,l)` is feasible only if:

- every station arrival satisfies `a_s <= arrival_s <= u_s`;
- return to depot is within `shift_end`;
- trip elapsed time is within `max_trip_elapsed`;
- the trip is compatible with at least one bay.

Each candidate has deterministic cost:

- vehicle fixed trip cost;
- distance cost;
- hazardous-route surcharge;
- preferred-window lateness penalty;
- loading-slot cost;
- a lower-bound bay usage term equal to the cheapest compatible bay cost.

Short-fill is not used in the final solver because exact-demand trip patterns are feasible for this instance.

## Master MIP

### Decision variables

- `x_c in {0,1}`: 1 if trip-slot candidate `c` is selected.

### Objective

Minimize

`sum_{c in C} cost_c x_c`

where `cost_c` contains all route-dependent costs except exact cleanout-transition cost, which is handled in the bay subproblem.

### Constraints

1. Station coverage

   For each station `s in S`:

   `sum_{c: s in stops(c)} x_c = 1`

2. Vehicle trip limit

   For each vehicle `v`:

   `sum_{c: veh(c)=v} x_c <= ntrip_v`

3. Rack capacity by loading slot

   For each slot `l`:

   `sum_{c: slot(c)=l} x_c <= slotcap_l`

4. Per-slot bay matching necessary conditions

   For each slot `l` and each nonempty subset `U subseteq B`:

   `sum_{c: slot(c)=l, compat_bays(c) subseteq U} x_c <= |U|`

   This is a Hall-type necessary condition ensuring that trips starting in the same slot can be injectively matched to bays.

5. Vehicle pairwise scheduling conflicts

   For incompatible candidate pairs `(c,d)` on the same vehicle:

   `x_c + x_d <= 1`

   A pair is incompatible if the earlier trip would not return and complete turnaround before the later load start, or if the span from the earlier load start to the later return exceeds the vehicle working-time limit.

## Bay-assignment MIP

After the master chooses a set `T` of trip-slot candidates, solve an exact sequencing model on the fixed trips.

### Additional interpretation for mixed-family trips

When a trip contains multiple hose families, the public statement does not define a single trip family. I use the least-cleanout feasible interpretation:

- a trip is bay-compatible only if the bay supports every hose family loaded on the trip;
- for two consecutive trips on the same bay, required cleanout time and cost are
  the minimum over all ordered family pairs `(f_prev, f_next)` where `f_prev` is loaded on the earlier trip and `f_next` is loaded on the later trip.

This reflects the operator choosing the within-trip loading order that minimizes the inter-trip cleanout requirement.

### Variables

- `y_{i,j,b} in {0,1}` for bay `b`, predecessor `i`, successor `j`.

Trips are time-ordered. `i=0` is a source node meaning that trip `j` is the first trip assigned to bay `b`.

### Objective

Minimize

`sum_{b in B} sum_{j in T} baycost_b y_{0,j,b} + sum_{b in B} sum_{i<j} (baycost_b + cleancost_{i,j}) y_{i,j,b}`

### Constraints

1. Each selected trip has exactly one incoming arc across all bays:

   `sum_{b in B} (y_{0,j,b} + sum_{i<j} y_{i,j,b}) = 1`

2. Each bay starts at most one chain:

   `sum_{j in T} y_{0,j,b} <= 1`

3. On each bay, a trip has at most one outgoing successor and only if it is assigned to that bay:

   `sum_{j>i} y_{i,j,b} <= y_{0,i,b} + sum_{h<i} y_{h,i,b}`

4. Arcs `(i,j,b)` exist only if both trips are bay-compatible and the slot-start gap is at least
   `loading_duration + cleantime_{i,j}`.

Because arcs only point forward in time, this network formulation is acyclic and needs no subtour constraints.

## Exported solution

From the chosen master candidates and the bay-assignment chains:

- `vehicle_id`, `trip_id`, `bay_id`, `load_start_min`;
- exact stop sequence with propagated arrivals and departures;
- exact deliveries by order, stop sequence, compartment, and liters.

All exported quantities are written with up to `1e-8` precision.
