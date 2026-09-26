# Exact-model implementation checklist

This checklist is a direct translation of `NEUTRAL_SPEC.md` and
`model_spec.json`. The equations, JSON values, and workbook ranges are the
authoritative inputs.

## Data and indexing

- [x] Read only `Sheet1!A2:D64`, `Sheet1!G2:H55`, and `Sheet3!B2:D97`.
- [x] Use 54 buses, 63 directed branch records, four scenarios, and 24 hours
  per scenario in scenario-major 96-column order.
- [x] Apply the load formula to every bus, including buses 51--54.
- [x] Derive incidence, per-unit R/X, current and apparent-power limits exactly.
- [x] Ignore Sheet2, the workbook power-factor column, and the EV column.

## Eight integral families

- [x] `uL` binary; `uNS` binary; `uEX` binary.
- [x] `zESS`, `zPVG`, and `zWTG` general integer with their documented bounds.
- [x] `u_ess` binary for all 7 x 96 entries.
- [x] `kt` general integer for all 4 x 96 entries.
- [x] Keep all eight families free and integral in the final benchmark solve.

## Planning and topology

- [x] Fix exactly the documented existing line rows to one.
- [x] Enforce `sum(uL) <= 52-sum(uNS)` and the per-scenario equality
  `sum(yL)=52-sum(uNS)` with `yL=uL`.
- [x] Enforce ESS/PV/WT bounds, ESS and asymmetric PV aggregate limits, and no
  aggregate WTG limit.
- [x] Enforce at most one option at each existing and candidate station.
- [x] Implement station capacities and the one-half capacity multiplier.
- [x] Derate only existing stations 51 and 52 in scenario 1, hours 7 and 8.
- [x] Implement `FF/FS` balance and line-gated `FF` bounds for every scenario.
- [x] Set `FS=0` at buses 1--50 and use the exact expansion-gated root bounds at
  51--54, including expansion gates at existing substations.

## Voltage, taps, branches, and faults

- [x] Bound bus voltages 1--50 by squared per-unit limits.
- [x] Set existing-station squared voltage equal to `1.01+0.025*kt`.
- [x] Use both candidate-station tap equalities with big-M, candidate-off
  voltage gates, and tap-on gates exactly as written.
- [x] Apply the fault mask only to rows 34 and 44 at scenario 1 hours 7 and 8.
- [x] Do not apply the fault mask to topology cardinality or `FF/FS`.
- [x] Enforce branch P/Q/l on/off bounds, voltage-drop big-M constraints, and
  the branch SOC cone for every branch-hour.

## Renewable generation

- [x] Use 0.1 MW units and forecast-scaled availability.
- [x] Dispatch each unit family between 30% and 100% of availability.
- [x] Enforce installed-capacity cones.
- [x] Enforce DG power-factor bounds against actual dispatched P.

## Storage

- [x] Use 0.1 MWh/unit except one-quarter capacity at IDC bus 50.
- [x] Enforce charge/discharge definition, 0.3 power-to-energy ratio, binary
  mutually exclusive mode, and 20--100% energy bounds.
- [x] For each scenario, use transitions for hours 1--23, the documented
  hour-24-to-hour-1 wrap equation, and `E[24]=0.6*cap`.

## SOP, shedding, and nodal balance

- [x] Treat both SOP unit-count vectors as fixed parameters independent of
  corresponding branch build status.
- [x] Enforce endpoint P/Q/loss bounds, both endpoint cone families, nonnegative
  Q, and active-power/loss balance.
- [x] Permit shedding at every bus only in scenario 1 hours 7 and 8.
- [x] Limit IDC shedding to 10% in the fault window.
- [x] Derive reactive shedding at load power factor.
- [x] Enforce directed active/reactive nodal balances with receiving-end line
  losses and all station, renewable, storage, and SOP injections.

## Objective and evidence

- [x] Recompute investment, CRF, maintenance, purchase, curtailment, and shed
  components exactly, including fixed SOP investment and maintenance.
- [x] Distinguish the solver objective from the reconstructed full model total.
- [x] Export every required array with exact shape as finite float64 values and
  unrounded solver integer levels.
- [x] Independently replay the NPZ from workbook + JSON without importing the
  construction model; audit every section-15 family and normalized residuals.
- [x] Retain each distinct feasible incumbent before starting a new strategy.
- [x] Final entry point: at most 900 aggregate optimizer seconds, four threads,
  no external solution, and a free-planning exact MISOCP final solve.
