# Climate Refuge Reserve Selection Model

This instance is best represented as a binary first-stage assignment and scenario-covering MIP, not as a network flow, routing, or time-indexed model. The only here-and-now decisions are which parcels to select and which single action to assign to each selected parcel. Scenario dependence appears only in feature coverage and the worst-scenario floor, so a deterministic equivalent with scenario-specific coverage binaries is the strongest direct formulation for this scale.

## Sets and indices

- \(P\): parcels.
- \(A=\{\text{protect}, \text{restore}, \text{migration\_easement}\}\): actions.
- \(S\): scenarios.
- \(F\): conservation features.
- \(R\): implementation regions.
- \(E \subseteq P \times A\): eligible parcel-action pairs.
- \(L \subseteq P \times F\): parcel-feature habitat links.
- \(C \subseteq P \times P\): unordered parcel-conflict pairs.
- \(F^{pri}=\{f \in F : \rho_f > 0\}\): priority features.

## Parameters

From `parcels.csv`:

- \(r_p \in R\): region of parcel \(p\).
- \(u_p\): acquisition cost.
- \(m_p\): annual maintenance cost.
- \(d_p\): staff days.
- \(\mu_p\): current marsh index.
- \(\nu_p\): upland migration index.
- \(\sigma_p\): salinity resilience.
- \(\kappa_p \in \{0,1\}\): migration corridor indicator.
- \(q_p \in \{0,1\}\): public-access indicator.

From `actions.csv`:

- \(\alpha_a\): fixed-cost multiplier.
- \(\beta_a\): maintenance multiplier.
- \(\gamma_a\): staff multiplier.
- \(\eta_a\): habitat multiplier.
- \(\upsilon_a\): upland multiplier.
- \(\nabla_a\): nursery units.

From `features.csv`:

- \(v_f\): conservation value.
- \(\omega_f\): vulnerability weight.
- \(t_f\): base target units.
- \(\rho_f\): priority minimum-rate requirement.

From `scenarios.csv`:

- \(\pi_s\): scenario probability.
- \(sl_s\): sea-level stress multiplier.
- \(dp_s\): development-pressure multiplier.
- \(sa_s\): salinity-shift multiplier.
- \(rs_s\): restoration-success multiplier.

From `parcel_feature_habitat.csv`:

- \(h_{pf}\): base habitat units for \((p,f) \in L\).
- \(g_{pf}\): connectivity factor for \((p,f) \in L\).

From `region_requirements.csv`:

- \(L^R_r,U^R_r\): min/max selected parcels in region \(r\).
- \(Q_r\): min selected public-access parcels in region \(r\).
- \(D_r\): staff-day cap in region \(r\).

From `config.json`:

- \(B^{cap}\): capital budget.
- \(B^{mnt}\): annual maintenance budget.
- \(B^{staff}\): total staff-day budget.
- \(B^{nur}\): nursery-unit budget.
- \(L^P,U^P\): min/max selected parcels.
- \(L^{cor}\): minimum migration-corridor parcels.
- \(L^{prot}\): minimum protection actions.
- \(L^{ease}\): minimum migration-easement actions.
- \(U^{rest}\): maximum restoration actions.
- \(\lambda\): robust-floor weight.
- \(\phi\): benefit scale.
- \(\psi\): cost scale.

Derived action-adjusted resource coefficients for \((p,a)\in E\):

- Capital cost: \(K_{pa}=u_p \alpha_a\).
- Maintenance cost: \(M_{pa}=m_p \beta_a\).
- Staff use: \(D_{pa}=d_p \gamma_a\).
- Nursery use: \(N_{pa}=\nabla_a\).

### Scenario contribution and target assumptions

To keep the solver aligned with the public statement, the habitat contribution of a selected parcel-action pair is modeled as:

\[
c_{pfas}
=
h_{pf} g_{pf}
\left(
\frac{\mu_p \eta_a}{sl_s}
+
\frac{\nu_p \upsilon_a}{dp_s}
\right)
\cdot \frac{\sigma_p}{sa_s}
\cdot \chi_{a,s}
\]

where

\[
\chi_{a,s} =
\begin{cases}
rs_s & \text{if } a=\text{restore}\\
1 & \text{otherwise}
\end{cases}
\]

and the intended multiplicative interpretation used in implementation is

\[
c_{pfas} =
h_{pf} g_{pf}
\left(
\frac{\mu_p \eta_a}{sl_s}
+
\frac{\nu_p \upsilon_a}{dp_s}
\right)
\cdot \frac{\sigma_p}{sa_s}
\cdot \chi_{a,s}.
\]

The scenario-adjusted target is

\[
T_{fs}
=
t_f
\left[
1 + (\omega_f-1)\left(\frac{sl_s+dp_s+sa_s}{3}-1\right)
\right].
\]

These assumptions use every public field that is operationally described in the statement. Descriptive metadata such as parcel coordinates, owner type, taxon group, and feature home region are not decision-driving in the public rules and are therefore not used directly in the MIP.

## Decision variables

- \(x_{pa} \in \{0,1\}\) for \((p,a)\in E\): 1 if parcel \(p\) is selected with action \(a\).
- \(z_{fs} \in \{0,1\}\) for \(f \in F, s \in S\): 1 if feature \(f\) is covered in scenario \(s\).
- \(w \ge 0\): worst-scenario covered conservation value floor.

## Objective

Let scenario score

\[
V_s = \sum_{f \in F} v_f z_{fs}.
\]

Maximize expected covered conservation value plus the robust floor reward, net of capital and maintenance burden:

\[
\max \quad
\phi \left(\sum_{s \in S} \pi_s V_s + \lambda w \right)
- \psi \sum_{(p,a)\in E} (K_{pa}+M_{pa}) x_{pa}.
\]

## Constraints

### 1. One action per selected parcel

\[
\sum_{a:(p,a)\in E} x_{pa} \le 1 \qquad \forall p \in P
\]

### 2. Global selected-parcel bounds

\[
L^P \le \sum_{(p,a)\in E} x_{pa} \le U^P
\]

### 3. Global budgets

\[
\sum_{(p,a)\in E} K_{pa} x_{pa} \le B^{cap}
\]

\[
\sum_{(p,a)\in E} M_{pa} x_{pa} \le B^{mnt}
\]

\[
\sum_{(p,a)\in E} D_{pa} x_{pa} \le B^{staff}
\]

\[
\sum_{(p,a)\in E} N_{pa} x_{pa} \le B^{nur}
\]

### 4. Action-balance rules

\[
\sum_{p:(p,\text{protect})\in E} x_{p,\text{protect}} \ge L^{prot}
\]

\[
\sum_{p:(p,\text{migration\_easement})\in E} x_{p,\text{migration\_easement}} \ge L^{ease}
\]

\[
\sum_{p:(p,\text{restore})\in E} x_{p,\text{restore}} \le U^{rest}
\]

### 5. Corridor minimum

\[
\sum_{(p,a)\in E} \kappa_p x_{pa} \ge L^{cor}
\]

### 6. Region selection and public-access rules

\[
L^R_r \le \sum_{(p,a)\in E: r_p=r} x_{pa} \le U^R_r
\qquad \forall r \in R
\]

\[
\sum_{(p,a)\in E: r_p=r} q_p x_{pa} \ge Q_r
\qquad \forall r \in R
\]

\[
\sum_{(p,a)\in E: r_p=r} D_{pa} x_{pa} \le D_r
\qquad \forall r \in R
\]

### 7. Parcel conflicts

For every \((p,p') \in C\),

\[
\sum_{a:(p,a)\in E} x_{pa}
+\sum_{a:(p',a)\in E} x_{p'a}
\le 1.
\]

### 8. Scenario-feature coverage

For every \(f \in F, s \in S\),

\[
\sum_{(p,a)\in E:(p,f)\in L} c_{pfas} x_{pa} \ge T_{fs} z_{fs}.
\]

### 9. Priority-feature conservation obligation

For every \(s \in S\),

\[
\sum_{f \in F^{pri}} v_f z_{fs}
\ge
\sum_{f \in F^{pri}} \rho_f v_f.
\]

This aggregates the per-feature minimum-rate requirements into the required covered share of total priority-feature value in each scenario.

### 10. Worst-scenario conservation floor

\[
w \le \sum_{f \in F} v_f z_{fs}
\qquad \forall s \in S.
\]

## Variable domains

\[
x_{pa}\in\{0,1\} \quad \forall (p,a)\in E
\]

\[
z_{fs}\in\{0,1\} \quad \forall f\in F,\ s\in S
\]

\[
w \ge 0
\]
