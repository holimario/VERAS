import copy
import csv
import json
import math
import os
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.json"

SCIP_GAP = 0.0005
MAX_RUN_SECONDS = 300.0
TOL = 1e-6


WINDOW_CHOICE = {
    ("SP01", "F01"): 0,
    ("SP01", "F25"): 0,
    ("SP01", "F02"): 0,
    ("SP01", "F03"): 0,
    ("SP01", "F05"): 0,
    ("SP01", "F04"): 0,
    ("SP01", "F06"): 1,
    ("SP01", "F07"): 1,
    ("SP01", "F08"): 1,
    ("SP04", "F26"): 0,
    ("SP04", "F27"): 0,
    ("SP04", "F34"): 0,
    ("SP04", "F35"): 0,
    ("SP04", "F36"): 0,
    ("SP04", "F22"): 0,
    ("SP04", "F23"): 0,
    ("SP04", "F24"): 0,
    ("SP02", "F09"): 0,
    ("SP02", "F28"): 0,
    ("SP02", "F10"): 0,
    ("SP02", "F11"): 0,
    ("SP02", "F12"): 0,
    ("SP02", "F29"): 1,
    ("SP02", "F30"): 1,
    ("SP02", "F13"): 1,
    ("SP02", "F14"): 0,
    ("SP02", "F15"): 0,
    ("SP02", "F16"): 0,
    ("SP03", "F17"): 0,
    ("SP03", "F18"): 0,
    ("SP03", "F31"): 0,
    ("SP03", "F32"): 0,
    ("SP03", "F19"): 0,
    ("SP03", "F20"): 0,
    ("SP03", "F21"): 0,
    ("SP03", "F33"): 0,
}

SYNC_TO_SPRAYER = {
    "SYNC_A1": "SP01",
    "SYNC_A2": "SP04",
    "SYNC_B1": "SP01",
    "SYNC_B2": "SP02",
    "SYNC_C1": "SP03",
    "SYNC_C2": "SP02",
}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def load_data():
    data = {"config": load_json(DATA_DIR / "config.json")}
    data["fields"] = {}
    for row in load_csv(DATA_DIR / "fields.csv"):
        row = dict(row)
        for key in (
            "lat",
            "lon",
            "area_ha",
            "terrain_factor",
            "min_l_per_ha",
            "ideal_l_per_ha",
            "max_l_per_ha",
            "earliest_min",
            "preferred_latest_min",
            "latest_min",
            "priority_weight",
        ):
            row[key] = float(row[key])
        row["steep"] = int(row["steep"])
        row["min_liters"] = row["area_ha"] * row["min_l_per_ha"]
        row["ideal_liters"] = row["area_ha"] * row["ideal_l_per_ha"]
        row["max_liters"] = row["area_ha"] * row["max_l_per_ha"]
        data["fields"][row["field_id"]] = row
    data["sprayers"] = {}
    for row in load_csv(DATA_DIR / "sprayers.csv"):
        row = dict(row)
        for key in (
            "tank_capacity_liters",
            "initial_liters",
            "shift_start_min",
            "shift_end_min",
            "speed_kmph",
            "base_ha_per_hour",
            "nozzle_lpm",
        ):
            row[key] = float(row[key])
        row["steep_allowed"] = int(row["steep_allowed"])
        data["sprayers"][row["sprayer_id"]] = row
    data["tenders"] = {}
    for row in load_csv(DATA_DIR / "tenders.csv"):
        row = dict(row)
        for key in (
            "capacity_liters",
            "shift_start_min",
            "shift_end_min",
            "speed_kmph",
            "refill_rate_lpm",
            "depot_reload_rate_lpm",
        ):
            row[key] = float(row[key])
        data["tenders"][row["tender_id"]] = row
    data["mixes"] = {}
    for row in load_csv(DATA_DIR / "mixes.csv"):
        row = dict(row)
        row["rinse_min"] = float(row["rinse_min"])
        data["mixes"][row["mix_id"]] = row
    data["pads"] = {}
    for row in load_csv(DATA_DIR / "pads.csv"):
        row = dict(row)
        row["lat"] = float(row["lat"])
        row["lon"] = float(row["lon"])
        row["water_available"] = int(row["water_available"])
        row["supported_mixes"] = row["supported_mixes"].split("|")
        data["pads"][row["pad_id"]] = row
    data["weather"] = {}
    for row in load_csv(DATA_DIR / "weather_windows.csv"):
        key = (row["weather_zone"], row["mix_id"])
        data["weather"].setdefault(key, []).append((float(row["open_min"]), float(row["close_min"])))
    data["sensitive"] = [(r["field_a"], r["field_b"], float(r["min_start_gap_min"])) for r in load_csv(DATA_DIR / "sensitive_separations.csv")]
    data["clusters"] = [(r["cluster_id"], r["field_ids"].split("|"), float(r["max_elapsed_min"])) for r in load_csv(DATA_DIR / "cluster_windows.csv")]
    data["blackouts"] = {}
    for row in load_csv(DATA_DIR / "pad_blackouts.csv"):
        data["blackouts"].setdefault(row["pad_id"], []).append((float(row["start_min"]), float(row["end_min"])))
    data["pad_capacity"] = {r["pad_id"]: int(r["simultaneous_services"]) for r in load_csv(DATA_DIR / "pad_capacity.csv")}
    return data


def time_limit():
    raw = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if raw is None:
        return MAX_RUN_SECONDS
    try:
        return min(float(raw), MAX_RUN_SECONDS)
    except ValueError:
        return MAX_RUN_SECONDS


def node_coords(data, node):
    if node == "DEPOT":
        return data["config"]["depot_lat"], data["config"]["depot_lon"]
    if node in data["fields"]:
        return data["fields"][node]["lat"], data["fields"][node]["lon"]
    return data["pads"][node]["lat"], data["pads"][node]["lon"]


def dist_km(data, a, b):
    lat1, lon1 = node_coords(data, a)
    lat2, lon2 = node_coords(data, b)
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    av = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2.0) ** 2
    return data["config"]["road_factor"] * 6371.0 * 2.0 * math.atan2(math.sqrt(av), math.sqrt(1.0 - av))


def sprayer_travel(data, sprayer_id, a, b):
    return 60.0 * dist_km(data, a, b) / data["sprayers"][sprayer_id]["speed_kmph"]


def tender_travel(data, tender_id, a, b):
    return 60.0 * dist_km(data, a, b) / data["tenders"][tender_id]["speed_kmph"]


def spray_service(data, sprayer_id, field_id, liters):
    sprayer = data["sprayers"][sprayer_id]
    field = data["fields"][field_id]
    area_term = 60.0 * field["area_ha"] * field["terrain_factor"] / sprayer["base_ha_per_hour"]
    nozzle_term = liters / sprayer["nozzle_lpm"]
    return data["config"]["spray_fixed_setup_min"] + max(area_term, nozzle_term)


def refill_service(data, tender_id, liters):
    return data["config"]["sync_setup_min"] + max(
        liters / data["config"]["sprayer_refill_rate_lpm"],
        liters / data["tenders"][tender_id]["refill_rate_lpm"],
    )


def reload_service(data, tender_id, liters):
    return liters / data["tenders"][tender_id]["depot_reload_rate_lpm"]


def rinse_service(data, from_mix, to_mix):
    if data["mixes"][from_mix]["residue_family"] == data["mixes"][to_mix]["residue_family"]:
        return 0.0
    return max(data["mixes"][from_mix]["rinse_min"], data["mixes"][to_mix]["rinse_min"])


def feasible_windows(data, sprayer_id, field_id, liters):
    field = data["fields"][field_id]
    dur = spray_service(data, sprayer_id, field_id, liters)
    out = []
    for open_min, close_min in data["weather"][(field["weather_zone"], field["mix_id"])]:
        lb = max(field["earliest_min"], open_min)
        ub = min(field["latest_min"], close_min) - dur
        if lb <= ub + TOL:
            out.append((lb, ub))
    return out


def round8(x):
    return round(float(x), 8)


def solve_prefix_liters(data, fields, target, tl):
    model = Model("liters")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/time", tl)
    model.setParam("limits/gap", SCIP_GAP)
    q = {}
    obj = 0.0
    for fid in fields:
        field = data["fields"][fid]
        q[fid] = model.addVar(lb=field["min_liters"], ub=field["ideal_liters"], name=f"q[{fid}]")
        obj += data["config"]["under_ideal_penalty_per_priority_liter"] * field["priority_weight"] * (field["ideal_liters"] - q[fid])
    model.addCons(quicksum(q.values()) == target)
    model.setObjective(obj, "minimize")
    model.optimize()
    if model.getNSols() == 0:
        raise RuntimeError("No solution for prefix liters LP.")
    sol = model.getBestSol()
    return {fid: model.getSolVal(sol, q[fid]) for fid in fields}


def build_liters(data, tl):
    liters = {fid: field["ideal_liters"] for fid, field in data["fields"].items()}
    liters.update(solve_prefix_liters(data, ["F01", "F25", "F02", "F03"], data["sprayers"]["SP01"]["initial_liters"], tl))
    liters.update(solve_prefix_liters(data, ["F09", "F28", "F10", "F11"], data["sprayers"]["SP02"]["initial_liters"], tl))
    return liters


def make_event(kind, event_id, **kwargs):
    ev = {"kind": kind, "event_id": event_id}
    ev.update(kwargs)
    ev["start"] = 0.0
    ev["arrival"] = 0.0
    ev["end"] = 0.0
    return ev


def event_node(ev):
    if ev["kind"] == "spray":
        return ev["field_id"]
    if ev["kind"] == "reload":
        return "DEPOT"
    return ev["pad_id"]


def build_routes(data, liters):
    sp01_a_refill = liters["F05"] + liters["F04"]
    sp04_prefix = liters["F26"] + liters["F27"] + liters["F34"] + liters["F35"]
    sp04_a_refill = liters["F36"] + liters["F22"] + liters["F23"] + liters["F24"] - (data["sprayers"]["SP04"]["initial_liters"] - sp04_prefix)
    sp01_b_refill = liters["F06"] + liters["F07"] + liters["F08"]
    sp02_b_refill = liters["F12"] + liters["F29"] + liters["F30"] + liters["F13"]
    sp03_prefix = liters["F17"] + liters["F18"] + liters["F31"] + liters["F32"]
    sp03_c_refill = liters["F19"] + liters["F20"] + liters["F21"] + liters["F33"] - (data["sprayers"]["SP03"]["initial_liters"] - sp03_prefix)
    sp02_c_refill = liters["F14"] + liters["F15"] + liters["F16"]

    sprayer = {
        "SP01": [
            make_event("spray", "S001", field_id="F01", mix_id="MIX_A", liters=liters["F01"]),
            make_event("spray", "S002", field_id="F25", mix_id="MIX_A", liters=liters["F25"]),
            make_event("spray", "S003", field_id="F02", mix_id="MIX_A", liters=liters["F02"]),
            make_event("spray", "S004", field_id="F03", mix_id="MIX_A", liters=liters["F03"]),
            make_event("rendezvous", "R001", sync_id="SYNC_A1", pad_id="P01", mix_id="MIX_A", liters=sp01_a_refill, tender_id="TN01"),
            make_event("spray", "S005", field_id="F05", mix_id="MIX_A", liters=liters["F05"]),
            make_event("spray", "S006", field_id="F04", mix_id="MIX_A", liters=liters["F04"]),
            make_event("rinse", "W001", pad_id="P02", from_mix_id="MIX_A", to_mix_id="MIX_B"),
            make_event("rendezvous", "R002", sync_id="SYNC_B1", pad_id="P02", mix_id="MIX_B", liters=sp01_b_refill, tender_id="TN02"),
            make_event("spray", "S007", field_id="F06", mix_id="MIX_B", liters=liters["F06"]),
            make_event("spray", "S008", field_id="F07", mix_id="MIX_B", liters=liters["F07"]),
            make_event("spray", "S009", field_id="F08", mix_id="MIX_B", liters=liters["F08"]),
        ],
        "SP04": [
            make_event("spray", "S001", field_id="F26", mix_id="MIX_A", liters=liters["F26"]),
            make_event("spray", "S002", field_id="F27", mix_id="MIX_A", liters=liters["F27"]),
            make_event("spray", "S003", field_id="F34", mix_id="MIX_A", liters=liters["F34"]),
            make_event("spray", "S004", field_id="F35", mix_id="MIX_A", liters=liters["F35"]),
            make_event("rendezvous", "R001", sync_id="SYNC_A2", pad_id="P09", mix_id="MIX_A", liters=sp04_a_refill, tender_id="TN01"),
            make_event("spray", "S005", field_id="F36", mix_id="MIX_A", liters=liters["F36"]),
            make_event("spray", "S006", field_id="F22", mix_id="MIX_A", liters=liters["F22"]),
            make_event("spray", "S007", field_id="F23", mix_id="MIX_A", liters=liters["F23"]),
            make_event("spray", "S008", field_id="F24", mix_id="MIX_A", liters=liters["F24"]),
        ],
        "SP02": [
            make_event("spray", "S001", field_id="F09", mix_id="MIX_B", liters=liters["F09"]),
            make_event("spray", "S002", field_id="F28", mix_id="MIX_B", liters=liters["F28"]),
            make_event("spray", "S003", field_id="F10", mix_id="MIX_B", liters=liters["F10"]),
            make_event("spray", "S004", field_id="F11", mix_id="MIX_B", liters=liters["F11"]),
            make_event("rendezvous", "R001", sync_id="SYNC_B2", pad_id="P04", mix_id="MIX_B", liters=sp02_b_refill, tender_id="TN02"),
            make_event("spray", "S005", field_id="F12", mix_id="MIX_B", liters=liters["F12"]),
            make_event("spray", "S006", field_id="F29", mix_id="MIX_B", liters=liters["F29"]),
            make_event("spray", "S007", field_id="F30", mix_id="MIX_B", liters=liters["F30"]),
            make_event("spray", "S008", field_id="F13", mix_id="MIX_B", liters=liters["F13"]),
            make_event("rinse", "W001", pad_id="P04", from_mix_id="MIX_B", to_mix_id="MIX_C"),
            make_event("rendezvous", "R002", sync_id="SYNC_C2", pad_id="P04", mix_id="MIX_C", liters=sp02_c_refill, tender_id="TN01"),
            make_event("spray", "S009", field_id="F14", mix_id="MIX_C", liters=liters["F14"]),
            make_event("spray", "S010", field_id="F15", mix_id="MIX_C", liters=liters["F15"]),
            make_event("spray", "S011", field_id="F16", mix_id="MIX_C", liters=liters["F16"]),
        ],
        "SP03": [
            make_event("spray", "S001", field_id="F17", mix_id="MIX_C", liters=liters["F17"]),
            make_event("spray", "S002", field_id="F18", mix_id="MIX_C", liters=liters["F18"]),
            make_event("spray", "S003", field_id="F31", mix_id="MIX_C", liters=liters["F31"]),
            make_event("spray", "S004", field_id="F32", mix_id="MIX_C", liters=liters["F32"]),
            make_event("rendezvous", "R001", sync_id="SYNC_C1", pad_id="P05", mix_id="MIX_C", liters=sp03_c_refill, tender_id="TN03"),
            make_event("spray", "S005", field_id="F19", mix_id="MIX_C", liters=liters["F19"]),
            make_event("spray", "S006", field_id="F20", mix_id="MIX_C", liters=liters["F20"]),
            make_event("spray", "S007", field_id="F21", mix_id="MIX_C", liters=liters["F21"]),
            make_event("spray", "S008", field_id="F33", mix_id="MIX_C", liters=liters["F33"]),
        ],
    }

    tender = {
        "TN01": [
            make_event("reload", "L001", mix_id="MIX_A", liters=sp01_a_refill + sp04_a_refill),
            make_event("rendezvous", "T001", sync_id="SYNC_A1", pad_id="P01", mix_id="MIX_A", liters=sp01_a_refill, sprayer_id="SP01"),
            make_event("rendezvous", "T002", sync_id="SYNC_A2", pad_id="P09", mix_id="MIX_A", liters=sp04_a_refill, sprayer_id="SP04"),
            make_event("reload", "L002", mix_id="MIX_C", liters=sp02_c_refill),
            make_event("rendezvous", "T003", sync_id="SYNC_C2", pad_id="P04", mix_id="MIX_C", liters=sp02_c_refill, sprayer_id="SP02"),
        ],
        "TN02": [
            make_event("reload", "L001", mix_id="MIX_B", liters=sp02_b_refill + sp01_b_refill),
            make_event("rendezvous", "T001", sync_id="SYNC_B2", pad_id="P04", mix_id="MIX_B", liters=sp02_b_refill, sprayer_id="SP02"),
            make_event("rendezvous", "T002", sync_id="SYNC_B1", pad_id="P02", mix_id="MIX_B", liters=sp01_b_refill, sprayer_id="SP01"),
        ],
        "TN03": [
            make_event("reload", "L001", mix_id="MIX_C", liters=sp03_c_refill),
            make_event("rendezvous", "T001", sync_id="SYNC_C1", pad_id="P05", mix_id="MIX_C", liters=sp03_c_refill, sprayer_id="SP03"),
        ],
    }

    for sid, route in sprayer.items():
        for ev in route:
            if ev["kind"] == "spray":
                ev["duration"] = spray_service(data, sid, ev["field_id"], ev["liters"])
            elif ev["kind"] == "rinse":
                ev["duration"] = rinse_service(data, ev["from_mix_id"], ev["to_mix_id"])
    for tid, route in tender.items():
        for ev in route:
            if ev["kind"] == "reload":
                ev["duration"] = reload_service(data, tid, ev["liters"])
    sync_to_tender = {}
    for tid, route in tender.items():
        for ev in route:
            if ev["kind"] == "rendezvous":
                sync_to_tender[ev["sync_id"]] = tid
    for sid, route in sprayer.items():
        for ev in route:
            if ev["kind"] == "rendezvous":
                ev["duration"] = refill_service(data, sync_to_tender[ev["sync_id"]], ev["liters"])
    for tid, route in tender.items():
        for ev in route:
            if ev["kind"] == "rendezvous":
                ev["duration"] = refill_service(data, tid, ev["liters"])
    return sprayer, tender


def schedule(data, sprayer, tender, tl):
    model = Model("schedule")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/time", tl)
    model.setParam("limits/gap", SCIP_GAP)
    start = {}
    late = {}

    def sname(vehicle_id, event_id):
        return f"{vehicle_id}:{event_id}"

    for sid, route in sprayer.items():
        prev = "DEPOT"
        prev_name = None
        prev_ev = None
        for ev in route:
            name = sname(sid, ev["event_id"])
            start[name] = model.addVar(lb=0.0, name=f"start[{name}]")
            if prev_name is None:
                model.addCons(start[name] >= data["sprayers"][sid]["shift_start_min"] + sprayer_travel(data, sid, "DEPOT", event_node(ev)))
            else:
                gap = 0.0
                if prev_ev["kind"] in {"rinse", "rendezvous"} and ev["kind"] in {"rinse", "rendezvous"} and event_node(prev_ev) == event_node(ev):
                    gap = data["config"]["pad_turnover_min"]
                model.addCons(start[name] >= start[prev_name] + prev_ev["duration"] + sprayer_travel(data, sid, prev, event_node(ev)) + gap)
            if ev["kind"] == "spray":
                lb, ub = feasible_windows(data, sid, ev["field_id"], ev["liters"])[WINDOW_CHOICE[(sid, ev["field_id"])]]
                model.addCons(start[name] >= lb)
                model.addCons(start[name] <= ub)
                late[name] = model.addVar(lb=0.0, name=f"late[{name}]")
                model.addCons(late[name] >= start[name] - data["fields"][ev["field_id"]]["preferred_latest_min"])
            model.addCons(start[name] + ev["duration"] <= data["sprayers"][sid]["shift_end_min"] + TOL)
            prev = event_node(ev)
            prev_name = name
            prev_ev = ev

    for tid, route in tender.items():
        prev = "DEPOT"
        prev_name = None
        prev_ev = None
        for ev in route:
            name = sname(tid, ev["event_id"])
            start[name] = model.addVar(lb=0.0, name=f"start[{name}]")
            if prev_name is None:
                model.addCons(start[name] >= data["tenders"][tid]["shift_start_min"])
            else:
                model.addCons(start[name] >= start[prev_name] + prev_ev["duration"] + tender_travel(data, tid, prev, event_node(ev)))
            model.addCons(start[name] + ev["duration"] <= data["tenders"][tid]["shift_end_min"] + TOL)
            prev = event_node(ev)
            prev_name = name
            prev_ev = ev

    sync_names = {}
    for sid, route in sprayer.items():
        for ev in route:
            if ev["kind"] == "rendezvous":
                sync_names.setdefault(ev["sync_id"], {})["sprayer"] = sname(sid, ev["event_id"])
    for tid, route in tender.items():
        for ev in route:
            if ev["kind"] == "rendezvous":
                sync_names.setdefault(ev["sync_id"], {})["tender"] = sname(tid, ev["event_id"])
    for sync_id, parts in sync_names.items():
        model.addCons(start[parts["sprayer"]] == start[parts["tender"]], name=f"sync_{sync_id}")

    sv = lambda sid, eid: start[sname(sid, eid)]
    model.addCons(sv("SP01", "S007") >= sv("SP01", "S005") + 44.0)
    model.addCons(sv("SP01", "S007") >= sv("SP01", "S004") + 38.0)
    model.addCons(sv("SP03", "S003") >= sv("SP04", "S001") + 45.0)
    model.addCons(sv("SP04", "S006") >= sv("SP03", "S005") + 41.0)
    model.addCons(sv("SP04", "S006") >= sv("SP03", "S007") + 47.0)
    model.addCons(sv("SP02", "S009") >= sv("SP02", "S008") + 52.0)
    model.addCons(sv("SP02", "S009") >= sv("SP02", "S007") + 48.0)
    model.addCons(sv("SP03", "S008") >= sv("SP04", "S003") + 42.0)

    model.addCons(sv("SP03", "R001") + next(ev["duration"] for ev in sprayer["SP03"] if ev["event_id"] == "R001") <= 690.0)
    model.addCons(sv("SP01", "W001") >= 730.0)
    model.addCons(sv("SP02", "R002") >= 806.0 + data["config"]["pad_turnover_min"])

    spray_lookup = {}
    for sid, route in sprayer.items():
        for ev in route:
            if ev["kind"] == "spray":
                spray_lookup[ev["field_id"]] = (sid, ev)
    for cluster_id, fields, max_elapsed in data["clusters"]:
        alpha = model.addVar(lb=0.0, name=f"alpha[{cluster_id}]")
        for fid in fields:
            sid, ev = spray_lookup[fid]
            model.addCons(alpha <= sv(sid, ev["event_id"]))
            model.addCons(sv(sid, ev["event_id"]) + ev["duration"] <= alpha + max_elapsed)

    model.setObjective(quicksum(late.values()) + 1e-5 * quicksum(start.values()), "minimize")
    model.optimize()
    if model.getNSols() == 0:
        raise RuntimeError(f"Scheduling model has no solution. Status: {model.getStatus()}")
    sol = model.getBestSol()

    for sid, route in sprayer.items():
        prev = "DEPOT"
        prev_end = data["sprayers"][sid]["shift_start_min"]
        for i, ev in enumerate(route):
            name = sname(sid, ev["event_id"])
            ev["start"] = model.getSolVal(sol, start[name])
            travel = sprayer_travel(data, sid, "DEPOT" if i == 0 else prev, event_node(ev))
            ev["arrival"] = (data["sprayers"][sid]["shift_start_min"] if i == 0 else prev_end) + travel
            ev["end"] = ev["start"] + ev["duration"]
            prev = event_node(ev)
            prev_end = ev["end"]
    for tid, route in tender.items():
        prev = "DEPOT"
        prev_end = data["tenders"][tid]["shift_start_min"]
        for i, ev in enumerate(route):
            name = sname(tid, ev["event_id"])
            ev["start"] = model.getSolVal(sol, start[name])
            if i == 0:
                ev["arrival"] = data["tenders"][tid]["shift_start_min"]
            else:
                ev["arrival"] = prev_end + tender_travel(data, tid, prev, event_node(ev))
            ev["end"] = ev["start"] + ev["duration"]
            prev = event_node(ev)
            prev_end = ev["end"]
    for sync_id, parts in sync_names.items():
        s_ev_name = parts["sprayer"].split(":")
        t_ev_name = parts["tender"].split(":")
        s_ev = next(ev for ev in sprayer[s_ev_name[0]] if ev["event_id"] == s_ev_name[1])
        t_ev = next(ev for ev in tender[t_ev_name[0]] if ev["event_id"] == t_ev_name[1])
        t_ev["start"] = s_ev["start"]
        t_ev["end"] = s_ev["end"]


def access_ok(vehicle_class, location_class):
    if vehicle_class == "all":
        return True
    return location_class in {"all", vehicle_class}


def validate(data, sprayer, tender):
    errors = []
    field_seen = set()
    spray_times = {}
    pad_intervals = []
    for sid, route in sprayer.items():
        sp = data["sprayers"][sid]
        mix = sp["initial_mix_id"]
        tank = sp["initial_liters"]
        prev = "DEPOT"
        prev_end = sp["shift_start_min"]
        for i, ev in enumerate(route):
            arrival = (sp["shift_start_min"] if i == 0 else prev_end) + sprayer_travel(data, sid, "DEPOT" if i == 0 else prev, event_node(ev))
            if abs(arrival - ev["arrival"]) > 1e-4:
                errors.append(f"{sid} arrival mismatch on {ev['event_id']}")
            if ev["arrival"] > ev["start"] + TOL:
                errors.append(f"{sid} starts before arrival on {ev['event_id']}")
            if ev["end"] > sp["shift_end_min"] + TOL:
                errors.append(f"{sid} exceeds shift on {ev['event_id']}")
            if ev["kind"] == "spray":
                field = data["fields"][ev["field_id"]]
                if ev["field_id"] in field_seen:
                    errors.append(f"duplicate spray {ev['field_id']}")
                field_seen.add(ev["field_id"])
                if mix != field["mix_id"]:
                    errors.append(f"mix mismatch on {ev['field_id']}")
                if field["steep"] and not sp["steep_allowed"]:
                    errors.append(f"steep incompatibility on {ev['field_id']}")
                if not access_ok(sp["access_class"], field["access_class"]):
                    errors.append(f"access incompatibility on {ev['field_id']}")
                if tank + TOL < ev["liters"]:
                    errors.append(f"tank shortage before {ev['field_id']}")
                tank -= ev["liters"]
                if ev["liters"] < field["min_liters"] - TOL or ev["liters"] > field["max_liters"] + TOL:
                    errors.append(f"liters out of bounds on {ev['field_id']}")
                windows = feasible_windows(data, sid, ev["field_id"], ev["liters"])
                if not any(lb - TOL <= ev["start"] <= ub + TOL for lb, ub in windows):
                    errors.append(f"window violation on {ev['field_id']}")
                spray_times[ev["field_id"]] = (ev["start"], ev["end"])
            elif ev["kind"] == "rinse":
                if not data["pads"][ev["pad_id"]]["water_available"]:
                    errors.append(f"rinse without water at {ev['pad_id']}")
                for b0, b1 in data["blackouts"].get(ev["pad_id"], []):
                    if not (ev["end"] <= b0 + TOL or ev["start"] >= b1 - TOL):
                        errors.append(f"blackout violation at {ev['pad_id']} on {ev['event_id']}")
                pad_intervals.append((ev["pad_id"], ev["start"], ev["end"], ev["event_id"]))
                mix = ev["to_mix_id"]
                tank = 0.0
            elif ev["kind"] == "rendezvous":
                pad = data["pads"][ev["pad_id"]]
                if ev["mix_id"] not in pad["supported_mixes"]:
                    errors.append(f"pad {ev['pad_id']} unsupported mix {ev['mix_id']}")
                for b0, b1 in data["blackouts"].get(ev["pad_id"], []):
                    if not (ev["end"] <= b0 + TOL or ev["start"] >= b1 - TOL):
                        errors.append(f"blackout violation at {ev['pad_id']} on {ev['event_id']}")
                if tank + ev["liters"] > sp["tank_capacity_liters"] + TOL:
                    errors.append(f"overfill on {sid} {ev['event_id']}")
                pad_intervals.append((ev["pad_id"], ev["start"], ev["end"], ev["sync_id"]))
                tank += ev["liters"]
            prev = event_node(ev)
            prev_end = ev["end"]

    if field_seen != set(data["fields"]):
        errors.append("not all fields sprayed")

    spr_sync = {}
    for route in sprayer.values():
        for ev in route:
            if ev["kind"] == "rendezvous":
                spr_sync[ev["sync_id"]] = ev

    for tid, route in tender.items():
        td = data["tenders"][tid]
        prev = "DEPOT"
        prev_end = td["shift_start_min"]
        onboard_mix = None
        onboard = 0.0
        for i, ev in enumerate(route):
            arrival = td["shift_start_min"] if i == 0 else prev_end + tender_travel(data, tid, prev, event_node(ev))
            if abs(arrival - ev["arrival"]) > 1e-4:
                errors.append(f"{tid} arrival mismatch on {ev['event_id']}")
            if ev["arrival"] > ev["start"] + TOL:
                errors.append(f"{tid} starts before arrival on {ev['event_id']}")
            if ev["end"] > td["shift_end_min"] + TOL:
                errors.append(f"{tid} exceeds shift on {ev['event_id']}")
            if ev["kind"] == "reload":
                if ev["liters"] > td["capacity_liters"] + TOL:
                    errors.append(f"{tid} reload exceeds capacity")
                onboard_mix = ev["mix_id"]
                onboard = ev["liters"]
            else:
                if onboard_mix != ev["mix_id"] or onboard + TOL < ev["liters"]:
                    errors.append(f"{tid} onboard mismatch on {ev['event_id']}")
                partner = spr_sync[ev["sync_id"]]
                if abs(partner["start"] - ev["start"]) > 1e-4 or abs(partner["end"] - ev["end"]) > 1e-4:
                    errors.append(f"sync timing mismatch on {ev['sync_id']}")
                if partner["pad_id"] != ev["pad_id"] or abs(partner["liters"] - ev["liters"]) > 1e-4:
                    errors.append(f"sync quantity mismatch on {ev['sync_id']}")
                onboard -= ev["liters"]
            prev = event_node(ev)
            prev_end = ev["end"]

    for a, b, gap in data["sensitive"]:
        if abs(spray_times[a][0] - spray_times[b][0]) + TOL < gap:
            errors.append(f"sensitive violation {a}-{b}")
    for cluster_id, fids, max_elapsed in data["clusters"]:
        if max(spray_times[f][1] for f in fids) - min(spray_times[f][0] for f in fids) > max_elapsed + TOL:
            errors.append(f"cluster violation {cluster_id}")
    by_pad = {}
    for pad_id, start, end, label in pad_intervals:
        by_pad.setdefault(pad_id, []).append((start, end, label))
    for pad_id, seq in by_pad.items():
        seq.sort()
        for i in range(1, len(seq)):
            prev_end = seq[i - 1][1]
            cur_start = seq[i][0]
            if cur_start < prev_end - TOL:
                errors.append(f"pad overlap at {pad_id}")
            if cur_start < prev_end + data["config"]["pad_turnover_min"] - TOL:
                errors.append(f"pad turnover violation at {pad_id}")

    obj = 0.0
    for sid, route in sprayer.items():
        prev = "DEPOT"
        for ev in route:
            obj += data["config"]["sprayer_travel_cost_per_km"] * dist_km(data, prev, event_node(ev))
            if ev["kind"] == "spray":
                field = data["fields"][ev["field_id"]]
                obj += data["config"]["spray_setup_cost_per_field"]
                obj += data["config"]["late_penalty_per_priority_min"] * field["priority_weight"] * max(0.0, ev["start"] - field["preferred_latest_min"])
                obj += data["config"]["under_ideal_penalty_per_priority_liter"] * field["priority_weight"] * max(0.0, field["ideal_liters"] - ev["liters"])
            if ev["kind"] == "rendezvous":
                obj += data["config"]["wait_cost_per_min"] * max(0.0, ev["start"] - ev["arrival"])
            prev = event_node(ev)
    for tid, route in tender.items():
        prev = "DEPOT"
        for ev in route:
            obj += data["config"]["tender_travel_cost_per_km"] * dist_km(data, prev, event_node(ev))
            if ev["kind"] == "reload":
                obj += data["config"]["tender_reload_fixed_cost"]
            if ev["kind"] == "rendezvous":
                obj += data["config"]["wait_cost_per_min"] * max(0.0, ev["start"] - ev["arrival"])
            prev = event_node(ev)
    return len(errors) == 0, errors, {"objective_public_score": obj, "violated_groups": len(errors), "max_residual": 0.0 if not errors else 1.0}


def export_solution(sprayer, tender):
    out = {"sprayer_routes": [], "tender_routes": []}
    for sid, route in sprayer.items():
        events = []
        for ev in route:
            if ev["kind"] == "spray":
                events.append({
                    "event_id": ev["event_id"],
                    "event_type": "spray",
                    "field_id": ev["field_id"],
                    "arrival_min": round8(ev["arrival"]),
                    "start_min": round8(ev["start"]),
                    "end_min": round8(ev["end"]),
                    "applied_liters": round8(ev["liters"]),
                })
            elif ev["kind"] == "rinse":
                events.append({
                    "event_id": ev["event_id"],
                    "event_type": "rinse",
                    "pad_id": ev["pad_id"],
                    "arrival_min": round8(ev["arrival"]),
                    "start_min": round8(ev["start"]),
                    "end_min": round8(ev["end"]),
                    "from_mix_id": ev["from_mix_id"],
                    "to_mix_id": ev["to_mix_id"],
                })
            else:
                events.append({
                    "event_id": ev["event_id"],
                    "event_type": "rendezvous",
                    "sync_id": ev["sync_id"],
                    "pad_id": ev["pad_id"],
                    "tender_id": ev["tender_id"],
                    "arrival_min": round8(ev["arrival"]),
                    "start_min": round8(ev["start"]),
                    "end_min": round8(ev["end"]),
                    "mix_id": ev["mix_id"],
                    "refill_liters": round8(ev["liters"]),
                })
        out["sprayer_routes"].append({"sprayer_id": sid, "events": events})
    for tid, route in tender.items():
        events = []
        for ev in route:
            if ev["kind"] == "reload":
                events.append({
                    "event_id": ev["event_id"],
                    "event_type": "reload",
                    "arrival_min": round8(ev["arrival"]),
                    "start_min": round8(ev["start"]),
                    "end_min": round8(ev["end"]),
                    "mix_id": ev["mix_id"],
                    "loaded_liters": round8(ev["liters"]),
                })
            else:
                events.append({
                    "event_id": ev["event_id"],
                    "event_type": "rendezvous",
                    "sync_id": ev["sync_id"],
                    "sprayer_id": ev["sprayer_id"],
                    "pad_id": ev["pad_id"],
                    "arrival_min": round8(ev["arrival"]),
                    "start_min": round8(ev["start"]),
                    "end_min": round8(ev["end"]),
                    "mix_id": ev["mix_id"],
                    "delivered_liters": round8(ev["liters"]),
                })
        out["tender_routes"].append({"tender_id": tid, "events": events})
    return out


def audit_solution_dict(data, solution):
    errors = []
    field_seen = set()
    spray_times = {}
    pad_intervals = []
    sync_s = {}
    sync_t = {}

    for route in solution["sprayer_routes"]:
        sid = route["sprayer_id"]
        sp = data["sprayers"][sid]
        prev = "DEPOT"
        prev_end = sp["shift_start_min"]
        mix = sp["initial_mix_id"]
        tank = sp["initial_liters"]
        for i, ev in enumerate(route["events"]):
            node = ev.get("field_id") or ev.get("pad_id")
            arrival_expected = (sp["shift_start_min"] if i == 0 else prev_end) + sprayer_travel(data, sid, "DEPOT" if i == 0 else prev, node)
            arrival = float(ev["arrival_min"])
            start = float(ev["start_min"])
            end = float(ev["end_min"])
            if abs(arrival_expected - arrival) > 1e-4:
                errors.append(f"{sid} arrival mismatch on {ev['event_id']}")
            if arrival > start + TOL:
                errors.append(f"{sid} starts before arrival on {ev['event_id']}")
            if end > sp["shift_end_min"] + TOL:
                errors.append(f"{sid} exceeds shift on {ev['event_id']}")

            if ev["event_type"] == "spray":
                field = data["fields"][ev["field_id"]]
                liters = float(ev["applied_liters"])
                if ev["field_id"] in field_seen:
                    errors.append(f"duplicate field {ev['field_id']}")
                field_seen.add(ev["field_id"])
                duration = spray_service(data, sid, ev["field_id"], liters)
                if abs(start + duration - end) > 1e-4:
                    errors.append(f"duration mismatch on {sid} {ev['field_id']}")
                if mix != field["mix_id"]:
                    errors.append(f"mix mismatch on {sid} {ev['field_id']}")
                if field["steep"] and not sp["steep_allowed"]:
                    errors.append(f"steep incompatibility on {sid} {ev['field_id']}")
                if not access_ok(sp["access_class"], field["access_class"]):
                    errors.append(f"access incompatibility on {sid} {ev['field_id']}")
                if liters < field["min_liters"] - TOL or liters > field["max_liters"] + TOL:
                    errors.append(f"liters out of bounds on {ev['field_id']}")
                if tank + TOL < liters:
                    errors.append(f"tank shortage on {sid} before {ev['field_id']}")
                tank -= liters
                if not any(lb - TOL <= start <= ub + TOL for lb, ub in feasible_windows(data, sid, ev["field_id"], liters)):
                    errors.append(f"window violation on {sid} {ev['field_id']}")
                spray_times[ev["field_id"]] = (start, end)
            elif ev["event_type"] == "rinse":
                duration = rinse_service(data, ev["from_mix_id"], ev["to_mix_id"])
                if abs(start + duration - end) > 1e-4:
                    errors.append(f"rinse duration mismatch on {sid} {ev['event_id']}")
                if not data["pads"][ev["pad_id"]]["water_available"]:
                    errors.append(f"rinse without water at {ev['pad_id']}")
                for b0, b1 in data["blackouts"].get(ev["pad_id"], []):
                    if not (end <= b0 + TOL or start >= b1 - TOL):
                        errors.append(f"blackout violation at {ev['pad_id']} on {ev['event_id']}")
                pad_intervals.append((ev["pad_id"], start, end, f"{sid}:{ev['event_id']}"))
                mix = ev["to_mix_id"]
                tank = 0.0
            else:
                liters = float(ev["refill_liters"])
                duration = refill_service(data, ev["tender_id"], liters)
                if abs(start + duration - end) > 1e-4:
                    errors.append(f"refill duration mismatch on {sid} {ev['event_id']}")
                if mix != ev["mix_id"]:
                    errors.append(f"refill mix mismatch on {sid} {ev['event_id']}")
                if tank + liters > sp["tank_capacity_liters"] + TOL:
                    errors.append(f"overfill on {sid} {ev['event_id']}")
                for b0, b1 in data["blackouts"].get(ev["pad_id"], []):
                    if not (end <= b0 + TOL or start >= b1 - TOL):
                        errors.append(f"blackout violation at {ev['pad_id']} on {ev['event_id']}")
                tank += liters
                pad_intervals.append((ev["pad_id"], start, end, ev["sync_id"]))
                sync_s[ev["sync_id"]] = ev
            prev = node
            prev_end = end

    if field_seen != set(data["fields"]):
        errors.append(f"missing fields {sorted(set(data['fields']) - field_seen)}")

    for route in solution["tender_routes"]:
        tid = route["tender_id"]
        td = data["tenders"][tid]
        prev = "DEPOT"
        prev_end = td["shift_start_min"]
        onboard_mix = None
        onboard = 0.0
        for i, ev in enumerate(route["events"]):
            node = ev.get("pad_id") or "DEPOT"
            arrival_expected = td["shift_start_min"] if i == 0 else prev_end + tender_travel(data, tid, prev, node)
            arrival = float(ev["arrival_min"])
            start = float(ev["start_min"])
            end = float(ev["end_min"])
            if abs(arrival_expected - arrival) > 1e-4:
                errors.append(f"{tid} arrival mismatch on {ev['event_id']}")
            if arrival > start + TOL:
                errors.append(f"{tid} starts before arrival on {ev['event_id']}")
            if end > td["shift_end_min"] + TOL:
                errors.append(f"{tid} exceeds shift on {ev['event_id']}")
            if ev["event_type"] == "reload":
                liters = float(ev["loaded_liters"])
                duration = reload_service(data, tid, liters)
                if abs(start + duration - end) > 1e-4:
                    errors.append(f"reload duration mismatch on {tid} {ev['event_id']}")
                if liters > td["capacity_liters"] + TOL:
                    errors.append(f"reload exceeds capacity on {tid} {ev['event_id']}")
                onboard_mix = ev["mix_id"]
                onboard = liters
            else:
                liters = float(ev["delivered_liters"])
                duration = refill_service(data, tid, liters)
                if abs(start + duration - end) > 1e-4:
                    errors.append(f"tender refill duration mismatch on {tid} {ev['event_id']}")
                if onboard_mix != ev["mix_id"] or onboard + TOL < liters:
                    errors.append(f"tender inventory mismatch on {tid} {ev['event_id']}")
                onboard -= liters
                sync_t[ev["sync_id"]] = ev
            prev = node
            prev_end = end

    for sync_id, spr_ev in sync_s.items():
        ten_ev = sync_t.get(sync_id)
        if ten_ev is None:
            errors.append(f"missing tender sync {sync_id}")
            continue
        if spr_ev["pad_id"] != ten_ev["pad_id"] or spr_ev["mix_id"] != ten_ev["mix_id"]:
            errors.append(f"sync attribute mismatch on {sync_id}")
        if abs(float(spr_ev["refill_liters"]) - float(ten_ev["delivered_liters"])) > 1e-4:
            errors.append(f"sync liters mismatch on {sync_id}")
        if abs(float(spr_ev["start_min"]) - float(ten_ev["start_min"])) > 1e-4 or abs(float(spr_ev["end_min"]) - float(ten_ev["end_min"])) > 1e-4:
            errors.append(f"sync timing mismatch on {sync_id}")

    for field_a, field_b, gap in data["sensitive"]:
        if abs(spray_times[field_a][0] - spray_times[field_b][0]) + TOL < gap:
            errors.append(f"sensitive violation {field_a}-{field_b}")

    for cluster_id, field_ids, max_elapsed in data["clusters"]:
        first_start = min(spray_times[field_id][0] for field_id in field_ids)
        last_end = max(spray_times[field_id][1] for field_id in field_ids)
        if last_end - first_start > max_elapsed + TOL:
            errors.append(f"cluster violation {cluster_id}")

    by_pad = {}
    for pad_id, start, end, label in pad_intervals:
        by_pad.setdefault(pad_id, []).append((start, end, label))
    for pad_id, seq in by_pad.items():
        seq.sort()
        for i in range(1, len(seq)):
            prev_end = seq[i - 1][1]
            cur_start = seq[i][0]
            if cur_start < prev_end - TOL:
                errors.append(f"pad overlap at {pad_id}")
            if cur_start < prev_end + data["config"]["pad_turnover_min"] - TOL:
                errors.append(f"pad turnover violation at {pad_id}")

    objective = 0.0
    for route in solution["sprayer_routes"]:
        prev = "DEPOT"
        for ev in route["events"]:
            node = ev.get("field_id") or ev.get("pad_id")
            objective += data["config"]["sprayer_travel_cost_per_km"] * dist_km(data, prev, node)
            if ev["event_type"] == "spray":
                field = data["fields"][ev["field_id"]]
                objective += data["config"]["spray_setup_cost_per_field"]
                objective += data["config"]["late_penalty_per_priority_min"] * field["priority_weight"] * max(0.0, float(ev["start_min"]) - field["preferred_latest_min"])
                objective += data["config"]["under_ideal_penalty_per_priority_liter"] * field["priority_weight"] * max(0.0, field["ideal_liters"] - float(ev["applied_liters"]))
            elif ev["event_type"] == "rendezvous":
                objective += data["config"]["wait_cost_per_min"] * max(0.0, float(ev["start_min"]) - float(ev["arrival_min"]))
            prev = node
    for route in solution["tender_routes"]:
        prev = "DEPOT"
        for ev in route["events"]:
            node = ev.get("pad_id") or "DEPOT"
            objective += data["config"]["tender_travel_cost_per_km"] * dist_km(data, prev, node)
            if ev["event_type"] == "reload":
                objective += data["config"]["tender_reload_fixed_cost"]
            elif ev["event_type"] == "rendezvous":
                objective += data["config"]["wait_cost_per_min"] * max(0.0, float(ev["start_min"]) - float(ev["arrival_min"]))
            prev = node

    return len(errors) == 0, errors, {
        "objective_public_score": objective,
        "violated_groups": len(errors),
        "max_residual": 0.0 if not errors else 1.0,
    }


def optimize_exported_timing(data, solution, warm_start_solution, time_limit_seconds):
    candidate = copy.deepcopy(solution)
    model = Model("timing_improvement")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/time", min(60.0, time_limit_seconds))
    model.setParam("limits/gap", SCIP_GAP)

    def ev_node(ev):
        return ev.get("field_id") or ev.get("pad_id") or "DEPOT"

    def ev_duration(owner_id, ev):
        if ev["event_type"] == "spray":
            return spray_service(data, owner_id, ev["field_id"], float(ev["applied_liters"]))
        if ev["event_type"] == "rinse":
            return rinse_service(data, ev["from_mix_id"], ev["to_mix_id"])
        if ev["event_type"] == "reload":
            return reload_service(data, owner_id, float(ev["loaded_liters"]))
        if owner_id.startswith("TN"):
            return refill_service(data, owner_id, float(ev["delivered_liters"]))
        return refill_service(data, ev["tender_id"], float(ev["refill_liters"]))

    chosen_windows = {}
    for route in candidate["sprayer_routes"]:
        sid = route["sprayer_id"]
        for ev in route["events"]:
            if ev["event_type"] == "spray":
                wins = feasible_windows(data, sid, ev["field_id"], float(ev["applied_liters"]))
                start0 = float(ev["start_min"])
                for idx, (lb, ub) in enumerate(wins):
                    if lb - TOL <= start0 <= ub + TOL:
                        chosen_windows[(sid, ev["event_id"])] = idx
                        break

    incumbent_times = {}
    if warm_start_solution is not None:
        for route in warm_start_solution.get("sprayer_routes", []):
            incumbent_times[(route["sprayer_id"], route["events"][0]["event_id"])] = None
            for ev in route["events"]:
                incumbent_times[(route["sprayer_id"], ev["event_id"])] = float(ev["start_min"])
        for route in warm_start_solution.get("tender_routes", []):
            for ev in route["events"]:
                incumbent_times[(route["tender_id"], ev["event_id"])] = float(ev["start_min"])

    start_vars = {}
    late_vars = {}
    wait_vars = {}
    warm = model.createSol()

    for route in candidate["sprayer_routes"]:
        sid = route["sprayer_id"]
        shift_start = data["sprayers"][sid]["shift_start_min"]
        prev_ev = None
        for i, ev in enumerate(route["events"]):
            key = (sid, ev["event_id"])
            start_vars[key] = model.addVar(lb=0.0, name=f"start[{sid},{ev['event_id']}]")
            model.setSolVal(warm, start_vars[key], incumbent_times.get(key, float(ev["start_min"])))
            if i == 0:
                arrival_expr = shift_start + sprayer_travel(data, sid, "DEPOT", ev_node(ev))
            else:
                arrival_expr = start_vars[(sid, prev_ev["event_id"])] + ev_duration(sid, prev_ev) + sprayer_travel(data, sid, ev_node(prev_ev), ev_node(ev))
            model.addCons(start_vars[key] >= arrival_expr, name=f"seq_{sid}_{ev['event_id']}")
            model.addCons(start_vars[key] + ev_duration(sid, ev) <= data["sprayers"][sid]["shift_end_min"] + TOL, name=f"shift_{sid}_{ev['event_id']}")
            if ev["event_type"] == "spray":
                lb, ub = feasible_windows(data, sid, ev["field_id"], float(ev["applied_liters"]))[chosen_windows[(sid, ev["event_id"])]]
                model.addCons(start_vars[key] >= lb, name=f"lb_{sid}_{ev['event_id']}")
                model.addCons(start_vars[key] <= ub, name=f"ub_{sid}_{ev['event_id']}")
                late_vars[key] = model.addVar(lb=0.0, name=f"late[{sid},{ev['event_id']}]")
                model.addCons(late_vars[key] >= start_vars[key] - data["fields"][ev["field_id"]]["preferred_latest_min"], name=f"late_def_{sid}_{ev['event_id']}")
            elif ev["event_type"] in {"rinse", "rendezvous"}:
                duration = ev_duration(sid, ev)
                for b0, b1 in data["blackouts"].get(ev["pad_id"], []):
                    if float(ev["end_min"]) <= b0 + TOL:
                        model.addCons(start_vars[key] + duration <= b0, name=f"blk_before_{sid}_{ev['event_id']}_{int(b0)}")
                    elif float(ev["start_min"]) >= b1 - TOL:
                        model.addCons(start_vars[key] >= b1, name=f"blk_after_{sid}_{ev['event_id']}_{int(b1)}")
            if ev["event_type"] == "rendezvous":
                wait_vars[key] = model.addVar(lb=0.0, name=f"wait[{sid},{ev['event_id']}]")
                model.addCons(wait_vars[key] >= start_vars[key] - arrival_expr, name=f"wait_def_{sid}_{ev['event_id']}")
            prev_ev = ev

    for route in candidate["tender_routes"]:
        tid = route["tender_id"]
        shift_start = data["tenders"][tid]["shift_start_min"]
        prev_ev = None
        for i, ev in enumerate(route["events"]):
            key = (tid, ev["event_id"])
            start_vars[key] = model.addVar(lb=0.0, name=f"start[{tid},{ev['event_id']}]")
            model.setSolVal(warm, start_vars[key], incumbent_times.get(key, float(ev["start_min"])))
            if i == 0:
                arrival_expr = shift_start
            else:
                arrival_expr = start_vars[(tid, prev_ev["event_id"])] + ev_duration(tid, prev_ev) + tender_travel(data, tid, ev_node(prev_ev), ev_node(ev))
            model.addCons(start_vars[key] >= arrival_expr, name=f"seq_{tid}_{ev['event_id']}")
            model.addCons(start_vars[key] + ev_duration(tid, ev) <= data["tenders"][tid]["shift_end_min"] + TOL, name=f"shift_{tid}_{ev['event_id']}")
            if ev["event_type"] == "rendezvous":
                wait_vars[key] = model.addVar(lb=0.0, name=f"wait[{tid},{ev['event_id']}]")
                model.addCons(wait_vars[key] >= start_vars[key] - arrival_expr, name=f"wait_def_{tid}_{ev['event_id']}")
            prev_ev = ev

    sync_s = {}
    sync_t = {}
    for route in candidate["sprayer_routes"]:
        sid = route["sprayer_id"]
        for ev in route["events"]:
            if ev["event_type"] == "rendezvous":
                sync_s[ev["sync_id"]] = (sid, ev["event_id"])
    for route in candidate["tender_routes"]:
        tid = route["tender_id"]
        for ev in route["events"]:
            if ev["event_type"] == "rendezvous":
                sync_t[ev["sync_id"]] = (tid, ev["event_id"])
    for sync_id in sync_s:
        model.addCons(start_vars[sync_s[sync_id]] == start_vars[sync_t[sync_id]], name=f"sync_{sync_id}")

    field_to_key = {}
    field_event = {}
    for route in candidate["sprayer_routes"]:
        sid = route["sprayer_id"]
        for ev in route["events"]:
            if ev["event_type"] == "spray":
                field_to_key[ev["field_id"]] = (sid, ev["event_id"])
                field_event[ev["field_id"]] = ev
    current_starts = {ev["field_id"]: float(ev["start_min"]) for route in candidate["sprayer_routes"] for ev in route["events"] if ev["event_type"] == "spray"}
    for field_a, field_b, gap in data["sensitive"]:
        key_a = field_to_key[field_a]
        key_b = field_to_key[field_b]
        if current_starts[field_a] <= current_starts[field_b]:
            model.addCons(start_vars[key_b] >= start_vars[key_a] + gap, name=f"sens_{field_a}_{field_b}")
        else:
            model.addCons(start_vars[key_a] >= start_vars[key_b] + gap, name=f"sens_{field_b}_{field_a}")

    for cluster_id, field_ids, max_elapsed in data["clusters"]:
        alpha = model.addVar(lb=0.0, name=f"alpha[{cluster_id}]")
        for field_id in field_ids:
            key = field_to_key[field_id]
            ev = field_event[field_id]
            model.addCons(alpha <= start_vars[key], name=f"alpha_lb_{cluster_id}_{field_id}")
            model.addCons(start_vars[key] + ev_duration(key[0], ev) <= alpha + max_elapsed, name=f"cluster_{cluster_id}_{field_id}")

    by_pad = {}
    for route in candidate["sprayer_routes"]:
        sid = route["sprayer_id"]
        for ev in route["events"]:
            if ev["event_type"] in {"rinse", "rendezvous"}:
                by_pad.setdefault(ev["pad_id"], []).append((float(ev["start_min"]), (sid, ev["event_id"]), ev_duration(sid, ev)))
    for pad_id, seq in by_pad.items():
        seq.sort()
        for i in range(1, len(seq)):
            _, key_prev, dur_prev = seq[i - 1]
            _, key_cur, _ = seq[i]
            model.addCons(start_vars[key_cur] >= start_vars[key_prev] + dur_prev + data["config"]["pad_turnover_min"], name=f"pad_{pad_id}_{i}")

    objective = quicksum(
        data["config"]["late_penalty_per_priority_min"] * data["fields"][field_id]["priority_weight"] * late_vars[field_to_key[field_id]]
        for field_id in field_to_key
    )
    objective += quicksum(data["config"]["wait_cost_per_min"] * var for var in wait_vars.values())
    objective += 1e-6 * quicksum(start_vars.values())
    model.setObjective(objective, "minimize")
    model.addSol(warm)
    model.optimize()
    meta = {"status": str(model.getStatus()), "has_solution": model.getNSols() > 0}
    if model.getNSols() == 0:
        return solution, meta

    sol = model.getBestSol()
    meta["lp_obj"] = model.getObjVal()
    try:
        meta["dualbound"] = model.getDualbound()
    except Exception:
        meta["dualbound"] = model.getObjVal()
    try:
        meta["gap"] = model.getGap()
    except Exception:
        meta["gap"] = 0.0

    for route in candidate["sprayer_routes"]:
        sid = route["sprayer_id"]
        prev = "DEPOT"
        prev_end = data["sprayers"][sid]["shift_start_min"]
        for i, ev in enumerate(route["events"]):
            key = (sid, ev["event_id"])
            ev["start_min"] = round(float(model.getSolVal(sol, start_vars[key])), 8)
            arrival = (data["sprayers"][sid]["shift_start_min"] if i == 0 else prev_end) + sprayer_travel(data, sid, "DEPOT" if i == 0 else prev, ev_node(ev))
            ev["arrival_min"] = round(float(arrival), 8)
            ev["end_min"] = round(float(ev["start_min"] + ev_duration(sid, ev)), 8)
            prev = ev_node(ev)
            prev_end = ev["end_min"]

    for route in candidate["tender_routes"]:
        tid = route["tender_id"]
        prev = "DEPOT"
        prev_end = data["tenders"][tid]["shift_start_min"]
        for i, ev in enumerate(route["events"]):
            key = (tid, ev["event_id"])
            ev["start_min"] = round(float(model.getSolVal(sol, start_vars[key])), 8)
            arrival = data["tenders"][tid]["shift_start_min"] if i == 0 else prev_end + tender_travel(data, tid, prev, ev_node(ev))
            ev["arrival_min"] = round(float(arrival), 8)
            ev["end_min"] = round(float(ev["start_min"] + ev_duration(tid, ev)), 8)
            prev = ev_node(ev)
            prev_end = ev["end_min"]

    tender_rendezvous = {}
    for route in candidate["tender_routes"]:
        for ev in route["events"]:
            if ev["event_type"] == "rendezvous":
                tender_rendezvous[ev["sync_id"]] = ev
    for route in candidate["sprayer_routes"]:
        for ev in route["events"]:
            if ev["event_type"] == "rendezvous":
                t_ev = tender_rendezvous[ev["sync_id"]]
                t_ev["start_min"] = ev["start_min"]
                t_ev["end_min"] = ev["end_min"]

    return candidate, meta


def write_solve_log(lines):
    with open(SUBMISSIONS_DIR / "solve_log.md", "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    tl = time_limit()
    data = load_data()
    log_lines = [
        "# Solve Log",
        "",
        "Command",
        "",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        f"- time limit used: `{tl}` seconds",
        "",
    ]

    incumbent_solution = None
    incumbent_ok = False
    incumbent_errors = []
    incumbent_metrics = None
    if SOLUTION_PATH.exists():
        incumbent_solution = load_json(SOLUTION_PATH)
        incumbent_ok, incumbent_errors, incumbent_metrics = audit_solution_dict(data, incumbent_solution)
        log_lines.extend([
            "Incumbent Audit",
            "",
            f"- exists: `True`",
            f"- feasible: `{incumbent_ok}`",
            f"- objective: `{incumbent_metrics['objective_public_score'] if incumbent_metrics else 'n/a'}`",
            f"- violations: `{len(incumbent_errors)}`",
            "",
        ])
    else:
        log_lines.extend([
            "Incumbent Audit",
            "",
            "- exists: `False`",
            "",
        ])

    liters = build_liters(data, tl)
    sprayer, tender = build_routes(data, liters)
    schedule(data, sprayer, tender, tl)
    baseline_solution = export_solution(sprayer, tender)
    baseline_ok, baseline_errors, baseline_metrics = audit_solution_dict(data, baseline_solution)
    if not baseline_ok:
        raise RuntimeError("Baseline candidate failed independent audit:\n" + "\n".join(baseline_errors))

    improved_solution, improve_meta = optimize_exported_timing(data, baseline_solution, incumbent_solution or baseline_solution, tl)
    candidate_ok, candidate_errors, candidate_metrics = audit_solution_dict(data, improved_solution)
    if not candidate_ok:
        raise RuntimeError("Improved candidate failed independent audit:\n" + "\n".join(candidate_errors))

    log_lines.extend([
        "Candidate Audit",
        "",
        f"- baseline objective: `{baseline_metrics['objective_public_score']}`",
        f"- improved objective: `{candidate_metrics['objective_public_score']}`",
        f"- feasible: `{candidate_ok}`",
        f"- violations: `{len(candidate_errors)}`",
        "",
        "Improvement Solve",
        "",
        f"- solver status: `{improve_meta.get('status')}`",
        f"- incumbent count: `{1 if improve_meta.get('has_solution') else 0}`",
        f"- objective (timing LP): `{improve_meta.get('lp_obj', 'n/a')}`",
        f"- bound: `{improve_meta.get('dualbound', 'n/a')}`",
        f"- gap: `{improve_meta.get('gap', 'n/a')}`",
        "",
    ])

    publish_candidate = (not incumbent_ok) or (candidate_metrics["objective_public_score"] < incumbent_metrics["objective_public_score"] - 1e-6)
    if publish_candidate:
        with open(SOLUTION_PATH, "w") as f:
            json.dump(improved_solution, f, indent=2)
        final_solution = improved_solution
        final_metrics = candidate_metrics
        log_lines.extend([
            "Publish Decision",
            "",
            "- result: `candidate published`",
            f"- final objective: `{final_metrics['objective_public_score']}`",
            "",
        ])
    else:
        final_solution = incumbent_solution
        final_metrics = incumbent_metrics
        log_lines.extend([
            "Publish Decision",
            "",
            "- result: `incumbent kept`",
            f"- incumbent objective: `{incumbent_metrics['objective_public_score']}`",
            f"- candidate objective: `{candidate_metrics['objective_public_score']}`",
            "",
        ])

    write_solve_log(log_lines)
    print(json.dumps(final_metrics, indent=2))


if __name__ == "__main__":
    main()
