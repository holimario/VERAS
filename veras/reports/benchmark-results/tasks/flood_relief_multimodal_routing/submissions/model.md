# Flood Relief Multimodal Routing Model

## Modeling choice

The instance is best represented as a **hybrid set-partitioning + optional-route scheduling model**:

- The truck decision is an **optional-node routing problem** on the staging-point network.
- Flooded-shelter boat service is naturally a **set-partitioning problem over feasible boat trips**, because a boat trip serves an ordered subset of shelters from one ramp and the exported artifact must respect the exact listed water arcs.
- Drone decisions are simple **assignment choices** from feasible launch nodes.

A naive arc-flow formulation over truck moves, drone launches, and full boat sequencing would be weak and unnecessarily large. Instead, I preprocess:

1. all-pairs shortest truck travel times between relevant staging points;
2. all feasible single-ramp boat trips that already satisfy capacity and duration;
3. node-level service patterns that combine mandatory dry deliveries with either:
   - a subset of feasible drone launches from that node, or
   - one feasible boat trip from that ramp.

This produces a compact MIP with strong coverage structure and explicit timing.

## Sets and indices

- \(N\): candidate staging points that may host service activity.
- \(D0\): depot.
- \(N^M \subseteq N\): mandatory staging points induced by dry shelters.
- \(N^O = N \setminus N^M\): optional staging points.
- \(H^{dry}\): dry shelters.
- \(H^{fld}\): flooded shelters.
- \(P\): weather periods.
- \(R_i\): feasible service patterns for node \(i \in N\).

Each pattern \(r \in R_i\) is precomputed and contains:

- the set of flooded shelters covered by that pattern, \(F_{ir} \subseteq H^{fld}\);
- whether it is a drone pattern or a boat pattern;
- for each period \(p\), a total stop duration \(dur_{irp}\);
- for each flooded shelter \(h \in F_{ir}\) served by boat, an in-trip service offset \(off_{irph}\) from truck arrival at node \(i\).

Mandatory dry shelters assigned to node \(i\) are denoted by \(M_i \subseteq H^{dry}\).

## Parameters

- \(c_{ij}\): shortest truck travel time from staging point \(i\) to \(j\), computed on the road network.
- \(a_p, b_p\): start and end of weather period \(p\). For the last period, \(b_p\) is treated as \(+\infty\).
- \(d_h\): shelter deadline.
- \(w_h\): shelter priority weight.
- \(\alpha\): lateness penalty per weighted minute.
- \(U\): global time upper bound used in big-\(M\) timing constraints.

Pattern preprocessing uses the public data as follows:

- direct-truck processing at node \(i\):
  \[
  truck\_time_i = 0.18 \cdot \sum_{h \in M_i} demand_h
  \]
- fixed node overhead:
  \[
  base_i = 5 + handling_i
  \]
- drone-trip duration in period \(p\):
  \[
  droneDur_{h i p} = 6 + \mu^{dr}_p \cdot flight_{hi} + 3 \cdot risk_{hi}
  \]
- boat-trip duration in period \(p\):
  \[
  boatDur_{t i p} = 10 + \mu^{bt}_p \cdot waterTravel_{ti} + 4 \cdot |t|
  \]

Boat trips are generated only if they satisfy:

- total boat load \(\le 96\) kg;
- worst-case weather duration using multiplier \(1.12\) is \(\le 115\) minutes;
- every launch-to-first, shelter-to-shelter, and last-to-launch hop is a listed `boat_travel.csv` edge (undirected).

## Decision variables

- \(y_{irp} \in \{0,1\}\): node \(i\) uses service pattern \(r\) and arrives in weather period \(p\).
- \(v_i \in \{0,1\}\): node \(i\) is visited by the truck.
- \(x_{ij} \in \{0,1\}\): truck goes directly from relevant node \(i\) to relevant node \(j\) in the route on the shortest-path closure, where \(i,j \in N \cup \{D0\}\), \(i \ne j\).
- \(A_i \ge 0\): truck arrival time at node \(i\).
- \(D_i \ge 0\): truck departure time from node \(i\).
- \(T_h \ge 0\): service completion time of shelter \(h\).
- \(L_h \ge 0\): lateness of shelter \(h\).
- \(C \ge 0\): truck route completion time at return to depot.

## Objective

Minimize completion time plus weighted lateness:

\[
\min \; C + \alpha \sum_{h \in H^{dry} \cup H^{fld}} w_h L_h
\]

where

\[
L_h \ge T_h - d_h, \quad L_h \ge 0
\]

## Constraints

### 1. Pattern selection and node activation

Mandatory dry-service nodes must choose exactly one active pattern-period pair:

\[
\sum_{r \in R_i} \sum_{p \in P} y_{irp} = 1 \quad \forall i \in N^M
\]

Optional nodes may be unused:

\[
\sum_{r \in R_i} \sum_{p \in P} y_{irp} = v_i \quad \forall i \in N^O
\]

For mandatory nodes, \(v_i = 1\).

### 2. Flooded-shelter coverage

Every flooded shelter is served exactly once by exactly one selected pattern:

\[
\sum_{i \in N} \sum_{r \in R_i : h \in F_{ir}} \sum_{p \in P} y_{irp} = 1
\quad \forall h \in H^{fld}
\]

Dry shelters are fixed to their listed dry node and are automatically served by the selected pattern of that node.

### 3. Weather-period consistency

If pattern-period \((i,r,p)\) is selected, arrival time must lie in that period:

\[
A_i \ge a_p - U(1-y_{irp})
\]
\[
A_i \le b_p + U(1-y_{irp})
\]

For the last period, \(b_p\) is replaced by \(U\).

### 4. Stop duration

Departure equals arrival plus the duration of the selected node pattern:

\[
D_i = A_i + \sum_{r \in R_i} \sum_{p \in P} dur_{irp} \, y_{irp}
\quad \forall i \in N
\]

Inactive optional nodes are forced to zero time:

\[
A_i \le U v_i, \quad D_i \le U v_i \quad \forall i \in N^O
\]

### 5. Truck route degree constraints

Exactly one departure from and one return to the depot:

\[
\sum_{j \in N} x_{D0,j} = 1, \qquad
\sum_{i \in N} x_{i,D0} = 1
\]

Visited nodes have one predecessor and one successor:

\[
\sum_{j \in N \cup \{D0\}\setminus\{i\}} x_{ij} = v_i
\quad \forall i \in N
\]
\[
\sum_{j \in N \cup \{D0\}\setminus\{i\}} x_{ji} = v_i
\quad \forall i \in N
\]

### 6. Truck timing / subtour elimination

From depot to first visited node:

\[
A_j \ge c_{D0,j} - U(1-x_{D0,j})
\quad \forall j \in N
\]

Between visited nodes:

\[
A_j \ge D_i + c_{ij} - U(1-x_{ij})
\quad \forall i,j \in N, i \ne j
\]

Because all travel times and active stop durations are positive, these timing constraints eliminate disconnected subtours.

Completion time on return to depot:

\[
C \ge D_i + c_{i,D0} - U(1-x_{i,D0})
\quad \forall i \in N
\]

### 7. Shelter service-time linking

For every dry shelter \(h \in M_i\):

\[
T_h = D_i
\]

For every flooded shelter \(h\) that is served by a drone pattern at node \(i\):

\[
T_h \ge D_i - U\left(1-\sum_{r \in R_i: h \in F_{ir},\, r \text{ drone}} \sum_{p \in P} y_{irp}\right)
\]
\[
T_h \le D_i + U\left(1-\sum_{r \in R_i: h \in F_{ir},\, r \text{ drone}} \sum_{p \in P} y_{irp}\right)
\]

For every flooded shelter \(h\) served by a boat pattern:

\[
T_h \ge A_i + off_{irph} - U(1-y_{irp})
\]
\[
T_h \le A_i + off_{irph} + U(1-y_{irp})
\]

for each \((i,r,p)\) whose boat pattern contains shelter \(h\).

### 8. Lateness

\[
L_h \ge T_h - d_h, \qquad L_h \ge 0
\quad \forall h
\]

## Export policy

After solving:

1. Recover the selected activity nodes and truck route on the shortest-path closure.
2. Expand each closure arc into the exact road-edge path and write those nodes as `truck_stop` rows.
3. Export:
   - dry shelters as `mode=truck` at their fixed staging point;
   - drone shelters as one row per shelter with its selected launch node;
   - boat shelters in the exact selected trip order with `sequence` inside each trip.

If shortest-path expansion passes through a previously active staging point, that
staging point may reappear as a transit `truck_stop`. Service is credited at the
first visited occurrence, and the exported route is audited directly on the exact
road-edge sequence.

## Explicit assumptions used by the solver

- Truck routing is optimized on shortest-path closure and exported back to direct road edges.
- At most one boat trip is launched from any ramp.
- A ramp stop uses either drone launches or one boat trip, not both.
- Dry-shelter and drone service times are credited at truck departure from the serving node.
- Boat service times are credited along the exported in-trip sequence.
- Weather multipliers are determined by the truck arrival period at the launch node; arrivals after the last listed period use the last period's multiplier.
- Repeated staging-point rows in the exported truck path are allowed when they arise from exact road-edge expansion of closure arcs; they are treated as transit revisits unless that node's service activity is first encountered there.

These assumptions keep the model consistent with the public schema and yield a compact exact MIP for the chosen benchmark interpretation.
