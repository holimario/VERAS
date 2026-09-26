# Electric Medical Waste EV Routing Model

## Formulation Choice

The public instance has 44 clinics on a complete directed 56-node graph with
time windows, battery state, treatment-facility windows, incompatibility pairs,
hazard onboard-time limits, and compartmented loads. A monolithic arc-flow MIP
with time and battery state on every possible event index would be much larger
than needed for this single instance.

The instance also has strong geographic structure:

- west clinics are naturally tied to `F1`
- southwest and south clinics are naturally tied to `F3`
- east clinics are naturally tied to `F2`
- north clinics are naturally tied to `F5`
- only a small number of cross-facility reallocations are needed to satisfy
  facility daily and window capacities

Because of that structure, the implemented solver uses a **restricted
set-partitioning / pattern-selection model**:

1. Enumerate feasible vehicle-day route patterns for a small set of fixed
   vehicle-region groups.
2. Each pattern is a complete ordered event sequence for that group.
3. A SCIP master problem chooses one pattern per group while enforcing facility
   capacities, receiving-window limits, minimum throughput, and maximum number
   of open facilities.

In the performance phase, the pattern pool is widened for the flexible
single-vehicle groups. In particular, `EV1` and `EV2` are allowed to use up to
three unload trips when that improves the true audited objective.

This is much tighter and faster on the given instance than a naive event-indexed
day-wide routing MIP.

## Full Planning Model

### Sets

- `V`: vehicles
- `C`: clinics
- `F`: treatment facilities
- `S`: chargers
- `N`: all nodes
- `A = {(i,j) in N x N, i != j}`: directed travel arcs
- `W_f`: receiving windows at facility `f`
- `T_v`: ordered event positions on vehicle `v` in a generic event-indexed model

### Parameters

- `r_c, y_c`: red and yellow kg at clinic `c`
- `srv_c`: service duration at clinic `c`
- `e_c, l_c`: earliest and latest service start at clinic `c`
- `obr_c, oby_c`: max onboard time for red and yellow waste from clinic `c`
- `prio_c`: clinic priority class
- `Q_v, Qv^r, Qv^y`: total, red, yellow capacities for vehicle `v`
- `B_v, Bv^0`: battery capacity and initial battery
- `ss_v, se_v`: shift start and end
- `fc_v`: fixed vehicle-use cost
- `d_ij, tau_ij, en_ij^0, rho_ij, curfew_ij`: distance, travel time, empty-trip
  energy, population-risk index, and loaded-curfew indicator on arc `(i,j)`
- `alpha`: load-energy coefficient in kWh per kg-km
- `open_f`: facility opening cost
- `cap_f, cap_f^r, cap_f^y`: total, red, yellow daily capacities
- `latest_f`: latest receiving time
- `acc_f^r, acc_f^y`: facility acceptance indicators for red and yellow
- `tr_f^r, tr_f^y`: treatment cost per kg
- `L`: maximum number of open facilities
- `mthrough`: minimum throughput of an opened facility
- `ws_fw, we_fw, nu_fw, kg_fw`: start, end, unload-count cap, and kg cap of
  facility window `w` at facility `f`
- `rate_s, queue_s, cap_s, sess_s, price_s`: charger parameters
- `perm_vp`: vehicle permit for priority class `p`
- `iso_v`: daily isolation-pickup cap
- `inc_cc'`: incompatibility indicator for clinic pair `(c,c')`
- `k_travel, k_risk, beta_r, beta_y, beta_wait`: public objective weights

### Exact Event-Indexed Model

An exact day-wide model can be written with:

- `x_{v,t,n}` = 1 if event position `t` of vehicle `v` occurs at node `n`
- `p_{v,t,c}` = 1 if event `t` is pickup of clinic `c`
- `u_{v,t,f}` = 1 if event `t` is unload at facility `f`
- `g_{v,t,s}` = 1 if event `t` is charge at charger `s`
- `q_{v,t}` >= 0 charging kWh at event `t`
- `a_{v,t}` arrival time at event `t`
- `b_{v,t}` battery on arrival to event `t`
- `lr_{v,t}, ly_{v,t}` onboard red and yellow loads after event `t`
- `z_f` = 1 if facility `f` is opened
- `h_{fw}` = unload count in facility window `(f,w)`
- `kg_{fw}` = unload kg in facility window `(f,w)`

The main constraints are:

1. Every clinic is picked up exactly once.
2. Every used vehicle starts at its depot and ends empty at its end node.
3. Event-type consistency: each event is exactly one of pickup, unload, charge,
   or start/end.
4. Time propagation on arcs, including service times, charging times, queue
   times, and curfew waiting on loaded restricted arcs.
5. Clinic service windows.
6. Battery propagation:
   `b_next = b_now - en_ij^0 - alpha * d_ij * current_load + charge`.
7. Total, red, and yellow compartment capacities.
8. Unload empties all onboard waste.
9. Onboard-time limits for each picked clinic until its unload event.
10. Permit compatibility and daily isolation-pickup limits.
11. Incompatible clinic pairs cannot be onboard simultaneously between unloads.
12. Facility acceptance and daily total/red/yellow capacities.
13. Each unload assigned to exactly one valid receiving window.
14. Facility-window unload-count and kg capacities.
15. Facility opening logic, maximum number of open facilities, and minimum
    throughput of each opened facility.
16. Charger session limits and daily energy limits.

The objective is

`min vehicle fixed + facility opening + travel km cost + charging cost + treatment cost + onboard infection-risk cost`.

## Implemented Restricted Master Model

### Route-Pattern Groups

The solver fixes seven geographic service groups plus one mixed north/central
group:

- west on `EV5` and `EV7`
- south on `EV3` and `EV6`
- southwest on `EV1`
- east on `EV2`
- southeast on `EV4`
- north + central on `EV8`

For each group `g`, the code enumerates a set `P_g` of **feasible patterns**.
Each pattern already contains:

- the exact ordered event sequence
- exact travel, waiting, service, unload times, and battery evolution
- exact unload kg by facility and by facility window
- exact treatment kg and route objective contribution

### Pattern Variables

- `x_gp in {0,1}`: pattern `p` selected for group `g`
- `z_f in {0,1}`: facility `f` opened

### Pattern Coefficients

For pattern `p`:

- `cost_p`: exact route cost excluding facility opening
- `a_pf`: kg sent to facility `f`
- `a_pf^r, a_pf^y`: red and yellow kg sent to facility `f`
- `u_pfw`: unload count in facility window `(f,w)`
- `k_pfw`: unload kg in facility window `(f,w)`
- `delta_pf`: 1 if pattern `p` uses facility `f`

### Master Problem

Minimize

`sum_g sum_{p in P_g} cost_p x_gp + sum_f open_f z_f`

subject to

1. One pattern per group:

`sum_{p in P_g} x_gp = 1  for all g`

2. Facility daily capacities:

`sum_g sum_p a_pf x_gp <= cap_f`

`sum_g sum_p a_pf^r x_gp <= cap_f^r`

`sum_g sum_p a_pf^y x_gp <= cap_f^y`

3. Facility-window capacities:

`sum_g sum_p u_pfw x_gp <= nu_fw`

`sum_g sum_p k_pfw x_gp <= kg_fw`

4. Opening logic:

`sum_g sum_p a_pf x_gp <= cap_f z_f`

`sum_g sum_p a_pf x_gp >= mthrough z_f`

5. Maximum number of open facilities:

`sum_f z_f <= L`

This master problem is what `solve.py` implements in SCIP.

## Why This Works Here

- The hard clinic-level coupling is handled inside exact pattern generation.
- The remaining day-wide coupling is almost entirely at facilities and receiving
  windows, which the master MIP captures exactly.
- The instance is small enough that exhaustive intra-trip permutation and
  partition search is tractable once the geography is exploited.
- The resulting model is much tighter than a naive assignment-plus-repair model
  and much smaller than a full unrestricted route-event MIP.
