# Stochastic Surgery Capacity Planning Model

## Formulation Choice

This instance is best handled as a block-assignment and continuous-time sequencing model, not as a minute-indexed formulation. The planning horizon is only one week, each OR block can hold at most four elective cases, and the hard rules are dominated by:

- assignment to compatible OR blocks,
- single-machine room sequencing,
- single-machine surgeon availability,
- limited shared equipment units with turnaround,
- daily anesthesia and block-count limits.

A time-indexed model would create many unnecessary start-time binaries. Instead, the implemented solver uses:

- binary assignment variables by compatible case-block pair,
- integer planned start times,
- pairwise disjunctive no-overlap constraints for rooms and surgeons,
- explicit equipment-unit assignment binaries for pools with more than one unit,
- scenario variables for expected room-load and downstream overflow cost,
- a simulator-based final ranking step to evaluate the public stochastic score more faithfully.

The exported artifact is always checked against the exact hard rules from the public statement.

## Sets

- \(C\): elective cases.
- \(B\): OR blocks.
- \(D=\{1,\dots,5\}\): planning days.
- \(S\): public stochastic scenarios.
- \(W\): wards.
- \(N\): recovery nights.
- \(P\): equipment pools.
- \(U_{pd}=\{1,\dots,\text{units}_p\}\): equipment units for pool \(p\) on day \(d\).

Derived subsets:

- \(B_c \subseteq B\): blocks compatible with case \(c\).
- \(C_b = \{c \in C: b \in B_c\}\).
- \(C_{kd}\): cases whose compatible blocks include some block on day \(d\).
- \(C^{\text{surg}}_{rd}\): cases of surgeon \(r\) that can be scheduled on day \(d\).
- \(C^{\text{pool}}_{pd}\): cases using equipment pool \(p\) that can be scheduled on day \(d\).

## Parameters

Case parameters:

- \(\text{dur}_c\): expected surgery duration.
- \(\text{due}_c\), \(\text{ready}_c\): ready and due day.
- \(\text{prio}_c\): priority weight.
- \(\text{deferpen}_c\): defer penalty.
- \(\text{pacu}_c\): PACU minutes.
- \(\text{los}_c\): ward length of stay in nights.
- \(\text{icu}_c \in \{0,1\}\): ICU-direct flag.
- \(\text{high}_c \in \{0,1\}\): high-acuity flag.
- \(\text{infect}_c \in \{0,1\}\): infection-control flag.
- \(\text{anes}_c\): anesthesia class.
- \(\text{surg}_c\): primary surgeon.
- \(\text{ward}_c\): downstream ward.
- \(\text{pool}_c\): required equipment pool.

Block parameters:

- \(\text{day}_b\), \(\text{room}_b\).
- \(\text{reg}_b\): regular staffed minutes.
- \(\text{opencost}_b\).
- \(\text{maxcases}_b\), \(\text{maxhigh}_b\).

Shared configuration:

- \(F\): first-case setup minutes.
- \(T\): room turnover minutes.
- \(O\): overtime cost per minute.
- \(I\): idle cost per minute.
- \(K\): tardiness cost per priority-day.
- \(L\): maximum planned overtime allowance.
- \(\alpha\): tail-risk confidence level.
- \(\lambda\): tail-risk weight.

Resource parameters:

- \(\text{maxcases}_{da}\): anesthesia limit for day \(d\), class \(a\).
- \(\text{surgmaxmin}_{rd}\), \(\text{surgmaxcases}_{rd}\).
- \(\text{poolunits}_p\), \(\text{poolturn}_p\), \(\text{poolmaxuses}_{pd}\).
- \(\text{wardcap}_{nw}\), \(\text{icucap}_n\).

Scenario parameters:

- \(\pi_s\): scenario probability.
- \(\gamma_{sa}\): duration multiplier for anesthesia class \(a\).
- \(h_s\): extra minutes for high-acuity cases.
- \(\text{emergmin}_{bs}\): emergency room minutes on block \(b\)'s room-day, or zero.
- \(\text{emergward}_{nws}\), \(\text{emergicu}_{ns}\): emergency downstream demand.

Derived scenario duration:

\[
\text{sdur}_{cs} = \gamma_{s,\text{anes}_c}\,\text{dur}_c + h_s \cdot \text{high}_c
\]

## Decision Variables

Primary planning variables:

- \(x_{cb} \in \{0,1\}\): case \(c\) assigned to block \(b\).
- \(z_c \in \{0,1\}\): case \(c\) deferred.
- \(y_b \in \{0,1\}\): block \(b\) opened by at least one elective case.
- \(t_c \in \mathbb{Z}_{+}\): planned start minute from beginning of the assigned day.
- \(w_{cd} \in \{0,1\}\): case \(c\) scheduled on day \(d\).

Room-order binaries:

- \(r_{ijb} \in \{0,1\}\): for \(i<j\), if both are in block \(b\), then \(r_{ijb}=1\) means \(i\) precedes \(j\).

Surgeon-order binaries:

- \(q_{ijd} \in \{0,1\}\): for \(i<j\) sharing the same surgeon and day \(d\), \(q_{ijd}=1\) means \(i\) precedes \(j\).

Equipment variables:

- \(g_{cdu} \in \{0,1\}\): case \(c\) on day \(d\) uses pool-unit \(u\) of its equipment pool when that pool has multiple units.
- \(e_{ijdu} \in \{0,1\}\): for \(i<j\) on the same pool/day/unit, \(e_{ijdu}=1\) means \(i\) precedes \(j\).
- For single-unit pools the same disjunctive structure is applied directly to \(w_{cd}\).

Scenario surrogate-cost variables:

- \(\text{ot}_{bs} \ge 0\), \(\text{idle}_{bs} \ge 0\): overtime and idle minutes for block \(b\), scenario \(s\).
- \(\text{wardov}_{nws} \ge 0\), \(\text{icuov}_{ns} \ge 0\): downstream overflow.
- \(\text{scost}_s \ge 0\): surrogate operating cost in scenario \(s\).
- \(\eta \in \mathbb{R}\), \(\xi_s \ge 0\): CVaR linearization variables.

## Objective

The implemented optimization problem minimizes

\[
\text{PlanCost} + \sum_{s\in S}\pi_s\,\text{scost}_s
+ \lambda\left(\eta + \frac{1}{1-\alpha}\sum_{s\in S}\pi_s \xi_s\right)
+ \varepsilon \sum_{c\in C} t_c
\]

where

\[
\text{PlanCost}
= \sum_{c\in C}\text{deferpen}_c z_c
+ \sum_{c\in C}\sum_{b\in B_c} K \cdot \text{prio}_c \cdot \max(0,\text{day}_b-\text{due}_c)\,x_{cb}
+ \sum_{b\in B}\text{opencost}_b y_b
\]

and \(\varepsilon\) is a small tie-break weight encouraging compact starts.

The surrogate scenario cost is

\[
\text{scost}_s
= \sum_{b\in B}\left(O\,\text{ot}_{bs} + I\,\text{idle}_{bs}\right)
+ \sum_{n\in N}\sum_{w\in W} c^{\text{ward}}\text{wardov}_{nws}
+ \sum_{n\in N} c^{\text{icu}}\text{icuov}_{ns}
\]

This MIP objective captures fixed planning cost, expected room-load cost, expected downstream overflow, and tail risk. PACU congestion is evaluated exactly in the final simulator-driven candidate ranking step rather than inside the MIP.

## Constraints

### 1. Assignment and opening

Each case is either scheduled once or deferred:

\[
z_c + \sum_{b\in B_c} x_{cb} = 1 \qquad \forall c\in C
\]

Day-linking:

\[
w_{cd} = \sum_{b\in B_c:\,\text{day}_b=d} x_{cb}
\qquad \forall c\in C,\ d\in D
\]

Open block if any case is assigned:

\[
x_{cb} \le y_b \qquad \forall c\in C_b,\ b\in B
\]

### 2. Block cardinality and acuity

\[
\sum_{c\in C_b} x_{cb} \le \text{maxcases}_b
\qquad \forall b\in B
\]

\[
\sum_{c\in C_b} \text{high}_c x_{cb} \le \text{maxhigh}_b
\qquad \forall b\in B
\]

\[
\sum_{c\in C_b} \text{infect}_c x_{cb} \le 1
\qquad \forall b\in B
\]

### 3. Planned start windows

If case \(c\) is assigned to block \(b\), its planned start is no earlier than room setup:

\[
t_c \ge F - M(1-x_{cb})
\qquad \forall c,\ b\in B_c
\]

and its planned expected finish is within the regular block plus the allowed planned overtime:

\[
t_c + \text{dur}_c \le \text{reg}_b + L + M(1-x_{cb})
\qquad \forall c,\ b\in B_c
\]

### 4. Room no-overlap and sequence feasibility

For every pair \(i<j\) that can both use block \(b\):

\[
t_j \ge t_i + \text{dur}_i + T - M\left(1-r_{ijb}\right) - M(2-x_{ib}-x_{jb})
\]

\[
t_i \ge t_j + \text{dur}_j + T - M r_{ijb} - M(2-x_{ib}-x_{jb})
\]

These are active only when both cases are assigned to the same block.

### 5. Infection-control final-position rule

If infected case \(i\) shares block \(b\) with any other case \(j\), then \(i\) must be after \(j\):

\[
t_i \ge t_j + \text{dur}_j + T - M(2-x_{ib}-x_{jb})
\qquad \forall b,\ \forall i,j\in C_b,\ i\ne j,\ \text{infect}_i=1
\]

### 6. Surgeon availability and workload

Unavailable days are excluded from \(B_c\); in addition:

\[
\sum_{c:\,\text{surg}_c=r} \text{dur}_c\, w_{cd} \le \text{surgmaxmin}_{rd}
\qquad \forall r,\ d
\]

\[
\sum_{c:\,\text{surg}_c=r} w_{cd} \le \text{surgmaxcases}_{rd}
\qquad \forall r,\ d
\]

For every pair \(i<j\) with the same primary surgeon on day \(d\):

\[
t_j \ge t_i + \text{dur}_i - M(1-q_{ijd}) - M(2-w_{id}-w_{jd})
\]

\[
t_i \ge t_j + \text{dur}_j - M q_{ijd} - M(2-w_{id}-w_{jd})
\]

### 7. Equipment pool limits

Daily usage bound:

\[
\sum_{c:\,\text{pool}_c=p} w_{cd} \le \text{poolmaxuses}_{pd}
\qquad \forall p,\ d
\]

If pool \(p\) has multiple identical units, unit assignment is explicit:

\[
\sum_{u\in U_{pd}} g_{cdu} = w_{cd}
\qquad \forall c\in C^{\text{pool}}_{pd}
\]

For every pair \(i<j\) using the same pool \(p\), same day \(d\), same unit \(u\):

\[
t_j \ge t_i + \text{dur}_i + \text{poolturn}_p
- M(1-e_{ijdu}) - M(2-g_{idu}-g_{jdu})
\]

\[
t_i \ge t_j + \text{dur}_j + \text{poolturn}_p
- M e_{ijdu} - M(2-g_{idu}-g_{jdu})
\]

If the pool has a single unit, the same pairwise disjunction is applied directly to \(w_{id}\) and \(w_{jd}\).

This models both limited parallelism and mandatory post-case turnaround.

### 8. Daily anesthesia staffing

\[
\sum_{c:\,\text{anes}_c=a} w_{cd} \le \text{maxcases}_{da}
\qquad \forall d,\ a
\]

### 9. Scenario room-load surrogate

Let

\[
\text{load}_{bs}
= F y_b
+ \sum_{c\in C_b} \text{sdur}_{cs} x_{cb}
+ T\left(\sum_{c\in C_b} x_{cb} - y_b\right)
+ \text{emergmin}_{bs} y_b
\]

Then

\[
\text{ot}_{bs} \ge \text{load}_{bs} - \text{reg}_b
\qquad \forall b,s
\]

\[
\text{idle}_{bs} \ge \text{reg}_b - \text{load}_{bs}
\qquad \forall b,s
\]

### 10. Scenario ward and ICU overflow

Elective ward occupancy:

\[
\text{wardload}_{nws}
= \sum_{c:\,\text{ward}_c=w}\sum_{b\in B_c:\ \text{day}_b \le n < \text{day}_b+\text{los}_c} x_{cb}
\]

Ward overflow:

\[
\text{wardov}_{nws} \ge \text{wardload}_{nws} + \text{emergward}_{nws} - \text{wardcap}_{nw}
\qquad \forall n,w,s
\]

Elective ICU occupancy on surgery night:

\[
\text{icuload}_{ns}
= \sum_{c:\,\text{icu}_c=1}\sum_{b\in B_c:\ \text{day}_b=n} x_{cb}
\]

\[
\text{icuov}_{ns} \ge \text{icuload}_{ns} + \text{emergicu}_{ns} - \text{icucap}_n
\qquad \forall n,s
\]

### 11. Scenario-cost aggregation and CVaR

\[
\text{scost}_s
= \sum_b \left(O\,\text{ot}_{bs} + I\,\text{idle}_{bs}\right)
+ \sum_{n,w} c^{\text{ward}}\text{wardov}_{nws}
+ \sum_n c^{\text{icu}}\text{icuov}_{ns}
\qquad \forall s
\]

\[
\xi_s \ge \text{scost}_s - \eta
\qquad \forall s
\]

\[
\xi_s \ge 0
\qquad \forall s
\]

## Exact Post-MIP Evaluation

The final candidate exported by the solver is audited and scored outside the MIP with an independent simulator that:

- rechecks every hard public rule from the written `solution.csv`,
- simulates each block under every public scenario using the submitted planned order and planned starts,
- computes elective case actual finish times under scenario durations and emergency-room occupation,
- computes PACU overflow from exact overlap minutes with the public one-hour buckets,
- computes ward and ICU overflow from the exported schedule,
- recomputes total score as

\[
\text{PlanCost} + \mathbb{E}[\text{OperatingCost}_s]
+ \lambda \cdot \text{CVaR}_{\alpha}(\text{OperatingCost}_s)
\]

Among multiple MIP candidates, the solver keeps the feasible candidate with the best exact post-audit score.

## Why This Is Appropriate Here

- The room horizon is continuous, but each block has at most four cases, so pairwise disjunctions are tighter and smaller than minute-indexed occupancy binaries.
- Surgeon and equipment interactions are cross-room and cross-block; continuous-time disjunctions model them directly.
- Explicit equipment-unit assignment is the cleanest exact way to handle pools with `units=2`.
- The stochastic recovery-cost terms are partially linear and partially simulation-like. Putting the strongest linear pieces into the MIP and using an exact simulator for final ranking gives a better tradeoff than forcing a very large weak extensive-form model.
