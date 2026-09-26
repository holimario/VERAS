# Container Gate DRO Appointment Planning Model

## Modeling choice

This instance is best represented as a two-stage stochastic mixed-integer
network-flow model with common first-stage decisions and scenario-specific
recourse:

- First stage: appointment quotas by class and slot, and extra yard teams by
  block and slot.
- Second stage: scenario admissions by class and slot, and scenario service by
  class, block, and service slot.

An assignment model from admission slot to service slot is unnecessary here.
Because trucks of the same class are interchangeable and the public rules only
require:

- service no earlier than admission through cumulative prefix inequalities,
- queue limits on cumulative admitted minus cumulative served,
- completion by each class's latest service slot,

the strongest compact formulation is a cumulative flow model. It keeps the same
hard logic with far fewer variables than an admission-slot to service-slot
dispatch model.

## Sets and indices

- \(S\): scenarios, index \(s\)
- \(T = \{1,\dots,|T|\}\): ordered time slots, index \(t\)
- \(C\): truck classes, index \(c\)
- \(B\): yard blocks, index \(b\)
- \(E \subseteq C \times B\): eligible class-block service pairs
- \(P\): shared swing-team pools, index \(p\)
- \(R_t\): slot-class shared channel rules active in slot \(t\)

For each class \(c\):

- \(A_c \subseteq T\): allowed appointment slots
- \(L_c \in T\): latest allowable service slot

For each pool \(p\) and slot \(t\):

- \(B_{pt} \subseteq B\): blocks borrowing from that pool in that slot

## Parameters

All parameters are read directly from the public data files.

- \(q^{\min}_t\): minimum total quota in slot \(t\)
- \(Q^{\min}, Q^{\max}\): day-level quota lower and upper bounds
- \(\Delta^Q\): quota ramp limit between adjacent slots
- \(G_t\): base gate capacity in slot \(t\)
- \(\gamma_s\): scenario gate multiplier
- \(\bar G_{st} = \lfloor G_t \gamma_s \rfloor\): conservative integer
  scenario gate capacity
- \(K_t\): queue limit in slot \(t\)
- \(R_t^{reef}\): reefer inspection cap in slot \(t\)
- \(X_t\): drayage exposure cap in slot \(t\)
- \(u_c\): exposure units for class \(c\)
- \(\rho_c \in \{0,1\}\): reefer flag for class \(c\)
- \(d_{sct}\): realized demand in scenario \(s\), class \(c\), slot \(t\)
- \(W_b\): base work capacity of block \(b\) per slot
- \(m_{sbt}\): scenario productivity multiplier for block \(b\), slot \(t\)
- \(g^{team}\): work-unit gain from one extra team
- \(U_b\): maximum extra teams assignable to block \(b\) in a slot
- \(U_t^{team}\): terminal-wide extra-team cap in slot \(t\)
- \(U_{pt}^{pool}\): shared pool cap for pool \(p\) and slot \(t\)
- \(a_{cb}\): work units consumed when class \(c\) is served in block \(b\)
- \(h_{cb}\): handling cost for serving class \(c\) in block \(b\)
- \(\pi_c^{short}\): shortage penalty for one unadmitted truck of class \(c\)
- \(\pi_c^{wait}\): queue waiting cost per truck-slot for class \(c\)
- \(f^{quota}\): quota administration cost per published appointment
- \(f^{team}\): cost per extra team
- \(p_s\): empirical probability of scenario \(s\)
- \(\alpha\): tail-risk confidence level
- \(\lambda\): tail-risk weight

For each slot-level mix rule \(r \in R_t\):

- \(C_{tr} \subseteq C\): classes sharing the restricted channel
- \(M_{tr}\): combined admissions cap for that channel

For each service commitment row:

- \(H\): commitment records, index \(h\)
- \(s(h)\): scenario
- \(c(h)\): class
- \(t(h)\): deadline slot
- \(v_h\): minimum cumulative served trucks by that slot

## Decision variables

### First-stage variables

- \(Q_{ct} \in \mathbb{Z}_{\ge 0}\): published quota for class \(c\) in slot
  \(t\). Defined only for \(t \in A_c\); otherwise fixed to zero.
- \(Y_{bt} \in \mathbb{Z}_{\ge 0}\): extra teams assigned to block \(b\) in
  slot \(t\).

### Second-stage variables

- \(A_{sct} \in \mathbb{Z}_{\ge 0}\): admitted trucks for scenario \(s\), class
  \(c\), appointment slot \(t\)
- \(Z_{scbt} \in \mathbb{Z}_{\ge 0}\): trucks of class \(c\) served in scenario
  \(s\), block \(b\), service slot \(t\); defined only for eligible pairs
  \((c,b) \in E\) and slots \(t \le L_c\)

Auxiliary continuous variables for the CVaR linearization:

- \(\eta \in \mathbb{R}\): tail threshold
- \(\xi_s \ge 0\): excess of scenario cost above \(\eta\)

## Objective

The exact model minimizes fixed first-stage cost, expected scenario operating
cost, and a CVaR-style tail term:

\[
\min \;
f^{quota}\sum_{c,t} Q_{ct}
 + f^{team}\sum_{b,t} Y_{bt}
 + \sum_{s \in S} p_s \, \mathrm{Cost}_s
 + \lambda \left(
\eta + \frac{1}{1-\alpha}\sum_{s \in S} p_s \xi_s
\right)
\]

where

\[
\mathrm{Cost}_s =
\sum_{c,b,t} h_{cb} Z_{scbt}
 + \sum_{c,t} \pi_c^{short}(d_{sct} - A_{sct})
 + \sum_{c,t} \pi_c^{wait} \, \mathrm{Queue}_{sct}
\]

and the queue state is represented implicitly by:

\[
\mathrm{Queue}_{sct} =
\sum_{\tau \le t} A_{sc\tau} -
\sum_{\tau \le t}\sum_{b:(c,b)\in E} Z_{scb\tau}.
\]

The CVaR linearization uses:

\[
\xi_s \ge \mathrm{Cost}_s - \eta,\quad \xi_s \ge 0 \qquad \forall s \in S.
\]

## Constraints

### 1. Quota publication rules

Allowed-slot restriction:

\[
Q_{ct} = 0 \qquad \forall c \in C,\; t \notin A_c
\]

Slot minimum quota:

\[
\sum_{c \in C} Q_{ct} \ge q^{\min}_t \qquad \forall t \in T
\]

Day-level quota bounds:

\[
Q^{\min} \le \sum_{c,t} Q_{ct} \le Q^{\max}
\]

Ramp limit between adjacent slots:

\[
\sum_c Q_{c,t} - \sum_c Q_{c,t-1} \le \Delta^Q \qquad \forall t=2,\dots,|T|
\]

\[
\sum_c Q_{c,t-1} - \sum_c Q_{c,t} \le \Delta^Q \qquad \forall t=2,\dots,|T|
\]

### 2. Extra-team rules

Per-block limit:

\[
Y_{bt} \le U_b \qquad \forall b,t
\]

Terminal-wide slot limit:

\[
\sum_b Y_{bt} \le U_t^{team} \qquad \forall t
\]

Shared-pool limit:

\[
\sum_{b \in B_{pt}} Y_{bt} \le U_{pt}^{pool} \qquad \forall p,t
\]

### 3. Scenario admissions

Admissions cannot exceed published quota or realized demand:

\[
A_{sct} \le Q_{ct} \qquad \forall s,c,t
\]

\[
A_{sct} \le d_{sct} \qquad \forall s,c,t
\]

Scenario gate capacity:

\[
\sum_c A_{sct} \le \bar G_{st} \qquad \forall s,t
\]

Reefer inspection limit:

\[
\sum_c \rho_c A_{sct} \le R_t^{reef} \qquad \forall s,t
\]

Exposure cap:

\[
\sum_c u_c A_{sct} \le X_t \qquad \forall s,t
\]

Slot-class shared channel rules:

\[
\sum_{c \in C_{tr}} A_{sct} \le M_{tr} \qquad \forall s,t,r \in R_t
\]

### 4. Service completion and queue dynamics

Every admitted truck must eventually be served by its class deadline:

\[
\sum_{t \le L_c}\sum_{b:(c,b)\in E} Z_{scbt}
= \sum_t A_{sct}
\qquad \forall s,c
\]

No service before admission, enforced on every prefix:

\[
\sum_{\tau \le t}\sum_{b:(c,b)\in E} Z_{scb\tau}
\le
\sum_{\tau \le t} A_{sc\tau}
\qquad \forall s,c,t
\]

Queue limit on every prefix:

\[
\sum_{\tau \le t} A_{sc\tau}
-
\sum_{\tau \le t}\sum_{b:(c,b)\in E} Z_{scb\tau}
\le K_t
\qquad \forall s,c,t
\]

For cutoff classes, zero queue at the published cutoff slot is already implied
by the full-service equality and the fact that no service is allowed after
\(L_c\). In the implementation, the equality above is still kept for all
classes because it is the strongest compact form.

### 5. Service commitments

For each commitment record \(h \in H\):

\[
\sum_{\tau \le t(h)} \sum_{b:(c(h),b)\in E} Z_{s(h),c(h),b,\tau}
\ge v_h
\]

### 6. Yard block work capacity

For each scenario, block, and slot:

\[
\sum_{c:(c,b)\in E} a_{cb} Z_{scbt}
\le
W_b \, m_{sbt} + g^{team} Y_{bt}
\qquad \forall s,b,t
\]

This links first-stage staffing to scenario recourse without any big-M terms.

### 7. Integrality and nonanticipativity

All quota, team, admission, and service variables are nonnegative integers.
Nonanticipativity for first-stage decisions is inherent because \(Q\) and \(Y\)
have no scenario index and are shared across all scenarios.

## Restoration fallback

If the exact model fails to produce an incumbent, the solver switches to an
elastic Phase-I model:

- every hard-constraint family receives explicit nonnegative slack variables,
- each slack is normalized by the natural right-hand-side scale of its
  constraint,
- the solve is lexicographic:
  1. minimize the maximum normalized violation \(\theta\),
  2. minimize total normalized violation,
  3. minimize distance to the best available candidate (or to zero if none
     exists).

Only after \(\theta = 0\) is an incumbent accepted as feasible and exported.
