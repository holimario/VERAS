## Disaster Relief Prepositioning Model

### Formulation choice

This instance is best modeled as a **two-stage extensive-form stochastic network-flow MIP**:

- First stage: open warehouses and pre-position inventory before the scenario is known.
- Second stage: for each public scenario and response period, route inventory, use limited fleet capacity, procure emergency stock locally, and leave unmet demand if necessary.

The instance is small enough for a direct extensive-form model to be stronger and simpler than decomposition:

- 9 candidate warehouses with only binary first-stage opening decisions.
- Continuous recourse after fixing openings/inventory.
- Scenario coupling only through first-stage decisions and the CVaR tail-risk term.

### Sets and indices

- \(W\): warehouses, index \(w\)
- \(Z\): zones, index \(z\)
- \(I\): items, index \(i\)
- \(S\): scenarios, index \(s\)
- \(P\): response periods in time order, index \(p\)
- \(F\): fleet types, index \(f\)
- \(L \subseteq W \times Z\): allowed warehouse-zone lanes
- \(C \subseteq Z\): critical zones
- \(N \subseteq Z\): non-critical zones, meaning standard or remote
- \(A \subseteq S \times P \times W \times Z \times I \times F\): shipment tuples that are operationally feasible

The feasible shipment set \(A\) keeps the model tight:

- \((w,z)\) must be a listed lane.
- If item \(i\) requires cold chain, then fleet \(f\) must be cold-chain capable.
- Let `road_class(w,z)` be the lane class and `travel(s,p,w,z)` the scenario-period travel time.
- For road classes in \(\{\text{mountain}, \text{bridge}, \text{coastal}\}\), `light_utility` and `airlift` are always allowed.
- Otherwise, and for heavier fleets on those rough classes, fleet \(f\) is allowed only if
  \[
  \text{travel}(s,p,w,z) \le \text{max\_travel\_time}(f).
  \]

### Parameters

From the public data:

- \(c^{open}_w\): warehouse opening cost
- \(cap^{stor}_w\): storage capacity
- \(cap^{out}_w\): outbound handling capacity
- \(cold_w \in \{0,1\}\): warehouse cold-chain capability
- \(vol_i\): storage volume per unit
- \(load_i\): load factor per unit
- \(c^{stock}_i\): pre-positioning cost per unit
- \(c^{proc}_i\): emergency procurement cost per unit
- \(c^{short}_i\): shortage penalty per unit
- \(coldreq_i \in \{0,1\}\): item cold-chain requirement
- \(ub^{stock}_i\): max stock per warehouse for item \(i\)
- \(d_{szi}\): demand
- \(p_s\): scenario probability
- \(cap^{proc}_{si}\): scenario-level procurement cap for item \(i\)
- \(\gamma_z\): zone emergency-procurement fraction cap
- \(floor_{zi}\): final service floor fraction
- \(target_{pri(i)}\): cumulative fraction of the final service floor required by the end of period \(p\), based on zone priority class and item
- \(cap^{lane}_{wz}\): base lane capacity
- \(mult^{lane}_{spwz}\): scenario-period lane capacity multiplier
- \(c^{trans}_{wz}\): base transport cost per unit load
- \(\Delta c^{trans}_{spwz}\): extra scenario-period transport cost per unit load
- \(travel_{spwz}\): scenario-period travel time
- \(use_{sw}\): usable fraction of pre-positioned stock after warehouse damage
- \(mult^{out}_{sw}\): scenario outbound multiplier
- \(trips_{wpf}\): available trips (trip equivalents)
- \(cap^{fleet}_f\): load capacity per trip
- \(c^{trip}_f\): dispatch cost per trip equivalent
- \(latepen\): late-response penalty per load-hour
- \(\mu_p\): period delay penalty multiplier
- \(T_z\): target response time in hours
- \(B\): planning budget
- \(W^{max}\): max number of open warehouses
- \(W^{cold,min}\): minimum number of open cold-chain warehouses
- \(g^{fair}\): fairness gap
- \(\alpha\): tail-risk confidence level
- \(\lambda\): tail-risk weight

### Decision variables

First stage:

- \(y_w \in \{0,1\}\): 1 if warehouse \(w\) is open
- \(x_{wi} \ge 0\): units of item \(i\) pre-positioned at warehouse \(w\)

Second stage:

- \(q_{spwzif} \ge 0\) for \((s,p,w,z,i,f)\in A\): shipped quantity
- \(r_{spzi} \ge 0\): emergency procurement quantity
- \(u_{spzi} \ge 0\): unmet-demand quantity assigned to period \(p\)
- \(h_{spzi} \ge 0\): cumulative delivered quantity through the end of period \(p\)
- \(\tau_{spwf} \ge 0\): trip equivalents used from warehouse \(w\), period \(p\), fleet \(f\), in scenario \(s\)

Tail-risk variables:

- \(\eta \in \mathbb{R}\): CVaR threshold
- \(\xi_s \ge 0\): excess scenario operating cost above \(\eta\)

### Objective

Minimize first-stage cost plus expected operating cost plus a CVaR tail-risk charge:

\[
\min \quad
\sum_{w \in W} c^{open}_w y_w
 + \sum_{w \in W}\sum_{i \in I} c^{stock}_i x_{wi}
 + \sum_{s \in S} p_s \, OC_s
 + \lambda \left(\eta + \frac{1}{1-\alpha}\sum_{s \in S} p_s \xi_s\right)
\]

where \(OC_s\) is the operating cost in scenario \(s\):

\[
OC_s =
\sum_{p,w,z,i,f:(s,p,w,z,i,f)\in A}
\left(c^{trans}_{wz} + \Delta c^{trans}_{spwz} + c^{delay}_{spwzi}\right) load_i \, q_{spwzif}
 + \sum_{p,w,f} c^{trip}_f \tau_{spwf}
 + \sum_{p,z,i} c^{proc}_i r_{spzi}
 + \sum_{p,z,i} c^{short}_i u_{spzi}
\]

and

\[
c^{delay}_{spwzi}
= latepen \cdot \mu_p \cdot
\max\left(0, start_p + travel_{spwz} - T_z\right).
\]

Assumption: the tail-risk term is applied to **scenario-dependent operating cost only**, not to first-stage cost, to avoid double-counting common pre-disaster spending.

### Constraints

#### 1. Warehouse opening and stocking

Open-count and cold-chain requirements:

\[
\sum_{w \in W} y_w \le W^{max}
\]

\[
\sum_{w \in W} cold_w y_w \ge W^{cold,min}
\]

Planning budget:

\[
\sum_{w \in W} c^{open}_w y_w + \sum_{w \in W}\sum_{i \in I} c^{stock}_i x_{wi} \le B
\]

Per-warehouse storage:

\[
\sum_{i \in I} vol_i x_{wi} \le cap^{stor}_w y_w \qquad \forall w \in W
\]

Per-item stock bounds and cold-chain storage:

\[
x_{wi} \le ub^{stock}_i y_w \qquad \forall w \in W,\ i \in I
\]

\[
x_{wi} = 0 \qquad \forall w \in W,\ i \in I \text{ with } coldreq_i = 1,\ cold_w = 0
\]

#### 2. Cumulative service accounting

For the first period \(p_1\):

\[
h_{sp_1zi}
 =
r_{sp_1zi}
 + \sum_{w,f:(s,p_1,w,z,i,f)\in A} q_{sp_1wzif}
\qquad \forall s,z,i
\]

For each later period \(p\) with predecessor \(p^-\):

\[
h_{spzi}
 =
h_{sp^-zi}
 + r_{spzi}
 + \sum_{w,f:(s,p,w,z,i,f)\in A} q_{spwzif}
\qquad \forall s,z,i,p \neq p_1
\]

Demand completion by the end of the horizon:

\[
h_{s,p_{last},z,i} + \sum_{p \in P} u_{spzi} = d_{szi}
\qquad \forall s,z,i
\]

#### 3. Service-floor schedule

For every scenario, period, zone, and item:

\[
h_{spzi}
\ge
target_{p,priority(z),i} \cdot floor_{zi} \cdot d_{szi}
\qquad \forall s,p,z,i
\]

This enforces the cumulative response targets, including the final floor at \(P3\).

#### 4. Fairness across critical and non-critical zones

For every scenario \(s\), item \(i\), critical zone \(z_c \in C\), and non-critical zone \(z_n \in N\):

\[
\frac{h_{s,p_{last},z_c,i}}{d_{sz_ci}}
 + g^{fair}
\ge
\frac{h_{s,p_{last},z_n,i}}{d_{sz_ni}}
\]

Since all demands are positive constants, the implementation uses the linear equivalent:

\[
d_{sz_ni} \, h_{s,p_{last},z_c,i}
 - d_{sz_ci} \, h_{s,p_{last},z_n,i}
\ge
- g^{fair} \, d_{sz_ci} \, d_{sz_ni}.
\]

#### 5. Emergency procurement limits

Zone-level local procurement cap:

\[
\sum_{p \in P} r_{spzi} \le \gamma_z d_{szi}
\qquad \forall s,z,i
\]

Scenario-level item procurement cap:

\[
\sum_{p \in P}\sum_{z \in Z} r_{spzi} \le cap^{proc}_{si}
\qquad \forall s,i
\]

#### 6. Inventory usability and warehouse outbound capacity

Usable pre-positioned stock under warehouse damage:

\[
\sum_{p \in P}\sum_{z \in Z}\sum_{f:(s,p,w,z,i,f)\in A} q_{spwzif}
\le
use_{sw} x_{wi}
\qquad \forall s,w,i
\]

Scenario-adjusted outbound handling capacity:

\[
\sum_{p \in P}\sum_{z \in Z}\sum_{i \in I}\sum_{f:(s,p,w,z,i,f)\in A} load_i q_{spwzif}
\le
cap^{out}_w \, mult^{out}_{sw} \, y_w
\qquad \forall s,w
\]

#### 7. Lane capacities

For each scenario, period, and listed lane:

\[
\sum_{i \in I}\sum_{f:(s,p,w,z,i,f)\in A} load_i q_{spwzif}
\le
cap^{lane}_{wz} \, mult^{lane}_{spwz}
\qquad \forall s,p,(w,z)\in L
\]

#### 8. Fleet-trip capacity

Used trip equivalents are limited by warehouse-period availability:

\[
\tau_{spwf} \le trips_{wpf} y_w
\qquad \forall s,p,w,f
\]

Load moved by a warehouse-period-fleet combination cannot exceed trip capacity:

\[
\sum_{z \in Z}\sum_{i \in I:(s,p,w,z,i,f)\in A} load_i q_{spwzif}
\le
cap^{fleet}_f \tau_{spwf}
\qquad \forall s,p,w,f
\]

#### 9. CVaR linearization

\[
\xi_s \ge OC_s - \eta \qquad \forall s \in S
\]

\[
\xi_s \ge 0 \qquad \forall s \in S
\]

### Output mapping

The final CSV uses the required schema:

- `warehouse` rows: one per warehouse with `decision` in `{open, closed}` from \(y_w\)
- `inventory` rows: one per warehouse-item pair with `quantity = x_{wi}` and `decision = stock`
- `ship` rows: one per positive \(q_{spwzif}\)
- `procure` rows: one per scenario-period-zone-item with `quantity = r_{spzi}`
- `unmet` rows: one per scenario-period-zone-item with `quantity = u_{spzi}`

Quantities are exported with 8 decimal places.

### Notes on interpretation

- The model is **scenario-feasible**, not average-feasible: the same first-stage decisions must support feasible recourse in every listed public scenario.
- Lane capacity multipliers and warehouse outbound multipliers are enforced in every scenario.
- Delay cost uses the earliest arrival within a period: `period start hour + scenario-period travel time`.
- Emergency procurement contributes to service targets and demand satisfaction but does not use fleet or lane capacity.
