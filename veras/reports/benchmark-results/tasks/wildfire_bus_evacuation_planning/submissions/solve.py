import csv
import itertools
import json
import math
import os
import sys
import time
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from pyscipopt import Model, quicksum, __version__ as PYSCIPOPT_VERSION


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.json"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"

KMAX = 4
MIP_GAP = 0.0005
TIME_BUFFER_SECONDS = 15.0


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def as_int(value):
    return int(float(value))


def as_float(value):
    return float(value)


def q8(value):
    rounded = round(float(value) + 0.0, 8)
    if abs(rounded) < 1e-9:
        rounded = 0.0
    return rounded


def load_data():
    with open(DATA_DIR / "config.json", encoding="utf-8") as f:
        config = json.load(f)

    depots = {}
    for row in read_csv(DATA_DIR / "depots.csv"):
        depots[row["depot_id"]] = {
            "depot_id": row["depot_id"],
            "name": row["name"],
            "stage_capacity": as_int(row["stage_capacity"]),
            "stage_cost_per_bus": as_float(row["stage_cost_per_bus"]),
            "release_delay_min": as_float(row["release_delay_min"]),
            "x": as_float(row["x"]),
            "y": as_float(row["y"]),
        }

    buses = {}
    for row in read_csv(DATA_DIR / "buses.csv"):
        buses[row["bus_id"]] = {
            "bus_id": row["bus_id"],
            "home_depot": row["home_depot"],
            "capacity": as_int(row["capacity"]),
            "wheelchair_slots": as_int(row["wheelchair_slots"]),
            "shift_limit_min": as_float(row["shift_limit_min"]),
            "operating_cost_per_min": as_float(row["operating_cost_per_min"]),
            "high_clearance": as_int(row["high_clearance"]),
            "medical_attendant": as_int(row["medical_attendant"]),
        }

    shelters = {}
    for row in read_csv(DATA_DIR / "shelters.csv"):
        shelters[row["shelter_id"]] = {
            "shelter_id": row["shelter_id"],
            "name": row["name"],
            "capacity": as_int(row["capacity"]),
            "wheelchair_capacity": as_int(row["wheelchair_capacity"]),
            "pet_capacity": as_int(row["pet_capacity"]),
            "medical_beds": as_float(row["medical_beds"]),
            "backup_power": as_int(row["backup_power"]),
            "smoke_safe_rating": as_int(row["smoke_safe_rating"]),
            "open_cost": as_float(row["open_cost"]),
            "x": as_float(row["x"]),
            "y": as_float(row["y"]),
        }

    pickups = {}
    for row in read_csv(DATA_DIR / "pickup_sites.csv"):
        pickups[row["pickup_id"]] = {
            "pickup_id": row["pickup_id"],
            "name": row["name"],
            "priority_class": row["priority_class"],
            "corridor_id": row["corridor_id"],
            "service_floor": as_float(row["service_floor"]),
            "boarding_bays": as_int(row["boarding_bays"]),
            "medical_share": as_float(row["medical_share"]),
            "shelter_in_place_limit_fraction": as_float(row["shelter_in_place_limit_fraction"]),
            "x": as_float(row["x"]),
            "y": as_float(row["y"]),
        }

    corridors = {}
    for row in read_csv(DATA_DIR / "corridors.csv"):
        corridors[row["corridor_id"]] = {
            "corridor_id": row["corridor_id"],
            "name": row["name"],
            "activation_cost": as_float(row["activation_cost"]),
            "travel_time_factor_if_active": as_float(row["travel_time_factor_if_active"]),
            "closure_extension_min": as_float(row["closure_extension_min"]),
            "capacity_bonus_trips": as_int(row["capacity_bonus_trips"]),
        }

    scenarios = {}
    for row in read_csv(DATA_DIR / "scenarios.csv"):
        scenarios[row["scenario_id"]] = {
            "scenario_id": row["scenario_id"],
            "probability": as_float(row["probability"]),
            "description": row["description"],
            "smoke_severity": as_float(row["smoke_severity"]),
            "global_travel_multiplier": as_float(row["global_travel_multiplier"]),
            "warning_time_min": as_float(row["warning_time_min"]),
            "min_shelter_smoke_rating": as_int(row["min_shelter_smoke_rating"]),
        }

    demand = defaultdict(dict)
    for row in read_csv(DATA_DIR / "scenario_pickup_demand.csv"):
        demand[row["scenario_id"]][row["pickup_id"]] = {
            "evacuees": as_int(row["evacuees"]),
            "wheelchair": as_int(row["wheelchair"]),
            "pets": as_int(row["pets"]),
        }

    corridor_impacts = defaultdict(dict)
    for row in read_csv(DATA_DIR / "scenario_corridor_impacts.csv"):
        corridor_impacts[row["scenario_id"]][row["corridor_id"]] = {
            "travel_multiplier": as_float(row["travel_multiplier"]),
            "capacity_trips": as_int(row["capacity_trips"]),
            "smoke_risk_multiplier": as_float(row["smoke_risk_multiplier"]),
            "latest_safe_departure_min": as_float(row["latest_safe_departure_min"]),
        }

    return {
        "config": config,
        "depots": depots,
        "buses": buses,
        "shelters": shelters,
        "pickups": pickups,
        "corridors": corridors,
        "scenarios": scenarios,
        "demand": demand,
        "corridor_impacts": corridor_impacts,
    }


def dist(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def choose_first_stage(data):
    config = data["config"]
    shelters = data["shelters"]
    corridors = data["corridors"]
    depots = data["depots"]
    buses = data["buses"]
    pickups = data["pickups"]
    scenarios = data["scenarios"]
    demand = data["demand"]
    impacts = data["corridor_impacts"]

    open_shelters = sorted(
        shelter_id
        for shelter_id, shelter in shelters.items()
        if shelter["smoke_safe_rating"] >= 4
    )
    if len(open_shelters) != 4:
        raise RuntimeError("Expected exactly four smoke-safe shelters with rating >= 4.")

    active_corridors = sorted(corridors)
    if len(active_corridors) > config["max_active_corridors"]:
        raise RuntimeError("Instance-specific dominant contraflow choice exceeds active-corridor limit.")

    fixed_open_cost = sum(shelters[s]["open_cost"] for s in open_shelters)
    fixed_corridor_cost = sum(corridors[c]["activation_cost"] for c in active_corridors)

    model = Model("staging")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", MIP_GAP)

    z = {}
    for b in buses:
        for d in depots:
            z[b, d] = model.addVar(vtype="B", name=f"stage[{b},{d}]")

    proxy = {}
    for b, bus in buses.items():
        for d, depot in depots.items():
            weighted = 0.0
            for w, scenario in scenarios.items():
                total_dem = sum(demand[w][p]["evacuees"] for p in pickups)
                for p, pickup in pickups.items():
                    weight = scenario["probability"] * demand[w][p]["evacuees"] / total_dem
                    c = pickup["corridor_id"]
                    factor = (
                        scenario["global_travel_multiplier"]
                        * impacts[w][c]["travel_multiplier"]
                        * corridors[c]["travel_time_factor_if_active"]
                    )
                    base = (
                        config["base_intersection_delay_min"]
                        + config["minutes_per_distance_unit"] * dist(depot, pickup)
                    )
                    weighted += weight * factor * base
            proxy[b, d] = depots[d]["stage_cost_per_bus"] + bus["operating_cost_per_min"] * weighted

    for b in buses:
        model.addCons(quicksum(z[b, d] for d in depots) == 1, name=f"assign[{b}]")

    for d, depot in depots.items():
        model.addCons(
            quicksum(z[b, d] for b in buses) <= depot["stage_capacity"],
            name=f"depot_cap[{d}]",
        )

    model.addCons(
        quicksum(depots[d]["stage_cost_per_bus"] * z[b, d] for b in buses for d in depots)
        + fixed_open_cost
        + fixed_corridor_cost
        <= config["planning_budget"],
        name="planning_budget",
    )

    model.setObjective(quicksum(proxy[b, d] * z[b, d] for b in buses for d in depots), "minimize")
    model.optimize()

    if model.getNSols() == 0:
        raise RuntimeError("No feasible staging assignment found.")

    sol = model.getBestSol()
    staging = {}
    for b in buses:
        chosen = None
        for d in depots:
            if model.getSolVal(sol, z[b, d]) > 0.5:
                chosen = d
                break
        if chosen is None:
            raise RuntimeError(f"Staging missing for bus {b}.")
        staging[b] = chosen

    return {
        "open_shelters": open_shelters,
        "active_corridors": active_corridors,
        "staging": staging,
        "fixed_open_cost": fixed_open_cost,
        "fixed_corridor_cost": fixed_corridor_cost,
    }


def shelter_allowed_for_pickup(data, scenario_id, pickup_id, shelter_id, first_stage):
    shelters = data["shelters"]
    pickups = data["pickups"]
    scenarios = data["scenarios"]
    if shelter_id not in first_stage["open_shelters"]:
        return False
    if shelters[shelter_id]["smoke_safe_rating"] < scenarios[scenario_id]["min_shelter_smoke_rating"]:
        return False
    if pickups[pickup_id]["priority_class"] == "medical" and shelters[shelter_id]["backup_power"] == 0:
        return False
    return True


def leg_time(data, scenario_id, corridor_id, loc_a, loc_b):
    config = data["config"]
    scenarios = data["scenarios"]
    impacts = data["corridor_impacts"]
    corridors = data["corridors"]
    base = config["base_intersection_delay_min"] + config["minutes_per_distance_unit"] * dist(loc_a, loc_b)
    return (
        base
        * scenarios[scenario_id]["global_travel_multiplier"]
        * impacts[scenario_id][corridor_id]["travel_multiplier"]
        * corridors[corridor_id]["travel_time_factor_if_active"]
    )


def scenario_time_horizon(data, scenario_id):
    config = data["config"]
    scenarios = data["scenarios"]
    max_shift = max(bus["shift_limit_min"] for bus in data["buses"].values())
    max_latest = max(
        data["corridor_impacts"][scenario_id][corridor_id]["latest_safe_departure_min"]
        + data["corridors"][corridor_id]["closure_extension_min"]
        for corridor_id in data["corridors"]
    )
    return scenarios[scenario_id]["warning_time_min"] + max(max_shift + 200.0, max_latest + 200.0, 900.0)


def earliest_boarding_start(intervals, arrival, duration, bays):
    candidates = [arrival] + [end for _, end in intervals if end >= arrival - 1e-9]
    for t in sorted(set(round(candidate, 8) for candidate in candidates)):
        peak = 0
        probes = [t + 1e-7]
        if duration > 1e-7:
            probes.extend([t + duration / 2.0, t + duration - 1e-7])
        for probe in probes:
            concurrent = sum(1 for start, end in intervals if start < probe < end or abs(probe - start) < 1e-7)
            peak = max(peak, concurrent)
        if peak < bays:
            return t

    t = arrival
    for _ in range(200):
        overlap = [(start, end) for start, end in intervals if not (end <= t + 1e-9 or start >= t + duration - 1e-9)]
        if len(overlap) < bays:
            return t
        t = min(end for _, end in overlap)
    return None


def solve_scenario_greedy(data, scenario_id, first_stage):
    config = data["config"]
    buses = data["buses"]
    depots = data["depots"]
    pickups = data["pickups"]
    shelters = data["shelters"]
    scenarios = data["scenarios"]
    impacts = data["corridor_impacts"][scenario_id]
    scenario = scenarios[scenario_id]
    active_corridors = set(first_stage["active_corridors"])

    remaining = deepcopy(data["demand"][scenario_id])
    shelter_remaining = {
        s: {
            "capacity": shelters[s]["capacity"],
            "wheelchair_capacity": shelters[s]["wheelchair_capacity"],
            "pet_capacity": shelters[s]["pet_capacity"],
            "medical_beds": shelters[s]["medical_beds"],
        }
        for s in first_stage["open_shelters"]
    }
    corridor_remaining = {
        c: impacts[c]["capacity_trips"] + (data["corridors"][c]["capacity_bonus_trips"] if c in active_corridors else 0)
        for c in data["corridors"]
    }
    transported = {p: 0 for p in pickups}
    pickup_intervals = {p: [] for p in pickups}
    bus_state = {
        b: {
            "time": scenario["warning_time_min"] + depots[first_stage["staging"][b]]["release_delay_min"],
            "loc_type": "depot",
            "loc_id": first_stage["staging"][b],
            "sequence": 1,
        }
        for b in buses
    }
    min_transport = {
        p: max(
            0,
            math.ceil(
                (pickups[p]["service_floor"] - pickups[p]["shelter_in_place_limit_fraction"])
                * remaining[p]["evacuees"]
                - 1e-9
            ),
        )
        for p in pickups
    }
    priority_weight = {"medical": 5, "high": 4, "standard": 2, "remote": 1}
    trips = []

    while True:
        deficits = {p: max(0, min_transport[p] - transported[p]) for p in pickups}
        floor_mode = any(deficits[p] > 0 and remaining[p]["evacuees"] > 0 for p in pickups)
        best = None

        for b, state in bus_state.items():
            if state["sequence"] > KMAX:
                continue
            current = depots[state["loc_id"]] if state["loc_type"] == "depot" else shelters[state["loc_id"]]

            for p, pickup in pickups.items():
                rem_e = remaining[p]["evacuees"]
                rem_w = remaining[p]["wheelchair"]
                rem_r = remaining[p]["pets"]
                corridor_id = pickup["corridor_id"]
                if rem_e <= 0 or corridor_remaining[corridor_id] <= 0:
                    continue
                if floor_mode and deficits[p] <= 0:
                    continue

                active_factor = data["corridors"][corridor_id]["travel_time_factor_if_active"] if corridor_id in active_corridors else 1.0
                scenario_factor = (
                    scenario["global_travel_multiplier"]
                    * impacts[corridor_id]["travel_multiplier"]
                    * active_factor
                )
                inbound = (
                    config["base_intersection_delay_min"]
                    + config["minutes_per_distance_unit"] * dist(current, pickup)
                ) * scenario_factor
                latest = impacts[corridor_id]["latest_safe_departure_min"] + (
                    data["corridors"][corridor_id]["closure_extension_min"] if corridor_id in active_corridors else 0.0
                )

                for s in first_stage["open_shelters"]:
                    shelter = shelters[s]
                    if shelter["smoke_safe_rating"] < scenario["min_shelter_smoke_rating"]:
                        continue
                    if pickup["priority_class"] == "medical" and shelter["backup_power"] == 0:
                        continue

                    max_e = min(rem_e, buses[b]["capacity"], int(shelter_remaining[s]["capacity"]))
                    if pickup["medical_share"] > 0:
                        max_e = min(max_e, int(shelter_remaining[s]["medical_beds"] / pickup["medical_share"] + 1e-9))
                    if max_e <= 0:
                        continue

                    feasible = None
                    for load_e in range(max_e, 0, -1):
                        min_w = max(0, load_e - (rem_e - rem_w))
                        max_w = min(rem_w, buses[b]["wheelchair_slots"], int(shelter_remaining[s]["wheelchair_capacity"]), load_e)
                        if min_w > max_w:
                            continue
                        load_w = max_w

                        min_r = max(0, load_e - (rem_e - rem_r))
                        max_r = min(rem_r, int(shelter_remaining[s]["pet_capacity"]), load_e)
                        if min_r > max_r:
                            continue
                        load_r = max_r

                        board = (
                            config["load_time_per_person_min"] * load_e
                            + config["wheelchair_extra_board_min"] * load_w
                        )
                        start = earliest_boarding_start(
                            pickup_intervals[p],
                            state["time"] + inbound,
                            board,
                            pickup["boarding_bays"],
                        )
                        if start is None:
                            continue

                        depart = start + board
                        loaded = (
                            config["base_intersection_delay_min"]
                            + config["minutes_per_distance_unit"] * dist(pickup, shelter)
                        ) * scenario_factor
                        completion = depart + loaded + config["unload_time_per_person_min"] * load_e

                        if depart <= latest + 1e-9 and completion <= scenario["warning_time_min"] + buses[b]["shift_limit_min"] + 1e-9:
                            feasible = (load_e, load_w, load_r, start, depart, completion)
                            break

                    if feasible is None:
                        continue

                    load_e, load_w, load_r, start, depart, completion = feasible
                    score = (
                        1000000 * min(deficits[p], load_e)
                        + 10000 * priority_weight[pickup["priority_class"]] * load_e
                        + 2000 * load_w
                        + 200 * load_r
                        - 10 * depart
                        - (completion - state["time"])
                    )
                    candidate = (
                        score,
                        -depart,
                        load_e,
                        load_w,
                        load_r,
                        b,
                        p,
                        s,
                        start,
                        depart,
                        completion,
                    )
                    if best is None or candidate > best:
                        best = candidate

        if best is None:
            break

        _, _, load_e, load_w, load_r, b, p, s, start, depart, completion = best
        trips.append(
            {
                "bus_id": b,
                "sequence": bus_state[b]["sequence"],
                "pickup_id": p,
                "shelter_id": s,
                "depart_min": q8(depart),
                "evacuees": int(load_e),
                "wheelchair": int(load_w),
                "pets": int(load_r),
            }
        )
        pickup_intervals[p].append((start, depart))
        pickup_intervals[p].sort()

        bus_state[b]["time"] = completion
        bus_state[b]["loc_type"] = "shelter"
        bus_state[b]["loc_id"] = s
        bus_state[b]["sequence"] += 1

        remaining[p]["evacuees"] -= load_e
        remaining[p]["wheelchair"] -= load_w
        remaining[p]["pets"] -= load_r
        transported[p] += load_e
        shelter_remaining[s]["capacity"] -= load_e
        shelter_remaining[s]["wheelchair_capacity"] -= load_w
        shelter_remaining[s]["pet_capacity"] -= load_r
        shelter_remaining[s]["medical_beds"] -= pickups[p]["medical_share"] * load_e
        corridor_remaining[pickups[p]["corridor_id"]] -= 1

    high_med = [p for p in pickups if pickups[p]["priority_class"] in {"high", "medical"}]
    standard_remote = [p for p in pickups if pickups[p]["priority_class"] in {"standard", "remote"}]
    total_demand = {p: transported[p] + remaining[p]["evacuees"] for p in pickups}
    sip_e = {p: 0 for p in pickups}

    for p in high_med:
        limit = int(math.floor(pickups[p]["shelter_in_place_limit_fraction"] * total_demand[p] + 1e-9))
        sip_e[p] = min(remaining[p]["evacuees"], limit)

    fairness_cap = min((transported[p] + sip_e[p]) / total_demand[p] + config["fairness_gap"] for p in high_med)
    for p in standard_remote:
        limit = int(math.floor(pickups[p]["shelter_in_place_limit_fraction"] * total_demand[p] + 1e-9))
        allowed = max(0, int(math.floor(fairness_cap * total_demand[p] + 1e-9)) - transported[p])
        sip_e[p] = min(remaining[p]["evacuees"], limit, allowed)

    for p in pickups:
        limit = int(math.floor(pickups[p]["shelter_in_place_limit_fraction"] * total_demand[p] + 1e-9))
        need = max(0, math.ceil(pickups[p]["service_floor"] * total_demand[p] - transported[p] - sip_e[p] - 1e-9))
        sip_e[p] += min(need, remaining[p]["evacuees"] - sip_e[p], limit - sip_e[p])

    scenario_plan = {
        "scenario_id": scenario_id,
        "bus_trips": sorted(trips, key=lambda row: (row["bus_id"], row["sequence"])),
        "unserved": [],
        "shelter_in_place": [],
    }
    for p in sorted(pickups):
        this_sip_e = sip_e[p]
        this_sip_w = min(remaining[p]["wheelchair"], this_sip_e)
        this_sip_r = min(remaining[p]["pets"], this_sip_e)
        scenario_plan["unserved"].append(
            {
                "pickup_id": p,
                "evacuees": int(remaining[p]["evacuees"] - this_sip_e),
                "wheelchair": int(remaining[p]["wheelchair"] - this_sip_w),
                "pets": int(remaining[p]["pets"] - this_sip_r),
            }
        )
        scenario_plan["shelter_in_place"].append(
            {
                "pickup_id": p,
                "evacuees": int(this_sip_e),
                "wheelchair": int(this_sip_w),
                "pets": int(this_sip_r),
            }
        )

    return {
        "plan": scenario_plan,
        "status": "heuristic_constructive",
        "objective": float("nan"),
        "best_bound": float("nan"),
        "gap": float("nan"),
        "nsols": 1,
        "solve_time": 0.0,
    }


def solve_scenario(data, scenario_id, first_stage, time_limit):
    config = data["config"]
    buses = data["buses"]
    depots = data["depots"]
    pickups = data["pickups"]
    shelters = data["shelters"]
    scenarios = data["scenarios"]
    demand = data["demand"][scenario_id]
    impacts = data["corridor_impacts"][scenario_id]

    scenario = scenarios[scenario_id]
    horizon = scenario_time_horizon(data, scenario_id)

    allowed_pairs = [
        (p, s)
        for p in pickups
        for s in first_stage["open_shelters"]
        if shelter_allowed_for_pickup(data, scenario_id, p, s, first_stage)
    ]

    stage_depot = first_stage["staging"]
    release_time = {b: depots[stage_depot[b]]["release_delay_min"] for b in buses}

    deadhead_from_depot = {}
    deadhead_from_shelter = {}
    loaded_time = {}
    latest_safe = {}
    smoke_coeff = {}
    corridor_of = {p: pickups[p]["corridor_id"] for p in pickups}

    for p in pickups:
        c = corridor_of[p]
        latest_safe[p] = impacts[c]["latest_safe_departure_min"] + data["corridors"][c]["closure_extension_min"]
        smoke_coeff[p] = (
            config["smoke_exposure_cost_per_person"]
            * scenario["smoke_severity"]
            * impacts[c]["smoke_risk_multiplier"]
        )
        for d in depots:
            deadhead_from_depot[d, p] = leg_time(data, scenario_id, c, depots[d], pickups[p])
        for s in first_stage["open_shelters"]:
            deadhead_from_shelter[s, p] = leg_time(data, scenario_id, c, shelters[s], pickups[p])
            loaded_time[p, s] = leg_time(data, scenario_id, c, pickups[p], shelters[s])

    model = Model(f"scenario_{scenario_id}")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", MIP_GAP)
    model.setParam("limits/time", max(1.0, time_limit))

    trip_slots = [(b, k) for b in buses for k in range(1, KMAX + 1)]

    u = {}
    start = {}
    depart = {}
    x = {}
    y_pickup = {}
    y_shelter = {}
    evac = {}
    wheel = {}
    pets = {}
    late_trip = {}
    trans = {}

    for b, k in trip_slots:
        u[b, k] = model.addVar(vtype="B", name=f"use[{b},{k}]")
        start[b, k] = model.addVar(lb=0.0, ub=horizon, name=f"start[{b},{k}]")
        depart[b, k] = model.addVar(lb=0.0, ub=horizon, name=f"depart[{b},{k}]")
        for p in pickups:
            y_pickup[b, k, p] = model.addVar(vtype="B", name=f"pickup[{b},{k},{p}]")
            late_trip[b, k, p] = model.addVar(lb=0.0, name=f"late_trip[{b},{k},{p}]")
        for s in first_stage["open_shelters"]:
            y_shelter[b, k, s] = model.addVar(vtype="B", name=f"shelter[{b},{k},{s}]")
        for p, s in allowed_pairs:
            x[b, k, p, s] = model.addVar(vtype="B", name=f"x[{b},{k},{p},{s}]")
            evac[b, k, p, s] = model.addVar(vtype="I", lb=0.0, name=f"e[{b},{k},{p},{s}]")
            wheel[b, k, p, s] = model.addVar(vtype="I", lb=0.0, name=f"w[{b},{k},{p},{s}]")
            pets[b, k, p, s] = model.addVar(vtype="I", lb=0.0, name=f"r[{b},{k},{p},{s}]")

    for b in buses:
        for k in range(1, KMAX):
            for s in first_stage["open_shelters"]:
                for p in pickups:
                    trans[b, k, s, p] = model.addVar(vtype="B", name=f"g[{b},{k},{s},{p}]")

    unserved_e = {}
    unserved_w = {}
    unserved_r = {}
    sip_e = {}
    sip_w = {}
    sip_r = {}
    served_e = {}

    for p in pickups:
        unserved_e[p] = model.addVar(vtype="I", lb=0.0, name=f"unserved_e[{p}]")
        unserved_w[p] = model.addVar(vtype="I", lb=0.0, name=f"unserved_w[{p}]")
        unserved_r[p] = model.addVar(vtype="I", lb=0.0, name=f"unserved_r[{p}]")
        sip_e[p] = model.addVar(vtype="I", lb=0.0, name=f"sip_e[{p}]")
        sip_w[p] = model.addVar(vtype="I", lb=0.0, name=f"sip_w[{p}]")
        sip_r[p] = model.addVar(vtype="I", lb=0.0, name=f"sip_r[{p}]")
        served_e[p] = model.addVar(lb=0.0, name=f"served[{p}]")

    order = {}
    ordered_slots = list(trip_slots)
    for p in pickups:
        for i in range(len(ordered_slots)):
            b1, k1 = ordered_slots[i]
            for j in range(i + 1, len(ordered_slots)):
                b2, k2 = ordered_slots[j]
                if b1 == b2:
                    continue
                order[p, b1, k1, b2, k2] = model.addVar(vtype="B", name=f"ord[{p},{b1},{k1},{b2},{k2}]")

    for b, k in trip_slots:
        model.addCons(quicksum(y_pickup[b, k, p] for p in pickups) == u[b, k], name=f"one_pickup[{b},{k}]")
        model.addCons(
            quicksum(y_shelter[b, k, s] for s in first_stage["open_shelters"]) == u[b, k],
            name=f"one_shelter[{b},{k}]",
        )
        for p in pickups:
            valid_shelters = [s for s in first_stage["open_shelters"] if (p, s) in allowed_pairs]
            model.addCons(
                quicksum(x[b, k, p, s] for s in valid_shelters) == y_pickup[b, k, p],
                name=f"pickup_link[{b},{k},{p}]",
            )
            model.addCons(late_trip[b, k, p] <= horizon * y_pickup[b, k, p], name=f"late_trip1[{b},{k},{p}]")
            model.addCons(late_trip[b, k, p] <= depart[b, k], name=f"late_trip2[{b},{k},{p}]")
            model.addCons(
                late_trip[b, k, p] >= depart[b, k] - horizon * (1 - y_pickup[b, k, p]),
                name=f"late_trip3[{b},{k},{p}]",
            )
        for s in first_stage["open_shelters"]:
            valid_pickups = [p for p in pickups if (p, s) in allowed_pairs]
            model.addCons(
                quicksum(x[b, k, p, s] for p in valid_pickups) == y_shelter[b, k, s],
                name=f"shelter_link[{b},{k},{s}]",
            )

        total_e = quicksum(evac[b, k, p, s] for p, s in allowed_pairs)
        total_w = quicksum(wheel[b, k, p, s] for p, s in allowed_pairs)
        total_r = quicksum(pets[b, k, p, s] for p, s in allowed_pairs)
        bus = buses[b]

        model.addCons(total_e <= bus["capacity"], name=f"bus_cap[{b},{k}]")
        model.addCons(total_w <= bus["wheelchair_slots"], name=f"bus_wc[{b},{k}]")
        model.addCons(total_w <= total_e, name=f"wheel_subset[{b},{k}]")
        model.addCons(total_r <= total_e, name=f"pet_subset[{b},{k}]")
        model.addCons(start[b, k] <= horizon * u[b, k], name=f"start_use[{b},{k}]")
        model.addCons(depart[b, k] <= horizon * u[b, k], name=f"depart_use[{b},{k}]")
        model.addCons(
            depart[b, k]
            == start[b, k]
            + config["load_time_per_person_min"] * total_e
            + config["wheelchair_extra_board_min"] * total_w,
            name=f"board_time[{b},{k}]",
        )

        for p, s in allowed_pairs:
            model.addCons(evac[b, k, p, s] <= bus["capacity"] * x[b, k, p, s], name=f"e_link[{b},{k},{p},{s}]")
            model.addCons(
                wheel[b, k, p, s] <= bus["wheelchair_slots"] * x[b, k, p, s],
                name=f"w_link[{b},{k},{p},{s}]",
            )
            model.addCons(pets[b, k, p, s] <= bus["capacity"] * x[b, k, p, s], name=f"r_link[{b},{k},{p},{s}]")

        if k < KMAX:
            model.addCons(u[b, k + 1] <= u[b, k], name=f"no_gaps[{b},{k}]")

    for p, pickup in pickups.items():
        carried_e = quicksum(evac[b, k, p, s] for b, k in trip_slots for s in first_stage["open_shelters"] if (p, s) in allowed_pairs)
        carried_w = quicksum(wheel[b, k, p, s] for b, k in trip_slots for s in first_stage["open_shelters"] if (p, s) in allowed_pairs)
        carried_r = quicksum(pets[b, k, p, s] for b, k in trip_slots for s in first_stage["open_shelters"] if (p, s) in allowed_pairs)

        model.addCons(carried_e + unserved_e[p] + sip_e[p] == demand[p]["evacuees"], name=f"dem_e[{p}]")
        model.addCons(carried_w + unserved_w[p] + sip_w[p] == demand[p]["wheelchair"], name=f"dem_w[{p}]")
        model.addCons(carried_r + unserved_r[p] + sip_r[p] == demand[p]["pets"], name=f"dem_r[{p}]")
        model.addCons(unserved_w[p] <= unserved_e[p], name=f"uw_subset[{p}]")
        model.addCons(unserved_r[p] <= unserved_e[p], name=f"ur_subset[{p}]")
        model.addCons(sip_w[p] <= sip_e[p], name=f"sw_subset[{p}]")
        model.addCons(sip_r[p] <= sip_e[p], name=f"sr_subset[{p}]")
        model.addCons(served_e[p] == carried_e + sip_e[p], name=f"served_def[{p}]")
        model.addCons(served_e[p] >= pickup["service_floor"] * demand[p]["evacuees"], name=f"floor[{p}]")
        model.addCons(
            sip_e[p] <= pickup["shelter_in_place_limit_fraction"] * demand[p]["evacuees"],
            name=f"sip_limit[{p}]",
        )

    high_or_med = [p for p, pickup in pickups.items() if pickup["priority_class"] in {"high", "medical"}]
    standard_or_remote = [p for p, pickup in pickups.items() if pickup["priority_class"] in {"standard", "remote"}]
    gap = config["fairness_gap"]
    for p in high_or_med:
        dp = demand[p]["evacuees"]
        for q in standard_or_remote:
            dq = demand[q]["evacuees"]
            model.addCons(
                dq * served_e[p] + gap * dp * dq >= dp * served_e[q],
                name=f"fair[{p},{q}]",
            )

    for s in first_stage["open_shelters"]:
        usable = 1 if shelters[s]["smoke_safe_rating"] >= scenario["min_shelter_smoke_rating"] else 0
        total_e = quicksum(evac[b, k, p, s] for b, k in trip_slots for p in pickups if (p, s) in allowed_pairs)
        total_w = quicksum(wheel[b, k, p, s] for b, k in trip_slots for p in pickups if (p, s) in allowed_pairs)
        total_r = quicksum(pets[b, k, p, s] for b, k in trip_slots for p in pickups if (p, s) in allowed_pairs)
        total_med = quicksum(
            pickups[p]["medical_share"] * evac[b, k, p, s]
            for b, k in trip_slots
            for p in pickups
            if (p, s) in allowed_pairs
        )
        model.addCons(total_e <= usable * shelters[s]["capacity"], name=f"shel_cap[{s}]")
        model.addCons(total_w <= usable * shelters[s]["wheelchair_capacity"], name=f"shel_wc[{s}]")
        model.addCons(total_r <= usable * shelters[s]["pet_capacity"], name=f"shel_pet[{s}]")
        model.addCons(total_med <= usable * shelters[s]["medical_beds"], name=f"shel_med[{s}]")

    for c in data["corridors"]:
        trips_on_corridor = quicksum(
            y_pickup[b, k, p]
            for b, k in trip_slots
            for p in pickups
            if corridor_of[p] == c
        )
        model.addCons(
            trips_on_corridor <= impacts[c]["capacity_trips"] + data["corridors"][c]["capacity_bonus_trips"],
            name=f"corr_cap[{c}]",
        )

    for b in buses:
        first_depot = stage_depot[b]
        for p in pickups:
            model.addCons(
                start[b, 1] >= scenario["warning_time_min"] + release_time[b] + deadhead_from_depot[first_depot, p] - horizon * (1 - y_pickup[b, 1, p]),
                name=f"first_start[{b},{p}]",
            )
        for k in range(1, KMAX):
            prev_e = quicksum(evac[b, k, p, s] for p, s in allowed_pairs)
            prev_loaded = quicksum(loaded_time[p, s] * x[b, k, p, s] for p, s in allowed_pairs)
            for s in first_stage["open_shelters"]:
                for p in pickups:
                    model.addCons(trans[b, k, s, p] <= y_shelter[b, k, s], name=f"trans1[{b},{k},{s},{p}]")
                    model.addCons(trans[b, k, s, p] <= y_pickup[b, k + 1, p], name=f"trans2[{b},{k},{s},{p}]")
                    model.addCons(
                        trans[b, k, s, p] >= y_shelter[b, k, s] + y_pickup[b, k + 1, p] - 1,
                        name=f"trans3[{b},{k},{s},{p}]",
                    )
            model.addCons(
                start[b, k + 1]
                >= depart[b, k]
                + prev_loaded
                + config["unload_time_per_person_min"] * prev_e
                + quicksum(deadhead_from_shelter[s, p] * trans[b, k, s, p] for s in first_stage["open_shelters"] for p in pickups)
                - horizon * (2 - u[b, k] - u[b, k + 1]),
                name=f"sequence[{b},{k}]",
            )

        for k in range(1, KMAX + 1):
            trip_e = quicksum(evac[b, k, p, s] for p, s in allowed_pairs)
            trip_loaded = quicksum(loaded_time[p, s] * x[b, k, p, s] for p, s in allowed_pairs)
            model.addCons(
                depart[b, k] + trip_loaded + config["unload_time_per_person_min"] * trip_e
                <= scenario["warning_time_min"] + buses[b]["shift_limit_min"] + horizon * (1 - u[b, k]),
                name=f"shift[{b},{k}]",
            )
            for p in pickups:
                model.addCons(
                    depart[b, k] <= latest_safe[p] + horizon * (1 - y_pickup[b, k, p]),
                    name=f"latest[{b},{k},{p}]",
                )

    for p in pickups:
        for i in range(len(ordered_slots)):
            b1, k1 = ordered_slots[i]
            for j in range(i + 1, len(ordered_slots)):
                b2, k2 = ordered_slots[j]
                if b1 == b2:
                    continue
                ord_var = order[p, b1, k1, b2, k2]
                model.addCons(
                    depart[b1, k1]
                    <= start[b2, k2] + horizon * (1 - ord_var + 2 - y_pickup[b1, k1, p] - y_pickup[b2, k2, p]),
                    name=f"pickup_ord1[{p},{b1},{k1},{b2},{k2}]",
                )
                model.addCons(
                    depart[b2, k2]
                    <= start[b1, k1] + horizon * (ord_var + 2 - y_pickup[b1, k1, p] - y_pickup[b2, k2, p]),
                    name=f"pickup_ord2[{p},{b1},{k1},{b2},{k2}]",
                )

    objective = 0
    for b in buses:
        first_depot = stage_depot[b]
        op_cost = buses[b]["operating_cost_per_min"]
        for p, s in allowed_pairs:
            objective += op_cost * deadhead_from_depot[first_depot, p] * x[b, 1, p, s]
        for k in range(1, KMAX + 1):
            total_e = quicksum(evac[b, k, p, s] for p, s in allowed_pairs)
            total_w = quicksum(wheel[b, k, p, s] for p, s in allowed_pairs)
            objective += op_cost * config["load_time_per_person_min"] * total_e
            objective += op_cost * config["wheelchair_extra_board_min"] * total_w
            objective += op_cost * config["unload_time_per_person_min"] * total_e
            objective += op_cost * quicksum(loaded_time[p, s] * x[b, k, p, s] for p, s in allowed_pairs)
            for p in pickups:
                late_weight = min(demand[p]["evacuees"], buses[b]["capacity"])
                objective += config["late_pickup_penalty_per_person_min"] * late_weight * late_trip[b, k, p]
            for p, s in allowed_pairs:
                objective += smoke_coeff[p] * evac[b, k, p, s]
        for k in range(1, KMAX):
            objective += op_cost * quicksum(
                deadhead_from_shelter[s, p] * trans[b, k, s, p]
                for s in first_stage["open_shelters"]
                for p in pickups
            )

    for p in pickups:
        objective += config["unserved_penalty_per_person"] * unserved_e[p]
        objective += config["unserved_penalty_per_person"] * (config["wheelchair_unserved_multiplier"] - 1.0) * unserved_w[p]
        objective += config["unserved_penalty_per_person"] * config["pet_unserved_multiplier"] * unserved_r[p]
        objective += config["shelter_in_place_penalty_per_person"] * sip_e[p]
        objective += config["shelter_in_place_penalty_per_person"] * (config["wheelchair_unserved_multiplier"] - 1.0) * sip_w[p]
        objective += config["shelter_in_place_penalty_per_person"] * config["pet_unserved_multiplier"] * sip_r[p]

    model.setObjective(objective, "minimize")
    model.optimize()

    status = str(model.getStatus())
    nsols = model.getNSols()

    if nsols == 0:
        raise RuntimeError(f"Scenario {scenario_id} produced no incumbent. Status: {status}")

    sol = model.getBestSol()
    scenario_plan = {
        "scenario_id": scenario_id,
        "bus_trips": [],
        "unserved": [],
        "shelter_in_place": [],
    }

    for p in pickups:
        scenario_plan["unserved"].append(
            {
                "pickup_id": p,
                "evacuees": int(round(model.getSolVal(sol, unserved_e[p]))),
                "wheelchair": int(round(model.getSolVal(sol, unserved_w[p]))),
                "pets": int(round(model.getSolVal(sol, unserved_r[p]))),
            }
        )
        scenario_plan["shelter_in_place"].append(
            {
                "pickup_id": p,
                "evacuees": int(round(model.getSolVal(sol, sip_e[p]))),
                "wheelchair": int(round(model.getSolVal(sol, sip_w[p]))),
                "pets": int(round(model.getSolVal(sol, sip_r[p]))),
            }
        )

    for b in sorted(buses):
        for k in range(1, KMAX + 1):
            if model.getSolVal(sol, u[b, k]) <= 0.5:
                continue
            chosen = None
            for p, s in allowed_pairs:
                if model.getSolVal(sol, x[b, k, p, s]) > 0.5:
                    chosen = (p, s)
                    break
            if chosen is None:
                raise RuntimeError(f"Scenario {scenario_id} has used slot without assignment for {b},{k}.")
            p, s = chosen
            scenario_plan["bus_trips"].append(
                {
                    "bus_id": b,
                    "sequence": k,
                    "pickup_id": p,
                    "shelter_id": s,
                    "depart_min": q8(model.getSolVal(sol, depart[b, k])),
                    "evacuees": int(round(model.getSolVal(sol, evac[b, k, p, s]))),
                    "wheelchair": int(round(model.getSolVal(sol, wheel[b, k, p, s]))),
                    "pets": int(round(model.getSolVal(sol, pets[b, k, p, s]))),
                }
            )

    scenario_plan["bus_trips"].sort(key=lambda row: (row["bus_id"], row["sequence"]))
    scenario_plan["unserved"].sort(key=lambda row: row["pickup_id"])
    scenario_plan["shelter_in_place"].sort(key=lambda row: row["pickup_id"])

    objective_value = model.getObjVal()
    best_bound = model.getDualbound()
    gap = model.getGap()

    return {
        "plan": scenario_plan,
        "status": status,
        "objective": objective_value,
        "best_bound": best_bound,
        "gap": gap,
        "nsols": nsols,
        "solve_time": model.getSolvingTime(),
    }


def compute_cvar(prob_costs, alpha):
    ordered = sorted(prob_costs, key=lambda item: item[1])
    tail_mass = 1.0 - alpha
    if tail_mass <= 0:
        return max(cost for _, cost in ordered)
    remaining = tail_mass
    cvar = 0.0
    consumed = 0.0
    for prob, cost in reversed(ordered):
        take = min(prob, remaining)
        if take > 0:
            cvar += take * cost
            consumed += take
            remaining -= take
        if remaining <= 1e-12:
            break
    if consumed <= 1e-12:
        return max(cost for _, cost in ordered)
    return cvar / consumed


def build_solution(data, first_stage, scenario_results):
    solution = {
        "shelters": [{"shelter_id": s, "open": s in first_stage["open_shelters"]} for s in sorted(data["shelters"])],
        "staging": [{"bus_id": b, "depot_id": first_stage["staging"][b]} for b in sorted(data["buses"])],
        "contraflow": [{"corridor_id": c, "active": c in first_stage["active_corridors"]} for c in sorted(data["corridors"])],
        "scenario_plans": [scenario_results[s]["plan"] for s in sorted(data["scenarios"])],
    }
    return solution


def extract_first_stage_from_solution(data, solution):
    shelter_open = {row["shelter_id"]: bool(row["open"]) for row in solution["shelters"]}
    staging = {row["bus_id"]: row["depot_id"] for row in solution["staging"]}
    contraflow = {row["corridor_id"]: bool(row["active"]) for row in solution["contraflow"]}
    if set(shelter_open) != set(data["shelters"]):
        return None
    if set(staging) != set(data["buses"]):
        return None
    if set(contraflow) != set(data["corridors"]):
        return None
    return {
        "open_shelters": sorted(key for key, value in shelter_open.items() if value),
        "active_corridors": sorted(key for key, value in contraflow.items() if value),
        "staging": staging,
    }


def evaluate_first_stage(data, first_stage):
    scenario_results = {
        scenario_id: solve_scenario_greedy(data, scenario_id, first_stage)
        for scenario_id in sorted(data["scenarios"])
    }
    solution = build_solution(data, first_stage, scenario_results)
    audit = audit_solution(data, solution)
    return {
        "first_stage": first_stage,
        "scenario_results": scenario_results,
        "solution": solution,
        "audit": audit,
    }


def audit_solution(data, solution):
    config = data["config"]
    depots = data["depots"]
    buses = data["buses"]
    shelters = data["shelters"]
    pickups = data["pickups"]
    corridors = data["corridors"]
    scenarios = data["scenarios"]
    demand = data["demand"]
    impacts = data["corridor_impacts"]
    tol = config["quantity_tolerance"]

    errors = []

    shelter_open = {row["shelter_id"]: bool(row["open"]) for row in solution["shelters"]}
    staging = {row["bus_id"]: row["depot_id"] for row in solution["staging"]}
    contraflow = {row["corridor_id"]: bool(row["active"]) for row in solution["contraflow"]}
    scenario_map = {row["scenario_id"]: row for row in solution["scenario_plans"]}

    if set(shelter_open) != set(shelters):
        errors.append("Shelter coverage mismatch in solution.")
    if set(staging) != set(buses):
        errors.append("Staging coverage mismatch in solution.")
    if set(contraflow) != set(corridors):
        errors.append("Contraflow coverage mismatch in solution.")
    if set(scenario_map) != set(scenarios):
        errors.append("Scenario coverage mismatch in solution.")

    backup_open = sum(shelter_open[s] and shelters[s]["backup_power"] for s in shelters)
    if backup_open < config["min_backup_power_shelters"]:
        errors.append("Minimum backup-power shelter count violated.")

    open_cost = sum(shelters[s]["open_cost"] for s in shelters if shelter_open[s])
    stage_cost = sum(depots[staging[b]]["stage_cost_per_bus"] for b in buses)
    contraflow_cost = sum(corridors[c]["activation_cost"] for c in corridors if contraflow[c])
    if open_cost + stage_cost + contraflow_cost > config["planning_budget"] + 1e-6:
        errors.append("Planning budget violated.")

    if sum(shelter_open.values()) > config["max_open_shelters"]:
        errors.append("Maximum open shelters violated.")
    if sum(contraflow.values()) > config["max_active_corridors"]:
        errors.append("Maximum active corridors violated.")

    for d in depots:
        used = sum(1 for b in buses if staging[b] == d)
        if used > depots[d]["stage_capacity"]:
            errors.append(f"Depot capacity violated at {d}.")

    scenario_costs = {}
    scenario_checks = {}

    for scenario_id, scenario in scenarios.items():
        plan = scenario_map[scenario_id]
        if len(plan["unserved"]) != len(pickups):
            errors.append(f"Scenario {scenario_id} missing unserved rows.")
        if len(plan["shelter_in_place"]) != len(pickups):
            errors.append(f"Scenario {scenario_id} missing shelter-in-place rows.")

        unserved_map = {row["pickup_id"]: row for row in plan["unserved"]}
        sip_map = {row["pickup_id"]: row for row in plan["shelter_in_place"]}
        if set(unserved_map) != set(pickups):
            errors.append(f"Scenario {scenario_id} unserved pickup coverage mismatch.")
        if set(sip_map) != set(pickups):
            errors.append(f"Scenario {scenario_id} shelter-in-place pickup coverage mismatch.")

        shelter_load_e = defaultdict(float)
        shelter_load_w = defaultdict(float)
        shelter_load_r = defaultdict(float)
        shelter_load_med = defaultdict(float)
        corridor_trips = defaultdict(int)
        pickup_intervals = defaultdict(list)
        served_trip_e = defaultdict(float)
        served_trip_w = defaultdict(float)
        served_trip_r = defaultdict(float)
        bus_trip_count = defaultdict(int)
        bus_state = {}
        op_cost = 0.0
        late_cost = 0.0
        smoke_cost = 0.0
        min_departure_slack = math.inf
        min_shift_slack = math.inf
        min_arrival_slack = math.inf
        min_fairness_slack = math.inf
        max_boarding_concurrency_ratio = 0.0
        max_corridor_trip_utilization = 0.0
        max_shelter_capacity_utilization = 0.0
        max_shelter_wheelchair_utilization = 0.0
        max_shelter_pet_utilization = 0.0
        max_shelter_medical_utilization = 0.0

        for bus_id, depot_id in staging.items():
            bus_state[bus_id] = {
                "time": scenario["warning_time_min"] + depots[depot_id]["release_delay_min"],
                "loc_type": "depot",
                "loc_id": depot_id,
            }

        trips_by_bus = defaultdict(list)
        for row in plan["bus_trips"]:
            trips_by_bus[row["bus_id"]].append(row)

        for b in trips_by_bus:
            trips_by_bus[b].sort(key=lambda row: row["sequence"])
            expected_seq = 1
            for row in trips_by_bus[b]:
                if row["sequence"] != expected_seq:
                    errors.append(f"Scenario {scenario_id} bus {b} has non-contiguous sequence numbers.")
                    break
                expected_seq += 1

        for b in sorted(buses):
            state = bus_state[b]
            for row in trips_by_bus.get(b, []):
                p = row["pickup_id"]
                s = row["shelter_id"]
                if p not in pickups or s not in shelters:
                    errors.append(f"Scenario {scenario_id} references unknown pickup or shelter.")
                    continue
                if not shelter_open.get(s, False):
                    errors.append(f"Scenario {scenario_id} uses closed shelter {s}.")
                if shelters[s]["smoke_safe_rating"] < scenario["min_shelter_smoke_rating"]:
                    errors.append(f"Scenario {scenario_id} uses smoke-ineligible shelter {s}.")
                if pickups[p]["priority_class"] == "medical" and shelters[s]["backup_power"] == 0:
                    errors.append(f"Scenario {scenario_id} sends medical pickup {p} to shelter without backup power.")

                e = float(row["evacuees"])
                w = float(row["wheelchair"])
                r = float(row["pets"])
                if e < -tol or w < -tol or r < -tol:
                    errors.append(f"Scenario {scenario_id} has negative trip load.")
                if w - e > tol or r - e > tol:
                    errors.append(f"Scenario {scenario_id} has subset load violation on bus {b}.")
                if e - buses[b]["capacity"] > tol:
                    errors.append(f"Scenario {scenario_id} exceeds bus capacity on {b}.")
                if w - buses[b]["wheelchair_slots"] > tol:
                    errors.append(f"Scenario {scenario_id} exceeds wheelchair slots on {b}.")

                c = pickups[p]["corridor_id"]
                active_factor = corridors[c]["travel_time_factor_if_active"] if contraflow[c] else 1.0
                scenario_factor = (
                    scenario["global_travel_multiplier"]
                    * impacts[scenario_id][c]["travel_multiplier"]
                    * active_factor
                )
                current_loc = depots[state["loc_id"]] if state["loc_type"] == "depot" else shelters[state["loc_id"]]
                inbound = (
                    config["base_intersection_delay_min"]
                    + config["minutes_per_distance_unit"] * dist(current_loc, pickups[p])
                ) * scenario_factor
                loaded = (
                    config["base_intersection_delay_min"]
                    + config["minutes_per_distance_unit"] * dist(pickups[p], shelters[s])
                ) * scenario_factor
                board = config["load_time_per_person_min"] * e + config["wheelchair_extra_board_min"] * w
                depart = float(row["depart_min"])
                board_start = depart - board

                min_arrival_slack = min(min_arrival_slack, board_start - (state["time"] + inbound))
                if board_start + tol < state["time"] + inbound:
                    errors.append(f"Scenario {scenario_id} bus {b} departs before it can reach and board at {p}.")
                latest = impacts[scenario_id][c]["latest_safe_departure_min"] + corridors[c]["closure_extension_min"]
                min_departure_slack = min(min_departure_slack, latest - depart)
                if depart - latest > tol:
                    errors.append(f"Scenario {scenario_id} violates latest safe departure at {p}.")

                completion = depart + loaded + config["unload_time_per_person_min"] * e
                min_shift_slack = min(min_shift_slack, scenario["warning_time_min"] + buses[b]["shift_limit_min"] - completion)
                if completion - (scenario["warning_time_min"] + buses[b]["shift_limit_min"]) > tol:
                    errors.append(f"Scenario {scenario_id} bus {b} violates shift limit.")

                pickup_intervals[p].append((board_start, depart))
                corridor_trips[c] += 1
                shelter_load_e[s] += e
                shelter_load_w[s] += w
                shelter_load_r[s] += r
                shelter_load_med[s] += pickups[p]["medical_share"] * e
                served_trip_e[p] += e
                served_trip_w[p] += w
                served_trip_r[p] += r
                bus_trip_count[b] += 1

                op_cost += buses[b]["operating_cost_per_min"] * (inbound + board + loaded + config["unload_time_per_person_min"] * e)
                late_cost += config["late_pickup_penalty_per_person_min"] * depart * e
                smoke_cost += (
                    config["smoke_exposure_cost_per_person"]
                    * scenario["smoke_severity"]
                    * impacts[scenario_id][c]["smoke_risk_multiplier"]
                    * e
                )

                state["time"] = completion
                state["loc_type"] = "shelter"
                state["loc_id"] = s

        unserved_cost = 0.0
        sip_cost = 0.0
        served_total = {}
        for p in pickups:
            urow = unserved_map[p]
            srow = sip_map[p]
            ue = float(urow["evacuees"])
            uw = float(urow["wheelchair"])
            ur = float(urow["pets"])
            se = float(srow["evacuees"])
            sw = float(srow["wheelchair"])
            sr = float(srow["pets"])

            if min(ue, uw, ur, se, sw, sr) < -tol:
                errors.append(f"Scenario {scenario_id} has negative unserved or shelter-in-place quantity at {p}.")
            if uw - ue > tol or ur - ue > tol or sw - se > tol or sr - se > tol:
                errors.append(f"Scenario {scenario_id} has subset accounting violation at {p}.")

            if abs(served_trip_e[p] + ue + se - demand[scenario_id][p]["evacuees"]) > tol:
                errors.append(f"Scenario {scenario_id} evacuee balance violated at {p}.")
            if abs(served_trip_w[p] + uw + sw - demand[scenario_id][p]["wheelchair"]) > tol:
                errors.append(f"Scenario {scenario_id} wheelchair balance violated at {p}.")
            if abs(served_trip_r[p] + ur + sr - demand[scenario_id][p]["pets"]) > tol:
                errors.append(f"Scenario {scenario_id} pet balance violated at {p}.")

            served_total[p] = served_trip_e[p] + se
            if served_total[p] + tol < pickups[p]["service_floor"] * demand[scenario_id][p]["evacuees"]:
                errors.append(f"Scenario {scenario_id} service floor violated at {p}.")
            if se - pickups[p]["shelter_in_place_limit_fraction"] * demand[scenario_id][p]["evacuees"] > tol:
                errors.append(f"Scenario {scenario_id} shelter-in-place limit violated at {p}.")

            unserved_cost += config["unserved_penalty_per_person"] * (
                ue + (config["wheelchair_unserved_multiplier"] - 1.0) * uw + config["pet_unserved_multiplier"] * ur
            )
            sip_cost += config["shelter_in_place_penalty_per_person"] * (
                se + (config["wheelchair_unserved_multiplier"] - 1.0) * sw + config["pet_unserved_multiplier"] * sr
            )

        high_or_med = [p for p in pickups if pickups[p]["priority_class"] in {"high", "medical"}]
        standard_or_remote = [p for p in pickups if pickups[p]["priority_class"] in {"standard", "remote"}]
        for p in high_or_med:
            frac_p = served_total[p] / demand[scenario_id][p]["evacuees"]
            for q in standard_or_remote:
                frac_q = served_total[q] / demand[scenario_id][q]["evacuees"]
                min_fairness_slack = min(min_fairness_slack, frac_p + config["fairness_gap"] - frac_q)
                if frac_p + config["fairness_gap"] + tol < frac_q:
                    errors.append(f"Scenario {scenario_id} fairness violated between {p} and {q}.")

        for s in first_true_keys(shelter_open):
            max_shelter_capacity_utilization = max(max_shelter_capacity_utilization, shelter_load_e[s] / shelters[s]["capacity"])
            max_shelter_wheelchair_utilization = max(
                max_shelter_wheelchair_utilization,
                shelter_load_w[s] / shelters[s]["wheelchair_capacity"] if shelters[s]["wheelchair_capacity"] > 0 else 0.0,
            )
            max_shelter_pet_utilization = max(
                max_shelter_pet_utilization,
                shelter_load_r[s] / shelters[s]["pet_capacity"] if shelters[s]["pet_capacity"] > 0 else 0.0,
            )
            max_shelter_medical_utilization = max(
                max_shelter_medical_utilization,
                shelter_load_med[s] / shelters[s]["medical_beds"] if shelters[s]["medical_beds"] > 0 else 0.0,
            )
            if shelter_load_e[s] - shelters[s]["capacity"] > tol:
                errors.append(f"Scenario {scenario_id} shelter capacity violated at {s}.")
            if shelter_load_w[s] - shelters[s]["wheelchair_capacity"] > tol:
                errors.append(f"Scenario {scenario_id} shelter wheelchair capacity violated at {s}.")
            if shelter_load_r[s] - shelters[s]["pet_capacity"] > tol:
                errors.append(f"Scenario {scenario_id} shelter pet capacity violated at {s}.")
            if shelter_load_med[s] - shelters[s]["medical_beds"] > tol:
                errors.append(f"Scenario {scenario_id} medical beds violated at {s}.")

        for c in corridors:
            cap = impacts[scenario_id][c]["capacity_trips"] + (corridors[c]["capacity_bonus_trips"] if contraflow[c] else 0)
            if cap > 0:
                max_corridor_trip_utilization = max(max_corridor_trip_utilization, corridor_trips[c] / cap)
            if corridor_trips[c] > cap + tol:
                errors.append(f"Scenario {scenario_id} corridor trip capacity violated at {c}.")

        for p in pickups:
            intervals = sorted(pickup_intervals[p])
            events = []
            for start_t, end_t in intervals:
                events.append((start_t, 1))
                events.append((end_t, -1))
            events.sort(key=lambda item: (item[0], item[1]))
            concurrent = 0
            max_concurrent = 0
            for _, delta in events:
                concurrent += delta
                max_concurrent = max(max_concurrent, concurrent)
            max_boarding_concurrency_ratio = max(max_boarding_concurrency_ratio, max_concurrent / pickups[p]["boarding_bays"])
            if max_concurrent > pickups[p]["boarding_bays"]:
                errors.append(f"Scenario {scenario_id} boarding-bay limit violated at {p}.")

        scenario_cost = op_cost + late_cost + smoke_cost + unserved_cost + sip_cost
        scenario_costs[scenario_id] = scenario_cost
        scenario_checks[scenario_id] = {
            "operating_cost": op_cost,
            "late_cost": late_cost,
            "smoke_cost": smoke_cost,
            "unserved_cost": unserved_cost,
            "shelter_in_place_cost": sip_cost,
            "bus_trip_count": sum(bus_trip_count.values()),
            "min_arrival_slack": q8(min_arrival_slack if min_arrival_slack < math.inf else 0.0),
            "min_departure_slack": q8(min_departure_slack if min_departure_slack < math.inf else 0.0),
            "min_shift_slack": q8(min_shift_slack if min_shift_slack < math.inf else 0.0),
            "min_fairness_slack": q8(min_fairness_slack if min_fairness_slack < math.inf else 0.0),
            "max_boarding_concurrency_ratio": q8(max_boarding_concurrency_ratio),
            "max_corridor_trip_utilization": q8(max_corridor_trip_utilization),
            "max_shelter_capacity_utilization": q8(max_shelter_capacity_utilization),
            "max_shelter_wheelchair_utilization": q8(max_shelter_wheelchair_utilization),
            "max_shelter_pet_utilization": q8(max_shelter_pet_utilization),
            "max_shelter_medical_utilization": q8(max_shelter_medical_utilization),
        }

    probability_costs = [(scenarios[w]["probability"], scenario_costs[w]) for w in scenarios]
    expected_cost = sum(prob * cost for prob, cost in probability_costs)
    cvar = compute_cvar(probability_costs, config["tail_risk_alpha"])
    total_cost = open_cost + stage_cost + contraflow_cost + expected_cost + config["tail_risk_weight"] * cvar

    return {
        "errors": errors,
        "scenario_costs": scenario_costs,
        "scenario_checks": scenario_checks,
        "open_cost": open_cost,
        "stage_cost": stage_cost,
        "contraflow_cost": contraflow_cost,
        "expected_cost": expected_cost,
        "cvar": cvar,
        "total_cost": total_cost,
        "violation_count": len(errors),
        "max_normalized_residual": 0.0 if not errors else 1.0,
    }


def first_true_keys(mapping):
    return [key for key, value in mapping.items() if value]


def load_existing_solution():
    if not SOLUTION_PATH.exists():
        return None
    try:
        with open(SOLUTION_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def remaining_time_seconds(start_wall, total_limit):
    return total_limit - (time.time() - start_wall)


def search_single_bus_staging(data, incumbent_eval, start_wall, total_limit):
    first_stage = incumbent_eval["first_stage"]
    buses = sorted(data["buses"])
    depots = sorted(data["depots"])
    capacities = {d: data["depots"][d]["stage_capacity"] for d in depots}
    counts = {d: sum(1 for b in buses if first_stage["staging"][b] == d) for d in depots}

    best_eval = incumbent_eval
    best_move = None
    for bus_id in buses:
        current_depot = first_stage["staging"][bus_id]
        for depot_id in depots:
            if depot_id == current_depot:
                continue
            if counts[depot_id] + 1 > capacities[depot_id]:
                continue
            if remaining_time_seconds(start_wall, total_limit) <= TIME_BUFFER_SECONDS:
                return best_eval, best_move
            candidate = {
                "open_shelters": list(first_stage["open_shelters"]),
                "active_corridors": list(first_stage["active_corridors"]),
                "staging": dict(first_stage["staging"]),
            }
            candidate["staging"][bus_id] = depot_id
            candidate_eval = evaluate_first_stage(data, candidate)
            if candidate_eval["audit"]["errors"]:
                continue
            if candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8:
                best_eval = candidate_eval
                best_move = (bus_id, current_depot, depot_id)
    return best_eval, best_move


def search_two_bus_staging(data, incumbent_eval, start_wall, total_limit):
    first_stage = incumbent_eval["first_stage"]
    buses = sorted(data["buses"])
    depots = sorted(data["depots"])
    capacities = {d: data["depots"][d]["stage_capacity"] for d in depots}
    base_counts = {d: sum(1 for b in buses if first_stage["staging"][b] == d) for d in depots}

    best_eval = incumbent_eval
    best_move = None
    for i in range(len(buses)):
        bus_1 = buses[i]
        current_1 = first_stage["staging"][bus_1]
        for j in range(i + 1, len(buses)):
            bus_2 = buses[j]
            current_2 = first_stage["staging"][bus_2]
            for depot_1 in depots:
                for depot_2 in depots:
                    if depot_1 == current_1 and depot_2 == current_2:
                        continue
                    if remaining_time_seconds(start_wall, total_limit) <= TIME_BUFFER_SECONDS:
                        return best_eval, best_move
                    counts = dict(base_counts)
                    counts[current_1] -= 1
                    counts[current_2] -= 1
                    counts[depot_1] += 1
                    counts[depot_2] += 1
                    if any(counts[d] > capacities[d] for d in depots):
                        continue
                    candidate = {
                        "open_shelters": list(first_stage["open_shelters"]),
                        "active_corridors": list(first_stage["active_corridors"]),
                        "staging": dict(first_stage["staging"]),
                    }
                    candidate["staging"][bus_1] = depot_1
                    candidate["staging"][bus_2] = depot_2
                    candidate_eval = evaluate_first_stage(data, candidate)
                    if candidate_eval["audit"]["errors"]:
                        continue
                    if candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8:
                        best_eval = candidate_eval
                        best_move = (bus_1, current_1, depot_1, bus_2, current_2, depot_2)
    return best_eval, best_move


def search_corridor_subsets(data, incumbent_eval, start_wall, total_limit):
    first_stage = incumbent_eval["first_stage"]
    corridors = sorted(data["corridors"])
    best_eval = incumbent_eval
    best_active = tuple(first_stage["active_corridors"])
    for mask in range(1 << len(corridors)):
        if remaining_time_seconds(start_wall, total_limit) <= TIME_BUFFER_SECONDS:
            return best_eval, best_active
        active = [corridors[i] for i in range(len(corridors)) if (mask >> i) & 1]
        if len(active) > data["config"]["max_active_corridors"]:
            continue
        candidate = {
            "open_shelters": list(first_stage["open_shelters"]),
            "active_corridors": active,
            "staging": dict(first_stage["staging"]),
        }
        candidate_eval = evaluate_first_stage(data, candidate)
        if candidate_eval["audit"]["errors"]:
            continue
        if candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8:
            best_eval = candidate_eval
            best_active = tuple(active)
    return best_eval, best_active


def search_shelter_subsets(data, incumbent_eval, start_wall, total_limit):
    first_stage = incumbent_eval["first_stage"]
    shelters = list(first_stage["open_shelters"])
    backup = {s: data["shelters"][s]["backup_power"] for s in shelters}
    best_eval = incumbent_eval
    best_open = tuple(shelters)
    for size in range(1, len(shelters) + 1):
        for combo in itertools.combinations(shelters, size):
            if remaining_time_seconds(start_wall, total_limit) <= TIME_BUFFER_SECONDS:
                return best_eval, best_open
            if sum(backup[s] for s in combo) < data["config"]["min_backup_power_shelters"]:
                continue
            candidate = {
                "open_shelters": list(combo),
                "active_corridors": list(first_stage["active_corridors"]),
                "staging": dict(first_stage["staging"]),
            }
            candidate_eval = evaluate_first_stage(data, candidate)
            if candidate_eval["audit"]["errors"]:
                continue
            if candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8:
                best_eval = candidate_eval
                best_open = tuple(combo)
    return best_eval, best_open


def write_log(lines):
    LOG_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main():
    start_wall = time.time()
    total_limit = float(os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS", "300"))
    total_limit = min(total_limit, 300.0)

    data = load_data()
    existing_solution = load_existing_solution()
    incumbent_first_stage = None
    incumbent_audit = None
    incumbent_valid = False

    if existing_solution is not None:
        incumbent_audit = audit_solution(data, existing_solution)
        incumbent_first_stage = extract_first_stage_from_solution(data, existing_solution)
        incumbent_valid = incumbent_first_stage is not None and not incumbent_audit["errors"]

    if incumbent_valid:
        incumbent_eval = {
            "first_stage": incumbent_first_stage,
            "solution": existing_solution,
            "audit": incumbent_audit,
            "scenario_results": None,
        }
    else:
        incumbent_eval = evaluate_first_stage(data, choose_first_stage(data))
        incumbent_first_stage = incumbent_eval["first_stage"]
        incumbent_audit = incumbent_eval["audit"]

    log_lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        "- `PYTHONPATH=/opt/or-deps python -c \"import pyscipopt; print(pyscipopt.__version__)\"`",
        f"- Reported `pyscipopt` version: `{PYSCIPOPT_VERSION}`",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Approach",
        "",
        "- Reparsed the exact `solution.json` artifact from disk and audited it independently before any search.",
        "- Used the current disk incumbent as the rollback state and as the starting point when that artifact audited cleanly.",
        "- Built scenario plans with an exact-simulation constructive heuristic using 4 trip slots per bus.",
        "- Ran bounded single-bus and two-bus staging neighborhoods, then corridor and shelter subset checks, accepting only strict audited improvements.",
        "- Bound/gap are `n/a` for the true objective search because the accepted improvement loop is audit-backed neighborhood search rather than an exact extensive-form MIP.",
        "",
    ]
    log_lines.extend(
        [
            "## Incumbent Audit",
            "",
            f"- Disk artifact present: `{existing_solution is not None}`",
            f"- Disk artifact audit result: `{'PASS' if incumbent_valid else 'FAIL'}`",
            f"- Disk incumbent objective: `{q8(incumbent_audit['total_cost']) if incumbent_audit is not None else 'n/a'}`",
            f"- Disk incumbent violations: `{incumbent_audit['violation_count'] if incumbent_audit is not None else 'n/a'}`",
            f"- Disk incumbent max normalized residual: `{incumbent_audit['max_normalized_residual'] if incumbent_audit is not None else 'n/a'}`",
            "",
        ]
    )
    if incumbent_audit is not None and incumbent_audit["errors"]:
        for err in incumbent_audit["errors"]:
            log_lines.append(f"- Incumbent FAIL: {err}")
        log_lines.append("")

    search_records = []
    best_eval = incumbent_eval

    for round_idx in range(2):
        if remaining_time_seconds(start_wall, total_limit) > 35.0:
            candidate_eval, move = search_single_bus_staging(data, best_eval, start_wall, total_limit)
            accepted = move is not None and candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8
            search_records.append(
                {
                    "name": f"single_bus_round_{round_idx + 1}",
                    "incumbent": best_eval["audit"]["total_cost"],
                    "candidate": candidate_eval["audit"]["total_cost"],
                    "bound_gap": "n/a",
                    "audit": "PASS" if not candidate_eval["audit"]["errors"] else "FAIL",
                    "accepted": accepted,
                    "detail": move if move is not None else "no improving move",
                }
            )
            if accepted:
                best_eval = candidate_eval

        if remaining_time_seconds(start_wall, total_limit) > 120.0:
            candidate_eval, move = search_two_bus_staging(data, best_eval, start_wall, total_limit)
            accepted = move is not None and candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8
            search_records.append(
                {
                    "name": f"two_bus_round_{round_idx + 1}",
                    "incumbent": best_eval["audit"]["total_cost"],
                    "candidate": candidate_eval["audit"]["total_cost"],
                    "bound_gap": "n/a",
                    "audit": "PASS" if not candidate_eval["audit"]["errors"] else "FAIL",
                    "accepted": accepted,
                    "detail": move if move is not None else "no improving move",
                }
            )
            if accepted:
                best_eval = candidate_eval

    if remaining_time_seconds(start_wall, total_limit) > 35.0:
        candidate_eval, active_set = search_corridor_subsets(data, best_eval, start_wall, total_limit)
        accepted = active_set != tuple(best_eval["first_stage"]["active_corridors"]) and candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8
        search_records.append(
            {
                "name": "corridor_subset_search",
                "incumbent": best_eval["audit"]["total_cost"],
                "candidate": candidate_eval["audit"]["total_cost"],
                "bound_gap": "n/a",
                "audit": "PASS" if not candidate_eval["audit"]["errors"] else "FAIL",
                "accepted": accepted,
                "detail": active_set,
            }
        )
        if accepted:
            best_eval = candidate_eval

    if remaining_time_seconds(start_wall, total_limit) > 20.0:
        candidate_eval, open_set = search_shelter_subsets(data, best_eval, start_wall, total_limit)
        accepted = open_set != tuple(best_eval["first_stage"]["open_shelters"]) and candidate_eval["audit"]["total_cost"] < best_eval["audit"]["total_cost"] - 1e-8
        search_records.append(
            {
                "name": "shelter_subset_search",
                "incumbent": best_eval["audit"]["total_cost"],
                "candidate": candidate_eval["audit"]["total_cost"],
                "bound_gap": "n/a",
                "audit": "PASS" if not candidate_eval["audit"]["errors"] else "FAIL",
                "accepted": accepted,
                "detail": open_set,
            }
        )
        if accepted:
            best_eval = candidate_eval

    log_lines.extend(["## Guarded Search", ""])
    for record in search_records:
        log_lines.append(
            f"- `{record['name']}`: incumbent `{q8(record['incumbent'])}`, candidate `{q8(record['candidate'])}`, "
            f"bound/gap `{record['bound_gap']}`, audit `{record['audit']}`, accepted `{record['accepted']}`, detail `{record['detail']}`"
        )

    publish_candidate = (
        existing_solution is None
        or incumbent_audit is None
        or incumbent_audit["errors"]
        or best_eval["audit"]["total_cost"] < incumbent_audit["total_cost"] - 1e-8
    )
    published = False
    final_solution = existing_solution
    final_audit = incumbent_audit
    final_first_stage = incumbent_first_stage

    if publish_candidate:
        SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
        with open(SOLUTION_PATH, "w", encoding="utf-8") as f:
            json.dump(best_eval["solution"], f, indent=2)
        final_solution = load_existing_solution()
        final_audit = audit_solution(data, final_solution)
        final_first_stage = extract_first_stage_from_solution(data, final_solution)
        published = not final_audit["errors"] and (
            incumbent_audit is None
            or incumbent_audit["errors"]
            or final_audit["total_cost"] < incumbent_audit["total_cost"] - 1e-8
        )
        if not published and existing_solution is not None:
            with open(SOLUTION_PATH, "w", encoding="utf-8") as f:
                json.dump(existing_solution, f, indent=2)
            final_solution = load_existing_solution()
            final_audit = audit_solution(data, final_solution)
            final_first_stage = extract_first_stage_from_solution(data, final_solution)
    else:
        final_solution = existing_solution
        final_audit = incumbent_audit
        final_first_stage = incumbent_first_stage

    log_lines.extend(
        [
            "",
            "## Publication Decision",
            "",
            f"- Incumbent objective: `{q8(incumbent_audit['total_cost']) if incumbent_audit is not None else 'n/a'}`",
            f"- Best candidate objective: `{q8(best_eval['audit']['total_cost'])}`",
            f"- Published changed artifact: `{published}`",
            f"- Final disk audit result: `{'PASS' if final_audit is not None and not final_audit['errors'] else 'FAIL'}`",
            "",
            "## Final First-Stage Decisions",
            "",
            f"- Open shelters: `{', '.join(final_first_stage['open_shelters']) if final_first_stage is not None else 'n/a'}`",
            f"- Active corridors: `{', '.join(final_first_stage['active_corridors']) if final_first_stage is not None else 'n/a'}`",
            f"- Staging: `{', '.join(f'{b}->{d}' for b, d in sorted(final_first_stage['staging'].items())) if final_first_stage is not None else 'n/a'}`",
            "",
            "## Validation",
            "",
        ]
    )

    if final_audit is not None and final_audit["errors"]:
        for err in final_audit["errors"]:
            log_lines.append(f"- FAIL: {err}")
    else:
        log_lines.append("- PASS: schema coverage, timing, route sequencing, shift limits, corridor capacities, shelter capacities, medical-bed usage, demand balance, service floors, fairness, boarding-bay limits, and budget/staging checks all satisfied.")

    log_lines.extend(
        [
            "",
            "## Objective Audit",
            "",
            f"- Shelter opening cost: `{q8(final_audit['open_cost'])}`",
            f"- Staging cost: `{q8(final_audit['stage_cost'])}`",
            f"- Contraflow cost: `{q8(final_audit['contraflow_cost'])}`",
            f"- Expected scenario cost: `{q8(final_audit['expected_cost'])}`",
            f"- CVaR_{q8(data['config']['tail_risk_alpha'])}: `{q8(final_audit['cvar'])}`",
            f"- Total score: `{q8(final_audit['total_cost'])}`",
            "",
            "## Scenario Cost Breakdown",
            "",
        ]
    )

    for scenario_id in sorted(data["scenarios"]):
        check = final_audit["scenario_checks"][scenario_id]
        log_lines.append(
            f"- `{scenario_id}`: operating `{q8(check['operating_cost'])}`, late `{q8(check['late_cost'])}`, "
            f"smoke `{q8(check['smoke_cost'])}`, unserved `{q8(check['unserved_cost'])}`, "
            f"shelter-in-place `{q8(check['shelter_in_place_cost'])}`, trips `{check['bus_trip_count']}`, "
            f"arrival slack `{q8(check['min_arrival_slack'])}`, closure slack `{q8(check['min_departure_slack'])}`, "
            f"shift slack `{q8(check['min_shift_slack'])}`, fairness slack `{q8(check['min_fairness_slack'])}`"
        )

    write_log(log_lines)

    if final_audit is None or final_audit["errors"]:
        raise SystemExit("Audit failed; see solve_log.md for details.")


if __name__ == "__main__":
    main()
