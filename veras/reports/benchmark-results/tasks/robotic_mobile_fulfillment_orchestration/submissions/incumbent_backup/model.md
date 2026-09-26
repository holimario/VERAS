# Robotic Mobile Fulfillment Orchestration

## Modeling choice

The public instance is a coupled warehouse scheduling problem with:

- assignment structure: each order must choose one station and a limited set of pods;
- sequencing/no-overlap structure: pods, robots, aisles, and stations are shared over time;
- small horizon: 72 discrete five-minute slots;
- moderate size: 42 orders, 30 pods, 6 robots, 3 stations.

An integrated visit-level MIP with robot sequencing, battery state, and cross-resource
event timing is possible but unnecessarily heavy for this instance. The implemented
solver therefore uses a **decomposition with an exact MIP core**:

1. enumerate the best local pod pattern for each `(order, station)` pair;
2. solve a small master MIP that chooses one station-pattern per order;
3. solve a time-indexed order-start MIP on the chosen station assignment;
4. run an exact forward scheduler that realizes every visit with robots, pods,
   aisle capacities, maintenance, cleaning gaps, and battery feasibility;
5. validate the exported artifact independently from `solution.csv`.

This preserves the hard constraints exactly in the final artifact while keeping the
MIP size small and strong.

## Time model assumption

The prompt gives coordinates, slot length, and energy per distance, but no explicit
robot speed. The implemented solver uses the public and explicit assumption

\[
\text{travel\_slots}(p,s)=\max\left(1,\left\lceil \frac{\text{ManhattanDistance}(z(p),s)}{10}\right\rceil\right),
\]

so one five-minute slot corresponds to 10 coordinate-distance units of loaded pod
movement. A visit duration is

\[
d_v = 2\cdot \text{travel\_slots}(p(v),s(v)) + \max\left(1,\left\lceil \frac{\text{pick\_minutes}_v}{5}\right\rceil\right).
\]

The round trip distance also drives visit travel and energy.

## Sets and indices

- \(O\): orders.
- \(S\): stations.
- \(P\): pods.
- \(R\): robots.
- \(G\): promise groups.
- \(T=\{0,\dots,H-1\}\): slots, with \(H=72\).
- \(K_o\): feasible station-patterns for order \(o\). In the implementation there is
  one locally optimal pod pattern per eligible station.
- \(V_o\): ordered visits generated for order \(o\) after a pattern is chosen.

## Parameters

- \(r_o\): release slot of order \(o\).
- \(d_o\): due slot of order \(o\).
- \(m_o\): maximum number of pods allowed for order \(o\).
- \(a^{cold}_s,a^{haz}_s\in\{0,1\}\): station certifications.
- \(q_{o\ell}\): required quantity on order line \(\ell\) of order \(o\).
- \(inv_{p,\sigma}\): inventory of SKU \(\sigma\) on pod \(p\).
- \(frag_\sigma\in\{0,1\}\): SKU \(\sigma\) is fragile.
- \(heavy_p\in\{0,1\}\): pod \(p\) is heavy.
- \(u_\sigma\): unit pick time in minutes.
- \(cap^{visit}_s\): maximum picked units in one visit at station \(s\).
- \(cap^{stat}_s\): simultaneous pod-visit capacity of station \(s\).
- \(cap^{aisle}_{a,t}\): aisle-group capacity in slot \(t\).
- \(buf^{pod}_p\): return buffer after a visit to pod \(p\).
- \(buf^{clean}\): hazmat cleaning buffer in slots.
- \(B_r^{0},B_r^{min},B_r^{max}\): initial, reserve, and capacity battery of robot \(r\).
- \(e_r\): energy used per distance unit.
- \(shift_r=[\underline h_r,\bar h_r)\): robot shift.
- \(M_r\): maintenance intervals for robot \(r\).
- \(w^{tard},w^{prio},w^{split},w^{travel},w^{energy},w^{imb},w^{spread}\): public objective weights from `config.json`.

For every feasible pattern \(k\in K_o\):

- \(s_{ok}\): station used by pattern \(k\).
- \(c^{travel}_{ok}\): pattern travel distance cost.
- \(c^{energy}_{ok}\): pattern energy cost.
- \(c^{split}_{ok}\): extra-pod count in the pattern.
- \(L_{ok}\): estimated order processing load for the order-start model.
- \(use_{ok,p,\sigma}\): quantity of SKU \(\sigma\) taken from pod \(p\) by the pattern.

For every realized visit \(v\):

- \(o(v)\): order of visit \(v\).
- \(p(v)\): pod of visit \(v\).
- \(s(v)\): station of visit \(v\).
- \(d_v\): visit duration.
- \(a(v)\): aisle group of the pod home zone.
- \(haz_v\in\{0,1\}\): visit is hazmat.

## Stage 1: Station-pattern master MIP

Decision variables:

- \(y_{ok}\in\{0,1\}\): choose pattern \(k\) for order \(o\).
- \(load_s\ge 0\): estimated total assigned load at station \(s\).
- \(\Delta_{s,s'}\ge 0\): absolute pairwise station-load difference.

Objective:

\[
\min
\sum_{o\in O}\sum_{k\in K_o}
\left(
w^{split} c^{split}_{ok}
 w^{travel} c^{travel}_{ok}
 w^{energy} c^{energy}_{ok}
\right) y_{ok}
 w^{imb}\sum_{s<s'} \Delta_{s,s'}.
\]

Constraints:

Choose one pattern per order:

\[
\sum_{k\in K_o} y_{ok}=1 \qquad \forall o\in O.
\]

Shared pod inventory:

\[
\sum_{o\in O}\sum_{k\in K_o} use_{ok,p,\sigma}\, y_{ok}
\le inv_{p,\sigma}
\qquad \forall p\in P,\ \forall \sigma.
\]

Station-load definitions:

\[
load_s=\sum_{o\in O}\sum_{k\in K_o:\ s_{ok}=s} L_{ok}\, y_{ok}
\qquad \forall s\in S.
\]

Absolute imbalance linearization:

\[
\Delta_{s,s'} \ge load_s-load_{s'},\qquad
\Delta_{s,s'} \ge load_{s'}-load_s
\qquad \forall s<s'.
\]

The pattern generation itself enforces:

- station eligibility for cold and hazmat orders;
- fragile-SKU exclusion from heavy pods;
- per-order split limit;
- exact line coverage inside each chosen pattern.

## Stage 2: Time-indexed order-start MIP

After Stage 1, each order has one chosen station and one explicit sequence of visits.
Let \(D_o=\sum_{v\in V_o} d_v\) be the nominal block duration of order \(o\).

Decision variables:

- \(x_{o,t}\in\{0,1\}\): order \(o\) starts at slot \(t\).
- \(C_o\ge 0\): completion slot of order \(o\).
- \(T_o\ge 0\): tardiness of order \(o\).
- \(P_o\ge 0\): priority excess lateness for priority orders.
- \(U_g,L_g\ge 0\): latest and earliest completion in promise group \(g\).
- \(S_g\ge 0\): promise-group spread excess.

Objective:

\[
\min
w^{tard}\sum_{o\in O} T_o
 w^{prio}\sum_{o\in O^{prio}} P_o
 w^{spread}\sum_{g\in G} S_g
 \varepsilon \sum_{o\in O} C_o,
\]

with a small \(\varepsilon>0\) to compact the schedule among equal tardiness solutions.

Constraints:

Choose one start:

\[
\sum_{t=r_o}^{H-D_o} x_{o,t}=1 \qquad \forall o\in O.
\]

Station capacity:

\[
\sum_{o:\ s(o)=s}\sum_{t:\ \tau\in[t,t+D_o)} x_{o,t}
\le cap^{stat}_s
\qquad \forall s\in S,\ \forall \tau\in T.
\]

Completion definition:

\[
C_o=\sum_{t=r_o}^{H-D_o} (t+D_o)\,x_{o,t}
\qquad \forall o\in O.
\]

Tardiness:

\[
T_o\ge C_o-d_o,\qquad T_o\ge 0
\qquad \forall o\in O.
\]

Priority excess lateness:

\[
P_o \ge T_o - \bar L^{prio},\qquad P_o\ge 0
\qquad \forall o\in O^{prio},
\]

where \(\bar L^{prio}\) is the public threshold from `config.json`.

Promise-group spread:

\[
U_g \ge C_o,\qquad L_g \le C_o \qquad \forall g\in G,\ o\in g,
\]
\[
S_g \ge U_g-L_g-\text{targetSpread}_g,\qquad S_g\ge 0
\qquad \forall g\in G.
\]

## Stage 3: Exact forward realization

The MIPs above provide the chosen station-pod pattern and an order-start blueprint.
The final artifact is built by an exact scheduler that processes orders lane by lane
and schedules each visit sequentially subject to all hard public rules:

- pod availability and return buffers;
- aisle-capacity event sweep on half-open intervals;
- robot shifts;
- maintenance windows;
- robot non-overlap;
- robot location continuity via deadhead travel between consecutive visits;
- hazmat cleaning gap before the next non-hazmat visit;
- battery reserve after every visit;
- order release and one-station consistency.

No charge rows are emitted unless charging is actually required; on this instance the
solver keeps all robot batteries above reserve without charging.

## Final validation from `solution.csv`

After export, the solver independently recomputes from the artifact:

- exact line fulfillment;
- pod inventory consumption;
- one-station and split-limit compliance per order;
- station certification rules;
- fragile/heavy compatibility;
- station interval occupancy;
- station open-order windows;
- pod no-overlap and return buffers;
- aisle capacities;
- robot shift, maintenance, non-overlap, location continuity, cleaning, and battery.

The submission is accepted by the solver only if this audit returns zero hard-constraint
violations.
