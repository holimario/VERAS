import csv
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

from pyscipopt import Model, quicksum


BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
SUBMISSIONS_DIR = BASE_DIR / "submissions"
MODEL_PATH = SUBMISSIONS_DIR / "model.md"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"
CANDIDATE_RECOURSE_PATH = SUBMISSIONS_DIR / ".candidate_recourse.csv"
CANDIDATE_LOCAL_BRANCH_PATH = SUBMISSIONS_DIR / ".candidate_local_branch.csv"

EPS = 1e-8
SCIP_GAP = 0.0005
DEFAULT_TIME_LIMIT = 295.0
IMPROVEMENT_EPS = 1e-6


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def to_float(value):
    return float(value)


def to_int(value):
    return int(value)


def format_quantity(value):
    return f"{value:.8f}"


def solution_header():
    return ["row_type", "scenario_id", "satellite_id", "slot", "request_id", "ground_station", "mode", "quantity"]


def read_data():
    with open(DATA_DIR / "config.json") as handle:
        config = json.load(handle)

    satellites_rows = read_csv(DATA_DIR / "satellites.csv")
    requests_rows = read_csv(DATA_DIR / "requests.csv")
    obs_rows = read_csv(DATA_DIR / "observation_windows.csv")
    dl_rows = read_csv(DATA_DIR / "downlink_windows.csv")
    scenarios_rows = read_csv(DATA_DIR / "scenarios.csv")
    quality_rows = read_csv(DATA_DIR / "scenario_observation_quality.csv")
    multiplier_rows = read_csv(DATA_DIR / "scenario_downlink_multipliers.csv")
    ingest_rows = read_csv(DATA_DIR / "region_ingest_capacity.csv")
    stations_rows = read_csv(DATA_DIR / "ground_stations.csv")

    satellites = {}
    for row in satellites_rows:
        satellites[row["satellite_id"]] = {
            "orbit_plane": row["orbit_plane"],
            "memory_capacity_gb": to_float(row["memory_capacity_gb"]),
            "initial_memory_gb": to_float(row["initial_memory_gb"]),
            "energy_capacity_kwh": to_float(row["energy_capacity_kwh"]),
            "initial_energy_kwh": to_float(row["initial_energy_kwh"]),
            "solar_recharge_kwh_per_slot": to_float(row["solar_recharge_kwh_per_slot"]),
            "downlink_energy_kwh_per_gb": to_float(row["downlink_energy_kwh_per_gb"]),
            "downlink_thermal_per_gb": to_float(row["downlink_thermal_per_gb"]),
            "thermal_limit_rolling": to_float(row["thermal_limit_rolling"]),
        }

    requests = {}
    regions = set()
    stereo_pairs = defaultdict(list)
    for row in requests_rows:
        request_id = row["request_id"]
        pair_id = row["stereo_pair_id"].strip()
        requests[request_id] = {
            "disaster_region": row["disaster_region"],
            "disaster_type": row["disaster_type"],
            "critical": to_int(row["critical"]),
            "data_gb": to_float(row["data_gb"]),
            "mission_value": to_float(row["mission_value"]),
            "deadline_slot": to_int(row["deadline_slot"]),
            "stereo_pair_id": pair_id,
        }
        regions.add(row["disaster_region"])
        if pair_id:
            stereo_pairs[pair_id].append(request_id)

    observation_windows = {}
    obs_by_request = defaultdict(list)
    obs_by_sat_slot = defaultdict(list)
    obs_high_by_sat_slot = defaultdict(list)
    for row in obs_rows:
        window_id = row["window_id"]
        slot = to_int(row["slot"])
        entry = {
            "satellite_id": row["satellite_id"],
            "request_id": row["request_id"],
            "slot": slot,
            "instrument_type": row["instrument_type"],
            "slew_class": row["slew_class"],
            "energy_kwh": to_float(row["energy_kwh"]),
            "thermal_load": to_float(row["thermal_load"]),
            "quality_score": to_float(row["quality_score"]),
        }
        observation_windows[window_id] = entry
        obs_by_request[entry["request_id"]].append(window_id)
        obs_by_sat_slot[(entry["satellite_id"], slot)].append(window_id)
        if entry["slew_class"] == "high":
            obs_high_by_sat_slot[(entry["satellite_id"], slot)].append(window_id)

    observation_lookup = {}
    for window_id, window in observation_windows.items():
        key = (window["satellite_id"], window["slot"], window["request_id"])
        observation_lookup[key] = window_id

    contacts = {}
    contacts_by_sat_slot = defaultdict(list)
    contacts_by_station_slot = defaultdict(list)
    for row in dl_rows:
        contact_id = row["contact_id"]
        slot = to_int(row["slot"])
        entry = {
            "satellite_id": row["satellite_id"],
            "ground_station": row["ground_station"],
            "slot": slot,
            "base_capacity_gb": to_float(row["base_capacity_gb"]),
        }
        contacts[contact_id] = entry
        contacts_by_sat_slot[(entry["satellite_id"], slot)].append(contact_id)
        contacts_by_station_slot[(entry["ground_station"], slot)].append(contact_id)

    contact_lookup = {}
    for contact_id, contact in contacts.items():
        key = (contact["satellite_id"], contact["slot"], contact["ground_station"])
        contact_lookup[key] = contact_id

    scenarios = {}
    scenario_order = []
    for row in scenarios_rows:
        scenario_id = row["scenario_id"]
        scenarios[scenario_id] = {
            "probability": to_float(row["probability"]),
            "description": row["description"],
            "target_profit": to_float(row["target_profit"]),
        }
        scenario_order.append(scenario_id)

    scenario_usable = defaultdict(dict)
    scenario_value = defaultdict(dict)
    for row in quality_rows:
        scenario_id = row["scenario_id"]
        request_id = row["request_id"]
        usable = to_int(row["usable"])
        multiplier = to_float(row["value_multiplier"])
        scenario_usable[scenario_id][request_id] = usable
        scenario_value[scenario_id][request_id] = requests[request_id]["mission_value"] * multiplier

    scenario_dl_multiplier = {}
    for row in multiplier_rows:
        scenario_dl_multiplier[(row["scenario_id"], row["ground_station"], to_int(row["slot"]))] = to_float(
            row["capacity_multiplier"]
        )

    region_ingest = {}
    for row in ingest_rows:
        region_ingest[(row["scenario_id"], row["disaster_region"], to_int(row["slot"]))] = to_float(row["max_ingest_gb"])

    ground_stations = {}
    for row in stations_rows:
        ground_stations[row["ground_station"]] = {
            "region": row["region"],
            "antenna_count": to_int(row["antenna_count"]),
            "emergency_priority": to_int(row["emergency_priority"]),
        }

    action_slots = sorted({entry["slot"] for entry in observation_windows.values()} | {entry["slot"] for entry in contacts.values()})
    horizon_slots = action_slots
    rolling_window = to_int(config["thermal_rolling_window_slots"])

    scenario_contact_capacity = {}
    for scenario_id in scenario_order:
        for contact_id, contact in contacts.items():
            multiplier = scenario_dl_multiplier[(scenario_id, contact["ground_station"], contact["slot"])]
            scenario_contact_capacity[(scenario_id, contact_id)] = contact["base_capacity_gb"] * multiplier

    usable_critical_count = {}
    usable_total_value = {}
    for scenario_id in scenario_order:
        usable_critical_count[scenario_id] = sum(
            requests[request_id]["critical"] * scenario_usable[scenario_id][request_id] for request_id in requests
        )
        usable_total_value[scenario_id] = sum(
            scenario_value[scenario_id][request_id] * scenario_usable[scenario_id][request_id] for request_id in requests
        )

    feasible_downlink_keys = []
    y_compatible_windows = {}
    for scenario_id in scenario_order:
        for contact_id, contact in contacts.items():
            cap = scenario_contact_capacity[(scenario_id, contact_id)]
            if cap <= config["quantity_tolerance"]:
                continue
            contact_sat = contact["satellite_id"]
            contact_slot = contact["slot"]
            for request_id, request in requests.items():
                if scenario_usable[scenario_id][request_id] != 1:
                    continue
                if contact_slot > request["deadline_slot"]:
                    continue
                compatible = [
                    window_id
                    for window_id in obs_by_request[request_id]
                    if observation_windows[window_id]["satellite_id"] == contact_sat
                    and observation_windows[window_id]["slot"] < contact_slot
                ]
                if not compatible:
                    continue
                key = (scenario_id, contact_id, request_id)
                feasible_downlink_keys.append(key)
                y_compatible_windows[key] = compatible

    y_keys_by_scenario_request = defaultdict(list)
    y_keys_by_scenario_contact = defaultdict(list)
    y_keys_by_scenario_sat_slot = defaultdict(list)
    y_keys_by_scenario_region_slot = defaultdict(list)
    for key in feasible_downlink_keys:
        scenario_id, contact_id, request_id = key
        contact = contacts[contact_id]
        y_keys_by_scenario_request[(scenario_id, request_id)].append(key)
        y_keys_by_scenario_contact[(scenario_id, contact_id)].append(key)
        y_keys_by_scenario_sat_slot[(scenario_id, contact["satellite_id"], contact["slot"])].append(key)
        y_keys_by_scenario_region_slot[
            (scenario_id, requests[request_id]["disaster_region"], contact["slot"])
        ].append(key)

    return {
        "config": config,
        "satellites": satellites,
        "requests": requests,
        "regions": sorted(regions),
        "stereo_pairs": {pair_id: tuple(reqs) for pair_id, reqs in stereo_pairs.items()},
        "observation_windows": observation_windows,
        "obs_by_request": dict(obs_by_request),
        "obs_by_sat_slot": dict(obs_by_sat_slot),
        "obs_high_by_sat_slot": dict(obs_high_by_sat_slot),
        "observation_lookup": observation_lookup,
        "contacts": contacts,
        "contacts_by_sat_slot": dict(contacts_by_sat_slot),
        "contacts_by_station_slot": dict(contacts_by_station_slot),
        "contact_lookup": contact_lookup,
        "scenarios": scenarios,
        "scenario_order": scenario_order,
        "scenario_usable": dict(scenario_usable),
        "scenario_value": dict(scenario_value),
        "scenario_contact_capacity": scenario_contact_capacity,
        "region_ingest": region_ingest,
        "ground_stations": ground_stations,
        "horizon_slots": horizon_slots,
        "rolling_window": rolling_window,
        "usable_critical_count": usable_critical_count,
        "usable_total_value": usable_total_value,
        "feasible_downlink_keys": feasible_downlink_keys,
        "y_compatible_windows": y_compatible_windows,
        "y_keys_by_scenario_request": dict(y_keys_by_scenario_request),
        "y_keys_by_scenario_contact": dict(y_keys_by_scenario_contact),
        "y_keys_by_scenario_sat_slot": dict(y_keys_by_scenario_sat_slot),
        "y_keys_by_scenario_region_slot": dict(y_keys_by_scenario_region_slot),
    }


def get_time_limit():
    env_value = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if not env_value:
        return DEFAULT_TIME_LIMIT
    try:
        parsed = float(env_value)
    except ValueError:
        return DEFAULT_TIME_LIMIT
    return min(DEFAULT_TIME_LIMIT, parsed)


def build_model(data, time_limit, fixed_x_values=None, local_branch_center=None, local_branch_radius=None):
    config = data["config"]
    satellites = data["satellites"]
    requests = data["requests"]
    observation_windows = data["observation_windows"]
    contacts = data["contacts"]
    scenarios = data["scenarios"]
    regions = data["regions"]
    stereo_pairs = data["stereo_pairs"]
    horizon_slots = data["horizon_slots"]
    usable_critical_count = data["usable_critical_count"]
    usable_total_value = data["usable_total_value"]

    model = Model("satellite_disaster_response")
    model.hideOutput()
    model.setParam("limits/gap", SCIP_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    x = {w: model.addVar(vtype="B", name=f"x[{w}]") for w in observation_windows}

    d = {}
    mem = {}
    energy = {}
    recharge = {}
    z = {}
    y = {}
    q = {}
    b = {}
    loss = {}
    excess = {}

    for scenario_id in data["scenario_order"]:
        for request_id in requests:
            ub = 1 if data["scenario_usable"][scenario_id][request_id] == 1 else 0
            d[(scenario_id, request_id)] = model.addVar(vtype="B", lb=0, ub=ub, name=f"d[{scenario_id},{request_id}]")
        for contact_id in contacts:
            cap = data["scenario_contact_capacity"][(scenario_id, contact_id)]
            ub = 1 if cap > config["quantity_tolerance"] else 0
            z[(scenario_id, contact_id)] = model.addVar(vtype="B", lb=0, ub=ub, name=f"z[{scenario_id},{contact_id}]")
        for region in regions:
            q[(scenario_id, region)] = model.addVar(vtype="B", name=f"q[{scenario_id},{region}]")
        for pair_id in stereo_pairs:
            b[(scenario_id, pair_id)] = model.addVar(vtype="B", name=f"b[{scenario_id},{pair_id}]")
        for satellite_id, sat in satellites.items():
            for slot in horizon_slots:
                mem[(scenario_id, satellite_id, slot)] = model.addVar(
                    lb=0.0, ub=sat["memory_capacity_gb"], name=f"mem[{scenario_id},{satellite_id},{slot}]"
                )
                energy[(scenario_id, satellite_id, slot)] = model.addVar(
                    lb=config["reserve_energy_kwh"], ub=sat["energy_capacity_kwh"],
                    name=f"energy[{scenario_id},{satellite_id},{slot}]"
                )
                recharge[(scenario_id, satellite_id, slot)] = model.addVar(
                    lb=0.0, ub=sat["solar_recharge_kwh_per_slot"], name=f"recharge[{scenario_id},{satellite_id},{slot}]"
                )
        loss[scenario_id] = model.addVar(lb=0.0, name=f"loss[{scenario_id}]")
        excess[scenario_id] = model.addVar(lb=0.0, name=f"excess[{scenario_id}]")

    eta = model.addVar(lb=-model.infinity(), name="eta")

    for key in data["feasible_downlink_keys"]:
        scenario_id, contact_id, request_id = key
        y[key] = model.addVar(lb=0.0, ub=requests[request_id]["data_gb"], name=f"y[{scenario_id},{contact_id},{request_id}]")

    if fixed_x_values is not None:
        for window_id, value in fixed_x_values.items():
            model.addCons(x[window_id] == float(value), name=f"fix_x[{window_id}]")

    for request_id, windows in data["obs_by_request"].items():
        model.addCons(quicksum(x[w] for w in windows) <= 1, name=f"observe_once[{request_id}]")

    for satellite_id in satellites:
        model.addCons(
            quicksum(x[w] for w, obs in observation_windows.items() if obs["satellite_id"] == satellite_id)
            <= config["max_observations_per_satellite"],
            name=f"max_obs[{satellite_id}]",
        )
        for slot in horizon_slots:
            slot_windows = data["obs_by_sat_slot"].get((satellite_id, slot), [])
            if slot_windows:
                model.addCons(quicksum(x[w] for w in slot_windows) <= 1, name=f"one_obs[{satellite_id},{slot}]")

    for satellite_id in satellites:
        for slot in horizon_slots[:-1]:
            high_now = data["obs_high_by_sat_slot"].get((satellite_id, slot), [])
            high_next = data["obs_high_by_sat_slot"].get((satellite_id, slot + 1), [])
            obs_now = data["obs_by_sat_slot"].get((satellite_id, slot), [])
            obs_next = data["obs_by_sat_slot"].get((satellite_id, slot + 1), [])
            if high_now and obs_next:
                model.addCons(
                    quicksum(x[w] for w in high_now) + quicksum(x[w] for w in obs_next) <= 1,
                    name=f"high_recovery_a[{satellite_id},{slot}]",
                )
            if obs_now and high_next:
                model.addCons(
                    quicksum(x[w] for w in obs_now) + quicksum(x[w] for w in high_next) <= 1,
                    name=f"high_recovery_b[{satellite_id},{slot}]",
                )

    for scenario_id in data["scenario_order"]:
        for contact_id in contacts:
            y_keys = data["y_keys_by_scenario_contact"].get((scenario_id, contact_id), [])
            cap = data["scenario_contact_capacity"][(scenario_id, contact_id)]
            if y_keys:
                model.addCons(
                    quicksum(y[key] for key in y_keys) <= cap * z[(scenario_id, contact_id)],
                    name=f"contact_cap[{scenario_id},{contact_id}]",
                )
            else:
                model.addCons(z[(scenario_id, contact_id)] == 0, name=f"unused_contact[{scenario_id},{contact_id}]")

        for ground_station, station in data["ground_stations"].items():
            for slot in horizon_slots:
                contact_ids = data["contacts_by_station_slot"].get((ground_station, slot), [])
                if not contact_ids:
                    continue
                model.addCons(
                    quicksum(z[(scenario_id, contact_id)] for contact_id in contact_ids) <= station["antenna_count"],
                    name=f"antenna[{scenario_id},{ground_station},{slot}]",
                )

        for satellite_id in satellites:
            for slot in horizon_slots:
                slot_windows = data["obs_by_sat_slot"].get((satellite_id, slot), [])
                slot_contacts = data["contacts_by_sat_slot"].get((satellite_id, slot), [])
                if not slot_windows and not slot_contacts:
                    continue
                model.addCons(
                    quicksum(x[w] for w in slot_windows) + quicksum(z[(scenario_id, contact_id)] for contact_id in slot_contacts) <= 1,
                    name=f"obs_downlink_mutex[{scenario_id},{satellite_id},{slot}]",
                )

    for key, compatible_windows in data["y_compatible_windows"].items():
        scenario_id, contact_id, request_id = key
        model.addCons(
            y[key] <= requests[request_id]["data_gb"] * quicksum(x[window_id] for window_id in compatible_windows),
            name=f"downlink_obs_link[{scenario_id},{contact_id},{request_id}]",
        )

    for scenario_id in data["scenario_order"]:
        for request_id, request in requests.items():
            y_keys = data["y_keys_by_scenario_request"].get((scenario_id, request_id), [])
            lhs = quicksum(y[key] for key in y_keys) if y_keys else 0.0
            model.addCons(lhs == request["data_gb"] * d[(scenario_id, request_id)], name=f"deliver_full[{scenario_id},{request_id}]")

    for scenario_id in data["scenario_order"]:
        for region in regions:
            for slot in horizon_slots:
                y_keys = data["y_keys_by_scenario_region_slot"].get((scenario_id, region, slot), [])
                if not y_keys:
                    continue
                model.addCons(
                    quicksum(y[key] for key in y_keys) <= data["region_ingest"][(scenario_id, region, slot)],
                    name=f"ingest[{scenario_id},{region},{slot}]",
                )

    for scenario_id in data["scenario_order"]:
        for satellite_id, sat in satellites.items():
            for slot in horizon_slots:
                obs_add = quicksum(
                    requests[observation_windows[window_id]["request_id"]]["data_gb"] * x[window_id]
                    for window_id in data["obs_by_sat_slot"].get((satellite_id, slot), [])
                )
                dl_remove_keys = data["y_keys_by_scenario_sat_slot"].get((scenario_id, satellite_id, slot), [])
                dl_remove = quicksum(y[key] for key in dl_remove_keys) if dl_remove_keys else 0.0
                previous_mem = sat["initial_memory_gb"] if slot == 1 else mem[(scenario_id, satellite_id, slot - 1)]
                model.addCons(
                    mem[(scenario_id, satellite_id, slot)] == previous_mem + obs_add - dl_remove,
                    name=f"memory[{scenario_id},{satellite_id},{slot}]",
                )

                obs_energy = quicksum(
                    observation_windows[window_id]["energy_kwh"] * x[window_id]
                    for window_id in data["obs_by_sat_slot"].get((satellite_id, slot), [])
                )
                dl_energy = sat["downlink_energy_kwh_per_gb"] * (
                    quicksum(y[key] for key in dl_remove_keys) if dl_remove_keys else 0.0
                )
                previous_energy = sat["initial_energy_kwh"] if slot == 1 else energy[(scenario_id, satellite_id, slot - 1)]
                model.addCons(
                    energy[(scenario_id, satellite_id, slot)] == previous_energy - obs_energy - dl_energy + recharge[(scenario_id, satellite_id, slot)],
                    name=f"energy_bal[{scenario_id},{satellite_id},{slot}]",
                )

            for slot in horizon_slots:
                thermal_slots = range(max(1, slot - data["rolling_window"] + 1), slot + 1)
                thermal_terms = []
                for tau in thermal_slots:
                    thermal_terms.extend(
                        observation_windows[window_id]["thermal_load"] * x[window_id]
                        for window_id in data["obs_by_sat_slot"].get((satellite_id, tau), [])
                    )
                    tau_keys = data["y_keys_by_scenario_sat_slot"].get((scenario_id, satellite_id, tau), [])
                    if tau_keys:
                        thermal_terms.append(
                            sat["downlink_thermal_per_gb"] * quicksum(y[key] for key in tau_keys)
                        )
                model.addCons(
                    quicksum(thermal_terms) <= sat["thermal_limit_rolling"],
                    name=f"thermal[{scenario_id},{satellite_id},{slot}]",
                )

    for scenario_id in data["scenario_order"]:
        usable_critical = usable_critical_count[scenario_id]
        if usable_critical > 0:
            model.addCons(
                quicksum(
                    requests[request_id]["critical"] * d[(scenario_id, request_id)]
                    for request_id in requests
                    if data["scenario_usable"][scenario_id][request_id] == 1
                )
                >= config["minimum_critical_delivery_fraction"] * usable_critical,
                name=f"critical_floor[{scenario_id}]",
            )

        model.addCons(
            quicksum(data["scenario_value"][scenario_id][request_id] * d[(scenario_id, request_id)] for request_id in requests)
            >= config["minimum_delivered_value_fraction"] * usable_total_value[scenario_id],
            name=f"value_floor[{scenario_id}]",
        )

        for region in regions:
            region_requests = [request_id for request_id, request in requests.items() if request["disaster_region"] == region]
            model.addCons(
                quicksum(d[(scenario_id, request_id)] for request_id in region_requests) >= q[(scenario_id, region)],
                name=f"region_cov_link[{scenario_id},{region}]",
            )

        model.addCons(
            quicksum(q[(scenario_id, region)] for region in regions) >= config["minimum_regions_delivered_per_scenario"],
            name=f"region_floor[{scenario_id}]",
        )

        for pair_id, pair_requests in stereo_pairs.items():
            for request_id in pair_requests:
                model.addCons(b[(scenario_id, pair_id)] <= d[(scenario_id, request_id)], name=f"pair_ub[{scenario_id},{pair_id},{request_id}]")
            model.addCons(
                b[(scenario_id, pair_id)] >= quicksum(d[(scenario_id, request_id)] for request_id in pair_requests) - len(pair_requests) + 1,
                name=f"pair_lb[{scenario_id},{pair_id}]",
            )

    alpha = config["tail_risk_alpha"]
    scenario_profit_expr = {}
    for scenario_id in data["scenario_order"]:
        delivered_value = quicksum(data["scenario_value"][scenario_id][request_id] * d[(scenario_id, request_id)] for request_id in requests)
        stereo_value = quicksum(config["stereo_pair_bonus"] * b[(scenario_id, pair_id)] for pair_id in stereo_pairs)
        downlink_cost = quicksum(config["downlink_cost_per_gb"] * y[key] for key in data["feasible_downlink_keys"] if key[0] == scenario_id)
        critical_penalty = quicksum(
            config["unmet_critical_penalty"]
            * requests[request_id]["critical"]
            * data["scenario_usable"][scenario_id][request_id]
            * (1 - d[(scenario_id, request_id)])
            for request_id in requests
        )
        scenario_profit_expr[scenario_id] = delivered_value + stereo_value - downlink_cost - critical_penalty
        model.addCons(loss[scenario_id] >= scenarios[scenario_id]["target_profit"] - scenario_profit_expr[scenario_id], name=f"loss_shortfall[{scenario_id}]")
        model.addCons(excess[scenario_id] >= loss[scenario_id] - eta, name=f"cvar_excess[{scenario_id}]")

    obs_cost_expr = config["observation_fixed_cost"] * quicksum(x[w] for w in observation_windows)
    expected_profit = quicksum(scenarios[scenario_id]["probability"] * scenario_profit_expr[scenario_id] for scenario_id in data["scenario_order"])
    cvar_expr = eta + (1.0 / (1.0 - alpha)) * quicksum(
        scenarios[scenario_id]["probability"] * excess[scenario_id] for scenario_id in data["scenario_order"]
    )
    objective_expr = expected_profit - obs_cost_expr - config["tail_risk_weight"] * cvar_expr

    if local_branch_center is not None and local_branch_radius is not None:
        center_on = {window_id for window_id, value in local_branch_center.items() if value > 0.5}
        local_branch_expr = len(center_on) - quicksum(x[window_id] for window_id in center_on) + quicksum(
            x[window_id] for window_id in observation_windows if window_id not in center_on
        )
        model.addCons(local_branch_expr <= local_branch_radius, name="local_branching")

    model.setObjective(objective_expr, sense="maximize")

    return {
        "model": model,
        "x": x,
        "y": y,
        "z": z,
        "d": d,
        "q": q,
        "b": b,
        "mem": mem,
        "energy": energy,
        "recharge": recharge,
        "loss": loss,
        "excess": excess,
        "eta": eta,
        "scenario_profit_expr": scenario_profit_expr,
        "obs_cost_expr": obs_cost_expr,
        "cvar_expr": cvar_expr,
        "objective_expr": objective_expr,
    }


def extract_solution_rows(data, model_artifacts):
    model = model_artifacts["model"]
    if model.getNSols() <= 0:
        raise RuntimeError("SCIP finished without an incumbent solution.")
    best_sol = model.getBestSol()

    x = model_artifacts["x"]
    y = model_artifacts["y"]
    observation_windows = data["observation_windows"]
    contacts = data["contacts"]

    rows = []
    for window_id, var in x.items():
        value = model.getSolVal(best_sol, var)
        if value <= 0.5:
            continue
        window = observation_windows[window_id]
        rows.append(
            {
                "row_type": "observe",
                "scenario_id": "",
                "satellite_id": window["satellite_id"],
                "slot": str(window["slot"]),
                "request_id": window["request_id"],
                "ground_station": "",
                "mode": "image",
                "quantity": "",
            }
        )

    for key, var in y.items():
        value = model.getSolVal(best_sol, var)
        if value <= data["config"]["quantity_tolerance"]:
            continue
        scenario_id, contact_id, request_id = key
        contact = contacts[contact_id]
        rows.append(
            {
                "row_type": "downlink",
                "scenario_id": scenario_id,
                "satellite_id": contact["satellite_id"],
                "slot": str(contact["slot"]),
                "request_id": request_id,
                "ground_station": contact["ground_station"],
                "mode": "downlink",
                "quantity": format_quantity(value),
            }
        )

    rows.sort(
        key=lambda row: (
            0 if row["row_type"] == "observe" else 1,
            row["scenario_id"],
            row["satellite_id"],
            int(row["slot"]),
            row["ground_station"],
            row["request_id"],
        )
    )
    return rows


def write_solution(rows, path=SOLUTION_PATH):
    header = solution_header()
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def read_solution_file(path):
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        rows = list(reader)
    return header, rows


def validate_solution(data, rows):
    config = data["config"]
    requests = data["requests"]
    satellites = data["satellites"]
    scenarios = data["scenarios"]
    observation_windows = data["observation_windows"]
    contacts = data["contacts"]
    stereo_pairs = data["stereo_pairs"]
    regions = data["regions"]
    quantity_tol = config["quantity_tolerance"]
    horizon_slots = set(data["horizon_slots"])

    expected_header = solution_header()

    diagnostics = {
        "row_count": len(rows),
        "max_residual": 0.0,
        "violations": [],
        "incumbent_summary": {},
        "scenario_profit": {},
        "objective": None,
        "expected_profit": None,
        "cvar": None,
    }

    observe_rows = []
    downlink_rows = []
    for idx, row in enumerate(rows, start=2):
        row_type = row.get("row_type", "")
        if row_type == "observe":
            observe_rows.append((idx, row))
        elif row_type == "downlink":
            downlink_rows.append((idx, row))
        else:
            diagnostics["violations"].append(f"Row {idx}: invalid row_type {row_type!r}.")

    observation_choice = {}
    observed_request_count = Counter()
    observed_sat_slot = Counter()
    observed_sat_count = Counter()

    for idx, row in observe_rows:
        if row["scenario_id"] != "" or row["ground_station"] != "" or row["quantity"] != "" or row["mode"] != "image":
            diagnostics["violations"].append(f"Row {idx}: malformed observation row.")
            continue
        try:
            slot = int(row["slot"])
        except ValueError:
            diagnostics["violations"].append(f"Row {idx}: non-integer observation slot.")
            continue
        key = (row["satellite_id"], slot, row["request_id"])
        if key not in data["observation_lookup"]:
            diagnostics["violations"].append(f"Row {idx}: observation row does not match a listed observation window.")
            continue
        window_id = data["observation_lookup"][key]
        observation_choice[row["request_id"]] = window_id
        observed_request_count[row["request_id"]] += 1
        observed_sat_slot[(row["satellite_id"], slot)] += 1
        observed_sat_count[row["satellite_id"]] += 1

    for request_id, count in observed_request_count.items():
        if count > 1:
            diagnostics["violations"].append(f"Request {request_id} observed more than once.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], count - 1)

    for key, count in observed_sat_slot.items():
        if count > 1:
            diagnostics["violations"].append(f"Satellite {key[0]} has more than one observation in slot {key[1]}.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], count - 1)

    for satellite_id, count in observed_sat_count.items():
        excess = count - config["max_observations_per_satellite"]
        if excess > 0:
            diagnostics["violations"].append(f"Satellite {satellite_id} exceeds max observations.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], excess / config["max_observations_per_satellite"])

    horizon_slots_sorted = data["horizon_slots"]

    for satellite_id in satellites:
        for slot in horizon_slots_sorted:
            if slot + 1 not in horizon_slots:
                continue
            window_now = data["obs_by_sat_slot"].get((satellite_id, slot), [])
            window_next = data["obs_by_sat_slot"].get((satellite_id, slot + 1), [])
            selected_now = [w for w in window_now if observation_choice.get(observation_windows[w]["request_id"]) == w]
            selected_next = [w for w in window_next if observation_choice.get(observation_windows[w]["request_id"]) == w]
            if selected_now and selected_next:
                high_now = observation_windows[selected_now[0]]["slew_class"] == "high"
                high_next = observation_windows[selected_next[0]]["slew_class"] == "high"
                if high_now or high_next:
                    diagnostics["violations"].append(
                        f"Satellite {satellite_id} violates high-slew recovery between slots {slot} and {slot + 1}."
                    )
                    diagnostics["max_residual"] = max(diagnostics["max_residual"], 1.0)

    downlink_qty = defaultdict(float)
    contact_usage = defaultdict(int)
    sat_slot_usage = defaultdict(int)
    region_slot_total = defaultdict(float)
    delivered = defaultdict(int)

    for idx, row in downlink_rows:
        if row["mode"] != "downlink":
            diagnostics["violations"].append(f"Row {idx}: downlink row must use mode=downlink.")
            continue
        scenario_id = row["scenario_id"]
        if scenario_id not in scenarios:
            diagnostics["violations"].append(f"Row {idx}: invalid scenario_id.")
            continue
        try:
            slot = int(row["slot"])
            qty = float(row["quantity"])
        except ValueError:
            diagnostics["violations"].append(f"Row {idx}: invalid slot or quantity.")
            continue
        if slot not in horizon_slots:
            diagnostics["violations"].append(f"Row {idx}: slot out of horizon.")
            continue
        if qty < -EPS:
            diagnostics["violations"].append(f"Row {idx}: negative downlink quantity.")
            continue
        contact_key = (row["satellite_id"], slot, row["ground_station"])
        if contact_key not in data["contact_lookup"]:
            diagnostics["violations"].append(f"Row {idx}: downlink row does not match a listed contact.")
            continue
        contact_id = data["contact_lookup"][contact_key]
        if row["request_id"] not in requests:
            diagnostics["violations"].append(f"Row {idx}: unknown request_id.")
            continue
        request_id = row["request_id"]
        downlink_qty[(scenario_id, contact_id, request_id)] += qty
        if qty > quantity_tol:
            contact_usage[(scenario_id, contact_id)] = 1
            sat_slot_usage[(scenario_id, row["satellite_id"], slot)] = 1
            region = requests[request_id]["disaster_region"]
            region_slot_total[(scenario_id, region, slot)] += qty

    for key, total in downlink_qty.items():
        scenario_id, contact_id, request_id = key
        contact = contacts[contact_id]
        request = requests[request_id]
        obs_window_id = observation_choice.get(request_id)
        if obs_window_id is None:
            diagnostics["violations"].append(f"Scenario {scenario_id}: request {request_id} is downlinked without being observed.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], total / max(request["data_gb"], 1.0))
            continue
        obs_window = observation_windows[obs_window_id]
        if obs_window["satellite_id"] != contact["satellite_id"]:
            diagnostics["violations"].append(f"Scenario {scenario_id}: request {request_id} downlinked by the wrong satellite.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], 1.0)
        if obs_window["slot"] >= contact["slot"]:
            diagnostics["violations"].append(f"Scenario {scenario_id}: request {request_id} downlinked before observation.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], 1.0)
        if contact["slot"] > request["deadline_slot"]:
            diagnostics["violations"].append(f"Scenario {scenario_id}: request {request_id} downlinked after deadline.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], 1.0)

    for scenario_id in data["scenario_order"]:
        for contact_id, contact in contacts.items():
            used_total = sum(
                downlink_qty[(scenario_id, contact_id, request_id)]
                for request_id in requests
                if (scenario_id, contact_id, request_id) in downlink_qty
            )
            cap = data["scenario_contact_capacity"][(scenario_id, contact_id)]
            residual = max(0.0, used_total - cap)
            if residual > 1e-7:
                diagnostics["violations"].append(f"Scenario {scenario_id}: contact {contact_id} exceeds capacity.")
                diagnostics["max_residual"] = max(diagnostics["max_residual"], residual / max(cap, 1.0))

        for ground_station, station in data["ground_stations"].items():
            for slot in data["horizon_slots"]:
                used = sum(
                    contact_usage.get((scenario_id, contact_id), 0)
                    for contact_id in data["contacts_by_station_slot"].get((ground_station, slot), [])
                )
                excess = used - station["antenna_count"]
                if excess > 0:
                    diagnostics["violations"].append(f"Scenario {scenario_id}: station {ground_station} exceeds antenna count in slot {slot}.")
                    diagnostics["max_residual"] = max(diagnostics["max_residual"], excess / station["antenna_count"])

        for satellite_id in satellites:
            for slot in data["horizon_slots"]:
                obs_used = 1 if any(
                    observation_choice.get(observation_windows[w]["request_id"]) == w
                    for w in data["obs_by_sat_slot"].get((satellite_id, slot), [])
                ) else 0
                dl_used = sat_slot_usage.get((scenario_id, satellite_id, slot), 0)
                if obs_used + dl_used > 1:
                    diagnostics["violations"].append(f"Scenario {scenario_id}: satellite {satellite_id} both observes and downlinks in slot {slot}.")
                    diagnostics["max_residual"] = max(diagnostics["max_residual"], 1.0)

        for region in regions:
            for slot in data["horizon_slots"]:
                used = region_slot_total.get((scenario_id, region, slot), 0.0)
                cap = data["region_ingest"][(scenario_id, region, slot)]
                residual = max(0.0, used - cap)
                if residual > 1e-7:
                    diagnostics["violations"].append(f"Scenario {scenario_id}: region {region} ingest exceeded in slot {slot}.")
                    diagnostics["max_residual"] = max(diagnostics["max_residual"], residual / max(cap, 1.0))

    memory_state = {}
    energy_state = {}
    delivered_value = defaultdict(float)
    downlink_cost = defaultdict(float)
    critical_penalty = defaultdict(float)
    stereo_bonus = defaultdict(float)
    region_delivered = defaultdict(set)
    delivered_critical = defaultdict(int)

    for scenario_id in data["scenario_order"]:
        for request_id, request in requests.items():
            total = sum(
                downlink_qty[(scenario_id, contact_id, request_id)]
                for contact_id in contacts
                if (scenario_id, contact_id, request_id) in downlink_qty
            )
            if data["scenario_usable"][scenario_id][request_id] == 1 and abs(total - request["data_gb"]) <= 1e-6:
                delivered[(scenario_id, request_id)] = 1
                delivered_value[scenario_id] += data["scenario_value"][scenario_id][request_id]
                region_delivered[scenario_id].add(request["disaster_region"])
                if request["critical"] == 1:
                    delivered_critical[scenario_id] += 1
            else:
                delivered[(scenario_id, request_id)] = 0
                if request["critical"] == 1 and data["scenario_usable"][scenario_id][request_id] == 1:
                    critical_penalty[scenario_id] += config["unmet_critical_penalty"]

        for pair_id, pair_requests in stereo_pairs.items():
            if all(delivered[(scenario_id, request_id)] == 1 for request_id in pair_requests):
                stereo_bonus[scenario_id] += config["stereo_pair_bonus"]

        for satellite_id, sat in satellites.items():
            prev_mem = sat["initial_memory_gb"]
            prev_energy = sat["initial_energy_kwh"]
            for slot in data["horizon_slots"]:
                obs_windows = data["obs_by_sat_slot"].get((satellite_id, slot), [])
                obs_add = 0.0
                obs_energy = 0.0
                obs_thermal = 0.0
                for window_id in obs_windows:
                    if observation_choice.get(observation_windows[window_id]["request_id"]) == window_id:
                        request_id = observation_windows[window_id]["request_id"]
                        obs_add += requests[request_id]["data_gb"]
                        obs_energy += observation_windows[window_id]["energy_kwh"]
                        obs_thermal += observation_windows[window_id]["thermal_load"]
                dl_remove = sum(
                    downlink_qty[(scenario_id, contact_id, request_id)]
                    for contact_id in data["contacts_by_sat_slot"].get((satellite_id, slot), [])
                    for request_id in requests
                    if (scenario_id, contact_id, request_id) in downlink_qty
                )
                mem_now = prev_mem + obs_add - dl_remove
                memory_state[(scenario_id, satellite_id, slot)] = mem_now
                prev_mem = mem_now

                max_recharge = sat["solar_recharge_kwh_per_slot"]
                recharge = min(max_recharge, sat["energy_capacity_kwh"] - (prev_energy - obs_energy - sat["downlink_energy_kwh_per_gb"] * dl_remove))
                recharge = max(0.0, recharge)
                energy_now = prev_energy - obs_energy - sat["downlink_energy_kwh_per_gb"] * dl_remove + recharge
                energy_state[(scenario_id, satellite_id, slot)] = energy_now
                prev_energy = energy_now

                mem_residual = max(0.0, -mem_now, mem_now - sat["memory_capacity_gb"])
                if mem_residual > 1e-7:
                    diagnostics["violations"].append(f"Scenario {scenario_id}: memory bound violated for {satellite_id} in slot {slot}.")
                    diagnostics["max_residual"] = max(diagnostics["max_residual"], mem_residual / max(sat["memory_capacity_gb"], 1.0))

                energy_low = max(0.0, config["reserve_energy_kwh"] - energy_now)
                energy_high = max(0.0, energy_now - sat["energy_capacity_kwh"])
                if energy_low > 1e-7 or energy_high > 1e-7:
                    diagnostics["violations"].append(f"Scenario {scenario_id}: energy bound violated for {satellite_id} in slot {slot}.")
                    diagnostics["max_residual"] = max(
                        diagnostics["max_residual"], max(energy_low, energy_high) / max(sat["energy_capacity_kwh"], 1.0)
                    )

            for slot in data["horizon_slots"]:
                start = max(1, slot - data["rolling_window"] + 1)
                thermal_total = 0.0
                for tau in range(start, slot + 1):
                    for window_id in data["obs_by_sat_slot"].get((satellite_id, tau), []):
                        if observation_choice.get(observation_windows[window_id]["request_id"]) == window_id:
                            thermal_total += observation_windows[window_id]["thermal_load"]
                    thermal_total += sat["downlink_thermal_per_gb"] * sum(
                        downlink_qty[(scenario_id, contact_id, request_id)]
                        for contact_id in data["contacts_by_sat_slot"].get((satellite_id, tau), [])
                        for request_id in requests
                        if (scenario_id, contact_id, request_id) in downlink_qty
                    )
                excess = max(0.0, thermal_total - sat["thermal_limit_rolling"])
                if excess > 1e-7:
                    diagnostics["violations"].append(f"Scenario {scenario_id}: thermal limit violated for {satellite_id} ending slot {slot}.")
                    diagnostics["max_residual"] = max(diagnostics["max_residual"], excess / max(sat["thermal_limit_rolling"], 1.0))

        downlink_cost[scenario_id] = config["downlink_cost_per_gb"] * sum(
            qty for (scn, _contact_id, _request_id), qty in downlink_qty.items() if scn == scenario_id
        )

        usable_critical = data["usable_critical_count"][scenario_id]
        if usable_critical > 0:
            achieved = delivered_critical[scenario_id]
            required = config["minimum_critical_delivery_fraction"] * usable_critical
            shortfall = max(0.0, required - achieved)
            if shortfall > 1e-7:
                diagnostics["violations"].append(f"Scenario {scenario_id}: critical delivery floor not met.")
                diagnostics["max_residual"] = max(diagnostics["max_residual"], shortfall / max(usable_critical, 1.0))

        value_required = config["minimum_delivered_value_fraction"] * data["usable_total_value"][scenario_id]
        value_shortfall = max(0.0, value_required - delivered_value[scenario_id])
        if value_shortfall > 1e-7:
            diagnostics["violations"].append(f"Scenario {scenario_id}: delivered value floor not met.")
            diagnostics["max_residual"] = max(
                diagnostics["max_residual"], value_shortfall / max(data["usable_total_value"][scenario_id], 1.0)
            )

        region_shortfall = max(0.0, config["minimum_regions_delivered_per_scenario"] - len(region_delivered[scenario_id]))
        if region_shortfall > 0:
            diagnostics["violations"].append(f"Scenario {scenario_id}: regional coverage floor not met.")
            diagnostics["max_residual"] = max(diagnostics["max_residual"], region_shortfall / max(len(regions), 1))

    observation_cost = config["observation_fixed_cost"] * len(observation_choice)
    scenario_profit = {}
    for scenario_id in data["scenario_order"]:
        scenario_profit[scenario_id] = (
            delivered_value[scenario_id] + stereo_bonus[scenario_id] - downlink_cost[scenario_id] - critical_penalty[scenario_id]
        )
    diagnostics["scenario_profit"] = scenario_profit

    losses = {
        scenario_id: max(0.0, scenarios[scenario_id]["target_profit"] - scenario_profit[scenario_id])
        for scenario_id in data["scenario_order"]
    }
    alpha = config["tail_risk_alpha"]
    candidate_losses = sorted(set(losses.values()))
    best_cvar = None
    for eta in candidate_losses + [0.0]:
        cvar = eta + (1.0 / (1.0 - alpha)) * sum(
            scenarios[scenario_id]["probability"] * max(0.0, losses[scenario_id] - eta) for scenario_id in data["scenario_order"]
        )
        if best_cvar is None or cvar < best_cvar:
            best_cvar = cvar
    expected_profit = sum(scenarios[scenario_id]["probability"] * scenario_profit[scenario_id] for scenario_id in data["scenario_order"])
    objective = expected_profit - observation_cost - config["tail_risk_weight"] * best_cvar
    diagnostics["expected_profit"] = expected_profit
    diagnostics["cvar"] = best_cvar
    diagnostics["objective"] = objective

    diagnostics["incumbent_summary"] = {
        "observations": len(observation_choice),
        "downlink_rows": len(downlink_rows),
        "scenario_deliveries": {
            scenario_id: sum(delivered[(scenario_id, request_id)] for request_id in requests) for scenario_id in data["scenario_order"]
        },
    }
    diagnostics["schema_ok"] = len(diagnostics["violations"]) == 0
    diagnostics["expected_header"] = expected_header
    return diagnostics


def audit_solution_file(data, path):
    header, rows = read_solution_file(path)
    diagnostics = validate_solution(data, rows)
    diagnostics["header"] = header
    diagnostics["header_ok"] = header == solution_header()
    if not diagnostics["header_ok"]:
        diagnostics["violations"].insert(0, f"Header mismatch: expected {solution_header()} got {header}.")
        diagnostics["max_residual"] = max(diagnostics["max_residual"], 1.0)
    return rows, diagnostics


def solution_rows_to_maps(data, rows):
    x_values = {window_id: 0.0 for window_id in data["observation_windows"]}
    y_values = defaultdict(float)

    for row in rows:
        if row["row_type"] == "observe":
            key = (row["satellite_id"], int(row["slot"]), row["request_id"])
            window_id = data["observation_lookup"].get(key)
            if window_id is not None:
                x_values[window_id] = 1.0
        elif row["row_type"] == "downlink":
            scenario_id = row["scenario_id"]
            if scenario_id not in data["scenarios"]:
                continue
            contact_key = (row["satellite_id"], int(row["slot"]), row["ground_station"])
            contact_id = data["contact_lookup"].get(contact_key)
            if contact_id is None:
                continue
            request_id = row["request_id"]
            try:
                qty = float(row["quantity"])
            except ValueError:
                continue
            y_values[(scenario_id, contact_id, request_id)] += qty

    delivered = {}
    z_values = {}
    q_values = {}
    b_values = {}
    delivered_by_scenario_region = defaultdict(set)

    delivered_qty = defaultdict(float)
    for (scenario_id, _contact_id, request_id), qty in y_values.items():
        delivered_qty[(scenario_id, request_id)] += qty

    for scenario_id in data["scenario_order"]:
        for request_id, request in data["requests"].items():
            total = delivered_qty[(scenario_id, request_id)]
            delivered[(scenario_id, request_id)] = 1.0 if (
                data["scenario_usable"][scenario_id][request_id] == 1 and abs(total - request["data_gb"]) <= 1e-6
            ) else 0.0
            if delivered[(scenario_id, request_id)] > 0.5:
                delivered_by_scenario_region[scenario_id].add(request["disaster_region"])

        for contact_id in data["contacts"]:
            used = any(
                y_values[(scenario_id, contact_id, request_id)] > data["config"]["quantity_tolerance"]
                for request_id in data["requests"]
            )
            z_values[(scenario_id, contact_id)] = 1.0 if used else 0.0

        for region in data["regions"]:
            q_values[(scenario_id, region)] = 1.0 if region in delivered_by_scenario_region[scenario_id] else 0.0

        for pair_id, pair_requests in data["stereo_pairs"].items():
            b_values[(scenario_id, pair_id)] = 1.0 if all(delivered[(scenario_id, request_id)] > 0.5 for request_id in pair_requests) else 0.0

    return {
        "x_values": x_values,
        "y_values": dict(y_values),
        "d_values": delivered,
        "z_values": z_values,
        "q_values": q_values,
        "b_values": b_values,
    }


def add_mip_start_from_rows(data, model_artifacts, rows):
    model = model_artifacts["model"]
    incumbent = solution_rows_to_maps(data, rows)
    sol = model.createPartialSol()

    for window_id, value in incumbent["x_values"].items():
        model.setSolVal(sol, model_artifacts["x"][window_id], value)

    for key, value in incumbent["y_values"].items():
        if key in model_artifacts["y"]:
            model.setSolVal(sol, model_artifacts["y"][key], value)

    for key, value in incumbent["z_values"].items():
        model.setSolVal(sol, model_artifacts["z"][key], value)

    for key, value in incumbent["d_values"].items():
        model.setSolVal(sol, model_artifacts["d"][key], value)

    for key, value in incumbent["q_values"].items():
        model.setSolVal(sol, model_artifacts["q"][key], value)

    for key, value in incumbent["b_values"].items():
        model.setSolVal(sol, model_artifacts["b"][key], value)

    stored = model.addSol(sol, free=True)
    return stored


def run_attempt(
    data,
    attempt_name,
    time_limit,
    candidate_path,
    incumbent_rows=None,
    fixed_x_values=None,
    local_branch_center=None,
    local_branch_radius=None,
):
    build_start = time.time()
    artifacts = build_model(
        data,
        time_limit,
        fixed_x_values=fixed_x_values,
        local_branch_center=local_branch_center,
        local_branch_radius=local_branch_radius,
    )
    build_seconds = time.time() - build_start

    mip_start_stored = False
    if incumbent_rows is not None:
        mip_start_stored = add_mip_start_from_rows(data, artifacts, incumbent_rows)

    model = artifacts["model"]
    solve_start = time.time()
    model.optimize()
    solve_seconds = time.time() - solve_start

    result = {
        "attempt_name": attempt_name,
        "build_seconds": build_seconds,
        "solve_seconds": solve_seconds,
        "time_limit": time_limit,
        "mip_start_stored": mip_start_stored,
        "status": str(model.getStatus()),
        "n_solutions": model.getNSols(),
        "objective": None,
        "best_bound": None,
        "gap": None,
        "rows": None,
        "audit": None,
    }

    if model.getNSols() > 0:
        result["objective"] = model.getObjVal()
        result["best_bound"] = model.getDualbound()
        result["gap"] = model.getGap()
        rows = extract_solution_rows(data, artifacts)
        write_solution(rows, candidate_path)
        audited_rows, audit = audit_solution_file(data, candidate_path)
        result["rows"] = audited_rows
        result["audit"] = audit

    return result


def solve():
    start = time.time()
    SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = read_data()
    time_limit = get_time_limit()

    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Required model file missing: {MODEL_PATH}")

    attempts = []

    incumbent_rows = None
    incumbent_audit = None
    if SOLUTION_PATH.exists():
        incumbent_rows, incumbent_audit = audit_solution_file(data, SOLUTION_PATH)

    if incumbent_audit is None or incumbent_audit["violations"]:
        baseline_attempt = run_attempt(
            data=data,
            attempt_name="baseline_full_solve",
            time_limit=time_limit,
            candidate_path=SOLUTION_PATH,
        )
        attempts.append(baseline_attempt)
        if baseline_attempt["n_solutions"] <= 0:
            raise RuntimeError("No incumbent solution found within the solve limit.")
        if baseline_attempt["audit"]["violations"]:
            raise RuntimeError(f"Validation failed with {len(baseline_attempt['audit']['violations'])} issue(s).")
        incumbent_rows = baseline_attempt["rows"]
        incumbent_audit = baseline_attempt["audit"]
    else:
        write_solution(incumbent_rows, SOLUTION_PATH)

    current_best_rows = incumbent_rows
    current_best_audit = incumbent_audit

    recourse_time = min(max(45.0, 0.28 * time_limit), 90.0)
    local_branch_time = max(0.0, min(150.0, time_limit - recourse_time - 5.0))

    incumbent_maps = solution_rows_to_maps(data, current_best_rows)
    recourse_attempt = run_attempt(
        data=data,
        attempt_name="fixed_observation_recourse",
        time_limit=recourse_time,
        candidate_path=CANDIDATE_RECOURSE_PATH,
        incumbent_rows=current_best_rows,
        fixed_x_values=incumbent_maps["x_values"],
    )
    attempts.append(recourse_attempt)

    if (
        recourse_attempt["audit"] is not None
        and not recourse_attempt["audit"]["violations"]
        and recourse_attempt["audit"]["objective"] > current_best_audit["objective"] + IMPROVEMENT_EPS
    ):
        current_best_rows = recourse_attempt["rows"]
        current_best_audit = recourse_attempt["audit"]

    if local_branch_time >= 30.0:
        branch_center = solution_rows_to_maps(data, current_best_rows)["x_values"]
        local_branch_attempt = run_attempt(
            data=data,
            attempt_name="local_branching_radius_4",
            time_limit=local_branch_time,
            candidate_path=CANDIDATE_LOCAL_BRANCH_PATH,
            incumbent_rows=current_best_rows,
            local_branch_center=branch_center,
            local_branch_radius=4,
        )
        attempts.append(local_branch_attempt)

        if (
            local_branch_attempt["audit"] is not None
            and not local_branch_attempt["audit"]["violations"]
            and local_branch_attempt["audit"]["objective"] > current_best_audit["objective"] + IMPROVEMENT_EPS
        ):
            current_best_rows = local_branch_attempt["rows"]
            current_best_audit = local_branch_attempt["audit"]

    published_improvement = current_best_audit["objective"] > incumbent_audit["objective"] + IMPROVEMENT_EPS
    if published_improvement:
        write_solution(current_best_rows, SOLUTION_PATH)
    final_rows, final_audit = audit_solution_file(data, SOLUTION_PATH)

    elapsed = time.time() - start
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        "1. `PYTHONPATH=/opt/or-deps python -c \"import pyscipopt; print(pyscipopt.__version__)\"`",
        "2. `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Incumbent Audit",
        "",
        f"- Source artifact: `{SOLUTION_PATH}`",
        f"- Header exact-match: `{incumbent_audit['header_ok']}`",
        f"- Incumbent objective: `{incumbent_audit['objective']:.8f}`",
        f"- Incumbent expected profit: `{incumbent_audit['expected_profit']:.8f}`",
        f"- Incumbent CVaR loss: `{incumbent_audit['cvar']:.8f}`",
        f"- Incumbent audit violations: `{len(incumbent_audit['violations'])}`",
        f"- Incumbent max normalized residual: `{incumbent_audit['max_residual']:.8f}`",
        "",
        "## Performance Attempts",
        "",
    ]

    for attempt in attempts:
        lines.append(f"### `{attempt['attempt_name']}`")
        lines.append("")
        lines.append(f"- Time limit: `{attempt['time_limit']:.2f}` seconds")
        lines.append(f"- Build time: `{attempt['build_seconds']:.2f}` seconds")
        lines.append(f"- Solve time: `{attempt['solve_seconds']:.2f}` seconds")
        lines.append(f"- MIP start stored: `{attempt['mip_start_stored']}`")
        lines.append(f"- SCIP status: `{attempt['status']}`")
        lines.append(f"- Incumbent count: `{attempt['n_solutions']}`")
        if attempt["objective"] is not None:
            lines.append(f"- Candidate objective: `{attempt['objective']:.8f}`")
            lines.append(f"- Candidate best bound: `{attempt['best_bound']:.8f}`")
            lines.append(f"- Candidate gap: `{attempt['gap']:.8f}`")
        else:
            lines.append("- Candidate objective: `None`")
            lines.append("- Candidate best bound: `None`")
            lines.append("- Candidate gap: `None`")
        if attempt["audit"] is not None:
            lines.append(f"- Recomputed candidate objective: `{attempt['audit']['objective']:.8f}`")
            lines.append(f"- Candidate audit violations: `{len(attempt['audit']['violations'])}`")
            lines.append(f"- Candidate max normalized residual: `{attempt['audit']['max_residual']:.8f}`")
        else:
            lines.append("- Candidate audit: `no incumbent exported`")
        lines.append("")

    lines.extend(
        [
            "## Final Artifact",
            "",
            f"- Published improved artifact: `{published_improvement}`",
            f"- Final objective on disk: `{final_audit['objective']:.8f}`",
            f"- Final audit violations: `{len(final_audit['violations'])}`",
            f"- Final max normalized residual: `{final_audit['max_residual']:.8f}`",
            f"- Total elapsed: `{elapsed:.2f}` seconds",
        ]
    )
    for scenario_id in data["scenario_order"]:
        lines.append(f"- Final scenario profit `{scenario_id}`: `{final_audit['scenario_profit'][scenario_id]:.8f}`")
    if final_audit["violations"]:
        lines.append("")
        lines.append("### Final Audit Findings")
        lines.append("")
        for violation in final_audit["violations"]:
            lines.append(f"- {violation}")

    with open(LOG_PATH, "w") as handle:
        handle.write("\n".join(lines) + "\n")

    if final_audit["violations"]:
        raise RuntimeError(f"Validation failed with {len(final_audit['violations'])} issue(s).")


if __name__ == "__main__":
    solve()
