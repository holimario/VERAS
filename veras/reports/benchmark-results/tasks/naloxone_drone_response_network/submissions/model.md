# Mathematical Model

## Modeling choice

This instance is best represented as a two-stage stochastic capacitated facility-allocation model with scenario-specific assignment decisions.

- First-stage decisions: which bases to open and how many drones and kits to stage.
- Second-stage decisions: for each scenario and zone, choose exactly one dispatch mode (`drone` from one open base or `ground`).

It is not a routing model: each zone receives one tactical dispatch choice per scenario, there are no multi-stop tours, and the public constraints are assignment, capacity, coverage, backup, and scenario-coupling rules.

## Sets

- \(B\): candidate bases.
- \(Z\): demand zones.
- \(S\): scenarios.
- \(R\): base regions.
- \(D\): response districts.
- \(G\): equity groups.
- \(Z_g \subseteq Z\): zones in equity group \(g\).
- \(Z_d \subseteq Z\): zones in district \(d\).
- \(B_r \subseteq B\): bases in region \(r\).
- \(P \subseteq Z\): priority zones.
- \(T \subseteq S\): tail scenarios, defined as the `tail_scenario_count` scenarios with the largest `weather_severity` (ties broken by descending `call_multiplier`, descending `ground_time_multiplier`, ascending `drone_speed_multiplier`, then scenario id). For this instance, \(T=\{\text{S5},\text{S6}\}\).

## Parameters

All parameters are read directly from the public data.

- \(c_b^{fix}\): weekly fixed cost of opening base \(b\).
- \(\overline d_b\): maximum drones that can be staged at base \(b\).
- \(\overline k_b\): maximum kits that can be staged at base \(b\).
- \(\ell_b\): launch delay at base \(b\) in minutes.
- \(q_b\): drone range of base \(b\) in km.
- \(h_b \in \{0,1\}\): 1 if base \(b\) is weather hardened.
- \((x_b,y_b)\), \((x_z,y_z)\): coordinates of bases and zones.
- \(\lambda_z\): base calls per day in zone \(z\).
- \(w_z\): overdose risk weight of zone \(z\).
- \(e_z\): baseline ground EMS response minutes for zone \(z\).
- \(p_z \in \{0,1\}\): 1 if zone \(z\) is high priority.
- \(\pi_s\): probability of scenario \(s\).
- \(\mu_s\): call multiplier in scenario \(s\).
- \(\sigma_s\): drone speed multiplier in scenario \(s\).
- \(\gamma_s\): ground-time multiplier in scenario \(s\).
- \(\rho_s\): kit reserve factor in scenario \(s\).
- \(nf_s \in \{0,1\}\): no-fly indicator in scenario \(s\).
- \(cap^{gr}_{sd}\): district ground fallback capacity for scenario \(s\), district \(d\).
- \(cap^{air}_{srd}\): air-corridor capacity from origin region \(r\) to destination district \(d\) in scenario \(s\). If a scenario-specific row exists it overrides the `ALL` row.
- \(\delta^{air}_{srd}\): additional corridor delay in minutes for region \(r\) to district \(d\) in scenario \(s\), with the same override logic.
- \(hard_{srd} \in \{0,1\}\): 1 if the scenario-specific corridor requires a weather-hardened base.
- \(a^{exc}_{sbz} \in \{0,1\}\): base-zone override from `base_zone_exceptions.csv`, with scenario-specific rows overriding `ALL`.
- \(u^{open}_r\): maximum open bases supportable in region \(r\).
- \(u^{drone}_r\): maximum staged drones supportable in region \(r\).
- \(BUD\): weekly budget.
- \(D^{tot}\): total drones available.
- \(B^{min}, B^{max}\): minimum and maximum number of open bases.
- \(c^{dr}\): weekly cost per drone.
- \(c^{kit}\): weekly cost per kit.
- \(H\): daily service minutes = `daily_service_hours * 60`.
- \(\eta\): queue-utilization ceiling = `max_queue_utilization`.
- \(v_0\): base drone speed in km/h.
- \(s^{scene}\): service minutes at scene.
- \(s^{reload}\): reload minutes.
- \(\alpha_g\): standard minimum fast-response share for equity group \(g\).
- \(\alpha_g^{tail}\): tail-scenario minimum fast-response share for group \(g\).
- \(\tau_g\): response target minutes for group \(g\).
- \(\kappa\): allowed backup slack in minutes = `high_priority_backup_extra_minutes`.
- \(\beta\): maximum flyable ground share.
- \(\lambda^{risk}\): death-risk decay per minute.
- \(W^{cost}, W^{exp}, W^{tail}, W^{risk}, W^{eq}, W^{ground}, W^{uns}\): scoring weights from `config.json`.

Derived parameters:

- Scenario-adjusted daily calls for zone \(z\) in scenario \(s\):
  \[
  dem_{sz} = \lambda_z \mu_s.
  \]

- Euclidean distance:
  \[
  dist_{bz} = \sqrt{(x_b-x_z)^2 + (y_b-y_z)^2}.
  \]

- Drone eligibility indicator:
  \[
  elig_{sbz}=1
  \]
  iff all of the following hold:
  1. \(nf_s=0\),
  2. \(dist_{bz} \le q_b\), unless overridden by `base_zone_exceptions.csv`,
  3. the effective exception row does not set `allowed = 0`,
  4. if \(hard_{s,r(b),d(z)}=1\), then \(h_b=1\).

- Drone response minutes if \(elig_{sbz}=1\):
  \[
  resp^{dr}_{sbz} = \ell_b + \frac{60 \cdot dist_{bz}}{v_0 \sigma_s} + \delta^{air}_{s,r(b),d(z)}.
  \]

- Ground response minutes:
  \[
  resp^{gr}_{sz} = e_z \gamma_s.
  \]

- Drone busy time used in the queueing constraint:
  \[
  busy_{sbz} = \ell_b + 2\frac{60 \cdot dist_{bz}}{v_0 \sigma_s} + 2\delta^{air}_{s,r(b),d(z)} + s^{scene} + s^{reload}.
  \]
  This is a conservative round-trip interpretation of a call cycle.

- Kit requirement generated by assigning zone \(z\) to base \(b\) in scenario \(s\):
  \[
  kitreq_{sbz} = dem_{sz}\rho_s.
  \]

- Fast-response indicators:
  \[
  fast^{gr}_{sz} = \mathbf{1}[resp^{gr}_{sz} \le \tau_{g(z)}], \quad
  fast^{dr}_{sbz} = \mathbf{1}[resp^{dr}_{sbz} \le \tau_{g(z)}].
  \]

- Mortality-risk proxy coefficients:
  \[
  risk^{gr}_{sz} = w_z\left(1-e^{-\lambda^{risk} resp^{gr}_{sz}}\right),
  \]
  \[
  risk^{dr}_{sbz} = w_z\left(1-e^{-\lambda^{risk} resp^{dr}_{sbz}}\right).
  \]

- Backup compatibility indicator for priority zones:
  \[
  back_{s z b b'} = 1
  \]
  iff \(b' \neq b\), \(elig_{sbz}=1\), \(elig_{sb'z}=1\), and
  \[
  resp^{dr}_{sb'z} \le resp^{dr}_{sbz} + \kappa.
  \]

## Decision variables

First stage:

- \(y_b \in \{0,1\}\): 1 if base \(b\) is open.
- \(d_b \in \mathbb{Z}_{+}\): drones staged at base \(b\).
- \(k_b \in \mathbb{Z}_{+}\): kits staged at base \(b\).

Second stage:

- \(x_{sbz} \in \{0,1\}\): 1 if scenario \(s\), zone \(z\) is served by drone from base \(b\).
- \(g_{sz} \in \{0,1\}\): 1 if scenario \(s\), zone \(z\) is served by ground EMS fallback.

Auxiliary expressions:

- Scenario response burden:
  \[
  R_s = \sum_{z \in Z} dem_{sz}\left(resp^{gr}_{sz} g_{sz} + \sum_{b \in B: elig_{sbz}=1} resp^{dr}_{sbz} x_{sbz}\right).
  \]

- Scenario mortality-risk burden:
  \[
  M_s = \sum_{z \in Z} dem_{sz}\left(risk^{gr}_{sz} g_{sz} + \sum_{b \in B: elig_{sbz}=1} risk^{dr}_{sbz} x_{sbz}\right).
  \]

## Objective

Minimize the weighted benchmark score:

\[
\min \;
W^{cost} \left(\sum_{b \in B} c_b^{fix} y_b + c^{dr}\sum_{b \in B} d_b + c^{kit}\sum_{b \in B} k_b\right)
\]
\[
{}+ W^{exp}\sum_{s \in S}\pi_s R_s
+ W^{tail}\frac{1}{|T|}\sum_{s \in T} R_s
+ W^{risk}\sum_{s \in S}\pi_s M_s
+ W^{ground}\sum_{s \in S}\pi_s \sum_{z \in Z} dem_{sz} g_{sz}
+ W^{eq}\cdot 0
+ W^{uns}\cdot 0.
\]

`equity_gap` and `unserved_penalty` are zero in the final strict model because equity and complete service are enforced as hard constraints.

## Constraints

### 1. Base-count limits

\[
B^{min} \le \sum_{b \in B} y_b \le B^{max}.
\]

### 2. Budget

\[
\sum_{b \in B} c_b^{fix} y_b + c^{dr}\sum_{b \in B} d_b + c^{kit}\sum_{b \in B} k_b \le BUD.
\]

### 3. Fleet and base activation

For every \(b \in B\):

\[
y_b \le d_b \le \overline d_b y_b,
\]
\[
y_b \le k_b \le \overline k_b y_b.
\]

Citywide fleet limit:

\[
\sum_{b \in B} d_b \le D^{tot}.
\]

### 4. Regional staffing

For every region \(r \in R\):

\[
\sum_{b \in B_r} y_b \le u^{open}_r,
\]
\[
\sum_{b \in B_r} d_b \le u^{drone}_r.
\]

### 5. Exactly one dispatch per scenario-zone pair

For every \(s \in S, z \in Z\):

\[
g_{sz} + \sum_{b \in B: elig_{sbz}=1} x_{sbz} = 1.
\]

If \(elig_{sbz}=0\), no variable \(x_{sbz}\) is created.

### 6. Drone assignments only from operational bases

For every created \(x_{sbz}\):

\[
x_{sbz} \le y_b,
\]
\[
x_{sbz} \le d_b.
\]

### 7. Queueing / utilization capacity at each base

For every \(s \in S, b \in B\):

\[
\sum_{z \in Z: elig_{sbz}=1} dem_{sz}\, busy_{sbz}\, x_{sbz}
\le H \eta \, d_b.
\]

### 8. Kit capacity at each base

For every \(s \in S, b \in B\):

\[
\sum_{z \in Z: elig_{sbz}=1} kitreq_{sbz}\, x_{sbz} \le k_b.
\]

### 9. Corridor capacity

For every \(s \in S\), region \(r \in R\), district \(d \in D\):

\[
\sum_{z \in Z_d}\sum_{b \in B_r: elig_{sbz}=1} dem_{sz} x_{sbz} \le cap^{air}_{srd}.
\]

### 10. Ground fallback district capacity

For every \(s \in S, d \in D\):

\[
\sum_{z \in Z_d} dem_{sz} g_{sz} \le cap^{gr}_{sd}.
\]

### 11. Citywide ground-share cap in flyable scenarios

For every \(s \in S\) with \(nf_s=0\):

\[
\sum_{z \in Z} dem_{sz} g_{sz} \le \beta \sum_{z \in Z} dem_{sz}.
\]

### 12. Backup-drone existence for priority zones in flyable scenarios

For every \(s \in S\) with \(nf_s=0\), every priority zone \(z \in P\), and every eligible primary base \(b\):

\[
x_{sbz} \le \sum_{b' \in B: back_{s z b b'} = 1} y_{b'}.
\]

This guarantees that if \(b\) is chosen as the primary drone base, at least one different compatible open backup base exists. Because open bases have at least one staged drone, an open compatible backup is operationally usable.

### 13. Equity fast-response shares by scenario and group

For every scenario \(s \in S\) and equity group \(g \in G\):

Define
\[
\bar \alpha_{sg} =
\begin{cases}
\alpha_g^{tail}, & s \in T,\\
\alpha_g, & s \notin T.
\end{cases}
\]

Then enforce

\[
\sum_{z \in Z_g} dem_{sz}\left(fast^{gr}_{sz} g_{sz} + \sum_{b \in B: elig_{sbz}=1} fast^{dr}_{sbz} x_{sbz}\right)
\ge
\bar \alpha_{sg}\sum_{z \in Z_g} dem_{sz}.
\]

## Export rule

After optimization:

- write one `base` row for every \(b \in B\), with `mode = open` iff \(y_b=1\), otherwise `closed`;
- write one `dispatch` row for every \((s,z)\);
- if \(g_{sz}=1\), export `mode = ground` and leave both base fields blank;
- otherwise export the unique primary base with \(x_{sbz}=1\), and for priority flyable rows choose any open base \(b'\) with \(back_{s z b b'}=1\); the implementation selects the fastest such open backup.
