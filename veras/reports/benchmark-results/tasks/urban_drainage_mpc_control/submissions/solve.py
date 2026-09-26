import csv
import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from pyscipopt import Model, quicksum


BASE_DIR = "/app"
DATA_DIR = os.path.join(BASE_DIR, "data")
SUBMISSIONS_DIR = os.path.join(BASE_DIR, "submissions")
SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.csv")
TMP_CANDIDATE_PATH = os.path.join(SUBMISSIONS_DIR, "_candidate_solution.csv")

REL_GAP = 0.0005
DEFAULT_TIME_LIMIT = 300.0
EXPORT_DECIMALS = 8
EXPORT_EPS = 5e-9
EPS_ON = 1e-4
SAFETY = 1e-6


@dataclass
class SolveSummary:
    solver_status: str
    has_incumbent: bool
    incumbent_count: int
    objective: float
    best_bound: float
    gap: float
    elapsed_seconds: float
    integrality_residual: float
    validation: dict
    fallback_used: bool
    incumbent_objective: float
    incumbent_feasible: bool
    adopted_candidate: bool
    phase_records: List[dict]


def read_csv(path: str) -> List[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def to_float_map(rows: Iterable[dict], *keys: str, value_key: str) -> Dict[Tuple[str, ...], float]:
    out = {}
    for row in rows:
        out[tuple(row[k] for k in keys)] = float(row[value_key])
    return out


def format_float(value: float) -> str:
    if abs(value) < EXPORT_EPS:
        value = 0.0
    return f"{value:.{EXPORT_DECIMALS}f}"


def safe_limit(raw_limit: float) -> float:
    if raw_limit <= 0.0:
        return 0.0
    return max(0.0, raw_limit - SAFETY)


def compute_cvar(costs: Dict[str, float], probs: Dict[str, float], alpha: float) -> float:
    _, _, best = compute_cvar_details(costs, probs, alpha)
    return best


def compute_cvar_details(costs: Dict[str, float], probs: Dict[str, float], alpha: float) -> Tuple[float, Dict[str, float], float]:
    candidates = sorted(set(costs.values()))
    if not candidates:
        return 0.0, {}, 0.0
    best = math.inf
    best_eta = candidates[0]
    for eta in candidates:
        value = eta + sum(probs[s] * max(0.0, costs[s] - eta) for s in costs) / alpha
        if value < best:
            best = value
            best_eta = eta
    if not math.isfinite(best):
        return 0.0, {s: 0.0 for s in costs}, 0.0
    z = {s: max(0.0, costs[s] - best_eta) for s in costs}
    return best_eta, z, best


def load_instance() -> dict:
    with open(os.path.join(DATA_DIR, "config.json")) as f:
        config = json.load(f)

    basins_rows = read_csv(os.path.join(DATA_DIR, "basins.csv"))
    controls_rows = read_csv(os.path.join(DATA_DIR, "controls.csv"))
    groups_rows = read_csv(os.path.join(DATA_DIR, "control_groups.csv"))
    duty_rows = read_csv(os.path.join(DATA_DIR, "group_duty_limits.csv"))
    district_rows = read_csv(os.path.join(DATA_DIR, "district_service.csv"))
    scenario_rows = read_csv(os.path.join(DATA_DIR, "scenarios.csv"))
    inflow_rows = read_csv(os.path.join(DATA_DIR, "scenario_inflows.csv"))
    availability_rows = read_csv(os.path.join(DATA_DIR, "control_availability.csv"))
    outfall_rows = read_csv(os.path.join(DATA_DIR, "outfall_limits.csv"))
    treatment_rows = read_csv(os.path.join(DATA_DIR, "treatment_capacity.csv"))
    tariff_rows = read_csv(os.path.join(DATA_DIR, "energy_tariffs.csv"))

    basins = {}
    basin_order = []
    district_basins = defaultdict(list)
    for row in basins_rows:
        basin_id = row["basin_id"]
        basin_order.append(basin_id)
        basins[basin_id] = {
            "district": row["district"],
            "capacity_ml": float(row["capacity_ml"]),
            "initial_storage_ml": float(row["initial_storage_ml"]),
            "target_final_storage_ml": float(row["target_final_storage_ml"]),
            "flood_damage_cost_per_ml": float(row["flood_damage_cost_per_ml"]),
            "pollution_cost_per_ml": float(row["pollution_cost_per_ml"]),
            "critical_asset": int(row["critical_asset"]),
            "combined_sewer": int(row["combined_sewer"]),
        }
        district_basins[row["district"]].append(basin_id)

    controls = {}
    control_order = []
    controls_by_group = defaultdict(list)
    outgoing_by_basin = defaultdict(list)
    incoming_to_basin = defaultdict(list)
    outfall_controls = defaultdict(list)
    wwtp_controls = []
    first_flush_outfall_controls = []
    for row in controls_rows:
        control_id = row["control_id"]
        control_order.append(control_id)
        to_node = row["to_node"]
        control = {
            "control_type": row["control_type"],
            "from_basin": row["from_basin"],
            "to_node": to_node,
            "group_id": row["group_id"],
            "max_volume_ml": float(row["max_volume_ml"]),
            "min_positive_volume_ml": float(row["min_positive_volume_ml"]),
            "ramp_volume_ml": float(row["ramp_volume_ml"]),
            "energy_kwh_per_ml": float(row["energy_kwh_per_ml"]),
            "movement_cost_per_ml": float(row["movement_cost_per_ml"]),
            "start_cost": float(row["start_cost"]),
            "max_starts": int(row["max_starts"]),
            "tide_limited": int(row["tide_limited"]),
            "first_flush_sensitive": int(row["first_flush_sensitive"]),
            "travel_delay_slots": int(row["travel_delay_slots"]),
        }
        controls[control_id] = control
        controls_by_group[control["group_id"]].append(control_id)
        outgoing_by_basin[control["from_basin"]].append(control_id)
        if to_node in basins:
            incoming_to_basin[to_node].append(control_id)
        elif to_node == "WWTP":
            wwtp_controls.append(control_id)
        elif to_node.startswith("O_"):
            outfall_controls[to_node].append(control_id)
            if control["first_flush_sensitive"]:
                first_flush_outfall_controls.append(control_id)

    groups = {}
    for row in groups_rows:
        groups[row["group_id"]] = {
            "max_active_controls_per_slot": int(row["max_active_controls_per_slot"]),
            "max_group_starts_per_scenario": int(row["max_group_starts_per_scenario"]),
        }

    duties = []
    for row in duty_rows:
        duties.append(
            {
                "group_id": row["group_id"],
                "window_slots": int(row["window_slots"]),
                "max_volume_ml": float(row["max_volume_ml"]),
            }
        )

    districts = {}
    district_order = []
    for row in district_rows:
        district = row["district"]
        district_order.append(district)
        districts[district] = {
            "expected_overflow_target_ml": float(row["expected_overflow_target_ml"]),
            "critical_multiplier": float(row["critical_multiplier"]),
        }

    scenarios = {}
    scenario_order = []
    for row in scenario_rows:
        scenario_id = row["scenario_id"]
        scenario_order.append(scenario_id)
        scenarios[scenario_id] = {
            "probability": float(row["probability"]),
            "rainfall_scale": float(row["rainfall_scale"]),
            "tide_scale": float(row["tide_scale"]),
        }

    inflows = defaultdict(float)
    for row in inflow_rows:
        inflows[(row["scenario_id"], row["basin_id"], int(row["slot"]))] = float(row["inflow_ml"])

    availability = defaultdict(float)
    for row in availability_rows:
        availability[(row["scenario_id"], row["control_id"], int(row["slot"]))] = float(row["capacity_multiplier"])

    outfall_limits = defaultdict(float)
    outfall_ids = set()
    for row in outfall_rows:
        key = (row["scenario_id"], row["outfall_id"], int(row["slot"]))
        outfall_limits[key] = float(row["max_release_ml"])
        outfall_ids.add(row["outfall_id"])

    treatment_capacity = defaultdict(float)
    for row in treatment_rows:
        treatment_capacity[(row["scenario_id"], int(row["slot"]))] = float(row["max_treatment_ml"])

    tariffs = {}
    for row in tariff_rows:
        tariffs[int(row["slot"])] = float(row["price_per_kwh"])

    horizon = int(config["planning_horizon_slots"])
    slots = list(range(horizon))

    return {
        "config": config,
        "basins": basins,
        "basin_order": basin_order,
        "district_basins": district_basins,
        "controls": controls,
        "control_order": control_order,
        "controls_by_group": controls_by_group,
        "outgoing_by_basin": outgoing_by_basin,
        "incoming_to_basin": incoming_to_basin,
        "outfall_controls": outfall_controls,
        "outfall_ids": sorted(outfall_ids),
        "wwtp_controls": wwtp_controls,
        "first_flush_outfall_controls": first_flush_outfall_controls,
        "groups": groups,
        "duties": duties,
        "districts": districts,
        "district_order": district_order,
        "scenarios": scenarios,
        "scenario_order": scenario_order,
        "slots": slots,
        "inflows": inflows,
        "availability": availability,
        "outfall_limits": outfall_limits,
        "treatment_capacity": treatment_capacity,
        "tariffs": tariffs,
    }


def max_flow_bound(data: dict, scenario_id: str, control_id: str, slot: int) -> float:
    control = data["controls"][control_id]
    raw = control["max_volume_ml"] * data["availability"][(scenario_id, control_id, slot)]
    if control["to_node"] in data["basins"]:
        if slot + control["travel_delay_slots"] >= len(data["slots"]):
            raw = 0.0
    return safe_limit(raw)


def activity_export_threshold(control: dict) -> float:
    base = control["min_positive_volume_ml"] if control["min_positive_volume_ml"] > 0.0 else EPS_ON
    return 0.5 * base


def load_solution_flow(data: dict, path: str) -> Dict[Tuple[str, str, int], float]:
    flow = defaultdict(float)
    if not os.path.exists(path):
        return flow

    valid_scenarios = set(data["scenario_order"])
    valid_controls = set(data["control_order"])
    valid_slots = set(data["slots"])
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                s = row["scenario_id"]
                t = int(row["slot"])
                c = row["control_id"]
                v = float(row["volume_ml"])
            except Exception:
                continue
            if s in valid_scenarios and c in valid_controls and t in valid_slots and v > EXPORT_EPS:
                flow[s, c, t] = v
    return flow


def build_solution_state(data: dict, flow: Dict[Tuple[str, str, int], float]) -> dict:
    scenario_order = data["scenario_order"]
    control_order = data["control_order"]
    basin_order = data["basin_order"]
    slots = data["slots"]
    tol = float(data["config"]["feasibility_tolerance"])

    active = {}
    starts = {}
    storage = {}
    overflow = {}
    final_excess = {}
    scenario_costs = {}

    for s in scenario_order:
        for c in control_order:
            prev_active = 0
            for t in slots:
                flag = 1 if flow.get((s, c, t), 0.0) > tol else 0
                active[s, c, t] = flag
                starts[s, c, t] = 1 if flag and not prev_active else 0
                prev_active = flag

        for b in basin_order:
            basin = data["basins"][b]
            prev_storage = basin["initial_storage_ml"]
            for t in slots:
                arrivals = 0.0
                for c in data["incoming_to_basin"][b]:
                    delay = data["controls"][c]["travel_delay_slots"]
                    if t - delay >= 0:
                        arrivals += flow.get((s, c, t - delay), 0.0)
                outflow = sum(flow.get((s, c, t), 0.0) for c in data["outgoing_by_basin"][b])
                available = prev_storage + data["inflows"][(s, b, t)] + arrivals
                end_raw = available - outflow
                storage[s, b, t] = min(basin["capacity_ml"], max(0.0, end_raw))
                overflow[s, b, t] = max(0.0, end_raw - basin["capacity_ml"])
                prev_storage = storage[s, b, t]
            final_excess[s, b] = max(0.0, storage[s, b, slots[-1]] - basin["target_final_storage_ml"])

        scenario_cost = 0.0
        for b in basin_order:
            basin = data["basins"][b]
            for t in slots:
                scenario_cost += overflow[s, b, t] * (
                    basin["flood_damage_cost_per_ml"] + basin["combined_sewer"] * basin["pollution_cost_per_ml"]
                )
        for c in control_order:
            control = data["controls"][c]
            for t in slots:
                volume = flow.get((s, c, t), 0.0)
                scenario_cost += volume * control["energy_kwh_per_ml"] * data["tariffs"][t]
                scenario_cost += volume * control["movement_cost_per_ml"]
                scenario_cost += starts[s, c, t] * control["start_cost"]
                if c in data["first_flush_outfall_controls"] and t < int(data["config"]["first_flush_slots"]):
                    scenario_cost += float(data["config"]["first_flush_release_penalty_per_ml"]) * volume
        for b in basin_order:
            scenario_cost += float(data["config"]["final_storage_penalty_per_ml"]) * final_excess[s, b]
        scenario_costs[s] = scenario_cost

    district_excess = {}
    for d in data["district_order"]:
        expected_overflow = sum(
            data["scenarios"][s]["probability"] * sum(overflow[s, b, t] for b in data["district_basins"][d] for t in slots)
            for s in scenario_order
        )
        district_excess[d] = max(0.0, expected_overflow - data["districts"][d]["expected_overflow_target_ml"])

    probs = {s: data["scenarios"][s]["probability"] for s in scenario_order}
    eta, z, cvar = compute_cvar_details(scenario_costs, probs, float(data["config"]["cvar_tail_probability"]))
    return {
        "active": active,
        "starts": starts,
        "storage": storage,
        "overflow": overflow,
        "final_excess": final_excess,
        "scenario_costs": scenario_costs,
        "district_excess": district_excess,
        "eta": eta,
        "z": z,
        "cvar": cvar,
    }


def build_model(data: dict, time_limit: float) -> Tuple[Model, dict]:
    model = Model("urban_drainage_mpc_control")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("numerics/feastol", float(data["config"]["feasibility_tolerance"]))

    scenario_order = data["scenario_order"]
    control_order = data["control_order"]
    basin_order = data["basin_order"]
    slots = data["slots"]

    x = {}
    y = {}
    u = {}
    for s in scenario_order:
        for c in control_order:
            control = data["controls"][c]
            min_if_on = control["min_positive_volume_ml"] if control["min_positive_volume_ml"] > 0 else EPS_ON
            for t in slots:
                ub = max_flow_bound(data, s, c, t)
                x[s, c, t] = model.addVar(lb=0.0, ub=ub, vtype="C", name=f"x[{s},{c},{t}]")
                y[s, c, t] = model.addVar(vtype="B", name=f"y[{s},{c},{t}]")
                u[s, c, t] = model.addVar(vtype="B", name=f"u[{s},{c},{t}]")

                model.addCons(x[s, c, t] <= ub * y[s, c, t], name=f"active_ub[{s},{c},{t}]")
                model.addCons(x[s, c, t] >= min_if_on * y[s, c, t], name=f"active_lb[{s},{c},{t}]")

    h = {}
    o = {}
    final_excess = {}
    for s in scenario_order:
        for b in basin_order:
            cap = safe_limit(data["basins"][b]["capacity_ml"])
            for t in slots:
                h[s, b, t] = model.addVar(lb=0.0, ub=cap, vtype="C", name=f"h[{s},{b},{t}]")
                o[s, b, t] = model.addVar(lb=0.0, vtype="C", name=f"o[{s},{b},{t}]")
            final_excess[s, b] = model.addVar(lb=0.0, vtype="C", name=f"finalex[{s},{b}]")

    district_excess = {
        d: model.addVar(lb=0.0, vtype="C", name=f"districtex[{d}]") for d in data["district_order"]
    }
    scenario_cost = {
        s: model.addVar(lb=0.0, vtype="C", name=f"cost[{s}]") for s in scenario_order
    }
    eta = model.addVar(lb=0.0, vtype="C", name="eta")
    z = {s: model.addVar(lb=0.0, vtype="C", name=f"z[{s}]") for s in scenario_order}

    for s in scenario_order:
        for b in basin_order:
            basin = data["basins"][b]
            outgoing = data["outgoing_by_basin"][b]
            incoming = data["incoming_to_basin"][b]
            for t in slots:
                prev = basin["initial_storage_ml"] if t == 0 else h[s, b, t - 1]
                arrivals = quicksum(
                    x[s, c, t - data["controls"][c]["travel_delay_slots"]]
                    for c in incoming
                    if t - data["controls"][c]["travel_delay_slots"] >= 0
                )
                outflow = quicksum(x[s, c, t] for c in outgoing)
                inflow = data["inflows"][(s, b, t)]
                model.addCons(
                    h[s, b, t] + o[s, b, t] == prev + inflow + arrivals - outflow,
                    name=f"balance[{s},{b},{t}]",
                )

        for c in control_order:
            ramp = data["controls"][c]["ramp_volume_ml"]
            model.addCons(x[s, c, 0] <= safe_limit(ramp), name=f"ramp0[{s},{c}]")
            model.addCons(u[s, c, 0] == y[s, c, 0], name=f"start0[{s},{c}]")
            for t in slots[1:]:
                model.addCons(x[s, c, t] - x[s, c, t - 1] <= safe_limit(ramp), name=f"rup[{s},{c},{t}]")
                model.addCons(x[s, c, t - 1] - x[s, c, t] <= safe_limit(ramp), name=f"rdown[{s},{c},{t}]")
                model.addCons(u[s, c, t] >= y[s, c, t] - y[s, c, t - 1], name=f"startlb[{s},{c},{t}]")
                model.addCons(u[s, c, t] <= y[s, c, t], name=f"startub1[{s},{c},{t}]")
                model.addCons(u[s, c, t] <= 1 - y[s, c, t - 1], name=f"startub2[{s},{c},{t}]")

            model.addCons(
                quicksum(u[s, c, t] for t in slots) <= data["controls"][c]["max_starts"],
                name=f"control_start_limit[{s},{c}]",
            )

        for group_id, group in data["groups"].items():
            controls = data["controls_by_group"][group_id]
            for t in slots:
                model.addCons(
                    quicksum(y[s, c, t] for c in controls) <= group["max_active_controls_per_slot"],
                    name=f"group_active[{s},{group_id},{t}]",
                )
            model.addCons(
                quicksum(u[s, c, t] for c in controls for t in slots) <= group["max_group_starts_per_scenario"],
                name=f"group_starts[{s},{group_id}]",
            )

        for duty in data["duties"]:
            controls = data["controls_by_group"][duty["group_id"]]
            window = duty["window_slots"]
            rhs = safe_limit(duty["max_volume_ml"])
            for start_t in range(0, len(slots) - window + 1):
                model.addCons(
                    quicksum(x[s, c, t] for c in controls for t in range(start_t, start_t + window)) <= rhs,
                    name=f"duty[{s},{duty['group_id']},{window},{start_t}]",
                )

        for t in slots:
            if data["wwtp_controls"]:
                model.addCons(
                    quicksum(x[s, c, t] for c in data["wwtp_controls"])
                    <= safe_limit(data["treatment_capacity"][(s, t)]),
                    name=f"wwtp[{s},{t}]",
                )
            for outfall_id, controls in data["outfall_controls"].items():
                model.addCons(
                    quicksum(x[s, c, t] for c in controls)
                    <= safe_limit(data["outfall_limits"][(s, outfall_id, t)]),
                    name=f"outfall[{s},{outfall_id},{t}]",
                )

        for b in basin_order:
            model.addCons(
                final_excess[s, b] >= h[s, b, slots[-1]] - data["basins"][b]["target_final_storage_ml"],
                name=f"final_excess[{s},{b}]",
            )

    lock_slots = int(data["config"]["locked_nonanticipative_slots"])
    anchor = scenario_order[0]
    for s in scenario_order[1:]:
        for c in control_order:
            for t in range(lock_slots):
                model.addCons(x[s, c, t] == x[anchor, c, t], name=f"lock[{s},{c},{t}]")

    first_flush_slots = int(data["config"]["first_flush_slots"])
    first_flush_controls = set(data["first_flush_outfall_controls"])
    alpha = float(data["config"]["cvar_tail_probability"])
    tail_weight = float(data["config"]["tail_risk_weight"])
    final_penalty = float(data["config"]["final_storage_penalty_per_ml"])
    first_flush_penalty = float(data["config"]["first_flush_release_penalty_per_ml"])
    district_penalty = float(data["config"]["district_overflow_penalty_per_ml"])

    for s in scenario_order:
        overflow_cost = quicksum(
            o[s, b, t]
            * (
                data["basins"][b]["flood_damage_cost_per_ml"]
                + data["basins"][b]["combined_sewer"] * data["basins"][b]["pollution_cost_per_ml"]
            )
            for b in basin_order
            for t in slots
        )
        energy_cost = quicksum(
            x[s, c, t] * data["controls"][c]["energy_kwh_per_ml"] * data["tariffs"][t]
            for c in control_order
            for t in slots
        )
        movement_cost = quicksum(
            x[s, c, t] * data["controls"][c]["movement_cost_per_ml"] for c in control_order for t in slots
        )
        start_cost = quicksum(
            u[s, c, t] * data["controls"][c]["start_cost"] for c in control_order for t in slots
        )
        first_flush_cost = quicksum(
            first_flush_penalty * x[s, c, t]
            for c in first_flush_controls
            for t in range(first_flush_slots)
        )
        final_cost = quicksum(final_penalty * final_excess[s, b] for b in basin_order)

        model.addCons(
            scenario_cost[s] == overflow_cost + energy_cost + movement_cost + start_cost + first_flush_cost + final_cost,
            name=f"scenario_cost[{s}]",
        )
        model.addCons(z[s] >= scenario_cost[s] - eta, name=f"cvar_excess[{s}]")

    district_term = []
    for d in data["district_order"]:
        expected_overflow = quicksum(
            data["scenarios"][s]["probability"] * o[s, b, t]
            for s in scenario_order
            for b in data["district_basins"][d]
            for t in slots
        )
        model.addCons(
            district_excess[d] >= expected_overflow - data["districts"][d]["expected_overflow_target_ml"],
            name=f"district_excess[{d}]",
        )
        district_term.append(
            district_penalty * data["districts"][d]["critical_multiplier"] * district_excess[d]
        )

    expected_cost = quicksum(data["scenarios"][s]["probability"] * scenario_cost[s] for s in scenario_order)
    cvar_term = eta + quicksum(data["scenarios"][s]["probability"] * z[s] for s in scenario_order) / alpha
    model.setObjective(expected_cost + quicksum(district_term) + tail_weight * cvar_term, "minimize")

    return model, {
        "x": x,
        "y": y,
        "u": u,
        "h": h,
        "o": o,
        "final_excess": final_excess,
        "district_excess": district_excess,
        "scenario_cost": scenario_cost,
        "eta": eta,
        "z": z,
    }


def write_solution(data: dict, flow_values: Dict[Tuple[str, str, int], float], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario_id", "slot", "control_id", "volume_ml"])
        for s in data["scenario_order"]:
            for t in data["slots"]:
                for c in data["control_order"]:
                    value = max(0.0, flow_values.get((s, c, t), 0.0))
                    if value < activity_export_threshold(data["controls"][c]):
                        value = 0.0
                    if value > EXPORT_EPS:
                        writer.writerow([s, t, c, format_float(value)])


def validate_solution(data: dict, path: str) -> dict:
    tol = float(data["config"]["feasibility_tolerance"])
    scenario_order = data["scenario_order"]
    control_order = data["control_order"]
    basin_order = data["basin_order"]
    slots = data["slots"]
    valid_scenarios = set(scenario_order)
    valid_controls = set(control_order)
    valid_slots = set(slots)

    families = defaultdict(int)
    max_residual = 0.0

    def register_violation(family: str, residual: float) -> None:
        nonlocal max_residual
        families[family] += 1
        max_residual = max(max_residual, residual)

    def norm_residual(viol: float, scale: float) -> float:
        return max(0.0, viol) / max(1.0, abs(scale))

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows = list(reader)

    schema_ok = header == ["scenario_id", "slot", "control_id", "volume_ml"]
    if not schema_ok:
        register_violation("schema", 1.0)

    flow = defaultdict(float)
    seen_keys = set()
    for row in rows:
        try:
            s = row["scenario_id"]
            t = int(row["slot"])
            c = row["control_id"]
            v = float(row["volume_ml"])
        except Exception:
            register_violation("schema", 1.0)
            continue

        if s not in valid_scenarios or c not in valid_controls or t not in valid_slots:
            register_violation("domain", 1.0)
            continue
        if (s, c, t) in seen_keys:
            register_violation("duplicates", 1.0)
            continue
        seen_keys.add((s, c, t))
        if v < -tol:
            register_violation("nonnegative", norm_residual(-v, 1.0))
        flow[s, c, t] = max(0.0, v)

    lock_slots = int(data["config"]["locked_nonanticipative_slots"])
    anchor = scenario_order[0]
    for c in control_order:
        for t in range(lock_slots):
            base = flow[anchor, c, t]
            for s in scenario_order[1:]:
                resid = abs(flow[s, c, t] - base)
                if resid > tol:
                    register_violation("locked_prefix", norm_residual(resid, max(1.0, base)))

    starts = defaultdict(int)
    scenario_costs = {s: 0.0 for s in scenario_order}
    storage = defaultdict(float)
    overflow = defaultdict(float)

    for s in scenario_order:
        for c in control_order:
            control = data["controls"][c]
            prev = 0.0
            prev_active = 0
            start_count = 0
            min_positive = control["min_positive_volume_ml"]
            for t in slots:
                v = flow[s, c, t]
                ub = max_flow_bound(data, s, c, t)
                if v - ub > tol:
                    register_violation("capacity", norm_residual(v - ub, ub))
                if v > tol and min_positive > 0.0 and min_positive - v > tol:
                    register_violation("min_positive", norm_residual(min_positive - v, min_positive))
                ramp_resid = abs(v - prev) - control["ramp_volume_ml"]
                if ramp_resid > tol:
                    register_violation("ramp", norm_residual(ramp_resid, control["ramp_volume_ml"]))
                active = 1 if v > tol else 0
                if active and not prev_active:
                    start_count += 1
                    starts[s, c, t] = 1
                prev = v
                prev_active = active
            if start_count - control["max_starts"] > tol:
                register_violation("control_starts", norm_residual(start_count - control["max_starts"], control["max_starts"]))

        for group_id, group in data["groups"].items():
            controls = data["controls_by_group"][group_id]
            group_starts = 0
            for t in slots:
                active_count = sum(1 for c in controls if flow[s, c, t] > tol)
                if active_count - group["max_active_controls_per_slot"] > tol:
                    register_violation(
                        "group_active",
                        norm_residual(active_count - group["max_active_controls_per_slot"], group["max_active_controls_per_slot"]),
                    )
                group_starts += sum(starts[s, c, t] for c in controls)
            if group_starts - group["max_group_starts_per_scenario"] > tol:
                register_violation(
                    "group_starts",
                    norm_residual(group_starts - group["max_group_starts_per_scenario"], group["max_group_starts_per_scenario"]),
                )

        for duty in data["duties"]:
            controls = data["controls_by_group"][duty["group_id"]]
            window = duty["window_slots"]
            limit = duty["max_volume_ml"]
            for start_t in range(0, len(slots) - window + 1):
                volume = sum(flow[s, c, t] for c in controls for t in range(start_t, start_t + window))
                if volume - limit > tol:
                    register_violation("duty", norm_residual(volume - limit, limit))

        for t in slots:
            if data["wwtp_controls"]:
                wwtp_volume = sum(flow[s, c, t] for c in data["wwtp_controls"])
                cap = data["treatment_capacity"][(s, t)]
                if wwtp_volume - cap > tol:
                    register_violation("wwtp", norm_residual(wwtp_volume - cap, cap))

            for outfall_id, controls in data["outfall_controls"].items():
                release = sum(flow[s, c, t] for c in controls)
                cap = data["outfall_limits"][(s, outfall_id, t)]
                if release - cap > tol:
                    register_violation("outfall", norm_residual(release - cap, cap))

        for b in basin_order:
            basin = data["basins"][b]
            prev_storage = basin["initial_storage_ml"]
            for t in slots:
                arrivals = 0.0
                for c in data["incoming_to_basin"][b]:
                    delay = data["controls"][c]["travel_delay_slots"]
                    if t - delay >= 0:
                        arrivals += flow[s, c, t - delay]
                outflow = sum(flow[s, c, t] for c in data["outgoing_by_basin"][b])
                available = prev_storage + data["inflows"][(s, b, t)] + arrivals
                if outflow - available > tol:
                    register_violation("source_availability", norm_residual(outflow - available, available))
                end_raw = available - outflow
                stor = min(basin["capacity_ml"], max(0.0, end_raw))
                of = max(0.0, end_raw - basin["capacity_ml"])
                storage[s, b, t] = stor
                overflow[s, b, t] = of
                prev_storage = stor

    expected_overflow_by_district = {}
    for d in data["district_order"]:
        expected_overflow_by_district[d] = sum(
            data["scenarios"][s]["probability"] * sum(overflow[s, b, t] for b in data["district_basins"][d] for t in slots)
            for s in scenario_order
        )

    first_flush_slots = int(data["config"]["first_flush_slots"])
    first_flush_penalty = float(data["config"]["first_flush_release_penalty_per_ml"])
    final_penalty = float(data["config"]["final_storage_penalty_per_ml"])
    district_penalty = float(data["config"]["district_overflow_penalty_per_ml"])
    tail_weight = float(data["config"]["tail_risk_weight"])
    alpha = float(data["config"]["cvar_tail_probability"])

    for s in scenario_order:
        cost = 0.0
        for b in basin_order:
            basin = data["basins"][b]
            for t in slots:
                cost += overflow[s, b, t] * (
                    basin["flood_damage_cost_per_ml"] + basin["combined_sewer"] * basin["pollution_cost_per_ml"]
                )
        for c in control_order:
            control = data["controls"][c]
            for t in slots:
                v = flow[s, c, t]
                cost += v * control["energy_kwh_per_ml"] * data["tariffs"][t]
                cost += v * control["movement_cost_per_ml"]
                if starts[s, c, t]:
                    cost += control["start_cost"]
                if c in data["first_flush_outfall_controls"] and t < first_flush_slots:
                    cost += first_flush_penalty * v
        for b in basin_order:
            final_excess = max(0.0, storage[s, b, slots[-1]] - data["basins"][b]["target_final_storage_ml"])
            cost += final_penalty * final_excess
        scenario_costs[s] = cost

    district_cost = sum(
        district_penalty
        * data["districts"][d]["critical_multiplier"]
        * max(0.0, expected_overflow_by_district[d] - data["districts"][d]["expected_overflow_target_ml"])
        for d in data["district_order"]
    )
    expected_cost = sum(data["scenarios"][s]["probability"] * scenario_costs[s] for s in scenario_order)
    cvar = compute_cvar(scenario_costs, {s: data["scenarios"][s]["probability"] for s in scenario_order}, alpha)
    objective = expected_cost + district_cost + tail_weight * cvar

    return {
        "schema_ok": schema_ok,
        "row_count": len(rows),
        "families_violated": sorted(families.keys()),
        "family_violation_count": dict(families),
        "violated_constraint_groups": len(families),
        "max_normalized_residual": max_residual,
        "objective_estimate": objective,
        "expected_cost_estimate": expected_cost,
        "district_penalty_estimate": district_cost,
        "cvar_estimate": cvar,
        "scenario_costs": scenario_costs,
        "decision_universe_coverage": {
            "scenario_count": len(scenario_order),
            "slot_count": len(slots),
            "control_count": len(control_order),
            "sparse_rows_written": len(rows),
            "implicit_zero_entries": len(scenario_order) * len(slots) * len(control_order) - len(seen_keys),
        },
        "feasible": len(families) == 0,
    }


def inject_mip_start(model: Model, variables: dict, data: dict, flow: Dict[Tuple[str, str, int], float], path: str) -> bool:
    if not flow:
        return False

    state = build_solution_state(data, flow)
    objective = (
        sum(data["scenarios"][s]["probability"] * state["scenario_costs"][s] for s in data["scenario_order"])
        + sum(
            float(data["config"]["district_overflow_penalty_per_ml"])
            * data["districts"][d]["critical_multiplier"]
            * state["district_excess"][d]
            for d in data["district_order"]
        )
        + float(data["config"]["tail_risk_weight"]) * state["cvar"]
    )

    with open(path, "w") as f:
        f.write(f"objective value:                                    {objective:.15g}\n")
        for key, var in variables["x"].items():
            f.write(f"{var.name} {flow.get(key, 0.0):.15g}\n")
        for key, var in variables["y"].items():
            f.write(f"{var.name} {state['active'][key]}\n")
        for key, var in variables["u"].items():
            f.write(f"{var.name} {state['starts'][key]}\n")
        for key, var in variables["h"].items():
            f.write(f"{var.name} {state['storage'][key]:.15g}\n")
        for key, var in variables["o"].items():
            f.write(f"{var.name} {state['overflow'][key]:.15g}\n")
        for key, var in variables["final_excess"].items():
            f.write(f"{var.name} {state['final_excess'][key]:.15g}\n")
        for key, var in variables["district_excess"].items():
            f.write(f"{var.name} {state['district_excess'][key]:.15g}\n")
        for key, var in variables["scenario_cost"].items():
            f.write(f"{var.name} {state['scenario_costs'][key]:.15g}\n")
        f.write(f"{variables['eta'].name} {state['eta']:.15g}\n")
        for key, var in variables["z"].items():
            f.write(f"{var.name} {state['z'][key]:.15g}\n")

    model.presolve()
    before = model.getNSols()
    model.readSol(path)
    return model.getNSols() > before


def add_local_branching_constraint(
    model: Model,
    variables: dict,
    data: dict,
    flow: Dict[Tuple[str, str, int], float],
    radius: int,
) -> None:
    tol = float(data["config"]["feasibility_tolerance"])
    lock_slots = int(data["config"]["locked_nonanticipative_slots"])
    active_keys = []
    inactive_keys = []
    for s in data["scenario_order"]:
        for c in data["control_order"]:
            for t in data["slots"]:
                if t < lock_slots:
                    continue
                key = (s, c, t)
                if flow.get(key, 0.0) > tol:
                    active_keys.append(key)
                else:
                    inactive_keys.append(key)

    expr = quicksum(1 - variables["y"][key] for key in active_keys) + quicksum(variables["y"][key] for key in inactive_keys)
    model.addCons(expr <= radius, name=f"local_branching[{radius}]")


def extract_solution_from_model(data: dict, variables: dict, model: Model) -> Tuple[Dict[Tuple[str, str, int], float], float]:
    flow_values = {}
    integrality_residual = 0.0
    best_sol = model.getBestSol()
    if best_sol is None:
        return flow_values, integrality_residual

    for key, var in variables["x"].items():
        value = model.getSolVal(best_sol, var)
        control = data["controls"][key[1]]
        if value >= activity_export_threshold(control):
            flow_values[key] = max(0.0, value)

    for family in ("y", "u"):
        for var in variables[family].values():
            value = model.getSolVal(best_sol, var)
            integrality_residual = max(integrality_residual, abs(value - round(value)))

    return flow_values, integrality_residual


def objective_improved(candidate: Optional[dict], incumbent: Optional[dict]) -> bool:
    if candidate is None or not candidate.get("feasible", False):
        return False
    if incumbent is None or not incumbent.get("feasible", False):
        return True
    return candidate["objective_estimate"] + 1e-6 < incumbent["objective_estimate"]


def phase_allocation(time_limit: float) -> List[Tuple[str, float, Optional[int]]]:
    if time_limit <= 0.0:
        return []
    if time_limit < 60.0:
        return [("exact", time_limit, None)]
    if time_limit < 180.0:
        exact = 0.6 * time_limit
        return [("exact", exact, None), ("local_branching_48", time_limit - exact, 48)]
    exact = 0.4 * time_limit
    lb1 = 0.3 * time_limit
    lb2 = time_limit - exact - lb1
    return [("exact", exact, None), ("local_branching_48", lb1, 48), ("local_branching_96", lb2, 96)]


def run_phase(
    data: dict,
    start_flow: Dict[Tuple[str, str, int], float],
    phase_name: str,
    phase_time_limit: float,
    local_branching_radius: Optional[int],
    candidate_path: str,
) -> dict:
    start_time = time.time()
    model, variables = build_model(data, phase_time_limit)
    if local_branching_radius is not None and start_flow:
        add_local_branching_constraint(model, variables, data, start_flow, local_branching_radius)
    accepted_mip_start = inject_mip_start(model, variables, data, start_flow, TMP_CANDIDATE_PATH + ".sol") if start_flow else False

    model.optimize()
    elapsed = time.time() - start_time
    status = str(model.getStatus())
    incumbent_count = model.getNSols()
    has_incumbent = incumbent_count > 0 and model.getBestSol() is not None

    flow_values = {}
    integrality_residual = 0.0
    if has_incumbent:
        flow_values, integrality_residual = extract_solution_from_model(data, variables, model)
    write_solution(data, flow_values, candidate_path)
    validation = validate_solution(data, candidate_path)
    if os.path.exists(TMP_CANDIDATE_PATH + ".sol"):
        os.remove(TMP_CANDIDATE_PATH + ".sol")

    return {
        "phase_name": phase_name,
        "local_branching_radius": local_branching_radius,
        "accepted_mip_start": accepted_mip_start,
        "solver_status": status,
        "has_incumbent": has_incumbent,
        "incumbent_count": incumbent_count,
        "objective": model.getObjVal() if has_incumbent else float("nan"),
        "best_bound": model.getDualbound() if has_incumbent else float("nan"),
        "gap": model.getGap() if has_incumbent else float("nan"),
        "elapsed_seconds": elapsed,
        "integrality_residual": integrality_residual,
        "validation": validation,
        "flow_values": flow_values,
    }


def solve() -> SolveSummary:
    data = load_instance()
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    time_limit = DEFAULT_TIME_LIMIT
    if env_limit:
        time_limit = min(time_limit, float(env_limit))

    incumbent_validation = validate_solution(data, SOLUTION_PATH) if os.path.exists(SOLUTION_PATH) else None
    incumbent_flow = load_solution_flow(data, SOLUTION_PATH) if incumbent_validation and incumbent_validation.get("feasible", False) else {}
    best_validation = incumbent_validation
    best_flow = dict(incumbent_flow)
    best_phase = None
    phase_records = []

    for phase_name, phase_time_limit, radius in phase_allocation(time_limit):
        if phase_time_limit <= 0.0:
            continue
        phase = run_phase(data, best_flow, phase_name, phase_time_limit, radius, TMP_CANDIDATE_PATH)
        adopted = objective_improved(phase["validation"], best_validation)
        phase_records.append(
            {
                "phase_name": phase["phase_name"],
                "local_branching_radius": phase["local_branching_radius"],
                "accepted_mip_start": phase["accepted_mip_start"],
                "solver_status": phase["solver_status"],
                "has_incumbent": phase["has_incumbent"],
                "incumbent_count": phase["incumbent_count"],
                "objective": phase["objective"],
                "best_bound": phase["best_bound"],
                "gap": phase["gap"],
                "elapsed_seconds": phase["elapsed_seconds"],
                "integrality_residual": phase["integrality_residual"],
                "candidate_objective": phase["validation"]["objective_estimate"],
                "candidate_feasible": phase["validation"]["feasible"],
                "candidate_violated_constraint_groups": phase["validation"]["violated_constraint_groups"],
                "candidate_max_normalized_residual": phase["validation"]["max_normalized_residual"],
                "adopted_as_best": adopted,
            }
        )
        if adopted:
            best_validation = phase["validation"]
            best_flow = dict(phase["flow_values"])
            best_phase = phase

    incumbent_feasible = bool(incumbent_validation and incumbent_validation.get("feasible", False))
    adopted_candidate = objective_improved(best_validation, incumbent_validation)
    fallback_used = False

    if adopted_candidate:
        write_solution(data, best_flow, SOLUTION_PATH)

    final_validation = validate_solution(data, SOLUTION_PATH)
    if os.path.exists(TMP_CANDIDATE_PATH):
        os.remove(TMP_CANDIDATE_PATH)

    chosen_phase = best_phase if adopted_candidate and best_phase is not None else (best_phase if best_phase is not None and not incumbent_feasible else None)
    solver_status = chosen_phase["solver_status"] if chosen_phase is not None else "incumbent_kept"
    has_incumbent = bool(chosen_phase and chosen_phase["has_incumbent"])
    incumbent_count = int(chosen_phase["incumbent_count"]) if chosen_phase is not None else 0
    objective = chosen_phase["objective"] if chosen_phase is not None else float("nan")
    best_bound = chosen_phase["best_bound"] if chosen_phase is not None else float("nan")
    gap = chosen_phase["gap"] if chosen_phase is not None else float("nan")
    elapsed_seconds = sum(record["elapsed_seconds"] for record in phase_records)
    integrality_residual = chosen_phase["integrality_residual"] if chosen_phase is not None else 0.0

    return SolveSummary(
        solver_status=solver_status,
        has_incumbent=has_incumbent,
        incumbent_count=incumbent_count,
        objective=objective,
        best_bound=best_bound,
        gap=gap,
        elapsed_seconds=elapsed_seconds,
        integrality_residual=integrality_residual,
        validation=final_validation,
        fallback_used=fallback_used,
        incumbent_objective=incumbent_validation["objective_estimate"] if incumbent_validation else float("nan"),
        incumbent_feasible=incumbent_feasible,
        adopted_candidate=adopted_candidate,
        phase_records=phase_records,
    )


def main() -> None:
    summary = solve()
    print(json.dumps(
        {
            "incumbent_feasible": summary.incumbent_feasible,
            "incumbent_objective": summary.incumbent_objective,
            "adopted_candidate": summary.adopted_candidate,
            "solver_status": summary.solver_status,
            "has_incumbent": summary.has_incumbent,
            "incumbent_count": summary.incumbent_count,
            "objective": summary.objective,
            "best_bound": summary.best_bound,
            "gap": summary.gap,
            "elapsed_seconds": summary.elapsed_seconds,
            "integrality_residual": summary.integrality_residual,
            "fallback_used": summary.fallback_used,
            "phase_records": summary.phase_records,
            "validation": summary.validation,
        },
        indent=2,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
