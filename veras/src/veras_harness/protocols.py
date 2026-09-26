"""Artifact discovery, validation, and task-specific control protocols."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .constants import SUBMISSION_REF

def submission_references(text: str) -> list[str]:
    return list(dict.fromkeys(f"submissions/{match.group(1)}" for match in SUBMISSION_REF.finditer(text)))


def target_candidates(app: Path, instruction: str) -> list[Path]:
    explicit = [
        app / value
        for value in submission_references(instruction)
        if Path(value).suffix.lower() in {".csv", ".json"}
        and "template" not in Path(value).name.lower()
    ]
    if explicit:
        return explicit
    templates = sorted((app / "submissions").glob("*_template.*"))
    return [
        template.with_name(
            template.stem.removesuffix("_template") + template.suffix.lower()
        )
        for template in templates
        if template.suffix.lower() in {".csv", ".json"}
    ]


def task_family_protocol(task: str, app: Path) -> list[str]:
    """Add concise public-constraint reminders selected from observed failures."""
    guidance: list[str] = []
    if any(token in task for token in ("routing", "evacuation", "drone_response")):
        guidance.append(
            "For routing, independently simulate the exact exported arc sequence. "
            "Propagate travel time, load, energy, compartments, pickup/delivery state, "
            "and fleet/lane capacities after every leg; never export metric-closure arcs."
        )
    if any(token in task for token in ("schedule", "scheduling", "orchestration", "dispatch")):
        guidance.append(
            "For scheduling, validate release/readiness, location continuity, turnaround, "
            "non-overlap, setup, maintenance, and every cumulative resource with an event "
            "sweep over the exported intervals before optimizing cost."
        )
    if list((app / "data").glob("*scenario*")):
        guidance.append(
            "For scenario models, keep first-stage decisions literally identical across "
            "scenarios and require complete feasible recourse in every public scenario; "
            "averaging violations across scenarios is invalid."
        )
    if task.startswith("online_") or "replanning" in task or "recovery" in task:
        guidance.append(
            "For online stages, treat prior accepted artifacts as state: preserve every "
            "frozen decision and validate the state transition before changing the suffix."
        )

    exact = {
        "disaster_relief_prepositioning": (
            "Apply scenario damage multipliers to every lane capacity and enforce each "
            "warehouse's outbound fleet capacity in every scenario, not just nominally."
        ),
        "climate_refuge_reserve_selection": (
            "After obtaining a robust feasible reserve, warm-start the unrestricted parcel/action "
            "master and optimize the true risk-adjusted value; do not stop at a Phase-I or greedy cover."
        ),
        "container_gate_dro_appointment_planning": (
            "After exact feasibility confirmation, run the unrestricted appointment/team model "
            "from the incumbent; confirmation of a fixed candidate is not a performance solve."
        ),
        "evtol_vertiport_recovery": (
            "Sort actions by scenario and aircraft. Initialize readiness from the public "
            "release slot; after each flight/reposition set readiness to end plus turnaround, "
            "and permit charging only at the aircraft's current location after readiness."
        ),
        "fuel_tanker_compartment_routing": (
            "Propagate arrival from the preceding location using the listed travel time, "
            "serve each delivery exactly once, and keep every compartment fill within its range."
        ),
        "flood_relief_multimodal_routing": (
            "After the route observer reports zero residual, warm-start the existing multimodal MIP and "
            "spend the performance slice closing its objective gap rather than rebuilding preprocessing."
        ),
        "naloxone_drone_response_network": (
            "Enforce the fast-response equity share separately for every scenario and protected "
            "population group; an aggregate or expected-share constraint is insufficient."
        ),
        "online_electric_bus_depot_replanning": (
            "Audit row-type nullability before simulation: trip rows leave item, charger, time, "
            "and energy blank; canceled trips also leave bus blank; charge rows leave trip blank."
        ),
        "recycling_mrf_stochastic_sorting_plan": (
            "Encode minimum QC tests for every open line and required batch-line pair, then apply "
            "scenario-adjusted line capacities rather than nominal capacities."
        ),
        "railway_disruption_recovery": (
            "Use the current feasible dispatch as a warm start, then jointly reopen train, rolling-stock, "
            "and passenger-flow decisions in neighborhoods rather than freezing the first feasible plan."
        ),
        "robotic_mobile_fulfillment_orchestration": (
            "Event-sweep robot, pod, and station occupancy on half-open intervals, including "
            "travel and handoff time; pairwise task checks do not prove resource feasibility."
        ),
        "robust_microgrid_islanding_dispatch": (
            "For every generator/scenario/hour constrain reserve by both committed headroom and "
            "ramp_up_kw. Recompute this directly from the exported policy."
        ),
        "satellite_disaster_response_downlink": (
            "Generate several first-stage observation schedules and solve scenario downlink recourse for "
            "each; use recourse values or cuts to update the master instead of accepting the first candidate."
        ),
        "sterile_processing_robust_schedule": (
            "All submitted case, cart, wash, assembly, and sterilizer times are nonnegative. "
            "Use explicit deferral instead of negative-time placeholder rows."
        ),
        "stochastic_surgery_capacity_planning": (
            "Once scenario feasibility is established, reopen elective-case selection and block allocation "
            "with recourse warm starts; a conservative feasible schedule is only the lower-quality baseline."
        ),
        "urban_drainage_mpc_control": (
            "Keep the header-only zero-control policy only as rollback. Repair and round the optimized MPC "
            "trajectory through the independent dynamics audit rather than reverting at the first mismatch."
        ),
    }
    if task in exact:
        guidance.append(exact[task])
    return guidance


def stage_protocol(task: str, app: Path, instruction: str) -> str:
    targets = target_candidates(app, instruction)
    contract = ", ".join(
        f"`/app/{path.relative_to(app)}`" for path in targets[:1]
    ) or "the exact current-step decision artifact named by the public statement"
    specialized = task_family_protocol(task, app)
    if (app / "data" / "booking_path_rules.csv").is_file():
        specialized.append(
            "For liner paths, transshipment count is the number of intermediate "
            "transfers (`number of loaded legs - 1`), not the number of service-name "
            "changes. Check every accepted booking against booking_path_rules.csv. "
            "Also encode every currently visible service-rule acceptance floor, "
            "medical/special booking mandate, protected-slot minimum, and transfer "
            "cap directly in the model; do not treat event tables as objective hints."
        )
    if (app / "data" / "facility_receiving_windows.csv").is_file():
        specialized.append(
            "For medical-waste routes, the CSV has no departure-time decision field. "
            "Do not invent delayed departures to place unloads into convenient dock "
            "windows. Simulate the event sequence represented by the exported rows "
            "and aggregate its actual unload kg/count by receiving window."
        )
    if (app / "data" / "boat_travel.csv").is_file():
        specialized.append(
            "For flood-relief boat trips, a shortest-path or metric-closure distance "
            "is not itself an exportable arc. Every launch-to-shelter, consecutive "
            "shelter-to-shelter, and final shelter-to-launch hop in solution.csv must "
            "be a directly listed boat_travel.csv edge (in either direction). If a "
            "shortest path uses an intermediate shelter, it must appear in the trip "
            "order and be served there exactly once. Audit the expanded exported "
            "sequence, capacity, and weather-tightened trip duration."
        )
    specialized_text = "\n".join(f"- {item}" for item in specialized)
    return f"""

Closed-loop feasibility and performance protocol:
- Required current-stage decision artifact: {contract}. Earlier-stage files are
  state and frozen commitments, not evidence that this stage is complete.
{specialized_text}
- Treat the stage as a finite-horizon control problem. Keep a known incumbent as
  the backup state and operate in three modes: RESTORATION for known violations,
  ADVERSARIAL AUDIT for structurally valid but incompletely checked artifacts,
  then PERFORMANCE. Never trade a hard violation for objective value.
- Build the required solver and a complete baseline artifact early. The model
  connection may be interrupted, so checkpoint executable code and incumbents;
  never export variable values after a time limit unless `getNSols()` or
  `getBestSol()` proves that a solution exists.
- Maintain a solver-derived state vector: artifact presence/schema, incumbent
  count, maximum normalized constraint residual, number of violated constraint
  groups, integrality residual, objective/best bound/gap when available, decision
  universe coverage, and elapsed time. A self-written checker is diagnostic, not
  proof; derive residuals independently from the exact exported artifact and
  public data rather than importing the submitted model.
- Use the maximum normalized hard-constraint residual as a Lyapunov-like
  feasibility measure `theta`. Accept restoration iterates only when `theta`
  decreases or a feasible incumbent is reached. If the same method stalls twice,
  change the search operator instead of repeating the same reasoning.
- When the exact model has no incumbent, solve an elastic Phase-I problem first:
  add explicit nonnegative slacks, lexicographically minimize maximum normalized
  violation, then total/count of violations, then distance to the best candidate.
  Keep schema, identifier domains, frozen commitments, and nonanticipativity as
  hard invariants unless the public statement explicitly permits violation.
- Project heuristic or rounded candidates back into the feasible set with a
  repair MIP. First minimize `theta`; after `theta=0`, minimize L1/Hamming distance
  to the candidate. Do not hand-edit coupled rows. For nonlinear continuous
  constraints, an exact L1 penalty or augmented-Lagrangian restoration is valid
  only with explicit residual and multiplier updates.
- Apply robust constraint tightening at numeric boundaries: leave a small
  scale-aware margin below capacities and time limits, round in the conservative
  direction, and recompute units, denominators, temporal propagation, reserves,
  ramps, conservation, activation equivalence, and both Big-M directions.
- Enter PERFORMANCE mode only after exact feasibility. Preserve the feasible
  incumbent while optimizing the real objective. Use solver bounds and gap, not
  confidence, as the stopping signal. A restricted candidate set, greedy subset,
  or decomposed master must be expanded unless there is a valid coverage or bound
  certificate showing that excluded decisions cannot improve the solution.
- Allocate short sequential portfolio slices only after a measurable stall:
  exact MIP with a warm start; feasibility pump/fix-and-propagate; local branching
  or large-neighborhood search around the incumbent; then an alternative exact
  formulation or decomposition if the coupling structure supports it. All slices
  share the incumbent and must return to it on regression.
- Before finishing, rerun the final generator from scratch and audit the exact
  artifact for schema, full key coverage, blank-field rules, domains, capacities,
  conservation, timing, scenario coupling, frozen decisions, and objective terms.
  Where practical, verify that the independent audit rejects a corrupted copy.
"""


def template_for(target: Path) -> Path | None:
    candidate = target.with_name(target.stem + "_template" + target.suffix)
    return candidate if candidate.is_file() else None


def validate_target(target: Path) -> tuple[bool, str, int]:
    if not target.is_file():
        return False, "missing", 0
    try:
        if target.suffix.lower() == ".csv":
            rows = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8-sig")), strict=True))
            if len(rows) < 2 or not rows[0] or any(not field for field in rows[0]):
                return False, "empty-or-invalid-csv", max(0, len(rows) - 1)
            if any(len(row) != len(rows[0]) for row in rows[1:]):
                return False, "ragged-csv", len(rows) - 1
            template = template_for(target)
            if template:
                template_rows = list(csv.reader(io.StringIO(template.read_text(encoding="utf-8-sig")), strict=True))
                if not template_rows or rows[0] != template_rows[0]:
                    return False, "csv-header-mismatch", len(rows) - 1
            return True, "valid-csv", len(rows) - 1
        if target.suffix.lower() == ".json":
            value = json.loads(target.read_text(encoding="utf-8"), parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
            if value in ({}, [], None):
                return False, "empty-json", 0
            template = template_for(target)
            if template:
                expected = json.loads(template.read_text(encoding="utf-8"))
                if isinstance(expected, dict) and (
                    not isinstance(value, dict) or set(value) != set(expected)
                ):
                    return False, "json-top-level-shape-mismatch", 0
            size = len(value) if isinstance(value, (dict, list)) else 1
            return True, "valid-json", size
    except (OSError, UnicodeError, csv.Error, ValueError, json.JSONDecodeError) as error:
        return False, f"parse-error:{type(error).__name__}", 0
    return False, "unsupported-extension", 0
