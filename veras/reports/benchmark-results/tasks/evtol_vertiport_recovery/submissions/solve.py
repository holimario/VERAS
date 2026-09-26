#!/usr/bin/env python3

import csv
import json
import math
import os
from collections import defaultdict

from pyscipopt import Model, quicksum


DATA_DIR = "/app/data"
SUBMISSIONS_DIR = "/app/submissions"
SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.csv")
LOG_PATH = os.path.join(SUBMISSIONS_DIR, "solve_log.md")
REL_GAP = 0.0005
DEFAULT_TIME_LIMIT_SECONDS = 120.0
TOL = 1e-6
FMT = "{:.8f}"
FIELDNAMES = [
    "row_type",
    "scenario_id",
    "aircraft_id",
    "request_id",
    "origin",
    "destination",
    "start_slot",
    "end_slot",
    "energy_kwh",
    "quantity",
    "decision",
]

# Restoration-mode first-stage policy.
ACCEPTED_REQUESTS = {
    "R005",
    "R006",
    "R017",
    "R018",
    "R021",
    "R022",
    "R023",
    "R024",
    "R025",
    "R026",
    "R035",
    "R045",
    "R046",
}

# Fixed scenario-independent request departure times.
REQUEST_STARTS = {
    "R005": 3,
    "R006": 4,
    "R017": 2,
    "R018": 4,
    "R021": 8,
    "R022": 14,
    "R023": 11,
    "R024": 15,
    "R025": 15,
    "R026": 17,
    "R035": 19,
    "R045": 22,
    "R046": 23,
}

# Fixed aircraft assignment for the restoration candidate.
REQUEST_AIRCRAFT = {
    "R005": "A11",
    "R006": "A06",
    "R017": "A05",
    "R018": "A10",
    "R021": "A08",
    "R022": "A02",
    "R023": "A01",
    "R024": "A07",
    "R025": "A05",
    "R026": "A10",
    "R035": "A04",
    "R045": "A12",
    "R046": "A03",
}

# Candidate charge slots and target energies used as the Phase-I repair center.
CHARGE_TARGETS = {
    "S_CLEAR": [
        ("A05", "V_HOSPITAL", 7, 20.0),
        ("A10", "V_HOSPITAL", 9, 20.0),
    ],
    "S_WIND_NORTH": [
        ("A05", "V_HOSPITAL", 10, 38.1),
        ("A10", "V_HOSPITAL", 12, 38.1),
    ],
    "S_STORM_CELLS": [
        ("A05", "V_HOSPITAL", 10, 45.1),
        ("A05", "V_HOSPITAL", 11, 4.0),
        ("A10", "V_HOSPITAL", 12, 45.1),
        ("A10", "V_HOSPITAL", 13, 4.0),
    ],
}

# Fixed reserve witnesses under the candidate schedule.
RESERVE_WITNESS = {
    ("S_CLEAR", "V_HOSPITAL", 12): "A05",
    ("S_WIND_NORTH", "V_HOSPITAL", 12): "A05",
    ("S_STORM_CELLS", "V_HOSPITAL", 12): "A05",
    ("S_STORM_CELLS", "V_AIRPORT", 18): "A12",
}


def fmt_num(value):
    return FMT.format(float(value))


def read_csv_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_data():
    with open(os.path.join(DATA_DIR, "config.json")) as f:
        config = json.load(f)

    vertiports = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "vertiports.csv")):
        vid = row["vertiport_id"]
        vertiports[vid] = {
            "pad_count": int(row["pad_count"]),
            "charger_count": int(row["charger_count"]),
            "parking_stands": int(row["parking_stands"]),
            "grid_kw_limit": float(row["grid_kw_limit"]),
            "departure_noise_quota_day": float(row["departure_noise_quota_day"]),
            "departure_noise_quota_evening": float(row["departure_noise_quota_evening"]),
        }

    aircraft = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "aircraft.csv")):
        aid = row["aircraft_id"]
        aircraft[aid] = {
            "home_vertiport": row["home_vertiport"],
            "seat_capacity": int(row["seat_capacity"]),
            "battery_capacity_kwh": float(row["battery_capacity_kwh"]),
            "initial_soc_kwh": float(row["initial_soc_kwh"]),
            "max_charge_kw": float(row["max_charge_kw"]),
            "turnaround_slots": int(row["turnaround_slots"]),
            "noise_class": float(row["noise_class"]),
        }

    routes = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "routes.csv")):
        routes[(row["origin"], row["destination"])] = {
            "corridor_id": row["corridor_id"],
            "flight_slots": int(row["flight_slots"]),
            "energy_kwh": float(row["energy_kwh"]),
            "operating_cost": float(row["operating_cost"]),
            "noise_points": float(row["noise_points"]),
            "corridor_capacity_per_slot": int(row["corridor_capacity_per_slot"]),
        }

    requests = {}
    request_order = []
    for row in read_csv_rows(os.path.join(DATA_DIR, "requests.csv")):
        rid = row["request_id"]
        request_order.append(rid)
        requests[rid] = {
            "origin": row["origin"],
            "destination": row["destination"],
            "earliest_slot": int(row["earliest_slot"]),
            "latest_depart_slot": int(row["latest_depart_slot"]),
            "passengers": int(row["passengers"]),
            "priority_class": row["priority_class"],
            "market": row["market"],
            "fare_per_passenger": float(row["fare_per_passenger"]),
            "decline_penalty": float(row["decline_penalty"]),
        }

    scenarios = {}
    scenario_order = []
    for row in read_csv_rows(os.path.join(DATA_DIR, "scenarios.csv")):
        sid = row["scenario_id"]
        scenario_order.append(sid)
        scenarios[sid] = {
            "probability": float(row["probability"]),
            "global_delay_slots": int(row["global_delay_slots"]),
            "charger_derate": float(row["charger_derate"]),
            "soc_derate": float(row["soc_derate"]),
            "target_profit": float(row["target_profit"]),
        }

    scenario_route = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "scenario_route_availability.csv")):
        scenario_route[(row["scenario_id"], row["origin"], row["destination"])] = {
            "open": int(row["open"]),
            "flight_slot_multiplier": float(row["flight_slot_multiplier"]),
            "energy_multiplier": float(row["energy_multiplier"]),
        }

    scenario_aircraft = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "scenario_aircraft_availability.csv")):
        scenario_aircraft[(row["scenario_id"], row["aircraft_id"])] = {
            "release_slot": int(row["release_slot"]),
            "soc_derate": float(row["soc_derate"]),
        }

    grid_prices = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "grid_prices.csv")):
        grid_prices[(row["vertiport_id"], int(row["slot"]))] = {
            "price_per_kwh": float(row["price_per_kwh"]),
            "grid_limit_multiplier": float(row["grid_limit_multiplier"]),
        }

    periods = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "time_slots.csv")):
        periods[int(row["slot"])] = row["period"]

    origin_floors = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "origin_service_floors.csv")):
        origin_floors[row["origin"]] = int(row["minimum_accepted_passengers"])

    groups = defaultdict(list)
    group_spread = {}
    for row in read_csv_rows(os.path.join(DATA_DIR, "request_groups.csv")):
        groups[row["group_id"]].append(row["request_id"])
        group_spread[row["group_id"]] = int(row["max_departure_spread_slots"])

    service_windows = defaultdict(list)
    for row in read_csv_rows(os.path.join(DATA_DIR, "aircraft_service_windows.csv")):
        service_windows[(row["scenario_id"], row["aircraft_id"])].append(
            {
                "vertiport_id": row["vertiport_id"],
                "start_slot": int(row["start_slot"]),
                "end_slot": int(row["end_slot"]),
                "min_soc_kwh": float(row["min_soc_kwh"]),
            }
        )

    reserve_requirements = []
    for row in read_csv_rows(os.path.join(DATA_DIR, "emergency_reserve_requirements.csv")):
        reserve_requirements.append(
            {
                "scenario_id": row["scenario_id"],
                "vertiport_id": row["vertiport_id"],
                "slot": int(row["slot"]),
                "min_idle_aircraft": int(row["min_idle_aircraft"]),
                "min_soc_kwh": float(row["min_soc_kwh"]),
            }
        )

    corridor_controls = defaultdict(list)
    for row in read_csv_rows(os.path.join(DATA_DIR, "corridor_slot_controls.csv")):
        corridor_controls[(row["scenario_id"], row["corridor_id"])].append(
            {
                "start_slot": int(row["start_slot"]),
                "end_slot": int(row["end_slot"]),
                "max_flights_per_slot": int(row["max_flights_per_slot"]),
            }
        )

    horizon = int(config["planning_horizon_slots"])
    slot_hours = float(config["slot_minutes"]) / 60.0
    return {
        "config": config,
        "vertiports": vertiports,
        "aircraft": aircraft,
        "routes": routes,
        "requests": requests,
        "request_order": request_order,
        "scenarios": scenarios,
        "scenario_order": scenario_order,
        "scenario_route": scenario_route,
        "scenario_aircraft": scenario_aircraft,
        "grid_prices": grid_prices,
        "periods": periods,
        "origin_floors": origin_floors,
        "groups": groups,
        "group_spread": group_spread,
        "service_windows": service_windows,
        "reserve_requirements": reserve_requirements,
        "corridor_controls": corridor_controls,
        "horizon": horizon,
        "slot_hours": slot_hours,
    }


def route_duration_energy(data, sid, origin, destination):
    route = data["routes"][(origin, destination)]
    scen = data["scenarios"][sid]
    sr = data["scenario_route"][(sid, origin, destination)]
    duration = int(
        math.ceil(
            route["flight_slots"] * sr["flight_slot_multiplier"]
            + scen["global_delay_slots"]
            - 1e-9
        )
    )
    energy = route["energy_kwh"] * sr["energy_multiplier"]
    return duration, energy


def build_fixed_schedule(data, start_overrides=None):
    schedule = defaultdict(lambda: defaultdict(list))
    flight_energy_by_time = defaultdict(float)
    charge_targets = {}
    start_overrides = start_overrides or {}

    for sid in data["scenario_order"]:
        for rid in ACCEPTED_REQUESTS:
            req = data["requests"][rid]
            aid = REQUEST_AIRCRAFT[rid]
            start = start_overrides.get((sid, rid), REQUEST_STARTS[rid])
            duration, energy = route_duration_energy(data, sid, req["origin"], req["destination"])
            end = start + duration
            schedule[sid][aid].append(
                {
                    "row_type": "flight",
                    "request_id": rid,
                    "origin": req["origin"],
                    "destination": req["destination"],
                    "start_slot": start,
                    "end_slot": end,
                    "energy_kwh": energy,
                    "quantity": float(req["passengers"]),
                }
            )
            flight_energy_by_time[(sid, aid, start)] += energy

        for aid, vid, slot, target in CHARGE_TARGETS[sid]:
            schedule[sid][aid].append(
                {
                    "row_type": "charge",
                    "request_id": "",
                    "origin": vid,
                    "destination": "",
                    "start_slot": slot,
                    "end_slot": slot + 1,
                    "energy_kwh": None,
                    "quantity": None,
                }
            )
            charge_targets[(sid, aid, vid, slot)] = target

        for aid in schedule[sid]:
            schedule[sid][aid].sort(
                key=lambda row: (row["start_slot"], 0 if row["row_type"] == "flight" else 1)
            )

    return schedule, flight_energy_by_time, charge_targets


def optimize_schedule_charges(data, schedule, flight_energy_by_time, charge_targets, time_limit):
    model = Model("restoration_phase1")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", float(time_limit))

    config = data["config"]
    slot_hours = data["slot_hours"]
    taper_frac = float(config["taper_charge_fraction"])
    high_frac = float(config["high_soc_threshold_fraction"])

    charge_vars = {}
    head = {}
    head_bin = {}
    battery = {}
    flight_slack = {}
    window_slack = {}
    reserve_slack = {}
    theta = model.addVar(lb=0.0, name="theta")

    for sid in data["scenario_order"]:
        for aid, arow in data["aircraft"].items():
            cap = arow["battery_capacity_kwh"]
            for t in range(data["horizon"] + 1):
                battery[(sid, aid, t)] = model.addVar(lb=0.0, ub=cap, name=f"b[{sid},{aid},{t}]")

    for sid in data["scenario_order"]:
        for aid, actions in schedule[sid].items():
            for row in actions:
                if row["row_type"] != "charge":
                    continue
                vid = row["origin"]
                slot = row["start_slot"]
                charge_vars[(sid, aid, vid, slot)] = model.addVar(lb=0.0, name=f"e[{sid},{aid},{vid},{slot}]")
                head[(sid, aid, slot)] = model.addVar(lb=0.0, name=f"head[{sid},{aid},{slot}]")
                head_bin[(sid, aid, slot)] = model.addVar(vtype="B", name=f"hbin[{sid},{aid},{slot}]")

    for sid in data["scenario_order"]:
        for aid, actions in schedule[sid].items():
            for row in actions:
                if row["row_type"] == "flight":
                    rid = row["request_id"]
                    flight_slack[(sid, aid, rid)] = model.addVar(lb=0.0, name=f"sflight[{sid},{aid},{rid}]")

    for key, windows in data["service_windows"].items():
        sid, aid = key
        for idx, window in enumerate(windows):
            window_slack[(sid, aid, idx)] = model.addVar(lb=0.0, name=f"swindow[{sid},{aid},{idx}]")

    for res in data["reserve_requirements"]:
        witness = RESERVE_WITNESS[(res["scenario_id"], res["vertiport_id"], res["slot"])]
        reserve_slack[(res["scenario_id"], witness, res["vertiport_id"], res["slot"])] = model.addVar(
            lb=0.0,
            name=f"sreserve[{res['scenario_id']},{witness},{res['vertiport_id']},{res['slot']}]",
        )

    normalized = []

    for sid in data["scenario_order"]:
        for aid, arow in data["aircraft"].items():
            start_soc = (
                arow["initial_soc_kwh"]
                * data["scenarios"][sid]["soc_derate"]
                * data["scenario_aircraft"][(sid, aid)]["soc_derate"]
            )
            model.addCons(battery[(sid, aid, 0)] == start_soc, name=f"start_soc[{sid},{aid}]")

            for t in range(data["horizon"]):
                charge_expr = quicksum(
                    charge_vars[key]
                    for key in charge_vars
                    if key[0] == sid and key[1] == aid and key[3] == t
                )
                model.addCons(
                    battery[(sid, aid, t + 1)]
                    == battery[(sid, aid, t)] + charge_expr - flight_energy_by_time[(sid, aid, t)],
                    name=f"battery[{sid},{aid},{t}]",
                )

            for row in schedule[sid].get(aid, []):
                if row["row_type"] == "flight":
                    rid = row["request_id"]
                    needed = row["energy_kwh"] + float(config["reserve_soc_kwh"])
                    slack = flight_slack[(sid, aid, rid)]
                    model.addCons(
                        battery[(sid, aid, row["start_slot"])] + slack >= needed,
                        name=f"preflight[{sid},{aid},{rid}]",
                    )
                    normalized.append((slack, needed))
                else:
                    vid = row["origin"]
                    slot = row["start_slot"]
                    evar = charge_vars[(sid, aid, vid, slot)]
                    cap = arow["battery_capacity_kwh"]
                    fast = arow["max_charge_kw"] * data["scenarios"][sid]["charger_derate"] * slot_hours
                    taper = fast * taper_frac
                    threshold = high_frac * cap
                    model.addCons(head[(sid, aid, slot)] >= threshold - battery[(sid, aid, slot)])
                    model.addCons(
                        head[(sid, aid, slot)]
                        <= threshold - battery[(sid, aid, slot)] + cap * (1 - head_bin[(sid, aid, slot)])
                    )
                    model.addCons(head[(sid, aid, slot)] <= cap * head_bin[(sid, aid, slot)])
                    model.addCons(evar <= fast, name=f"charge_fast[{sid},{aid},{slot}]")
                    model.addCons(
                        evar <= cap - battery[(sid, aid, slot)],
                        name=f"charge_cap[{sid},{aid},{slot}]",
                    )
                    model.addCons(
                        evar <= taper + (1.0 - taper_frac) * head[(sid, aid, slot)],
                        name=f"charge_taper[{sid},{aid},{slot}]",
                    )

    for key, windows in data["service_windows"].items():
        sid, aid = key
        for idx, window in enumerate(windows):
            slack = window_slack[(sid, aid, idx)]
            model.addCons(
                battery[(sid, aid, window["start_slot"])] + slack >= window["min_soc_kwh"],
                name=f"window_soc[{sid},{aid},{idx}]",
            )
            normalized.append((slack, window["min_soc_kwh"]))

    for res in data["reserve_requirements"]:
        sid = res["scenario_id"]
        vid = res["vertiport_id"]
        slot = res["slot"]
        witness = RESERVE_WITNESS[(sid, vid, slot)]
        slack = reserve_slack[(sid, witness, vid, slot)]
        model.addCons(
            battery[(sid, witness, slot)] + slack >= res["min_soc_kwh"],
            name=f"reserve_soc[{sid},{witness},{vid},{slot}]",
        )
        normalized.append((slack, res["min_soc_kwh"]))

    for slack, scale in normalized:
        denom = max(float(scale), 1.0)
        model.addCons(slack <= denom * theta + 1e-9)

    model.setObjective(theta, "minimize")
    model.optimize()
    if model.getNSols() <= 0:
        raise RuntimeError("Phase-I stage 1 found no solution.")
    sol = model.getBestSol()
    best_theta = model.getSolVal(sol, theta)
    model.freeTransform()
    model.addCons(theta <= best_theta + 1e-9, name="fix_theta")

    total_violation = quicksum(slack / max(float(scale), 1.0) for slack, scale in normalized)
    model.setObjective(total_violation, "minimize")
    model.optimize()
    if model.getNSols() <= 0:
        raise RuntimeError("Phase-I stage 2 found no solution.")
    sol = model.getBestSol()
    best_total = sum(
        model.getSolVal(sol, slack) / max(float(scale), 1.0) for slack, scale in normalized
    )
    model.freeTransform()
    model.addCons(total_violation <= best_total + 1e-9, name="fix_total_violation")

    charge_cost = quicksum(
        (
            data["grid_prices"][(vid, slot)]["price_per_kwh"] / float(config["charge_efficiency"])
        )
        * charge_vars[(sid, aid, vid, slot)]
        for sid, aid, vid, slot in charge_vars
    )

    model.setObjective(charge_cost, "minimize")
    model.optimize()
    if model.getNSols() <= 0:
        raise RuntimeError("Charge-optimization stage found no solution.")

    sol = model.getBestSol()
    result = {
        "stage1_theta": best_theta,
        "stage2_total_violation": best_total,
        "status": str(model.getStatus()),
        "dual_bound": float(model.getDualbound()),
        "gap": float(model.getGap()),
        "charges": {key: model.getSolVal(sol, var) for key, var in charge_vars.items()},
    }
    return result


def export_rows(data, schedule, solved_charges):
    rows = []
    for rid in data["request_order"]:
        rows.append(
            {
                "row_type": "accept",
                "scenario_id": "",
                "aircraft_id": "",
                "request_id": rid,
                "origin": "",
                "destination": "",
                "start_slot": "",
                "end_slot": "",
                "energy_kwh": "",
                "quantity": "",
                "decision": "accept" if rid in ACCEPTED_REQUESTS else "decline",
            }
        )

    action_rows = []
    type_order = {"flight": 0, "reposition": 1, "charge": 2}
    for sid in data["scenario_order"]:
        for aid, actions in schedule[sid].items():
            for row in actions:
                if row["row_type"] == "flight":
                    action_rows.append(
                        {
                            "row_type": "flight",
                            "scenario_id": sid,
                            "aircraft_id": aid,
                            "request_id": row["request_id"],
                            "origin": row["origin"],
                            "destination": row["destination"],
                            "start_slot": str(row["start_slot"]),
                            "end_slot": str(row["end_slot"]),
                            "energy_kwh": fmt_num(row["energy_kwh"]),
                            "quantity": fmt_num(row["quantity"]),
                            "decision": "serve",
                        }
                    )
                else:
                    key = (sid, aid, row["origin"], row["start_slot"])
                    energy = solved_charges[key]
                    if energy <= 1e-8:
                        continue
                    action_rows.append(
                        {
                            "row_type": "charge",
                            "scenario_id": sid,
                            "aircraft_id": aid,
                            "request_id": "",
                            "origin": row["origin"],
                            "destination": "",
                            "start_slot": str(row["start_slot"]),
                            "end_slot": str(row["end_slot"]),
                            "energy_kwh": fmt_num(energy),
                            "quantity": "",
                            "decision": "charge",
                        }
                    )

    action_rows.sort(
        key=lambda row: (
            row["scenario_id"],
            row["aircraft_id"],
            int(row["start_slot"]),
            int(row["end_slot"]),
            type_order[row["row_type"]],
            row["request_id"],
        )
    )
    rows.extend(action_rows)
    return rows


def cvar_value(shortfalls, probabilities, alpha):
    candidates = sorted({0.0, *shortfalls})
    best = None
    scale = 1.0 / (1.0 - alpha)
    for eta in candidates:
        value = eta + scale * sum(
            p * max(shortfall - eta, 0.0)
            for shortfall, p in zip(shortfalls, probabilities)
        )
        if best is None or value < best:
            best = value
    return 0.0 if best is None else best


def read_solution_rows(path):
    if not os.path.exists(path):
        return None
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def validate_solution(data, rows):
    config = data["config"]
    horizon = data["horizon"]
    accept_map = {}
    action_by_scenario_aircraft = defaultdict(list)
    flights_by_scenario_request = defaultdict(list)
    errors = []

    for row in rows:
        if list(row.keys()) != FIELDNAMES:
            errors.append("CSV schema mismatch.")
            break
        if row["row_type"] == "accept":
            rid = row["request_id"]
            if rid not in data["requests"]:
                errors.append(f"Unknown request in accept row: {rid}")
                continue
            if row["decision"] not in {"accept", "decline"}:
                errors.append(f"Invalid accept decision for {rid}")
            for field in ("scenario_id", "aircraft_id", "origin", "destination", "start_slot", "end_slot", "energy_kwh", "quantity"):
                if row[field] != "":
                    errors.append(f"Accept row must leave {field} blank for {rid}")
            if rid in accept_map:
                errors.append(f"Duplicate accept row for {rid}")
                continue
            accept_map[rid] = row["decision"] == "accept"
        else:
            sid = row["scenario_id"]
            aid = row["aircraft_id"]
            if sid not in data["scenarios"]:
                errors.append(f"Unknown scenario in action row: {sid}")
                continue
            if aid not in data["aircraft"]:
                errors.append(f"Unknown aircraft in action row: {aid}")
                continue
            if row["row_type"] not in {"flight", "reposition", "charge"}:
                errors.append(f"Unknown row_type {row['row_type']}")
                continue
            if row["start_slot"] == "" or row["end_slot"] == "":
                errors.append(f"Missing slots in {row['row_type']} row for {sid}/{aid}")
                continue
            action_by_scenario_aircraft[(sid, aid)].append(row)
            if row["row_type"] == "flight":
                if row["decision"] != "serve":
                    errors.append(f"Flight decision must be serve for {sid}/{row['request_id']}")
                flights_by_scenario_request[(sid, row["request_id"])].append(row)
            if row["row_type"] == "charge":
                if row["decision"] != "charge":
                    errors.append(f"Charge decision must be charge for {sid}/{aid}")
                if row["request_id"] != "" or row["destination"] != "" or row["quantity"] != "":
                    errors.append(f"Charge row has invalid nonblank fields for {sid}/{aid}/{row['start_slot']}")
            if row["row_type"] == "reposition":
                if row["decision"] != "reposition":
                    errors.append(f"Reposition decision must be reposition for {sid}/{aid}")
                if row["request_id"] != "" or row["quantity"] != "":
                    errors.append(f"Reposition row has invalid nonblank fields for {sid}/{aid}/{row['start_slot']}")

    for rid in data["request_order"]:
        if rid not in accept_map:
            errors.append(f"Missing accept row for {rid}")

    for gid, reqs in data["groups"].items():
        vals = {accept_map.get(rid) for rid in reqs}
        if len(vals) > 1:
            errors.append(f"Group acceptance violated for {gid}")

    total_passengers = sum(r["passengers"] for r in data["requests"].values())
    accepted_passengers = sum(
        data["requests"][rid]["passengers"] for rid, acc in accept_map.items() if acc
    )
    if accepted_passengers + TOL < float(config["minimum_accepted_passenger_fraction"]) * total_passengers:
        errors.append("Minimum accepted passenger fraction violated")

    total_medical = sum(
        r["passengers"] for r in data["requests"].values() if r["priority_class"] == "medical"
    )
    accepted_medical = sum(
        data["requests"][rid]["passengers"]
        for rid, acc in accept_map.items()
        if acc and data["requests"][rid]["priority_class"] == "medical"
    )
    if accepted_medical + TOL < float(config["minimum_medical_accepted_fraction"]) * total_medical:
        errors.append("Minimum medical fraction violated")

    airport_market = sum(
        data["requests"][rid]["passengers"]
        for rid, acc in accept_map.items()
        if acc and data["requests"][rid]["market"] == "airport"
    )
    if airport_market + TOL < int(config["minimum_airport_accepted_passengers"]):
        errors.append("Minimum airport-market passengers violated")

    declined_priority = sum(
        1
        for rid, acc in accept_map.items()
        if not acc and data["requests"][rid]["priority_class"] in {"airport", "medical"}
    )
    if declined_priority > int(config["maximum_declined_priority_requests"]):
        errors.append("Maximum declined priority requests violated")

    for origin, floor in data["origin_floors"].items():
        accepted_origin = sum(
            data["requests"][rid]["passengers"]
            for rid, acc in accept_map.items()
            if acc and data["requests"][rid]["origin"] == origin
        )
        if accepted_origin + TOL < floor:
            errors.append(f"Origin floor violated at {origin}")

    for sid in data["scenario_order"]:
        for rid in data["request_order"]:
            count = len(flights_by_scenario_request[(sid, rid)])
            if accept_map.get(rid, False) and count != 1:
                errors.append(f"Accepted request {rid} not served exactly once in {sid}")
            if not accept_map.get(rid, False) and count != 0:
                errors.append(f"Declined request {rid} served in {sid}")

    scenario_profit = {}
    for sid in data["scenario_order"]:
        pad = defaultdict(int)
        park = defaultdict(int)
        charger = defaultdict(int)
        charge_energy = defaultdict(float)
        corridor = defaultdict(int)
        noise = defaultdict(float)
        idle_info = {}
        dep_times = {}

        acceptance_profit = sum(
            data["requests"][rid]["passengers"] * data["requests"][rid]["fare_per_passenger"]
            if accept_map.get(rid, False)
            else -data["requests"][rid]["decline_penalty"]
            for rid in data["request_order"]
        )
        op_cost = 0.0
        charge_cost = 0.0
        delay_cost = 0.0

        for aid, arow in data["aircraft"].items():
            loc = arow["home_vertiport"]
            soc = (
                arow["initial_soc_kwh"]
                * data["scenarios"][sid]["soc_derate"]
                * data["scenario_aircraft"][(sid, aid)]["soc_derate"]
            )
            ready = data["scenario_aircraft"][(sid, aid)]["release_slot"]
            actions = []
            for row in action_by_scenario_aircraft[(sid, aid)]:
                action = dict(row)
                action["start"] = int(row["start_slot"])
                action["end"] = int(row["end_slot"])
                action["energy"] = float(row["energy_kwh"]) if row["energy_kwh"] else 0.0
                action["quantity_f"] = float(row["quantity"]) if row["quantity"] else 0.0
                if action["end"] <= action["start"]:
                    errors.append(f"Nonpositive duration in {sid}/{aid}/{row['row_type']} row")
                actions.append(action)
            actions.sort(key=lambda row: (row["start"], 0 if row["row_type"] == "flight" else 1))

            for action in actions:
                if action["start"] < ready:
                    errors.append(f"Readiness violation for {sid}/{aid} at slot {action['start']}")
                for slot in range(max(ready, 0), min(action["start"], horizon)):
                    idle_info[(aid, slot)] = {"loc": loc, "soc": soc}
                    park[(loc, slot)] += 1

                if action["row_type"] == "charge":
                    if action["origin"] != loc or action["end"] != action["start"] + 1:
                        errors.append(f"Charge continuity violation for {sid}/{aid} at slot {action['start']}")
                    fast = arow["max_charge_kw"] * data["scenarios"][sid]["charger_derate"] * data["slot_hours"]
                    taper = fast * float(config["taper_charge_fraction"])
                    threshold = arow["battery_capacity_kwh"] * float(config["high_soc_threshold_fraction"])
                    head = max(threshold - soc, 0.0)
                    max_charge = min(
                        fast,
                        taper + (1.0 - float(config["taper_charge_fraction"])) * head,
                        arow["battery_capacity_kwh"] - soc,
                    )
                    if action["energy"] > max_charge + 1e-5:
                        errors.append(f"Charge cap/taper violated for {sid}/{aid} at slot {action['start']}")
                    charger[(loc, action["start"])] += 1
                    charge_energy[(loc, action["start"])] += action["energy"]
                    park[(loc, action["start"])] += 1
                    charge_cost += (
                        data["grid_prices"][(loc, action["start"])]["price_per_kwh"]
                        / float(config["charge_efficiency"])
                    ) * action["energy"]
                    soc += action["energy"]
                    ready = action["end"]
                else:
                    rid = action["request_id"]
                    req = data["requests"][rid]
                    duration, expected_energy = route_duration_energy(data, sid, action["origin"], action["destination"])
                    if action["origin"] != loc:
                        errors.append(f"Origin continuity violated for {sid}/{aid} at slot {action['start']}")
                    if action["end"] - action["start"] < duration:
                        errors.append(f"Duration violated for {sid}/{rid}")
                    if abs(action["energy"] - expected_energy) > 1e-5:
                        errors.append(f"Energy violated for {sid}/{rid}")
                    if soc + TOL < action["energy"] + float(config["reserve_soc_kwh"]):
                        errors.append(f"Reserve SOC violated before {sid}/{rid}")
                    if not (req["earliest_slot"] <= action["start"] <= req["latest_depart_slot"]):
                        errors.append(f"Departure window violated for {sid}/{rid}")
                    if abs(action["quantity_f"] - req["passengers"]) > 1e-5:
                        errors.append(f"Passenger quantity violated for {sid}/{rid}")
                    dep_times[rid] = action["start"]
                    route = data["routes"][(action["origin"], action["destination"])]
                    pad[(action["origin"], action["start"])] += 1
                    if action["end"] < horizon:
                        pad[(action["destination"], action["end"])] += 1
                    for slot in range(action["start"], action["end"]):
                        corridor[(route["corridor_id"], slot)] += 1
                    for slot in range(action["end"], min(action["end"] + arow["turnaround_slots"], horizon)):
                        park[(action["destination"], slot)] += 1
                    noise[(action["origin"], data["periods"][action["start"]])] += (
                        route["noise_points"] * arow["noise_class"]
                    )
                    soc -= action["energy"]
                    op_cost += route["operating_cost"]
                    delay_cost += (
                        float(config["delay_penalty_per_passenger_slot"])
                        * req["passengers"]
                        * (action["start"] - req["earliest_slot"])
                    )
                    loc = action["destination"]
                    ready = action["end"] + arow["turnaround_slots"]

            for slot in range(max(ready, 0), horizon):
                idle_info[(aid, slot)] = {"loc": loc, "soc": soc}
                park[(loc, slot)] += 1

            for window in data["service_windows"].get((sid, aid), []):
                for slot in range(window["start_slot"], window["end_slot"]):
                    info = idle_info.get((aid, slot))
                    if not info or info["loc"] != window["vertiport_id"]:
                        errors.append(f"Service window location violated for {sid}/{aid} at slot {slot}")
                info = idle_info.get((aid, window["start_slot"]))
                if not info or info["soc"] + TOL < window["min_soc_kwh"]:
                    errors.append(f"Service window SOC violated for {sid}/{aid}")

        for gid, reqs in data["groups"].items():
            if all(accept_map.get(rid, False) for rid in reqs):
                vals = [dep_times.get(rid) for rid in reqs]
                if any(v is None for v in vals):
                    errors.append(f"Grouped request missing in {sid}/{gid}")
                elif max(vals) - min(vals) > data["group_spread"][gid]:
                    errors.append(f"Group spread violated in {sid}/{gid}")

        for vid, vrow in data["vertiports"].items():
            for slot in range(horizon):
                if pad[(vid, slot)] > vrow["pad_count"] + 1e-5:
                    errors.append(f"Pad capacity violated at {sid}/{vid}/{slot}")
                if park[(vid, slot)] > vrow["parking_stands"] + 1e-5:
                    errors.append(f"Parking violated at {sid}/{vid}/{slot}")
                if charger[(vid, slot)] > vrow["charger_count"] + 1e-5:
                    errors.append(f"Charger violated at {sid}/{vid}/{slot}")
                grid_cap = (
                    vrow["grid_kw_limit"]
                    * data["scenarios"][sid]["charger_derate"]
                    * data["grid_prices"][(vid, slot)]["grid_limit_multiplier"]
                    * data["slot_hours"]
                    * float(config["charge_efficiency"])
                )
                if charge_energy[(vid, slot)] > grid_cap + 1e-5:
                    errors.append(f"Grid violated at {sid}/{vid}/{slot}")
            day_noise = noise[(vid, "day")]
            eve_noise = noise[(vid, "evening")]
            if day_noise > vrow["departure_noise_quota_day"] + 1e-5:
                errors.append(f"Day noise violated at {sid}/{vid}")
            if eve_noise > vrow["departure_noise_quota_evening"] + 1e-5:
                errors.append(f"Evening noise violated at {sid}/{vid}")

        corridors = sorted({route["corridor_id"] for route in data["routes"].values()})
        for corridor_id in corridors:
            base_cap = min(
                route["corridor_capacity_per_slot"]
                for route in data["routes"].values()
                if route["corridor_id"] == corridor_id
            )
            for slot in range(horizon):
                cap = base_cap
                for ctrl in data["corridor_controls"].get((sid, corridor_id), []):
                    if ctrl["start_slot"] <= slot < ctrl["end_slot"]:
                        cap = min(cap, ctrl["max_flights_per_slot"])
                if corridor[(corridor_id, slot)] > cap + 1e-5:
                    errors.append(f"Corridor violated at {sid}/{corridor_id}/{slot}")

        for res in data["reserve_requirements"]:
            if res["scenario_id"] != sid:
                continue
            count = 0
            for aid in data["aircraft"]:
                info = idle_info.get((aid, res["slot"]))
                if info and info["loc"] == res["vertiport_id"] and info["soc"] + TOL >= res["min_soc_kwh"]:
                    count += 1
            if count < res["min_idle_aircraft"]:
                errors.append(f"Reserve violated at {sid}/{res['vertiport_id']}/{res['slot']}")

        scenario_profit[sid] = acceptance_profit - op_cost - charge_cost - delay_cost

    shortfalls = [
        max(data["scenarios"][sid]["target_profit"] - scenario_profit[sid], 0.0)
        for sid in data["scenario_order"]
    ]
    probabilities = [data["scenarios"][sid]["probability"] for sid in data["scenario_order"]]
    cvar = cvar_value(shortfalls, probabilities, float(config["tail_risk_alpha"]))
    objective = sum(
        data["scenarios"][sid]["probability"] * scenario_profit[sid]
        for sid in data["scenario_order"]
    ) - float(config["tail_risk_weight"]) * cvar

    return {
        "feasible": not errors,
        "errors": errors,
        "scenario_profit": scenario_profit,
        "objective": objective,
        "cvar": cvar,
    }


def build_rows_from_overrides(data, start_overrides, time_limit):
    schedule, flight_energy_by_time, charge_targets = build_fixed_schedule(data, start_overrides)
    charge_result = optimize_schedule_charges(
        data,
        schedule,
        flight_energy_by_time,
        charge_targets,
        time_limit,
    )
    rows = export_rows(data, schedule, charge_result["charges"])
    audit = validate_solution(data, rows)
    return rows, charge_result, audit


def identify_delay_candidates(data, rows):
    accepted = {row["request_id"] for row in rows if row["row_type"] == "accept" and row["decision"] == "accept"}
    by_scenario_aircraft = defaultdict(list)
    current_start = {}
    for row in rows:
        if row["row_type"] != "flight":
            continue
        by_scenario_aircraft[(row["scenario_id"], row["aircraft_id"])].append(row)
        current_start[(row["scenario_id"], row["request_id"])] = int(row["start_slot"])

    candidates = []
    for rid in sorted(accepted):
        req = data["requests"][rid]
        if all(current_start[(sid, rid)] == req["earliest_slot"] for sid in data["scenario_order"]):
            continue
        if REQUEST_AIRCRAFT[rid] in {"A05", "A10"}:
            continue
        single_flight_aircraft = True
        for sid in data["scenario_order"]:
            actions = by_scenario_aircraft[(sid, REQUEST_AIRCRAFT[rid])]
            if len(actions) != 1 or actions[0]["request_id"] != rid:
                single_flight_aircraft = False
                break
        if single_flight_aircraft:
            candidates.append(rid)
    return candidates, current_start


def apply_start_overrides(data, rows, scenario_id, overrides):
    updated = []
    for row in rows:
        new_row = dict(row)
        if (
            row["row_type"] == "flight"
            and row["scenario_id"] == scenario_id
            and row["request_id"] in overrides
        ):
            rid = row["request_id"]
            start = overrides[rid]
            duration, energy = route_duration_energy(
                data,
                scenario_id,
                data["requests"][rid]["origin"],
                data["requests"][rid]["destination"],
            )
            new_row["start_slot"] = str(start)
            new_row["end_slot"] = str(start + duration)
            new_row["energy_kwh"] = fmt_num(energy)
        updated.append(new_row)
    return updated


def search_delay_neighborhood(data, incumbent_rows, incumbent_audit):
    candidate_rids, current_start = identify_delay_candidates(data, incumbent_rows)
    search_details = []
    if not candidate_rids:
        return incumbent_rows, search_details

    working_rows = [dict(row) for row in incumbent_rows]
    best_objective = incumbent_audit["objective"]

    for sid in data["scenario_order"]:
        scenario_candidates = [rid for rid in candidate_rids if (sid, rid) in current_start]
        if not scenario_candidates:
            continue

        ranges = {
            rid: range(
                data["requests"][rid]["earliest_slot"],
                data["requests"][rid]["latest_depart_slot"] + 1,
            )
            for rid in scenario_candidates
        }

        best_rows_for_scenario = working_rows
        best_obj_for_scenario = best_objective
        best_override = {rid: current_start[(sid, rid)] for rid in scenario_candidates}
        explored = 0

        if len(scenario_candidates) == 1:
            rid = scenario_candidates[0]
            for start in ranges[rid]:
                explored += 1
                trial_rows = apply_start_overrides(data, working_rows, sid, {rid: start})
                audit = validate_solution(data, trial_rows)
                if audit["feasible"] and audit["objective"] > best_obj_for_scenario + 1e-9:
                    best_obj_for_scenario = audit["objective"]
                    best_rows_for_scenario = trial_rows
                    best_override = {rid: start}
        elif len(scenario_candidates) == 2:
            rid1, rid2 = scenario_candidates
            for start1 in ranges[rid1]:
                for start2 in ranges[rid2]:
                    explored += 1
                    trial_rows = apply_start_overrides(
                        data,
                        working_rows,
                        sid,
                        {rid1: start1, rid2: start2},
                    )
                    audit = validate_solution(data, trial_rows)
                    if audit["feasible"] and audit["objective"] > best_obj_for_scenario + 1e-9:
                        best_obj_for_scenario = audit["objective"]
                        best_rows_for_scenario = trial_rows
                        best_override = {rid1: start1, rid2: start2}
        else:
            rid1, rid2, rid3 = scenario_candidates[:3]
            for start1 in ranges[rid1]:
                for start2 in ranges[rid2]:
                    for start3 in ranges[rid3]:
                        explored += 1
                        trial_rows = apply_start_overrides(
                            data,
                            working_rows,
                            sid,
                            {rid1: start1, rid2: start2, rid3: start3},
                        )
                        audit = validate_solution(data, trial_rows)
                        if audit["feasible"] and audit["objective"] > best_obj_for_scenario + 1e-9:
                            best_obj_for_scenario = audit["objective"]
                            best_rows_for_scenario = trial_rows
                            best_override = {rid1: start1, rid2: start2, rid3: start3}

        search_details.append(
            {
                "scenario_id": sid,
                "candidates": scenario_candidates,
                "explored": explored,
                "best_override": best_override,
                "best_objective": best_obj_for_scenario,
            }
        )
        working_rows = best_rows_for_scenario
        best_objective = best_obj_for_scenario

    return working_rows, search_details


def extract_start_overrides(rows):
    overrides = {}
    for row in rows:
        if row["row_type"] == "flight":
            overrides[(row["scenario_id"], row["request_id"])] = int(row["start_slot"])
    return overrides


def write_solution(rows):
    with open(SOLUTION_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_log(time_limit, incumbent_audit, candidate_audit, solver_result, search_details, published):
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Incumbent Audit",
        "",
        f"- Incumbent feasible: `{incumbent_audit['feasible']}`",
        f"- Incumbent objective: `{incumbent_audit['objective']:.8f}`",
        "",
        "## Performance Phase",
        "",
        f"- Mode: `GUARDED REPAIR-AND-PERFORMANCE`",
        f"- Bounded neighborhood search: exact enumeration over delayed single-flight requests",
        f"- Time limit used: `{time_limit}` seconds",
        f"- Charge LP status: `{solver_result['status']}`",
        f"- Charge LP dual bound: `{solver_result['dual_bound']:.8f}`",
        f"- Charge LP gap: `{solver_result['gap']:.8f}`",
        f"- Phase-I max normalized violation `theta`: `{solver_result['stage1_theta']:.12g}`",
        f"- Phase-I total normalized violation: `{solver_result['stage2_total_violation']:.12g}`",
        "",
        "## Candidate Audit",
        "",
        f"- Candidate feasible: `{candidate_audit['feasible']}`",
        f"- Candidate objective: `{candidate_audit['objective']:.8f}`",
        f"- Candidate CVaR tail term: `{candidate_audit['cvar']:.8f}`",
        f"- Published candidate: `{published}`",
    ]
    for detail in search_details:
        lines.append(
            f"- Neighborhood `{detail['scenario_id']}` explored `{detail['explored']}` combinations; best overrides `{detail['best_override']}`"
        )
    for sid, value in candidate_audit["scenario_profit"].items():
        lines.append(f"- Candidate scenario profit `{sid}`: `{value:.8f}`")
    if candidate_audit["errors"]:
        lines.append("")
        lines.append("## Audit Errors")
        lines.append("")
        for err in candidate_audit["errors"]:
            lines.append(f"- {err}")
    else:
        lines.append("- Public-data checks passed for acceptance, timing, energy, pads, parking, chargers, grid, corridors, noise, windows, reserves, and group spreads.")
    lines.append("")
    with open(LOG_PATH, "w") as f:
        f.write("\n".join(lines))


def main():
    data = load_data()
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    time_limit = min(DEFAULT_TIME_LIMIT_SECONDS, float(env_limit)) if env_limit else DEFAULT_TIME_LIMIT_SECONDS

    incumbent_rows = read_solution_rows(SOLUTION_PATH)
    if incumbent_rows is None:
        incumbent_rows, _, _ = build_rows_from_overrides(data, {}, time_limit)
    incumbent_audit = validate_solution(data, incumbent_rows)
    if not incumbent_audit["feasible"]:
        for err in incumbent_audit["errors"][:20]:
            print(f"INCUMBENT AUDIT ERROR: {err}")
        raise RuntimeError("Incumbent artifact is not feasible under the independent audit.")

    improved_rows_guess, search_details = search_delay_neighborhood(data, incumbent_rows, incumbent_audit)
    start_overrides = extract_start_overrides(improved_rows_guess)
    candidate_rows, solver_result, candidate_audit = build_rows_from_overrides(
        data,
        start_overrides,
        time_limit,
    )

    published = False
    if candidate_audit["feasible"] and candidate_audit["objective"] > incumbent_audit["objective"] + 1e-9:
        write_solution(candidate_rows)
        published = True

    final_rows = read_solution_rows(SOLUTION_PATH)
    final_audit = validate_solution(data, final_rows)

    write_log(time_limit, incumbent_audit, candidate_audit, solver_result, search_details, published)

    if not final_audit["feasible"]:
        for err in final_audit["errors"][:20]:
            print(f"AUDIT ERROR: {err}")
        raise RuntimeError("Final artifact failed the independent audit.")

    print(f"SCIP status: {solver_result['status']}")
    print(f"Phase-I theta: {solver_result['stage1_theta']:.12g}")
    print(f"Incumbent objective: {incumbent_audit['objective']:.8f}")
    print(f"Candidate objective: {candidate_audit['objective']:.8f}")
    print(f"Published: {published}")


if __name__ == "__main__":
    main()
