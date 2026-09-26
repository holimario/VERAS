import csv
import json
import math
import os
from collections import defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
SUBMISSIONS_DIR = BASE_DIR / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"
CANDIDATE_PATH = SUBMISSIONS_DIR / "solution_candidate.csv"

EPS = 1e-8
FEAS_TOL = 1e-6
INT_TOL = 1e-6
DEFAULT_TIME_LIMIT = 300.0
REL_GAP = 0.0005
IMPROVEMENT_TOL = 1e-9


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def read_json(path):
    with open(path) as f:
        return json.load(f)


def parse_int(value):
    return int(value)


def parse_float(value):
    return float(value)


def as_number_string(value):
    rounded = round(value)
    if abs(value - rounded) <= INT_TOL:
        return str(int(rounded))
    text = f"{value:.8f}"
    text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def bounded_time_limit():
    raw = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if raw is None or raw == "":
        return DEFAULT_TIME_LIMIT
    try:
        return min(DEFAULT_TIME_LIMIT, max(0.0, float(raw)))
    except ValueError:
        return DEFAULT_TIME_LIMIT


def read_solution_csv(solution_path):
    with open(solution_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    solution = {"loads": {}, "used": {}, "schedules": {}}
    for row in rows:
        quantity = float(row["quantity"])
        rounded = int(round(quantity))
        if abs(quantity - rounded) > INT_TOL:
            raise RuntimeError(f"Nonintegral quantity in incumbent artifact: {row}")
        quantity = rounded
        if row["variable"] == "load":
            solution["loads"][(row["container_id"], row["sku_id"])] = quantity
        elif row["variable"] == "use":
            solution["used"][row["container_id"]] = quantity
        elif row["variable"] == "schedule":
            solution["schedules"][(row["container_id"], row["slot_id"])] = quantity
    return solution


def load_instance():
    config = read_json(DATA_DIR / "config.json")

    containers_raw = read_csv(DATA_DIR / "containers.csv")
    containers = {}
    for row in containers_raw:
        containers[row["container_id"]] = {
            "container_id": row["container_id"],
            "container_type": row["container_type"],
            "wave": row["wave"],
            "lane": row["lane"],
            "max_weight_tons": parse_float(row["max_weight_tons"]),
            "min_weight_tons": parse_float(row["min_weight_tons"]),
            "pallet_capacity": parse_float(row["pallet_capacity"]),
            "fixed_cost": parse_float(row["fixed_cost"]),
            "security_tier": parse_int(row["security_tier"]),
            "has_liner": parse_int(row["has_liner"]),
            "hazmat_approved": parse_int(row["hazmat_approved"]),
            "reefer": parse_int(row["reefer"]),
            "customs_window": row["customs_window"],
        }

    skus_raw = read_csv(DATA_DIR / "skus.csv")
    skus = {}
    for row in skus_raw:
        skus[row["sku_id"]] = {
            "sku_id": row["sku_id"],
            "sku_family": row["sku_family"],
            "required_units": parse_int(row["required_units"]),
            "unit_weight_tons": parse_float(row["unit_weight_tons"]),
            "pallet_positions": parse_float(row["pallet_positions"]),
            "handling_cost_per_unit": parse_float(row["handling_cost_per_unit"]),
            "temperature_class": row["temperature_class"],
            "hazmat": parse_int(row["hazmat"]),
            "requires_liner": parse_int(row["requires_liner"]),
            "min_security_tier": parse_int(row["min_security_tier"]),
        }

    overrides = {}
    for row in read_csv(DATA_DIR / "sku_container_overrides.csv"):
        overrides[(row["container_id"], row["sku_id"])] = parse_int(row["allowed"])

    wave_deadlines = {
        row["wave"]: parse_int(row["max_sequence"])
        for row in read_csv(DATA_DIR / "wave_loading_deadlines.csv")
    }

    slots = {}
    for row in read_csv(DATA_DIR / "loading_slots.csv"):
        slots[row["slot_id"]] = {
            "slot_id": row["slot_id"],
            "period_id": row["period_id"],
            "bay_id": row["bay_id"],
            "sequence": parse_int(row["sequence"]),
            "customs_window": row["customs_window"],
            "allowed_container_types": set(row["allowed_container_types"].split("|")),
            "allows_dg": parse_int(row["allows_dg"]),
            "reefer_plug_available": parse_int(row["reefer_plug_available"]),
            "max_loaded_weight_tons": parse_float(row["max_loaded_weight_tons"]),
            "slot_cost": parse_float(row["slot_cost"]),
        }

    handling_requirements = {}
    for row in read_csv(DATA_DIR / "container_handling_requirements.csv"):
        handling_requirements[row["container_type"]] = {
            "forklift_hours": parse_float(row["forklift_hours"]),
            "inspection_hours": parse_float(row["inspection_hours"]),
            "dg_escort_units": parse_float(row["dg_escort_units"]),
            "reefer_plug_units": parse_float(row["reefer_plug_units"]),
        }

    bay_capacity = {}
    for row in read_csv(DATA_DIR / "bay_resource_capacity.csv"):
        bay_capacity[row["period_id"]] = {
            "max_scheduled_containers": parse_float(row["max_scheduled_containers"]),
            "forklift_hours": parse_float(row["forklift_hours"]),
            "inspection_hours": parse_float(row["inspection_hours"]),
            "dg_escort_units": parse_float(row["dg_escort_units"]),
            "reefer_plug_slots": parse_float(row["reefer_plug_slots"]),
            "max_loaded_weight_tons": parse_float(row["max_loaded_weight_tons"]),
        }

    companion_rules = []
    for row in read_csv(DATA_DIR / "companion_rules.csv"):
        companion_rules.append(
            (
                row["trigger_sku"],
                row["companion_sku"],
                parse_float(row["min_companion_per_trigger"]),
            )
        )

    min_sku_rules = []
    for row in read_csv(DATA_DIR / "container_minimum_skus.csv"):
        min_sku_rules.append(
            (
                row["sku_id"],
                parse_int(row["min_units_per_used_container"]),
                row["applies_to_container_type"],
            )
        )

    pair_conflicts = []
    for row in read_csv(DATA_DIR / "pair_conflicts.csv"):
        pair_conflicts.append((row["sku_id"], row["conflict_sku_id"]))

    wave_commitments = []
    for row in read_csv(DATA_DIR / "wave_commitments.csv"):
        wave_commitments.append(
            (
                row["sku_id"],
                row["wave"],
                parse_int(row["min_units"]) if row["min_units"] else None,
                parse_int(row["max_units"]) if row["max_units"] else None,
            )
        )

    lane_commitments = []
    for row in read_csv(DATA_DIR / "lane_commitments.csv"):
        lane_commitments.append(
            (
                row["sku_id"],
                row["lane"],
                parse_int(row["min_units"]) if row["min_units"] else None,
                parse_int(row["max_units"]) if row["max_units"] else None,
            )
        )

    family_share_rules = []
    for row in read_csv(DATA_DIR / "family_weight_share_rules.csv"):
        family_share_rules.append(
            (
                row["sku_family"],
                parse_float(row["max_weight_share"]),
                row["applies_to_container_type"],
            )
        )

    split_rules = {}
    for row in read_csv(DATA_DIR / "sku_split_rules.csv"):
        split_rules[row["sku_id"]] = {
            "max_units_per_container": parse_int(row["max_units_per_container"]),
            "min_distinct_containers": parse_int(row["min_distinct_containers"]),
        }

    customs_capacity = {}
    for row in read_csv(DATA_DIR / "customs_window_capacity.csv"):
        customs_capacity[row["customs_window"]] = {
            "max_used_containers": parse_float(row["max_used_containers"]),
            "max_weight_tons": parse_float(row["max_weight_tons"]),
        }

    lane_doc_rules = []
    for row in read_csv(DATA_DIR / "lane_document_rules.csv"):
        lane_doc_rules.append(
            (
                row["lane"],
                row["controlled_sku"],
                row["document_sku"],
                parse_float(row["min_document_units_per_control_unit"]),
            )
        )

    periods = sorted(bay_capacity)
    families = defaultdict(list)
    for sku_id, sku in skus.items():
        families[sku["sku_family"]].append(sku_id)

    compatible_pairs = []
    compatible_by_container = defaultdict(list)
    compatible_by_sku = defaultdict(list)
    upper_bounds = {}
    for container_id, container in containers.items():
        for sku_id, sku in skus.items():
            allowed = True
            if sku["temperature_class"] == "CHILLED" and not container["reefer"]:
                allowed = False
            if sku["hazmat"] and not container["hazmat_approved"]:
                allowed = False
            if sku["requires_liner"] and not container["has_liner"]:
                allowed = False
            if container["security_tier"] < sku["min_security_tier"]:
                allowed = False
            if (container_id, sku_id) in overrides:
                allowed = bool(overrides[(container_id, sku_id)])
            if not allowed:
                continue
            split_cap = split_rules.get(sku_id, {}).get("max_units_per_container", sku["required_units"])
            weight_cap = int(math.floor(container["max_weight_tons"] / sku["unit_weight_tons"] + FEAS_TOL))
            pallet_cap = int(math.floor(container["pallet_capacity"] / sku["pallet_positions"] + FEAS_TOL))
            upper = min(sku["required_units"], split_cap, weight_cap, pallet_cap)
            if upper <= 0:
                continue
            compatible_pairs.append((container_id, sku_id))
            compatible_by_container[container_id].append(sku_id)
            compatible_by_sku[sku_id].append(container_id)
            upper_bounds[(container_id, sku_id)] = upper

    eligible_slots = defaultdict(list)
    slots_by_period = defaultdict(list)
    for slot_id, slot in slots.items():
        slots_by_period[slot["period_id"]].append(slot_id)
    for container_id, container in containers.items():
        for slot_id, slot in slots.items():
            if slot["customs_window"] != container["customs_window"]:
                continue
            if container["container_type"] not in slot["allowed_container_types"]:
                continue
            if slot["sequence"] > wave_deadlines[container["wave"]]:
                continue
            if container["hazmat_approved"] and not slot["allows_dg"]:
                continue
            if container["reefer"] and not slot["reefer_plug_available"]:
                continue
            if slot["max_loaded_weight_tons"] + FEAS_TOL < container["min_weight_tons"]:
                continue
            eligible_slots[container_id].append(slot_id)

    return {
        "config": config,
        "containers": containers,
        "skus": skus,
        "slots": slots,
        "handling_requirements": handling_requirements,
        "bay_capacity": bay_capacity,
        "companion_rules": companion_rules,
        "min_sku_rules": min_sku_rules,
        "pair_conflicts": pair_conflicts,
        "wave_commitments": wave_commitments,
        "lane_commitments": lane_commitments,
        "family_share_rules": family_share_rules,
        "split_rules": split_rules,
        "customs_capacity": customs_capacity,
        "lane_doc_rules": lane_doc_rules,
        "periods": periods,
        "families": dict(families),
        "compatible_pairs": compatible_pairs,
        "compatible_by_container": dict(compatible_by_container),
        "compatible_by_sku": dict(compatible_by_sku),
        "upper_bounds": upper_bounds,
        "eligible_slots": dict(eligible_slots),
        "slots_by_period": dict(slots_by_period),
    }


def build_model(data, time_limit):
    containers = data["containers"]
    skus = data["skus"]
    slots = data["slots"]
    compatible_pairs = data["compatible_pairs"]
    upper_bounds = data["upper_bounds"]
    eligible_slots = data["eligible_slots"]
    split_rules = data["split_rules"]

    for sku_id in skus:
        if not data["compatible_by_sku"].get(sku_id):
            raise RuntimeError(f"SKU {sku_id} has no compatible containers.")
    for sku_id, rule in split_rules.items():
        if len(data["compatible_by_sku"].get(sku_id, [])) < rule["min_distinct_containers"]:
            raise RuntimeError(f"SKU {sku_id} cannot satisfy min distinct container requirement.")

    model = Model("blueriver_container_loading")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("randomization/permutationseed", 0)
    model.setParam("randomization/randomseedshift", 0)
    model.setParam("parallel/maxnthreads", 1)

    x = {}
    b = {}
    for container_id, sku_id in compatible_pairs:
        x[(container_id, sku_id)] = model.addVar(vtype="I", lb=0, ub=upper_bounds[(container_id, sku_id)], name=f"x[{container_id},{sku_id}]")
        b[(container_id, sku_id)] = model.addVar(vtype="B", name=f"b[{container_id},{sku_id}]")

    y = {
        container_id: model.addVar(vtype="B", name=f"y[{container_id}]")
        for container_id in containers
    }
    z = {}
    g = {}
    for container_id, slot_ids in eligible_slots.items():
        for slot_id in slot_ids:
            z[(container_id, slot_id)] = model.addVar(vtype="B", name=f"z[{container_id},{slot_id}]")
            g[(container_id, slot_id)] = model.addVar(vtype="C", lb=0.0, name=f"g[{container_id},{slot_id}]")
    w = {
        container_id: model.addVar(vtype="C", lb=0.0, name=f"w[{container_id}]")
        for container_id in containers
    }

    for sku_id, sku in skus.items():
        model.addCons(
            quicksum(x[(container_id, sku_id)] for container_id in data["compatible_by_sku"][sku_id]) == sku["required_units"],
            name=f"demand[{sku_id}]",
        )

    for container_id, sku_id in compatible_pairs:
        ub = upper_bounds[(container_id, sku_id)]
        model.addCons(x[(container_id, sku_id)] <= ub * b[(container_id, sku_id)], name=f"ub_link[{container_id},{sku_id}]")
        model.addCons(x[(container_id, sku_id)] >= b[(container_id, sku_id)], name=f"lb_link[{container_id},{sku_id}]")
        model.addCons(b[(container_id, sku_id)] <= y[container_id], name=f"use_link[{container_id},{sku_id}]")

    for container_id, container in containers.items():
        model.addCons(
            w[container_id] == quicksum(skus[sku_id]["unit_weight_tons"] * x[(container_id, sku_id)] for sku_id in data["compatible_by_container"].get(container_id, [])),
            name=f"weight_def[{container_id}]",
        )
        model.addCons(w[container_id] >= container["min_weight_tons"] * y[container_id], name=f"min_weight[{container_id}]")
        model.addCons(w[container_id] <= container["max_weight_tons"] * y[container_id], name=f"max_weight[{container_id}]")
        model.addCons(
            quicksum(skus[sku_id]["pallet_positions"] * x[(container_id, sku_id)] for sku_id in data["compatible_by_container"].get(container_id, []))
            <= container["pallet_capacity"] * y[container_id],
            name=f"pallet[{container_id}]",
        )

    for sku_id, min_units, applies_to_type in data["min_sku_rules"]:
        for container_id, container in containers.items():
            if applies_to_type != "ALL" and container["container_type"] != applies_to_type:
                continue
            if (container_id, sku_id) not in x:
                raise RuntimeError(f"Minimum SKU rule for {sku_id} cannot be applied to {container_id}.")
            model.addCons(x[(container_id, sku_id)] >= min_units * y[container_id], name=f"minsku[{container_id},{sku_id}]")

    for trigger_sku, companion_sku, ratio in data["companion_rules"]:
        for container_id in containers:
            if (container_id, trigger_sku) in x and (container_id, companion_sku) in x:
                model.addCons(
                    x[(container_id, companion_sku)] >= ratio * x[(container_id, trigger_sku)],
                    name=f"companion[{container_id},{trigger_sku},{companion_sku}]",
                )

    for sku_id, conflict_sku_id in data["pair_conflicts"]:
        for container_id in containers:
            if (container_id, sku_id) in b and (container_id, conflict_sku_id) in b:
                model.addCons(
                    b[(container_id, sku_id)] + b[(container_id, conflict_sku_id)] <= 1,
                    name=f"conflict[{container_id},{sku_id},{conflict_sku_id}]",
                )

    for sku_id, wave, min_units, max_units in data["wave_commitments"]:
        relevant = [container_id for container_id, container in containers.items() if container["wave"] == wave and (container_id, sku_id) in x]
        expr = quicksum(x[(container_id, sku_id)] for container_id in relevant)
        if min_units is not None:
            model.addCons(expr >= min_units, name=f"wave_min[{sku_id},{wave}]")
        if max_units is not None:
            model.addCons(expr <= max_units, name=f"wave_max[{sku_id},{wave}]")

    for sku_id, lane, min_units, max_units in data["lane_commitments"]:
        relevant = [container_id for container_id, container in containers.items() if container["lane"] == lane and (container_id, sku_id) in x]
        expr = quicksum(x[(container_id, sku_id)] for container_id in relevant)
        if min_units is not None:
            model.addCons(expr >= min_units, name=f"lane_min[{sku_id},{lane}]")
        if max_units is not None:
            model.addCons(expr <= max_units, name=f"lane_max[{sku_id},{lane}]")

    for family, max_share, applies_to_type in data["family_share_rules"]:
        family_skus = data["families"].get(family, [])
        for container_id, container in containers.items():
            if applies_to_type != "ALL" and container["container_type"] != applies_to_type:
                continue
            expr = quicksum(
                skus[sku_id]["unit_weight_tons"] * x[(container_id, sku_id)]
                for sku_id in family_skus
                if (container_id, sku_id) in x
            )
            model.addCons(expr <= max_share * w[container_id], name=f"share[{container_id},{family}]")

    for sku_id, rule in split_rules.items():
        for container_id in data["compatible_by_sku"].get(sku_id, []):
            model.addCons(
                x[(container_id, sku_id)] <= rule["max_units_per_container"] * y[container_id],
                name=f"splitmax[{container_id},{sku_id}]",
            )
        model.addCons(
            quicksum(b[(container_id, sku_id)] for container_id in data["compatible_by_sku"].get(sku_id, []))
            >= rule["min_distinct_containers"],
            name=f"splitmin[{sku_id}]",
        )

    for lane, controlled_sku, document_sku, ratio in data["lane_doc_rules"]:
        doc_expr = quicksum(
            x[(container_id, document_sku)]
            for container_id, container in containers.items()
            if container["lane"] == lane and (container_id, document_sku) in x
        )
        control_expr = quicksum(
            x[(container_id, controlled_sku)]
            for container_id, container in containers.items()
            if container["lane"] == lane and (container_id, controlled_sku) in x
        )
        model.addCons(doc_expr >= ratio * control_expr, name=f"doc[{lane},{controlled_sku},{document_sku}]")

    for customs_window, capacity in data["customs_capacity"].items():
        window_containers = [container_id for container_id, container in containers.items() if container["customs_window"] == customs_window]
        model.addCons(quicksum(y[container_id] for container_id in window_containers) <= capacity["max_used_containers"], name=f"customs_cnt[{customs_window}]")
        model.addCons(quicksum(w[container_id] for container_id in window_containers) <= capacity["max_weight_tons"], name=f"customs_wt[{customs_window}]")

    for container_id in containers:
        slot_ids = eligible_slots.get(container_id, [])
        model.addCons(quicksum(z[(container_id, slot_id)] for slot_id in slot_ids) == y[container_id], name=f"schedule[{container_id}]")

    for slot_id in slots:
        users = [container_id for container_id in containers if (container_id, slot_id) in z]
        model.addCons(quicksum(z[(container_id, slot_id)] for container_id in users) <= 1, name=f"slot_cap[{slot_id}]")

    for container_id, container in containers.items():
        slot_ids = eligible_slots.get(container_id, [])
        model.addCons(quicksum(g[(container_id, slot_id)] for slot_id in slot_ids) == w[container_id], name=f"g_sum[{container_id}]")
        for slot_id in slot_ids:
            model.addCons(g[(container_id, slot_id)] <= slots[slot_id]["max_loaded_weight_tons"] * z[(container_id, slot_id)], name=f"g_slot[{container_id},{slot_id}]")
            model.addCons(g[(container_id, slot_id)] <= w[container_id], name=f"g_w[{container_id},{slot_id}]")
            model.addCons(
                g[(container_id, slot_id)] >= w[container_id] - container["max_weight_tons"] * (1 - z[(container_id, slot_id)]),
                name=f"g_lb[{container_id},{slot_id}]",
            )

    for period_id, capacity in data["bay_capacity"].items():
        period_assignments = [(container_id, slot_id) for container_id, slot_id in z if slots[slot_id]["period_id"] == period_id]
        model.addCons(quicksum(z[key] for key in period_assignments) <= capacity["max_scheduled_containers"], name=f"period_cnt[{period_id}]")
        model.addCons(
            quicksum(data["handling_requirements"][containers[container_id]["container_type"]]["forklift_hours"] * z[(container_id, slot_id)] for container_id, slot_id in period_assignments)
            <= capacity["forklift_hours"],
            name=f"period_fork[{period_id}]",
        )
        model.addCons(
            quicksum(data["handling_requirements"][containers[container_id]["container_type"]]["inspection_hours"] * z[(container_id, slot_id)] for container_id, slot_id in period_assignments)
            <= capacity["inspection_hours"],
            name=f"period_insp[{period_id}]",
        )
        model.addCons(
            quicksum(data["handling_requirements"][containers[container_id]["container_type"]]["dg_escort_units"] * z[(container_id, slot_id)] for container_id, slot_id in period_assignments)
            <= capacity["dg_escort_units"],
            name=f"period_dg[{period_id}]",
        )
        model.addCons(
            quicksum(data["handling_requirements"][containers[container_id]["container_type"]]["reefer_plug_units"] * z[(container_id, slot_id)] for container_id, slot_id in period_assignments)
            <= capacity["reefer_plug_slots"],
            name=f"period_plug[{period_id}]",
        )
        model.addCons(
            quicksum(g[(container_id, slot_id)] for container_id, slot_id in period_assignments) <= capacity["max_loaded_weight_tons"],
            name=f"period_wt[{period_id}]",
        )

    model.setObjective(
        quicksum(containers[container_id]["fixed_cost"] * y[container_id] for container_id in containers)
        + quicksum(skus[sku_id]["handling_cost_per_unit"] * x[(container_id, sku_id)] for container_id, sku_id in compatible_pairs)
        + quicksum(slots[slot_id]["slot_cost"] * z[(container_id, slot_id)] for container_id, slot_id in z),
        "minimize",
    )

    return model, x, b, y, z, w, g


def apply_incumbent_start(model, variables, data, incumbent_solution):
    x, b, y, z, w, g = variables
    containers = data["containers"]
    skus = data["skus"]

    weight_by_container = defaultdict(float)
    slot_by_container = {}
    for (container_id, sku_id), quantity in incumbent_solution["loads"].items():
        weight_by_container[container_id] += skus[sku_id]["unit_weight_tons"] * quantity
    for (container_id, slot_id), quantity in incumbent_solution["schedules"].items():
        if quantity > 0:
            slot_by_container[container_id] = slot_id

    sol = model.createSol()
    for key, var in x.items():
        model.setSolVal(sol, var, incumbent_solution["loads"].get(key, 0))
    for key, var in b.items():
        model.setSolVal(sol, var, 1 if incumbent_solution["loads"].get(key, 0) > 0 else 0)
    for container_id, var in y.items():
        model.setSolVal(sol, var, incumbent_solution["used"].get(container_id, 0))
    for key, var in z.items():
        model.setSolVal(sol, var, incumbent_solution["schedules"].get(key, 0))
    for container_id, var in w.items():
        model.setSolVal(sol, var, weight_by_container.get(container_id, 0.0))
    for key, var in g.items():
        container_id, slot_id = key
        value = 0.0
        if slot_by_container.get(container_id) == slot_id:
            value = weight_by_container.get(container_id, 0.0)
        model.setSolVal(sol, var, value)

    return model.addSol(sol, free=True)


def extract_solution(model, variables):
    x, y, z = variables
    if model.getNSols() <= 0:
        raise RuntimeError("SCIP finished without an incumbent solution.")
    sol = model.getBestSol()
    loads = {}
    used = {}
    schedules = {}

    for key, var in x.items():
        value = model.getSolVal(sol, var)
        rounded = int(round(value))
        if abs(value - rounded) > INT_TOL:
            raise RuntimeError(f"Load variable {key} is not integral within tolerance: {value}")
        if rounded > 0:
            loads[key] = rounded

    for container_id, var in y.items():
        value = model.getSolVal(sol, var)
        rounded = int(round(value))
        if abs(value - rounded) > INT_TOL:
            raise RuntimeError(f"Use variable {container_id} is not integral within tolerance: {value}")
        if rounded > 0:
            used[container_id] = rounded

    for key, var in z.items():
        value = model.getSolVal(sol, var)
        rounded = int(round(value))
        if abs(value - rounded) > INT_TOL:
            raise RuntimeError(f"Schedule variable {key} is not integral within tolerance: {value}")
        if rounded > 0:
            schedules[key] = rounded

    return {"loads": loads, "used": used, "schedules": schedules}


def write_solution_csv(solution, solution_path):
    rows = []
    for (container_id, sku_id), quantity in sorted(solution["loads"].items()):
        rows.append(
            {
                "variable": "load",
                "container_id": container_id,
                "sku_id": sku_id,
                "slot_id": "",
                "quantity": as_number_string(quantity),
            }
        )
    for container_id, quantity in sorted(solution["used"].items()):
        rows.append(
            {
                "variable": "use",
                "container_id": container_id,
                "sku_id": "",
                "slot_id": "",
                "quantity": as_number_string(quantity),
            }
        )
    for (container_id, slot_id), quantity in sorted(solution["schedules"].items()):
        rows.append(
            {
                "variable": "schedule",
                "container_id": container_id,
                "sku_id": "",
                "slot_id": slot_id,
                "quantity": as_number_string(quantity),
            }
        )

    with open(solution_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["variable", "container_id", "sku_id", "slot_id", "quantity"])
        writer.writeheader()
        writer.writerows(rows)


def normalize_violation(lhs, sense, rhs):
    scale = max(1.0, abs(rhs), abs(lhs))
    if sense == "eq":
        return abs(lhs - rhs) / scale
    if sense == "le":
        return max(0.0, lhs - rhs) / scale
    if sense == "ge":
        return max(0.0, rhs - lhs) / scale
    raise ValueError(sense)


def audit_solution(data, solution_path):
    expected_columns = data["config"]["solution_columns"]
    with open(solution_path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != expected_columns:
            raise RuntimeError(f"Unexpected solution schema: {reader.fieldnames}")
        rows = list(reader)

    containers = data["containers"]
    skus = data["skus"]
    slots = data["slots"]
    eligible_slots = data["eligible_slots"]

    load_qty = defaultdict(float)
    use_qty = defaultdict(float)
    schedule_qty = defaultdict(float)

    max_integrality_residual = 0.0
    diagnostics = []
    semantic_keys = set()
    row_counts = defaultdict(int)

    for row in rows:
        variable = row["variable"]
        container_id = row["container_id"]
        sku_id = row["sku_id"]
        slot_id = row["slot_id"]
        quantity = float(row["quantity"])
        semantic_key = (variable, container_id, sku_id, slot_id)

        row_counts[variable] += 1
        if semantic_key in semantic_keys:
            diagnostics.append((f"duplicate_row[{variable},{container_id},{sku_id},{slot_id}]", 1.0))
        semantic_keys.add(semantic_key)

        max_integrality_residual = max(max_integrality_residual, abs(quantity - round(quantity)))

        if variable == "load":
            if not container_id or not sku_id or slot_id:
                diagnostics.append(("schema_load", 1.0))
                continue
            if container_id not in containers or sku_id not in skus:
                diagnostics.append(("id_load", 1.0))
                continue
            load_qty[(container_id, sku_id)] += quantity
        elif variable == "use":
            if not container_id or sku_id or slot_id:
                diagnostics.append(("schema_use", 1.0))
                continue
            if container_id not in containers:
                diagnostics.append(("id_use", 1.0))
                continue
            use_qty[container_id] += quantity
        elif variable == "schedule":
            if not container_id or sku_id or not slot_id:
                diagnostics.append(("schema_schedule", 1.0))
                continue
            if container_id not in containers or slot_id not in slots:
                diagnostics.append(("id_schedule", 1.0))
                continue
            schedule_qty[(container_id, slot_id)] += quantity
        else:
            diagnostics.append(("schema_variable", 1.0))

    def add_check(name, lhs, sense, rhs):
        diagnostics.append((name, normalize_violation(lhs, sense, rhs)))

    compatible_set = set(data["compatible_pairs"])
    for key, value in load_qty.items():
        if key not in compatible_set:
            diagnostics.append(("compatibility", 1.0))
        if value < -FEAS_TOL:
            diagnostics.append(("nonnegative_load", 1.0))

    for container_id, value in use_qty.items():
        add_check(f"use_binary[{container_id}]", value, "ge", 0.0)
        diagnostics.append((f"use_upper[{container_id}]", normalize_violation(value, "le", 1.0)))
    for key, value in schedule_qty.items():
        diagnostics.append((f"schedule_nonnegative[{key[0]},{key[1]}]", normalize_violation(value, "ge", 0.0)))
        diagnostics.append((f"schedule_upper[{key[0]},{key[1]}]", normalize_violation(value, "le", 1.0)))

    load_by_container = defaultdict(lambda: defaultdict(float))
    weight_by_container = defaultdict(float)
    pallet_by_container = defaultdict(float)
    for (container_id, sku_id), quantity in load_qty.items():
        load_by_container[container_id][sku_id] += quantity
        weight_by_container[container_id] += skus[sku_id]["unit_weight_tons"] * quantity
        pallet_by_container[container_id] += skus[sku_id]["pallet_positions"] * quantity

    use_indicator = {container_id: use_qty.get(container_id, 0.0) for container_id in containers}
    schedule_by_container = defaultdict(float)
    schedule_by_slot = defaultdict(float)
    assigned_slot = {}
    for (container_id, slot_id), quantity in schedule_qty.items():
        schedule_by_container[container_id] += quantity
        schedule_by_slot[slot_id] += quantity
        if quantity > 1.0 - FEAS_TOL:
            assigned_slot[container_id] = slot_id

    for sku_id, sku in skus.items():
        lhs = sum(load_qty.get((container_id, sku_id), 0.0) for container_id in containers)
        add_check(f"demand[{sku_id}]", lhs, "eq", sku["required_units"])

    for container_id, container in containers.items():
        y_val = use_indicator[container_id]
        add_check(f"schedule_match[{container_id}]", schedule_by_container.get(container_id, 0.0), "eq", y_val)
        if weight_by_container[container_id] > FEAS_TOL:
            add_check(f"load_implies_use[{container_id}]", y_val, "ge", 1.0)
        if y_val > FEAS_TOL:
            add_check(f"min_weight[{container_id}]", weight_by_container[container_id], "ge", container["min_weight_tons"] * y_val)
            add_check(f"max_weight[{container_id}]", weight_by_container[container_id], "le", container["max_weight_tons"] * y_val)
            add_check(f"pallet[{container_id}]", pallet_by_container[container_id], "le", container["pallet_capacity"] * y_val)
        else:
            add_check(f"unused_weight[{container_id}]", weight_by_container[container_id], "eq", 0.0)
            add_check(f"unused_pallet[{container_id}]", pallet_by_container[container_id], "eq", 0.0)

    for slot_id in slots:
        add_check(f"slot_unique[{slot_id}]", schedule_by_slot.get(slot_id, 0.0), "le", 1.0)

    for sku_id, min_units, applies_to_type in data["min_sku_rules"]:
        for container_id, container in containers.items():
            if applies_to_type != "ALL" and container["container_type"] != applies_to_type:
                continue
            lhs = load_qty.get((container_id, sku_id), 0.0)
            rhs = min_units * use_indicator[container_id]
            add_check(f"minsku[{container_id},{sku_id}]", lhs, "ge", rhs)

    for trigger_sku, companion_sku, ratio in data["companion_rules"]:
        for container_id in containers:
            if (container_id, trigger_sku) in compatible_set and (container_id, companion_sku) in compatible_set:
                lhs = load_qty.get((container_id, companion_sku), 0.0)
                rhs = ratio * load_qty.get((container_id, trigger_sku), 0.0)
                add_check(f"companion[{container_id},{trigger_sku},{companion_sku}]", lhs, "ge", rhs)

    for sku_id, conflict_sku_id in data["pair_conflicts"]:
        for container_id in containers:
            if (container_id, sku_id) in compatible_set and (container_id, conflict_sku_id) in compatible_set:
                lhs = (1.0 if load_qty.get((container_id, sku_id), 0.0) > FEAS_TOL else 0.0) + (
                    1.0 if load_qty.get((container_id, conflict_sku_id), 0.0) > FEAS_TOL else 0.0
                )
                add_check(f"conflict[{container_id},{sku_id},{conflict_sku_id}]", lhs, "le", 1.0)

    for sku_id, wave, min_units, max_units in data["wave_commitments"]:
        lhs = sum(load_qty.get((container_id, sku_id), 0.0) for container_id, container in containers.items() if container["wave"] == wave)
        if min_units is not None:
            add_check(f"wave_min[{sku_id},{wave}]", lhs, "ge", min_units)
        if max_units is not None:
            add_check(f"wave_max[{sku_id},{wave}]", lhs, "le", max_units)

    for sku_id, lane, min_units, max_units in data["lane_commitments"]:
        lhs = sum(load_qty.get((container_id, sku_id), 0.0) for container_id, container in containers.items() if container["lane"] == lane)
        if min_units is not None:
            add_check(f"lane_min[{sku_id},{lane}]", lhs, "ge", min_units)
        if max_units is not None:
            add_check(f"lane_max[{sku_id},{lane}]", lhs, "le", max_units)

    for family, max_share, applies_to_type in data["family_share_rules"]:
        family_skus = set(data["families"].get(family, []))
        for container_id, container in containers.items():
            if applies_to_type != "ALL" and container["container_type"] != applies_to_type:
                continue
            lhs = sum(
                skus[sku_id]["unit_weight_tons"] * quantity
                for sku_id, quantity in load_by_container[container_id].items()
                if sku_id in family_skus
            )
            rhs = max_share * weight_by_container[container_id]
            add_check(f"share[{container_id},{family}]", lhs, "le", rhs)

    for sku_id, rule in data["split_rules"].items():
        distinct = 0.0
        for container_id in containers:
            qty = load_qty.get((container_id, sku_id), 0.0)
            add_check(f"splitmax[{container_id},{sku_id}]", qty, "le", rule["max_units_per_container"])
            if qty > FEAS_TOL:
                distinct += 1.0
        add_check(f"splitmin[{sku_id}]", distinct, "ge", rule["min_distinct_containers"])

    for lane, controlled_sku, document_sku, ratio in data["lane_doc_rules"]:
        controlled = sum(
            load_qty.get((container_id, controlled_sku), 0.0)
            for container_id, container in containers.items()
            if container["lane"] == lane
        )
        documents = sum(
            load_qty.get((container_id, document_sku), 0.0)
            for container_id, container in containers.items()
            if container["lane"] == lane
        )
        add_check(f"doc[{lane},{controlled_sku},{document_sku}]", documents, "ge", ratio * controlled)

    for customs_window, capacity in data["customs_capacity"].items():
        used = sum(use_indicator[container_id] for container_id, container in containers.items() if container["customs_window"] == customs_window)
        weight = sum(weight_by_container[container_id] for container_id, container in containers.items() if container["customs_window"] == customs_window)
        add_check(f"customs_cnt[{customs_window}]", used, "le", capacity["max_used_containers"])
        add_check(f"customs_wt[{customs_window}]", weight, "le", capacity["max_weight_tons"])

    period_weight = defaultdict(float)
    period_count = defaultdict(float)
    period_fork = defaultdict(float)
    period_insp = defaultdict(float)
    period_dg = defaultdict(float)
    period_plug = defaultdict(float)
    for container_id, slot_id in assigned_slot.items():
        if slot_id not in eligible_slots.get(container_id, []):
            diagnostics.append((f"slot_eligibility[{container_id},{slot_id}]", 1.0))
            continue
        slot = slots[slot_id]
        period = slot["period_id"]
        period_count[period] += 1.0
        period_weight[period] += weight_by_container[container_id]
        req = data["handling_requirements"][containers[container_id]["container_type"]]
        period_fork[period] += req["forklift_hours"]
        period_insp[period] += req["inspection_hours"]
        period_dg[period] += req["dg_escort_units"]
        period_plug[period] += req["reefer_plug_units"]
        add_check(f"slot_weight[{container_id},{slot_id}]", weight_by_container[container_id], "le", slot["max_loaded_weight_tons"])

    for period_id, capacity in data["bay_capacity"].items():
        add_check(f"period_cnt[{period_id}]", period_count.get(period_id, 0.0), "le", capacity["max_scheduled_containers"])
        add_check(f"period_fork[{period_id}]", period_fork.get(period_id, 0.0), "le", capacity["forklift_hours"])
        add_check(f"period_insp[{period_id}]", period_insp.get(period_id, 0.0), "le", capacity["inspection_hours"])
        add_check(f"period_dg[{period_id}]", period_dg.get(period_id, 0.0), "le", capacity["dg_escort_units"])
        add_check(f"period_plug[{period_id}]", period_plug.get(period_id, 0.0), "le", capacity["reefer_plug_slots"])
        add_check(f"period_wt[{period_id}]", period_weight.get(period_id, 0.0), "le", capacity["max_loaded_weight_tons"])

    objective = 0.0
    for container_id, qty in use_indicator.items():
        objective += containers[container_id]["fixed_cost"] * qty
    for (container_id, sku_id), qty in load_qty.items():
        objective += skus[sku_id]["handling_cost_per_unit"] * qty
    for (container_id, slot_id), qty in schedule_qty.items():
        objective += slots[slot_id]["slot_cost"] * qty

    max_residual = 0.0
    violated = 0
    for _, residual in diagnostics:
        max_residual = max(max_residual, residual)
        if residual > FEAS_TOL:
            violated += 1

    return {
        "objective": objective,
        "rows": len(rows),
        "row_counts": dict(row_counts),
        "used_containers": int(round(sum(use_indicator.values()))),
        "scheduled_containers": int(round(sum(schedule_by_container.values()))),
        "max_normalized_residual": max_residual,
        "violated_constraint_groups": violated,
        "integrality_residual": max_integrality_residual,
    }


def audit_passes(audit):
    return audit["integrality_residual"] <= INT_TOL and audit["max_normalized_residual"] <= FEAS_TOL and audit["violated_constraint_groups"] == 0


def write_log(
    time_limit,
    incumbent_exists,
    incumbent_audit,
    warm_start_added,
    solver_status,
    solver_obj,
    dualbound,
    gap,
    candidate_audit,
    published,
    final_audit,
):
    incumbent_obj = incumbent_audit["objective"] if incumbent_audit is not None else None
    candidate_obj = candidate_audit["objective"] if candidate_audit is not None else None
    lines = [
        "# Solve Log",
        "",
        "## Command",
        "",
        "```bash",
        "PYTHONPATH=/opt/or-deps python /app/submissions/solve.py",
        "```",
        "",
        "## Guarded Phase",
        "",
        "- Public challenger issues addressed: no concrete public-rule violations were reported; incumbent was reparsed and re-audited from disk before optimization.",
        f"- Incumbent artifact present: {incumbent_exists}",
        f"- Incumbent audit pass: {audit_passes(incumbent_audit) if incumbent_audit is not None else False}",
        f"- Incumbent objective: {incumbent_obj if incumbent_obj is not None else 'n/a'}",
        f"- Warm start accepted by SCIP: {warm_start_added}",
        "",
        "## Solver Summary",
        "",
        f"- Time limit: {time_limit}",
        f"- SCIP status: {solver_status}",
        f"- Candidate objective from solver: {solver_obj if solver_obj is not None else 'n/a'}",
        f"- Candidate objective from audited artifact: {candidate_obj if candidate_obj is not None else 'n/a'}",
        f"- Best bound: {dualbound if dualbound is not None else 'n/a'}",
        f"- Relative gap: {gap if gap is not None else 'n/a'}",
        f"- Candidate audit pass: {audit_passes(candidate_audit) if candidate_audit is not None else False}",
        "",
        "## Publication Decision",
        "",
        f"- Published improved candidate: {published}",
        f"- Final artifact objective: {final_audit['objective']}",
        f"- Final artifact rows: {final_audit['rows']}",
        f"- Final artifact row counts: {final_audit['row_counts']}",
        f"- Final max normalized hard-constraint residual: {final_audit['max_normalized_residual']}",
        f"- Final violated constraint groups: {final_audit['violated_constraint_groups']}",
        f"- Final integrality residual from exported artifact: {final_audit['integrality_residual']}",
    ]
    with open(LOG_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = load_instance()
    time_limit = bounded_time_limit()
    incumbent_exists = SOLUTION_PATH.exists()
    incumbent_solution = None
    incumbent_audit = None
    if incumbent_exists:
        incumbent_audit = audit_solution(data, SOLUTION_PATH)
        if not audit_passes(incumbent_audit):
            raise RuntimeError(f"Current incumbent artifact failed independent audit: {incumbent_audit}")
        incumbent_solution = read_solution_csv(SOLUTION_PATH)

    model, x, b, y, z, w, g = build_model(data, time_limit)
    warm_start_added = False
    if incumbent_solution is not None:
        warm_start_added = apply_incumbent_start(model, (x, b, y, z, w, g), data, incumbent_solution)
    model.optimize()

    status = str(model.getStatus())
    objective = None
    dualbound = None
    gap = None
    if model.getNSols() > 0:
        objective = model.getObjVal()
    try:
        dualbound = model.getDualbound()
    except Exception:
        dualbound = None
    try:
        gap = model.getGap()
    except Exception:
        gap = None

    candidate_audit = None
    published = False
    if model.getNSols() > 0:
        solution = extract_solution(model, (x, y, z))
        write_solution_csv(solution, CANDIDATE_PATH)
        candidate_audit = audit_solution(data, CANDIDATE_PATH)
        if not audit_passes(candidate_audit):
            raise RuntimeError(f"Candidate artifact failed independent audit: {candidate_audit}")

        incumbent_obj = incumbent_audit["objective"] if incumbent_audit is not None else math.inf
        if candidate_audit["objective"] < incumbent_obj - IMPROVEMENT_TOL:
            os.replace(CANDIDATE_PATH, SOLUTION_PATH)
            published = True
        else:
            CANDIDATE_PATH.unlink(missing_ok=True)

    final_audit = audit_solution(data, SOLUTION_PATH)
    if not audit_passes(final_audit):
        raise RuntimeError(f"Final published artifact failed independent audit: {final_audit}")

    write_log(
        time_limit=time_limit,
        incumbent_exists=incumbent_exists,
        incumbent_audit=incumbent_audit,
        warm_start_added=warm_start_added,
        solver_status=status,
        solver_obj=objective,
        dualbound=dualbound,
        gap=gap,
        candidate_audit=candidate_audit,
        published=published,
        final_audit=final_audit,
    )


if __name__ == "__main__":
    main()
