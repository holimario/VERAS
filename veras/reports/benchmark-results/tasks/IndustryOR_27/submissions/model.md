# Meridian Components Production Planning Model

## Formulation choice

This instance is best modeled as a **multi-period production-distribution MIP with activation state and time-lagged inventory flow**, not as a routing, time-indexed scheduling, or set-partitioning model.

- The continuous core is a time-expanded network flow over monthly production, release, inventory, and shipment quantities.
- Binary structure is limited to monthly product campaign activation and campaign starts.
- Route-level production variables are required because machine hours and B-stage yields depend on the selected machine.
- This is stronger than a naive product-level model because it keeps the capacity-driving routes explicit while avoiding unnecessary sequencing variables.

## Sets and indices

- \(T\): months, in chronological order.
- \(P\): products.
- \(C\): customer channels.
- \(M\): machines.
- \(M^A \subseteq M\): stage-A machines.
- \(M^B \subseteq M\): stage-B machines.
- \(R^A_p \subseteq M^A\): eligible A-stage machines for product \(p\).
- \(R^B_p \subseteq M^B\): eligible B-stage machines for product \(p\).
- \(K\): materials.
- \(W^{mix}\): channel-mix windows.
- \(W^{ctr}\): contract windows.
- \(F\): product families.
- \(Q\): product-conflict rules.

## Parameters

- \(h^A_{pm}\): hours per gross unit on A-stage machine \(m \in R^A_p\).
- \(h^B_{pm}\): hours per gross unit on B-stage machine \(m \in R^B_p\).
- \(yld_{pm}\): good-yield rate on B-stage machine \(m \in R^B_p\).
- \(cap^{reg}_{tm}\), \(cap^{ot}_{tm}\): regular and overtime hours for month \(t\), machine \(m\).
- \(cost^{reg}_m\): regular processing cost per processing hour on machine \(m\).
- \(cost^{ot}_m\): overtime premium per overtime hour on machine \(m\).
- \(setuphrs_{tmp}\): setup-hour load on machine \(m\) in month \(t\) when product \(p\) is active. This is zero when the row is absent in `machine_setup_hours.csv`.
- \(price_p\): base unit selling price of product \(p\).
- \(mult_c\): channel price multiplier for channel \(c\).
- \(rmcost_p\): raw-material cost per gross completed unit of product \(p\).
- \(dem_{tpc}\): maximum demand.
- \(svc_{pc}\): minimum service fraction.
- \(scost_p\): monthly setup cost when campaign \(p\) is active.
- \(q^{min}_p, q^{max}_p\): minimum and maximum campaign size.
- \(inv^0_p\): initial released finished-goods inventory.
- \(inv^{max}_p\): maximum ending released inventory.
- \(inv^{final}_p\): required final released inventory.
- \(hold_p\): monthly holding cost per unit of ending released inventory.
- \(lag_p\): quality-release lag in months.
- \(pipe^{max}_p\): maximum unreleased good units in the quality pipeline.
- \(bom_{pk}\): units of material \(k\) required per gross completed unit of product \(p\).
- \(avail_{tk}\): available material units.
- \(res_{tk}\): minimum unused reserve of material \(k\).
- \(init_p \in \{0,1\}\): initial active campaign status before the horizon.
- \(crew^{cold}_p\), \(crew^{cont}_p\): cold-start and continuation crew usage.
- \(restart_p\): restart penalty cost when a campaign starts after inactivity.
- \(run_p\): minimum run length in months.
- \(crewcap_t\): monthly shared setup-crew capacity.
- \(fam(p)\): family of product \(p\).
- \(famcap_{tf}\): maximum active products in family \(f\) during month \(t\).
- For each mix window \(w \in W^{mix}\):
  - product \(p(w)\), channel \(c(w)\), start month \(s(w)\), end month \(e(w)\),
    minimum share \(\alpha_w\), maximum share \(\beta_w\).
- For each contract window \(u \in W^{ctr}\):
  - product \(p(u)\), channel \(c(u)\), start month \(s(u)\), end month \(e(u)\),
    minimum total shipment \(ctr_u\).
- For each conflict rule \(q \in Q\):
  - month \(t(q)\), products \(p(q)\), \(p'(q)\), maximum simultaneous setups \(conf_q\).

Context-only files `quality_context.csv` and `maintenance_notes.csv` are explanatory and do not add constraints beyond the public numeric tables already used above.

## Decision variables

Continuous variables:

- \(x^A_{tpm} \ge 0\): gross units of product \(p\) processed on A-stage machine \(m \in R^A_p\) in month \(t\).
- \(x^B_{tpm} \ge 0\): gross units of product \(p\) processed on B-stage machine \(m \in R^B_p\) in month \(t\).
- \(q_{tp} \ge 0\): gross completed units of product \(p\) in month \(t\).
- \(g_{tp} \ge 0\): good units produced in month \(t\) after B-stage yield.
- \(I_{tp} \ge 0\): released finished-goods ending inventory.
- \(U_{tp} \ge 0\): unreleased good units remaining in the quality pipeline at end of month \(t\).
- \(ot_{tm} \ge 0\): overtime hours used on machine \(m\) in month \(t\).
- \(s_{tpc} \ge 0\): shipments of product \(p\) to channel \(c\) in month \(t\).

Binary variables:

- \(a_{tp} \in \{0,1\}\): 1 if product \(p\)'s campaign is active in month \(t\).
- \(z_{tp} \in \{0,1\}\): 1 if product \(p\)'s campaign starts in month \(t\) after inactivity.

## Objective

Maximize total profit:

\[
\max
\sum_{t,p,c} price_p \cdot mult_c \cdot s_{tpc}
- \sum_{t,p} rmcost_p \cdot q_{tp}
- \sum_{t,p,m \in R^A_p} cost^{reg}_m \cdot h^A_{pm} \cdot x^A_{tpm}
- \sum_{t,p,m \in R^B_p} cost^{reg}_m \cdot h^B_{pm} \cdot x^B_{tpm}
- \sum_{t,m} cost^{ot}_m \cdot ot_{tm}
- \sum_{t,p} scost_p \cdot a_{tp}
- \sum_{t,p} restart_p \cdot z_{tp}
- \sum_{t,p} hold_p \cdot I_{tp}.
\]

## Constraints

### 1. Stage balance and route aggregation

For every \(t,p\):

\[
q_{tp} = \sum_{m \in R^A_p} x^A_{tpm}
\]

\[
q_{tp} = \sum_{m \in R^B_p} x^B_{tpm}
\]

\[
g_{tp} = \sum_{m \in R^B_p} yld_{pm} \cdot x^B_{tpm}
\]

This enforces same-month A/B gross completion while allowing B-route-dependent yield.

### 2. Campaign activation links

For every \(t,p\):

\[
q^{min}_p \cdot a_{tp} \le q_{tp} \le q^{max}_p \cdot a_{tp}
\]

These tight bounds eliminate a loose big-\(M\): if production occurs, setup is active; if setup is active, campaign size stays within the allowed range.

### 3. Machine-hour capacity

For every \(t,m\):

\[
\sum_{p: m \in R^A_p} h^A_{pm} x^A_{tpm}
\;+\;
\sum_{p: m \in R^B_p} h^B_{pm} x^B_{tpm}
\;+\;
\sum_{p} setuphrs_{tmp} a_{tp}
\;\le\;
cap^{reg}_{tm} + ot_{tm}
\]

\[
ot_{tm} \le cap^{ot}_{tm}
\]

### 4. Quality release, released inventory, and unreleased pipeline

Let released good units in month \(t\) be:

\[
rel_{tp} =
\begin{cases}
g_{t-lag_p,p} & \text{if } t-lag_p \text{ is within the horizon} \\
0 & \text{otherwise}
\end{cases}
\]

For every \(t,p\), with \(I_{0p}=inv^0_p\) and \(U_{0p}=0\):

\[
I_{tp} = I_{t-1,p} + rel_{tp} - \sum_c s_{tpc}
\]

\[
U_{tp} = U_{t-1,p} + g_{tp} - rel_{tp}
\]

\[
I_{tp} \le inv^{max}_p
\]

\[
U_{tp} \le pipe^{max}_p
\]

For the final month \(T\):

\[
I_{Tp} \ge inv^{final}_p
\]

### 5. Shipment demand and service requirements

For every \(t,p,c\):

\[
0 \le s_{tpc} \le dem_{tpc}
\]

\[
s_{tpc} \ge svc_{pc} \cdot dem_{tpc}
\]

### 6. Material availability with reserves

For every \(t,k\):

\[
\sum_p bom_{pk} \cdot q_{tp} \le avail_{tk} - res_{tk}
\]

### 7. Campaign start logic

For the first month \(t=1\):

\[
z_{1p} \ge a_{1p} - init_p,\quad
z_{1p} \le a_{1p},\quad
z_{1p} \le 1 - init_p
\]

For months \(t>1\):

\[
z_{tp} \ge a_{tp} - a_{t-1,p},\quad
z_{tp} \le a_{tp},\quad
z_{tp} \le 1 - a_{t-1,p}
\]

This makes \(z_{tp}=1\) exactly when the product becomes active after inactivity.

### 8. Minimum run length

For every product \(p\) and every feasible start month \(t\):

\[
\sum_{\tau=t}^{t+run_p-1} a_{\tau p} \ge run_p \cdot z_{tp}
\]

If fewer than \(run_p\) months remain in the horizon, new starts are disallowed:

\[
z_{tp} = 0
\]

This is the horizon-end assumption used in the solver: a new campaign cannot start unless its full minimum run can be observed within the planning horizon.

### 9. Setup-crew capacity

For every month \(t\):

\[
\sum_p \left( crew^{cold}_p \cdot z_{tp}
+ crew^{cont}_p \cdot (a_{tp} - z_{tp}) \right)
\le crewcap_t
\]

### 10. Family campaign limits

For every month \(t\) and family \(f\):

\[
\sum_{p : fam(p)=f} a_{tp} \le famcap_{tf}
\]

### 11. Product conflict rules

For each conflict rule \(q\):

\[
a_{t(q),p(q)} + a_{t(q),p'(q)} \le conf_q
\]

### 12. Contract commitments

For each contract window \(u\):

\[
\sum_{t=s(u)}^{e(u)} s_{t,p(u),c(u)} \ge ctr_u
\]

### 13. Channel-mix windows

For each mix window \(w\), let
\[
Sel_w = \sum_{t=s(w)}^{e(w)} s_{t,p(w),c(w)}
\]
and
\[
Tot_w = \sum_{t=s(w)}^{e(w)} \sum_c s_{t,p(w),c}.
\]

Then:

\[
Sel_w \ge \alpha_w \cdot Tot_w
\]

\[
Sel_w \le \beta_w \cdot Tot_w
\]

These are linear because the denominator is the variable total shipment \(Tot_w\).

## Output mapping

The required `solution.csv` contains:

- `production` rows for every eligible `(month, stage, machine, product)` route, with quantity equal to the corresponding \(x^A\) or \(x^B\) value.
- `shipment` rows for every `(month, product, channel)` shipment key, with quantity equal to \(s_{tpc}\).
- `setup` rows for every `(month, product)`, with quantity equal to \(a_{tp}\) as `0` or `1`.

Blank fields are left empty exactly where the schema requires:

- `production`: empty `channel`
- `shipment`: empty `stage` and `machine`
- `setup`: empty `stage`, `machine`, and `channel`

## Assumptions

- Initial unreleased quality-pipeline inventory is zero because no public file provides a nonzero initial pipeline.
- `raw_material_cost` in `products.csv` is charged per gross completed unit.
- Setup-hour consumption is only applied on machine/product pairs explicitly listed in `machine_setup_hours.csv`; missing rows mean zero setup-hour load.
- A campaign start in the final incomplete run window is disallowed so the public minimum-run rule is enforced conservatively within the finite horizon.
