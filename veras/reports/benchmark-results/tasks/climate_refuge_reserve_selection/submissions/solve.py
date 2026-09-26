import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Tuple

from pyscipopt import Model, quicksum


BASE_DIR = "/app"
DATA_DIR = os.path.join(BASE_DIR, "data")
SUBMISSIONS_DIR = os.path.join(BASE_DIR, "submissions")
SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.csv")
TEMP_SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution_candidate.csv")
LOG_PATH = os.path.join(SUBMISSIONS_DIR, "solve_log.md")

REL_GAP = 0.0005
MAX_SINGLE_ATTEMPT_SECONDS = 300.0
FEAS_TOL = 1e-6


def _read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, "r", newline="") as fh:
        return list(csv.DictReader(fh))


def _float_fmt(value: float) -> str:
    if value is None or (isinstance(value, float) and (math.isinf(value) or math.isnan(value))):
        return "n/a"
    return f"{value:.8f}"


def _norm_residual_lte(lhs: float, rhs: float) -> float:
    return max(0.0, lhs - rhs) / max(1.0, abs(rhs))


def _norm_residual_gte(lhs: float, rhs: float) -> float:
    return max(0.0, rhs - lhs) / max(1.0, abs(rhs))


def get_time_limit() -> float:
    env_value = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_value is None:
        return MAX_SINGLE_ATTEMPT_SECONDS
    try:
        parsed = float(env_value)
    except ValueError:
        return MAX_SINGLE_ATTEMPT_SECONDS
    return max(1.0, min(MAX_SINGLE_ATTEMPT_SECONDS, parsed))


def load_instance() -> Dict:
    with open(os.path.join(DATA_DIR, "config.json"), "r") as fh:
        config = json.load(fh)

    action_rows = _read_csv(os.path.join(DATA_DIR, "actions.csv"))
    parcel_rows = _read_csv(os.path.join(DATA_DIR, "parcels.csv"))
    feature_rows = _read_csv(os.path.join(DATA_DIR, "features.csv"))
    scenario_rows = _read_csv(os.path.join(DATA_DIR, "scenarios.csv"))
    conflict_rows = _read_csv(os.path.join(DATA_DIR, "parcel_conflicts.csv"))
    region_rows = _read_csv(os.path.join(DATA_DIR, "region_requirements.csv"))
    link_rows = _read_csv(os.path.join(DATA_DIR, "parcel_feature_habitat.csv"))

    actions = {}
    for row in action_rows:
        act = row["action"]
        actions[act] = {
            "fixed_cost_multiplier": float(row["fixed_cost_multiplier"]),
            "maintenance_multiplier": float(row["maintenance_multiplier"]),
            "staff_multiplier": float(row["staff_multiplier"]),
            "habitat_multiplier": float(row["habitat_multiplier"]),
            "upland_multiplier": float(row["upland_multiplier"]),
            "nursery_units": float(row["nursery_units"]),
            "public_access_required": int(row["public_access_required"]),
        }

    parcels = {}
    for row in parcel_rows:
        pid = row["parcel_id"]
        parcels[pid] = {
            "region": row["region"],
            "owner_type": row["owner_type"],
            "x": float(row["x"]),
            "y": float(row["y"]),
            "area_ha": float(row["area_ha"]),
            "current_marsh_index": float(row["current_marsh_index"]),
            "upland_migration_index": float(row["upland_migration_index"]),
            "acquisition_cost": float(row["acquisition_cost"]),
            "annual_maintenance_cost": float(row["annual_maintenance_cost"]),
            "staff_days": float(row["staff_days"]),
            "salinity_resilience": float(row["salinity_resilience"]),
            "migration_corridor": int(row["migration_corridor"]),
            "public_access": int(row["public_access"]),
            "protect_allowed": int(row["protect_allowed"]),
            "restore_allowed": int(row["restore_allowed"]),
            "easement_allowed": int(row["easement_allowed"]),
        }

    features = {}
    for row in feature_rows:
        fid = row["feature_id"]
        features[fid] = {
            "taxon_group": row["taxon_group"],
            "home_region": row["home_region"],
            "conservation_value": float(row["conservation_value"]),
            "vulnerability_weight": float(row["vulnerability_weight"]),
            "base_target_units": float(row["base_target_units"]),
            "priority_min_rate": float(row["priority_min_rate"]),
        }

    scenarios = []
    for row in scenario_rows:
        scenarios.append(
            {
                "scenario_id": row["scenario_id"],
                "probability": float(row["probability"]),
                "sea_level_stress": float(row["sea_level_stress"]),
                "development_pressure": float(row["development_pressure"]),
                "salinity_shift": float(row["salinity_shift"]),
                "restoration_success": float(row["restoration_success"]),
            }
        )

    regions = {}
    for row in region_rows:
        region = row["region"]
        regions[region] = {
            "min_selected": int(row["min_selected"]),
            "max_selected": int(row["max_selected"]),
            "min_public_access": int(row["min_public_access"]),
            "staff_day_cap": float(row["staff_day_cap"]),
        }

    conflicts = [(row["parcel_id_1"], row["parcel_id_2"], row["reason"]) for row in conflict_rows]

    links_by_feature = defaultdict(list)
    link_value = {}
    for row in link_rows:
        pid = row["parcel_id"]
        fid = row["feature_id"]
        base_link = float(row["base_habitat_units"]) * float(row["connectivity_factor"])
        links_by_feature[fid].append((pid, base_link))
        link_value[(pid, fid)] = base_link

    eligible = []
    eligible_by_parcel = defaultdict(list)
    for pid, pdata in parcels.items():
        if pdata["protect_allowed"]:
            eligible.append((pid, "protect"))
            eligible_by_parcel[pid].append("protect")
        if pdata["restore_allowed"]:
            eligible.append((pid, "restore"))
            eligible_by_parcel[pid].append("restore")
        if pdata["easement_allowed"]:
            eligible.append((pid, "migration_easement"))
            eligible_by_parcel[pid].append("migration_easement")

    priority_features = [fid for fid, fdata in features.items() if fdata["priority_min_rate"] > 0.0]

    return {
        "config": config,
        "actions": actions,
        "parcels": parcels,
        "features": features,
        "scenarios": scenarios,
        "regions": regions,
        "conflicts": conflicts,
        "links_by_feature": dict(links_by_feature),
        "link_value": link_value,
        "eligible": eligible,
        "eligible_by_parcel": dict(eligible_by_parcel),
        "priority_features": priority_features,
    }


def action_quality(parcel: Dict, action: Dict, action_name: str, scenario: Dict) -> float:
    marsh_term = (
        parcel["current_marsh_index"] * action["habitat_multiplier"] / scenario["sea_level_stress"]
    )
    upland_term = (
        parcel["upland_migration_index"] * action["upland_multiplier"] / scenario["development_pressure"]
    )
    salinity_term = parcel["salinity_resilience"] / scenario["salinity_shift"]
    restore_term = scenario["restoration_success"] if action_name == "restore" else 1.0
    return (marsh_term + upland_term) * salinity_term * restore_term


def feature_target(feature: Dict, scenario: Dict) -> float:
    avg_stress = (
        scenario["sea_level_stress"]
        + scenario["development_pressure"]
        + scenario["salinity_shift"]
    ) / 3.0
    target = feature["base_target_units"] * (
        1.0 + (feature["vulnerability_weight"] - 1.0) * (avg_stress - 1.0)
    )
    return max(FEAS_TOL, target)


def derive_parameters(instance: Dict) -> Dict:
    parcels = instance["parcels"]
    actions = instance["actions"]
    features = instance["features"]
    scenarios = instance["scenarios"]
    eligible = instance["eligible"]
    links_by_feature = instance["links_by_feature"]

    costs = {}
    maintenance = {}
    staff = {}
    nursery = {}
    quality = {}
    target = {}
    max_possible_cover = {}

    for pid, action_name in eligible:
        pdata = parcels[pid]
        adata = actions[action_name]
        costs[(pid, action_name)] = pdata["acquisition_cost"] * adata["fixed_cost_multiplier"]
        maintenance[(pid, action_name)] = (
            pdata["annual_maintenance_cost"] * adata["maintenance_multiplier"]
        )
        staff[(pid, action_name)] = pdata["staff_days"] * adata["staff_multiplier"]
        nursery[(pid, action_name)] = adata["nursery_units"]

    for scenario in scenarios:
        sid = scenario["scenario_id"]
        for pid, action_name in eligible:
            quality[(pid, action_name, sid)] = action_quality(
                parcels[pid], actions[action_name], action_name, scenario
            )
        for fid, fdata in features.items():
            target[(fid, sid)] = feature_target(fdata, scenario)
            upper = 0.0
            for pid, base_link in links_by_feature.get(fid, []):
                best = max(quality[(pid, a, sid)] for a in instance["eligible_by_parcel"][pid])
                upper += base_link * best
            max_possible_cover[(fid, sid)] = upper

    return {
        "capital_cost": costs,
        "maintenance_cost": maintenance,
        "staff_cost": staff,
        "nursery_cost": nursery,
        "quality": quality,
        "target": target,
        "max_possible_cover": max_possible_cover,
    }


def add_common_constraints(model: Model, x: Dict, instance: Dict, params: Dict) -> None:
    config = instance["config"]
    parcels = instance["parcels"]
    regions = instance["regions"]
    eligible = instance["eligible"]
    conflicts = instance["conflicts"]
    capital_cost = params["capital_cost"]
    maintenance_cost = params["maintenance_cost"]
    staff_cost = params["staff_cost"]
    nursery_cost = params["nursery_cost"]

    for pid, allowed in instance["eligible_by_parcel"].items():
        model.addCons(
            quicksum(x[(pid, action_name)] for action_name in allowed) <= 1,
            name=f"one_action[{pid}]",
        )

    total_selected = quicksum(x[key] for key in eligible)
    model.addCons(total_selected >= int(config["minimum_selected_parcels"]), name="min_selected")
    model.addCons(total_selected <= int(config["maximum_selected_parcels"]), name="max_selected")

    model.addCons(
        quicksum(capital_cost[key] * x[key] for key in eligible) <= float(config["capital_budget"]),
        name="capital_budget",
    )
    model.addCons(
        quicksum(maintenance_cost[key] * x[key] for key in eligible)
        <= float(config["annual_maintenance_budget"]),
        name="maintenance_budget",
    )
    model.addCons(
        quicksum(staff_cost[key] * x[key] for key in eligible) <= float(config["staff_day_budget"]),
        name="total_staff_budget",
    )
    model.addCons(
        quicksum(nursery_cost[key] * x[key] for key in eligible)
        <= float(config["nursery_unit_budget"]),
        name="nursery_budget",
    )

    model.addCons(
        quicksum(x[(pid, "protect")] for pid, action_name in eligible if action_name == "protect")
        >= int(config["minimum_protect_actions"]),
        name="min_protect_actions",
    )
    model.addCons(
        quicksum(
            x[(pid, "migration_easement")]
            for pid, action_name in eligible
            if action_name == "migration_easement"
        )
        >= int(config["minimum_migration_easement_actions"]),
        name="min_easement_actions",
    )
    model.addCons(
        quicksum(x[(pid, "restore")] for pid, action_name in eligible if action_name == "restore")
        <= int(config["maximum_restore_actions"]),
        name="max_restore_actions",
    )

    model.addCons(
        quicksum(
            parcels[pid]["migration_corridor"] * x[(pid, action_name)]
            for pid, action_name in eligible
        )
        >= int(config["minimum_migration_corridors"]),
        name="min_corridors",
    )

    for region, rdata in regions.items():
        keys = [key for key in eligible if parcels[key[0]]["region"] == region]
        model.addCons(
            quicksum(x[key] for key in keys) >= rdata["min_selected"],
            name=f"region_min_selected[{region}]",
        )
        model.addCons(
            quicksum(x[key] for key in keys) <= rdata["max_selected"],
            name=f"region_max_selected[{region}]",
        )
        model.addCons(
            quicksum(parcels[key[0]]["public_access"] * x[key] for key in keys)
            >= rdata["min_public_access"],
            name=f"region_min_public[{region}]",
        )
        model.addCons(
            quicksum(staff_cost[key] * x[key] for key in keys) <= rdata["staff_day_cap"],
            name=f"region_staff_cap[{region}]",
        )

    for p1, p2, reason in conflicts:
        model.addCons(
            quicksum(x[(p1, a)] for a in instance["eligible_by_parcel"][p1])
            + quicksum(x[(p2, a)] for a in instance["eligible_by_parcel"][p2])
            <= 1,
            name=f"conflict[{p1},{p2},{reason}]",
        )


def build_model(instance: Dict, params: Dict, time_limit: float) -> Tuple[Model, Dict, Dict, object, object]:
    config = instance["config"]
    parcels = instance["parcels"]
    features = instance["features"]
    scenarios = instance["scenarios"]
    eligible = instance["eligible"]
    links_by_feature = instance["links_by_feature"]
    priority_features = instance["priority_features"]

    capital_cost = params["capital_cost"]
    maintenance_cost = params["maintenance_cost"]
    quality = params["quality"]
    target = params["target"]
    max_possible_cover = params["max_possible_cover"]

    model = Model("climate_refuge_reserve_selection")
    model.hideOutput()
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("randomization/permutationseed", 0)

    x = {}
    for pid, action_name in eligible:
        x[(pid, action_name)] = model.addVar(vtype="B", name=f"x[{pid},{action_name}]")

    z = {}
    for fid in features:
        for scenario in scenarios:
            sid = scenario["scenario_id"]
            ub = 1.0 if max_possible_cover[(fid, sid)] + FEAS_TOL >= target[(fid, sid)] else 0.0
            z[(fid, sid)] = model.addVar(vtype="B", ub=ub, name=f"z[{fid},{sid}]")

    floor_ub = sum(fdata["conservation_value"] for fdata in features.values())
    worst_floor = model.addVar(lb=0.0, ub=floor_ub, vtype="C", name="worst_floor")

    add_common_constraints(model, x, instance, params)

    score_expr = {}
    for scenario in scenarios:
        sid = scenario["scenario_id"]
        for fid in features:
            coverage_terms = [
                base_link * quality[(pid, action_name, sid)] * x[(pid, action_name)]
                for pid, base_link in links_by_feature.get(fid, [])
                for action_name in instance["eligible_by_parcel"][pid]
            ]
            lhs = quicksum(coverage_terms) if coverage_terms else 0.0
            model.addCons(
                lhs >= target[(fid, sid)] * z[(fid, sid)],
                name=f"coverage[{fid},{sid}]",
            )

        score_expr[sid] = quicksum(
            features[fid]["conservation_value"] * z[(fid, sid)] for fid in features
        )
        model.addCons(worst_floor <= score_expr[sid], name=f"floor[{sid}]")

        priority_rhs = sum(
            features[fid]["priority_min_rate"] * features[fid]["conservation_value"]
            for fid in priority_features
        )
        model.addCons(
            quicksum(features[fid]["conservation_value"] * z[(fid, sid)] for fid in priority_features)
            >= priority_rhs,
            name=f"priority_value[{sid}]",
        )

    expected_score_expr = quicksum(
        scenario["probability"] * score_expr[scenario["scenario_id"]] for scenario in scenarios
    )
    cost_expr = quicksum(
        (capital_cost[key] + maintenance_cost[key]) * x[key] for key in eligible
    )
    objective = config["benefit_scale"] * (
        expected_score_expr + config["robust_floor_weight"] * worst_floor
    ) - config["cost_scale"] * cost_expr
    model.setObjective(objective, sense="maximize")

    return model, x, z, worst_floor, objective


def build_phase1_model(instance: Dict, params: Dict, time_limit: float) -> Tuple[Model, Dict]:
    features = instance["features"]
    scenarios = instance["scenarios"]
    eligible = instance["eligible"]
    links_by_feature = instance["links_by_feature"]
    priority_features = instance["priority_features"]
    quality = params["quality"]
    target = params["target"]
    max_possible_cover = params["max_possible_cover"]

    model = Model("climate_refuge_phase1")
    model.hideOutput()
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("randomization/permutationseed", 0)

    x = {}
    for pid, action_name in eligible:
        x[(pid, action_name)] = model.addVar(vtype="B", name=f"x[{pid},{action_name}]")

    add_common_constraints(model, x, instance, params)

    z = {}
    slack = {}
    theta = model.addVar(lb=0.0, vtype="C", name="theta")
    priority_rhs = sum(
        features[fid]["priority_min_rate"] * features[fid]["conservation_value"]
        for fid in priority_features
    )

    priority_score_expr = {}
    for scenario in scenarios:
        sid = scenario["scenario_id"]
        for fid in priority_features:
            ub = 1.0 if max_possible_cover[(fid, sid)] + FEAS_TOL >= target[(fid, sid)] else 0.0
            z[(fid, sid)] = model.addVar(vtype="B", ub=ub, name=f"zp[{fid},{sid}]")
            coverage_terms = [
                base_link * quality[(pid, action_name, sid)] * x[(pid, action_name)]
                for pid, base_link in links_by_feature.get(fid, [])
                for action_name in instance["eligible_by_parcel"][pid]
            ]
            lhs = quicksum(coverage_terms) if coverage_terms else 0.0
            model.addCons(
                lhs >= target[(fid, sid)] * z[(fid, sid)],
                name=f"priority_coverage[{fid},{sid}]",
            )

        priority_score_expr[sid] = quicksum(
            features[fid]["conservation_value"] * z[(fid, sid)] for fid in priority_features
        )
        slack[sid] = model.addVar(lb=0.0, vtype="C", name=f"slack[{sid}]")
        model.addCons(priority_score_expr[sid] + slack[sid] >= priority_rhs, name=f"priority_slack[{sid}]")
        model.addCons(theta >= slack[sid], name=f"theta_lb[{sid}]")

    expected_priority = quicksum(
        scenario["probability"] * priority_score_expr[scenario["scenario_id"]] for scenario in scenarios
    )
    model.setObjective(
        1_000_000.0 * theta
        + 1_000.0 * quicksum(slack[s["scenario_id"]] for s in scenarios)
        - expected_priority,
        sense="minimize",
    )
    return model, x


def build_base_feasibility_model(instance: Dict, params: Dict, time_limit: float) -> Tuple[Model, Dict]:
    config = instance["config"]
    eligible = instance["eligible"]
    capital_cost = params["capital_cost"]
    maintenance_cost = params["maintenance_cost"]
    staff_cost = params["staff_cost"]
    nursery_cost = params["nursery_cost"]

    model = Model("base_feasibility")
    model.hideOutput()
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("randomization/permutationseed", 0)

    x = {}
    for pid, action_name in eligible:
        x[(pid, action_name)] = model.addVar(vtype="B", name=f"x[{pid},{action_name}]")

    add_common_constraints(model, x, instance, params)

    norm_cost = quicksum(
        capital_cost[key] / max(1.0, float(config["capital_budget"])) * x[key] for key in eligible
    )
    norm_cost += quicksum(
        maintenance_cost[key] / max(1.0, float(config["annual_maintenance_budget"])) * x[key]
        for key in eligible
    )
    norm_cost += quicksum(
        staff_cost[key] / max(1.0, float(config["staff_day_budget"])) * x[key] for key in eligible
    )
    norm_cost += quicksum(
        nursery_cost[key] / max(1.0, float(config["nursery_unit_budget"])) * x[key] for key in eligible
    )
    norm_cost += 0.001 * quicksum(x[key] for key in eligible)
    model.setObjective(norm_cost, sense="minimize")
    return model, x


def build_priority_repair_model(
    instance: Dict, params: Dict, candidate: Dict[str, str], time_limit: float
) -> Tuple[Model, Dict, Dict, Dict, object, float]:
    features = instance["features"]
    scenarios = instance["scenarios"]
    eligible = instance["eligible"]
    links_by_feature = instance["links_by_feature"]
    priority_features = instance["priority_features"]
    quality = params["quality"]
    target = params["target"]
    max_possible_cover = params["max_possible_cover"]

    model = Model("priority_repair")
    model.hideOutput()
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("randomization/permutationseed", 0)

    x = {}
    for pid, action_name in eligible:
        x[(pid, action_name)] = model.addVar(vtype="B", name=f"x[{pid},{action_name}]")

    add_common_constraints(model, x, instance, params)

    z = {}
    slack = {}
    theta = model.addVar(lb=0.0, vtype="C", name="theta")
    priority_rhs = sum(
        features[fid]["priority_min_rate"] * features[fid]["conservation_value"]
        for fid in priority_features
    )
    priority_rhs = max(priority_rhs, FEAS_TOL)

    for scenario in scenarios:
        sid = scenario["scenario_id"]
        for fid in priority_features:
            ub = 1.0 if max_possible_cover[(fid, sid)] + FEAS_TOL >= target[(fid, sid)] else 0.0
            z[(fid, sid)] = model.addVar(vtype="B", ub=ub, name=f"zp[{fid},{sid}]")
            coverage_terms = [
                base_link * quality[(pid, action_name, sid)] * x[(pid, action_name)]
                for pid, base_link in links_by_feature.get(fid, [])
                for action_name in instance["eligible_by_parcel"][pid]
            ]
            lhs = quicksum(coverage_terms) if coverage_terms else 0.0
            model.addCons(
                lhs >= target[(fid, sid)] * z[(fid, sid)],
                name=f"priority_coverage[{fid},{sid}]",
            )

        slack[sid] = model.addVar(lb=0.0, vtype="C", name=f"slack[{sid}]")
        priority_score = quicksum(
            features[fid]["conservation_value"] * z[(fid, sid)] for fid in priority_features
        )
        model.addCons(priority_score + slack[sid] >= priority_rhs, name=f"priority_slack[{sid}]")
        model.addCons(theta >= slack[sid] / priority_rhs, name=f"theta_lb[{sid}]")

    distance_expr = quicksum(
        (1.0 - x[(pid, action_name)]) if candidate.get(pid) == action_name else x[(pid, action_name)]
        for pid, action_name in eligible
    )
    total_norm_slack = quicksum(slack[s["scenario_id"]] / priority_rhs for s in scenarios)
    model.setObjective(1_000_000.0 * theta + 10_000.0 * total_norm_slack + distance_expr, sense="minimize")
    return model, x, z, slack, theta, priority_rhs


def extract_solution(model: Model, xvars: Dict) -> Dict[str, str]:
    if model.getNSols() <= 0 or model.getBestSol() is None:
        raise RuntimeError("SCIP finished without an incumbent solution.")
    selected = {}
    for (pid, action_name), var in xvars.items():
        if model.getVal(var) > 0.5:
            selected[pid] = action_name
    return dict(sorted(selected.items()))


def add_warm_start(
    model: Model, xvars: Dict, zvars: Dict, worst_floor_var, audit: Dict, solution: Dict[str, str]
) -> None:
    sol = model.createSol()
    for (pid, action_name), var in xvars.items():
        model.setSolVal(sol, var, 1.0 if solution.get(pid) == action_name else 0.0)
    for key, var in zvars.items():
        model.setSolVal(sol, var, float(audit["covered"][key]))
    model.setSolVal(sol, worst_floor_var, audit["worst_floor"])
    model.addSol(sol, free=True)


def add_priority_repair_warm_start(
    model: Model,
    xvars: Dict,
    zvars: Dict,
    slackvars: Dict,
    theta_var,
    candidate_audit: Dict,
    solution: Dict[str, str],
    features: Dict,
    priority_features: List[str],
    priority_rhs: float,
) -> None:
    sol = model.createSol()
    for (pid, action_name), var in xvars.items():
        model.setSolVal(sol, var, 1.0 if solution.get(pid) == action_name else 0.0)
    for (fid, sid), var in zvars.items():
        model.setSolVal(sol, var, float(candidate_audit["covered"][(fid, sid)]))
    theta_val = 0.0
    for sid, var in slackvars.items():
        priority_value = 0.0
        for fid in priority_features:
            if candidate_audit["covered"][(fid, sid)]:
                priority_value += features[fid]["conservation_value"]
        slack = max(0.0, priority_rhs - priority_value)
        theta_val = max(theta_val, slack / max(priority_rhs, FEAS_TOL))
        model.setSolVal(sol, var, slack)
    model.setSolVal(sol, theta_var, theta_val)
    model.addSol(sol, free=True)


def write_solution(solution: Dict[str, str], path: str = SOLUTION_PATH) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["parcel_id", "action"])
        for pid, action in sorted(solution.items()):
            writer.writerow([pid, action])


def read_solution(path: str) -> Dict[str, str]:
    with open(path, "r", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ["parcel_id", "action"]:
            raise ValueError(f"Unexpected columns in solution file: {reader.fieldnames}")
        solution = {}
        for row in reader:
            pid = row["parcel_id"]
            action = row["action"]
            if pid in solution:
                raise ValueError(f"Duplicate parcel row in solution: {pid}")
            solution[pid] = action
    return solution


def audit_solution_file(instance: Dict, params: Dict, path: str) -> Tuple[Dict[str, str], Dict]:
    solution = read_solution(path)
    audit = audit_solution(instance, params, solution)
    return solution, audit


def try_load_existing_incumbent(instance: Dict, params: Dict) -> Tuple[Dict[str, str], Dict, str]:
    if not os.path.exists(SOLUTION_PATH):
        return None, None, "missing"
    try:
        solution, audit = audit_solution_file(instance, params, SOLUTION_PATH)
    except Exception as exc:
        return None, None, f"invalid: {exc}"
    if audit["violated_group_count"] > 0 or audit["max_normalized_residual"] > FEAS_TOL:
        return None, None, "audit_failed"
    return solution, audit, "valid"


def audit_solution(instance: Dict, params: Dict, solution: Dict[str, str]) -> Dict:
    config = instance["config"]
    parcels = instance["parcels"]
    features = instance["features"]
    scenarios = instance["scenarios"]
    regions = instance["regions"]
    links_by_feature = instance["links_by_feature"]
    conflicts = instance["conflicts"]
    eligible_by_parcel = instance["eligible_by_parcel"]
    priority_features = instance["priority_features"]

    capital_cost = params["capital_cost"]
    maintenance_cost = params["maintenance_cost"]
    staff_cost = params["staff_cost"]
    nursery_cost = params["nursery_cost"]
    quality = params["quality"]
    target = params["target"]

    selected_pairs = []
    for pid, action in solution.items():
        if pid not in parcels:
            raise ValueError(f"Unknown parcel in solution: {pid}")
        if action not in eligible_by_parcel.get(pid, []):
            raise ValueError(f"Action {action} is not allowed for parcel {pid}")
        selected_pairs.append((pid, action))

    residuals = {}
    violated_groups = set()

    total_selected = len(selected_pairs)
    residuals["min_selected"] = _norm_residual_gte(
        total_selected, float(config["minimum_selected_parcels"])
    )
    residuals["max_selected"] = _norm_residual_lte(
        total_selected, float(config["maximum_selected_parcels"])
    )
    if residuals["min_selected"] > FEAS_TOL or residuals["max_selected"] > FEAS_TOL:
        violated_groups.add("selected_count")

    capital = sum(capital_cost[key] for key in selected_pairs)
    maintenance = sum(maintenance_cost[key] for key in selected_pairs)
    staff = sum(staff_cost[key] for key in selected_pairs)
    nursery = sum(nursery_cost[key] for key in selected_pairs)
    corridor = sum(parcels[pid]["migration_corridor"] for pid, _ in selected_pairs)

    residuals["capital_budget"] = _norm_residual_lte(capital, float(config["capital_budget"]))
    residuals["maintenance_budget"] = _norm_residual_lte(
        maintenance, float(config["annual_maintenance_budget"])
    )
    residuals["total_staff_budget"] = _norm_residual_lte(
        staff, float(config["staff_day_budget"])
    )
    residuals["nursery_budget"] = _norm_residual_lte(
        nursery, float(config["nursery_unit_budget"])
    )
    residuals["corridor_min"] = _norm_residual_gte(
        corridor, float(config["minimum_migration_corridors"])
    )

    for key in [
        "capital_budget",
        "maintenance_budget",
        "total_staff_budget",
        "nursery_budget",
        "corridor_min",
    ]:
        if residuals[key] > FEAS_TOL:
            violated_groups.add(key)

    action_counts = defaultdict(int)
    for _, action in selected_pairs:
        action_counts[action] += 1
    residuals["min_protect_actions"] = _norm_residual_gte(
        action_counts["protect"], float(config["minimum_protect_actions"])
    )
    residuals["min_easement_actions"] = _norm_residual_gte(
        action_counts["migration_easement"], float(config["minimum_migration_easement_actions"])
    )
    residuals["max_restore_actions"] = _norm_residual_lte(
        action_counts["restore"], float(config["maximum_restore_actions"])
    )
    for key in ["min_protect_actions", "min_easement_actions", "max_restore_actions"]:
        if residuals[key] > FEAS_TOL:
            violated_groups.add(key)

    selected_parcels = set(solution)
    max_conflict_resid = 0.0
    for p1, p2, _ in conflicts:
        lhs = float(p1 in selected_parcels) + float(p2 in selected_parcels)
        max_conflict_resid = max(max_conflict_resid, _norm_residual_lte(lhs, 1.0))
    residuals["conflicts"] = max_conflict_resid
    if max_conflict_resid > FEAS_TOL:
        violated_groups.add("conflicts")

    region_counts = defaultdict(int)
    region_public = defaultdict(int)
    region_staff = defaultdict(float)
    for pid, action in selected_pairs:
        region = parcels[pid]["region"]
        region_counts[region] += 1
        region_public[region] += parcels[pid]["public_access"]
        region_staff[region] += staff_cost[(pid, action)]

    region_residuals = {}
    for region, rdata in regions.items():
        region_residuals[f"{region}_min_selected"] = _norm_residual_gte(
            region_counts[region], float(rdata["min_selected"])
        )
        region_residuals[f"{region}_max_selected"] = _norm_residual_lte(
            region_counts[region], float(rdata["max_selected"])
        )
        region_residuals[f"{region}_min_public"] = _norm_residual_gte(
            region_public[region], float(rdata["min_public_access"])
        )
        region_residuals[f"{region}_staff_cap"] = _norm_residual_lte(
            region_staff[region], float(rdata["staff_day_cap"])
        )
    residuals.update(region_residuals)
    if any(value > FEAS_TOL for value in region_residuals.values()):
        violated_groups.add("region_requirements")

    coverage = {}
    covered = {}
    scenario_scores = {}
    priority_requirement = sum(
        features[fid]["priority_min_rate"] * features[fid]["conservation_value"]
        for fid in priority_features
    )
    priority_residual = 0.0

    for scenario in scenarios:
        sid = scenario["scenario_id"]
        score = 0.0
        priority_score = 0.0
        for fid in features:
            total = 0.0
            for pid, base_link in links_by_feature.get(fid, []):
                action = solution.get(pid)
                if action is None:
                    continue
                total += base_link * quality[(pid, action, sid)]
            coverage[(fid, sid)] = total
            is_covered = total + FEAS_TOL >= target[(fid, sid)]
            covered[(fid, sid)] = int(is_covered)
            if is_covered:
                score += features[fid]["conservation_value"]
                if fid in priority_features:
                    priority_score += features[fid]["conservation_value"]
        scenario_scores[sid] = score
        priority_residual = max(priority_residual, _norm_residual_gte(priority_score, priority_requirement))

    residuals["priority_requirement"] = priority_residual
    if priority_residual > FEAS_TOL:
        violated_groups.add("priority_requirement")

    expected_score = sum(
        scenario["probability"] * scenario_scores[scenario["scenario_id"]] for scenario in scenarios
    )
    worst_floor = min(scenario_scores.values()) if scenario_scores else 0.0
    objective = config["benefit_scale"] * (
        expected_score + config["robust_floor_weight"] * worst_floor
    ) - config["cost_scale"] * (capital + maintenance)

    max_normalized_residual = max(residuals.values()) if residuals else 0.0
    integrality_residual = 0.0
    decision_universe_coverage = len(solution) / max(1, len(instance["parcels"]))

    return {
        "selected_count": total_selected,
        "capital": capital,
        "maintenance": maintenance,
        "staff": staff,
        "nursery": nursery,
        "corridor_count": corridor,
        "action_counts": dict(action_counts),
        "scenario_scores": scenario_scores,
        "expected_score": expected_score,
        "worst_floor": worst_floor,
        "objective": objective,
        "priority_requirement_rhs": priority_requirement,
        "max_normalized_residual": max_normalized_residual,
        "violated_constraint_groups": sorted(violated_groups),
        "violated_group_count": len(violated_groups),
        "integrality_residual": integrality_residual,
        "decision_universe_coverage": decision_universe_coverage,
        "residuals": residuals,
        "coverage": coverage,
        "covered": covered,
    }


def add_local_branching_constraint(model: Model, xvars: Dict, instance: Dict, incumbent_solution: Dict[str, str], radius: int) -> None:
    diff_expr = quicksum(
        (1.0 - xvars[(pid, action_name)]) if incumbent_solution.get(pid) == action_name else xvars[(pid, action_name)]
        for pid, action_name in instance["eligible"]
    )
    model.addCons(diff_expr <= radius, name=f"local_branching[{radius}]")


def run_restoration(instance: Dict, params: Dict, total_budget: float, phase_results: List[Dict]) -> Tuple[Dict[str, str], Dict]:
    base_budget = min(30.0, max(10.0, 0.12 * total_budget))
    base_model, base_xvars = build_base_feasibility_model(instance, params, base_budget)
    base_start = time.time()
    base_model.optimize()
    base_runtime = time.time() - base_start
    base_status = str(base_model.getStatus())
    base_nsols = base_model.getNSols()
    base_obj = (
        base_model.getObjVal()
        if base_nsols > 0 and base_model.getBestSol() is not None
        else float("nan")
    )
    phase_results.append(
        {
            "name": "base_feasibility",
            "status": base_status,
            "runtime": base_runtime,
            "nsols": base_nsols,
            "objective": base_obj,
        }
    )
    if base_nsols <= 0 or base_model.getBestSol() is None:
        raise RuntimeError("Base feasibility MIP did not produce an incumbent.")

    base_solution = extract_solution(base_model, base_xvars)
    base_audit = audit_solution(instance, params, base_solution)

    remaining = max(1.0, total_budget - base_runtime)
    repair_model, repair_xvars, repair_zvars, repair_slacks, repair_theta, priority_rhs = build_priority_repair_model(
        instance, params, base_solution, remaining
    )
    add_priority_repair_warm_start(
        repair_model,
        repair_xvars,
        repair_zvars,
        repair_slacks,
        repair_theta,
        base_audit,
        base_solution,
        instance["features"],
        instance["priority_features"],
        priority_rhs,
    )
    repair_start = time.time()
    repair_model.optimize()
    repair_runtime = time.time() - repair_start
    repair_status = str(repair_model.getStatus())
    repair_nsols = repair_model.getNSols()
    repair_obj = (
        repair_model.getObjVal()
        if repair_nsols > 0 and repair_model.getBestSol() is not None
        else float("nan")
    )
    phase_results.append(
        {
            "name": "priority_repair",
            "status": repair_status,
            "runtime": repair_runtime,
            "nsols": repair_nsols,
            "objective": repair_obj,
        }
    )
    if repair_nsols <= 0 or repair_model.getBestSol() is None:
        raise RuntimeError("Priority repair MIP did not produce an incumbent.")

    solution = extract_solution(repair_model, repair_xvars)
    audit = audit_solution(instance, params, solution)
    if audit["violated_group_count"] > 0 or audit["max_normalized_residual"] > FEAS_TOL:
        raise RuntimeError("Restoration candidate failed the independent audit.")
    return solution, audit


def run_performance_search(
    instance: Dict,
    params: Dict,
    incumbent_solution: Dict[str, str],
    incumbent_audit: Dict,
    total_budget: float,
    phase_results: List[Dict],
) -> Tuple[Dict[str, str], Dict, Dict]:
    best_solution = incumbent_solution
    best_audit = incumbent_audit
    best_obj = incumbent_audit["objective"]
    best_stats = {
        "status": "not_run",
        "nsols": 0,
        "solver_obj": float("nan"),
        "dual_bound": float("nan"),
        "gap": float("nan"),
        "candidate_audit": incumbent_audit,
        "candidate_source": "rollback_incumbent",
        "updated": False,
    }

    if total_budget <= 1.0:
        return best_solution, best_audit, best_stats

    performance_slices = [("performance_exact", max(1.0, min(total_budget * 0.7, total_budget)))]
    remaining = max(0.0, total_budget - performance_slices[0][1])
    if remaining >= 15.0:
        performance_slices.append(("local_branching", remaining))

    for name, budget in performance_slices:
        if budget <= 1.0:
            continue

        model, xvars, zvars, worst_floor_var, objective_expr = build_model(instance, params, budget)
        model.addCons(objective_expr >= best_obj + 1e-6, name=f"improve_cut[{name}]")
        if name == "local_branching":
            add_local_branching_constraint(model, xvars, instance, best_solution, radius=12)
        add_warm_start(model, xvars, zvars, worst_floor_var, best_audit, best_solution)

        solve_start = time.time()
        model.optimize()
        solve_runtime = time.time() - solve_start
        status = str(model.getStatus())
        nsols = model.getNSols()
        solver_obj = model.getObjVal() if nsols > 0 and model.getBestSol() is not None else float("nan")
        dual_bound = model.getDualbound()
        gap = model.getGap() if hasattr(model, "getGap") else float("nan")
        phase_results.append(
            {
                "name": name,
                "status": status,
                "runtime": solve_runtime,
                "nsols": nsols,
                "objective": solver_obj,
            }
        )

        candidate_solution = None
        candidate_audit = None
        if nsols > 0 and model.getBestSol() is not None:
            candidate_solution = extract_solution(model, xvars)
            write_solution(candidate_solution, TEMP_SOLUTION_PATH)
            _, candidate_audit = audit_solution_file(instance, params, TEMP_SOLUTION_PATH)
            if (
                candidate_audit["violated_group_count"] == 0
                and candidate_audit["max_normalized_residual"] <= FEAS_TOL
                and candidate_audit["objective"] > best_obj + 1e-6
            ):
                best_solution = candidate_solution
                best_audit = candidate_audit
                best_obj = candidate_audit["objective"]
                best_stats = {
                    "status": status,
                    "nsols": nsols,
                    "solver_obj": solver_obj,
                    "dual_bound": dual_bound,
                    "gap": gap,
                    "candidate_audit": candidate_audit,
                    "candidate_source": name,
                    "updated": True,
                }
                os.replace(TEMP_SOLUTION_PATH, SOLUTION_PATH)
            else:
                if os.path.exists(TEMP_SOLUTION_PATH):
                    os.remove(TEMP_SOLUTION_PATH)
                if not best_stats["updated"]:
                    best_stats = {
                        "status": status,
                        "nsols": nsols,
                        "solver_obj": solver_obj,
                        "dual_bound": dual_bound,
                        "gap": gap,
                        "candidate_audit": candidate_audit if candidate_audit is not None else incumbent_audit,
                        "candidate_source": name,
                        "updated": False,
                    }
        else:
            if not best_stats["updated"]:
                best_stats = {
                    "status": status,
                    "nsols": nsols,
                    "solver_obj": solver_obj,
                    "dual_bound": dual_bound,
                    "gap": gap,
                    "candidate_audit": best_audit,
                    "candidate_source": name,
                    "updated": False,
                }

    return best_solution, best_audit, best_stats


def write_log(
    command: str,
    time_limit: float,
    runtime: float,
    status: str,
    nsols: int,
    solver_obj: float,
    dual_bound: float,
    gap: float,
    audit: Dict,
    incumbent_audit: Dict = None,
    candidate_audit: Dict = None,
    artifact_updated: bool = False,
    candidate_source: str = "n/a",
    phases: List[Dict] = None,
) -> None:
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        f"- `PYTHONPATH=/opt/or-deps python -c \"import pyscipopt; print(pyscipopt.__version__)\"`",
        f"- `{command}`",
        "",
        "## Solver",
        "",
    ]

    if phases:
        for phase in phases:
            lines.append(
                f"- Phase `{phase['name']}`: status `{phase['status']}`, runtime `{_float_fmt(phase['runtime'])}`, incumbents `{phase['nsols']}`, objective `{_float_fmt(phase['objective'])}`"
            )
        lines.append("")

    lines.extend(
        [
            f"- Status: `{status}`",
            f"- Time limit seconds: `{_float_fmt(time_limit)}`",
            f"- Runtime seconds: `{_float_fmt(runtime)}`",
            f"- Incumbent count: `{nsols}`",
            f"- Solver objective: `{_float_fmt(solver_obj)}`",
            f"- Dual bound: `{_float_fmt(dual_bound)}`",
            f"- Relative gap: `{_float_fmt(gap)}`",
            "",
            "## Validation",
            "",
            f"- Rollback incumbent objective: `{_float_fmt(incumbent_audit['objective'])}`" if incumbent_audit else "- Rollback incumbent objective: `n/a`",
            f"- Candidate source: `{candidate_source}`",
            f"- Candidate objective: `{_float_fmt(candidate_audit['objective'])}`" if candidate_audit else "- Candidate objective: `n/a`",
            f"- Artifact updated: `{'yes' if artifact_updated else 'no'}`",
            "",
            f"- Selected parcels: `{audit['selected_count']}`",
            f"- Capital used: `{_float_fmt(audit['capital'])}`",
            f"- Maintenance used: `{_float_fmt(audit['maintenance'])}`",
            f"- Staff used: `{_float_fmt(audit['staff'])}`",
            f"- Nursery units used: `{_float_fmt(audit['nursery'])}`",
            f"- Corridor parcels selected: `{audit['corridor_count']}`",
            f"- Expected covered value: `{_float_fmt(audit['expected_score'])}`",
            f"- Worst-scenario covered value: `{_float_fmt(audit['worst_floor'])}`",
            f"- Recomputed objective: `{_float_fmt(audit['objective'])}`",
            f"- Max normalized hard-constraint residual: `{_float_fmt(audit['max_normalized_residual'])}`",
            f"- Violated constraint groups: `{audit['violated_group_count']}`",
            f"- Integrality residual: `{_float_fmt(audit['integrality_residual'])}`",
            f"- Decision universe coverage: `{_float_fmt(audit['decision_universe_coverage'])}`",
            "",
            "## Action Counts",
            "",
        ]
    )

    for action_name in ["protect", "restore", "migration_easement"]:
        lines.append(f"- `{action_name}`: `{audit['action_counts'].get(action_name, 0)}`")

    lines.extend(["", "## Violations", ""])
    if audit["violated_constraint_groups"]:
        for group in audit["violated_constraint_groups"]:
            lines.append(f"- `{group}`")
    else:
        lines.append("- None")

    lines.extend(["", "## Scenario Scores", ""])
    for sid in sorted(audit["scenario_scores"]):
        lines.append(f"- `{sid}`: `{_float_fmt(audit['scenario_scores'][sid])}`")

    with open(LOG_PATH, "w", newline="") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    start = time.time()
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    command = "PYTHONPATH=/opt/or-deps python /app/submissions/solve.py"
    if env_limit is not None:
        command = f"ORCLAW_SOLVE_TIME_LIMIT_SECONDS={env_limit} " + command
    time_limit = get_time_limit()

    instance = load_instance()
    params = derive_parameters(instance)
    phase_results = []
    incumbent_solution, incumbent_audit, incumbent_state = try_load_existing_incumbent(instance, params)
    artifact_updated = False
    candidate_audit = None
    candidate_source = "rollback_incumbent"

    if incumbent_solution is None:
        restoration_budget = min(time_limit, max(30.0, 0.35 * time_limit))
        incumbent_solution, incumbent_audit = run_restoration(instance, params, restoration_budget, phase_results)
        write_solution(incumbent_solution, SOLUTION_PATH)
        incumbent_solution, incumbent_audit = audit_solution_file(instance, params, SOLUTION_PATH)
        artifact_updated = True
        candidate_audit = incumbent_audit
        candidate_source = "restoration"
    else:
        phase_results.append(
            {
                "name": "disk_incumbent_audit",
                "status": incumbent_state,
                "runtime": 0.0,
                "nsols": 1,
                "objective": incumbent_audit["objective"],
            }
        )

    if incumbent_audit["violated_group_count"] > 0 or incumbent_audit["max_normalized_residual"] > FEAS_TOL:
        raise RuntimeError("Rollback incumbent is not hard-feasible after independent audit.")

    performance_budget = max(0.0, time_limit - (time.time() - start))
    final_solution = incumbent_solution
    final_audit = incumbent_audit
    performance_stats = {
        "status": "not_run",
        "nsols": 0,
        "solver_obj": float("nan"),
        "dual_bound": float("nan"),
        "gap": float("nan"),
        "candidate_audit": incumbent_audit,
        "candidate_source": candidate_source,
        "updated": artifact_updated,
    }
    if performance_budget > 1.0:
        final_solution, final_audit, performance_stats = run_performance_search(
            instance, params, incumbent_solution, incumbent_audit, performance_budget, phase_results
        )
        artifact_updated = performance_stats["updated"] or artifact_updated
        candidate_audit = performance_stats["candidate_audit"]
        candidate_source = performance_stats["candidate_source"]
    else:
        candidate_audit = incumbent_audit

    if not artifact_updated:
        # Ensure the rollback incumbent remains the exact published artifact.
        write_solution(incumbent_solution, SOLUTION_PATH)
        final_solution, final_audit = audit_solution_file(instance, params, SOLUTION_PATH)
    else:
        final_solution, final_audit = audit_solution_file(instance, params, SOLUTION_PATH)

    runtime = time.time() - start

    write_log(
        command=command,
        time_limit=time_limit,
        runtime=runtime,
        status=performance_stats["status"],
        nsols=performance_stats["nsols"],
        solver_obj=performance_stats["solver_obj"],
        dual_bound=performance_stats["dual_bound"],
        gap=performance_stats["gap"],
        audit=final_audit,
        incumbent_audit=incumbent_audit,
        candidate_audit=candidate_audit,
        artifact_updated=artifact_updated,
        candidate_source=candidate_source,
        phases=phase_results,
    )

    if final_audit["violated_group_count"] > 0 or final_audit["max_normalized_residual"] > FEAS_TOL:
        raise RuntimeError("Exported solution failed the independent audit; see solve_log.md for details.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
