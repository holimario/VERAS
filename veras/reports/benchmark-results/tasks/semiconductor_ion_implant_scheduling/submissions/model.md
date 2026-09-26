# Mathematical Model

## Modeling choice

The instance is a short-horizon continuous-time scheduling problem with:

- alternative qualified machines,
- sequence-dependent setup times,
- reticle and operator shared resources,
- fixed maintenance windows,
- implant chamber-load resets with optional clean events,
- queue-time limits between consecutive operations,
- FOUP buffer occupancy between implant and furnace,
- optional furnace batching,
- soft lot tardiness, flow-time, cleaning, setup, batch-start, and family-commitment penalties.

The strongest exact representation for this instance class is a continuous-time disjunctive model with:

- assignment binaries for operation-machine choices,
- start-time variables,
- pairwise precedence binaries on every conflicting machine or shared resource pair,
- explicit optional clean tasks on implant tools,
- feasible furnace-batch set-partitioning variables,
- cumulative-calendar checks for operator and buffer windows.

The implemented solver uses a staged constructive heuristic plus an exact exported-schedule audit, but the benchmark objective is represented by the exact model below.

## Sets

- \(L\): lots.
- \(O_l\): ordered operations of lot \(l\).
- \(O = \cup_l O_l\): all production operations.
- \(O^I, O^F, O^M\): implant, furnace, and metrology operations.
- \(M\): machines.
- \(M_o \subseteq M\): machines qualified for operation \(o\) and not forbidden by assignment-expiry data.
- \(M^I, M^F, M^M\): implant, furnace, and metrology machines.
- \(R\): reticles.
- \(O_r \subseteq O^I\): implant operations using reticle \(r\).
- \(K\): operator skills.
- \(W_k\): calendar windows for skill \(k\), with capacity \(cap_{kw}\).
- \(Z\): FOUP buffer zones.
- \(W_z\): calendar windows for zone \(z\), with capacity \(cap_{zw}\).
- \(FAM\): product families.
- \(B\): feasible furnace batches. Each batch \(b \in B\) is a compatible subset of furnace operations sharing recipe and batch group, with total wafers within capacity.
- \(B_o \subseteq B\): feasible batches that contain furnace operation \(o\).
- \(C_m\): candidate clean tasks on implant machine \(m\). In an exact model these are optional reset tasks inserted between consecutive implant operations on \(m\).

## Parameters

- \(rel_l\), \(due_l\), \(prio_l\), \(hot_l\), \(wafer_l\): release, due date, priority weight, hot-lot flag, and wafer count of lot \(l\).
- \(p_{om}\): processing time of operation \(o\) on machine \(m\), including the machine multiplier.
- \(q^-_o, q^+_o\): minimum and maximum wait after the previous operation of \(o\); \(q^+_o\) may be absent.
- \(exp_{om}\): latest allowed start if \(o\) is assigned to \(m\).
- \(setup_{m,ab}\): setup time on machine \(m\)'s station type when setup family changes from \(a\) to \(b\).
- \(load_o = wafer_{l(o)} \cdot risk_o\): chamber-load increment of implant operation \(o\).
- \(thr_m\), \(clean_m\): implant clean threshold and clean duration for implant machine \(m\).
- \(opmin_o\): operator time consumed at the start of operation \(o\).
- \(skill_o\): operator skill required by operation \(o\).
- \(ret_o\): reticle required by operation \(o\), if any.
- \(zone_l\): FOUP buffer zone used by lot \(l\) between its implant and following furnace operation.
- \(c^{late}, c^{flow}, c^{setup}, c^{batch}, c^{clean}\): objective weights from `config.json`.
- \(mult^{hot}\): tardiness multiplier for hot lots.
- \(commit_f, minlots_f, pen_f\): family completion commitment time, minimum committed completed lots, and penalty per missing lot for family \(f\).
- Fixed maintenance windows and reticle holds are modeled as unavailable intervals.

## Decision variables

- \(x_{om} \in \{0,1\}\): 1 if operation \(o\) is assigned to machine \(m \in M_o\).
- \(s_o \ge 0\): start time of operation \(o\).
- \(e_o \ge 0\): end time of operation \(o\).
- \(y_{ijm} \in \{0,1\}\): for conflicting operations \(i,j\) that can both use machine \(m\), 1 if \(i\) precedes \(j\) on \(m\).
- \(u_{ijr} \in \{0,1\}\): reticle-order binary for implant operations \(i,j \in O_r\).
- \(g_c \in \{0,1\}\): 1 if candidate implant clean task \(c\) is used.
- \(s_c, e_c\): start and end of clean task \(c\).
- \(h_b \in \{0,1\}\): 1 if feasible furnace batch \(b\) is used.
- \(S_b, E_b\): batch start and end.
- \(a_{ob} \in \{0,1\}\): 1 if furnace operation \(o\) is assigned to batch \(b \in B_o\).
- \(T_l \ge 0\): tardiness of lot \(l\).
- \(F_l \ge 0\): flow time of lot \(l\).
- \(miss_f \ge 0\): missing lots versus family commitment for family \(f\).
- \(done_{lf} \in \{0,1\}\): 1 if lot \(l\) of family \(f\) completes by \(commit_f\).

## Objective

Minimize

\[
\sum_{l \in L} c^{flow} \, prio_l \, F_l
+ \sum_{l \in L} c^{late} \, prio_l \, (mult^{hot})^{hot_l} \, T_l
+ c^{setup} \sum_{m \in M} \sum_{(i,j)} setup_{m,fam_i,fam_j}\, y_{ijm}^{chg}
+ c^{batch} \sum_{b \in B} h_b
+ c^{clean} \sum_{c} g_c
+ \sum_{f \in FAM} pen_f \, miss_f
\]

where \(y_{ijm}^{chg}\) is the subset of machine-order binaries corresponding to a real setup-family change.

## Core constraints

### Assignment and timing

\[
\sum_{m \in M_o} x_{om} = 1 \qquad \forall o \in O
\]

\[
e_o = s_o + \sum_{m \in M_o} p_{om} x_{om} \qquad \forall o \in O
\]

For first operations:

\[
s_o \ge rel_{l(o)} \qquad \forall o \in O_{l(o)} \text{ with first index}
\]

For every non-first operation \(o\) with predecessor \(pred(o)\):

\[
s_o \ge e_{pred(o)} + q^-_o
\]

\[
s_o \le e_{pred(o)} + q^+_o \qquad \text{if } q^+_o \text{ exists}
\]

Assignment expiry:

\[
s_o \le exp_{om} + H(1-x_{om}) \qquad \forall o \in O,\; m \in M_o
\]

### Machine non-overlap and setups

For every pair \(i \ne j\) that can both run on machine \(m\):

\[
s_j \ge e_i + setup_{m,fam_i,fam_j} - H(1-y_{ijm}) - H(2-x_{im}-x_{jm})
\]

\[
s_i \ge e_j + setup_{m,fam_j,fam_i} - H(y_{ijm}) - H(2-x_{im}-x_{jm})
\]

Maintenance windows are treated as fixed occupied intervals. For each operation \(o\), assignment \(m\), and maintenance window \(\tau\) on \(m\), one of the two disjunctive sides must hold:

\[
e_o \le start_\tau + H(1-\alpha_{o\tau m}) + H(1-x_{om})
\]

\[
s_o \ge end_\tau - H\alpha_{o\tau m} - H(1-x_{om})
\]

### Reticle non-overlap

For each reticle-sharing implant pair \(i,j \in O_r\):

\[
s_j \ge e_i - H(1-u_{ijr})
\]

\[
s_i \ge e_j - H u_{ijr}
\]

Reticle hold intervals are handled with the same two-sided disjunction used for maintenance windows.

### Furnace batching

Each furnace operation must belong to exactly one chosen feasible batch:

\[
\sum_{b \in B_o} a_{ob} = 1 \qquad \forall o \in O^F
\]

\[
a_{ob} \le h_b \qquad \forall o \in O^F,\; b \in B_o
\]

Batch timing:

\[
S_b \ge s_o - H(1-a_{ob}), \qquad
E_b \le e_o + H(1-a_{ob}), \qquad
E_b - S_b = p_b \quad \forall o \in b
\]

All operations in a chosen batch share the same furnace machine and the same interval \([S_b,E_b]\); furnace machine non-overlap is enforced on batches instead of individual furnace operations.

### Implant chamber load and cleaning

Let implant sequence arcs on each machine define immediate predecessor-successor relations among implant operations and chosen clean tasks. Chamber-load state \(L_o\) before each implant operation satisfies:

\[
L_o \le thr_m
\]

\[
L_j \ge L_i + load_j - H(1-\delta_{ijm}^{noclean})
\]

\[
L_j \le load_j + H(1-\delta_{ijm}^{reset})
\]

where \(\delta^{noclean}\) is active when \(j\) follows \(i\) on implant machine \(m\) without an intervening clean or maintenance reset, and \(\delta^{reset}\) is active when a clean or maintenance reset occurs before \(j\). Clean tasks consume duration \(clean_m\), occupy the machine, and consume maintenance technician capacity.

### Operator calendar capacities

For each skill window \(w \in W_k\), the number of process-start support intervals and clean intervals overlapping the window cannot exceed capacity. In event-sweep form:

\[
\sum_{o: skill_o=k} \mathbf{1}\big([s_o, s_o+opmin_o) \cap w \ne \emptyset\big)
+ \sum_{c \in C: skill(c)=k} \mathbf{1}\big([s_c,e_c) \cap w \ne \emptyset\big)
\le cap_{kw}
\]

In a MIP this is linearized with overlap binaries between each task-support interval and each capacity window.

### FOUP buffer-zone capacities

For every lot \(l\) whose first implant is immediately followed by a furnace step, define waiting interval:

\[
I_l = [e_{implant(l)}, s_{furnace(l)})
\]

For every zone window \(w \in W_{zone_l}\):

\[
\sum_{l: zone_l=z} \mathbf{1}(I_l \cap w \ne \emptyset) \le cap_{zw}
\]

Again the exact MIP uses overlap binaries or an equivalent event-based linearization.

### Lot completion, tardiness, and flow time

Let \(last(l)\) be the final operation of lot \(l\):

\[
T_l \ge e_{last(l)} - due_l,\qquad T_l \ge 0
\]

\[
F_l = e_{last(l)} - rel_l
\]

### Family commitments

\[
done_l = 1 \Rightarrow e_{last(l)} \le commit_{fam(l)}
\]

\[
miss_f \ge minlots_f - \sum_{l: fam(l)=f} done_l,\qquad miss_f \ge 0
\]

## Notes on implementation

- The exact model above is the benchmark model.
- The submitted `solve.py` uses a staged constructive policy:
  1. first implants,
  2. furnaces,
  3. metrology,
  4. trim implants,
  with exact calendar, maintenance, reticle, queue, buffer, and clean-threshold checks at insertion time.
- The script then reruns an independent audit on the exported artifact and writes `solution.json` only from validated event data.
