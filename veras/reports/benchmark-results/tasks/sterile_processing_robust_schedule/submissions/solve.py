import csv
import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUB_DIR = ROOT / "submissions"
SOLUTION_PATH = SUB_DIR / "solution.csv"
MODEL_PATH = SUB_DIR / "model.md"
LOG_PATH = SUB_DIR / "solve_log.md"


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def ceil_int(value: float) -> int:
    return int(math.ceil(value - 1e-12))


def floor_int(value: float) -> int:
    return int(math.floor(value + 1e-12))


def format_num(value):
    if value == "" or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rounded = round(value, 8)
        if abs(rounded - round(rounded)) <= 1e-8:
            return str(int(round(rounded)))
        text = f"{rounded:.8f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(value)


@dataclass
class Candidate:
    case_id: str
    room_id: str
    start: int
    occ_end: int
    surg_end: int
    delay: int
    latest_dep: int


@dataclass
class DirtyUnit:
    tray_type: str
    source_case_id: str
    available_time: int


@dataclass
class SterileUnit:
    tray_type: str
    ready_time: int
    source: str
    batch_id: str = ""
    loaner_vendor: str = ""


@dataclass
class BatchJob:
    scenario_id: str
    batch_id: str
    tray_type: str
    quantity: int
    source_case_id: str
    washer_id: str
    wash_start: int
    assembly_station: str
    assembly_start: int
    sterilizer_id: str
    sterilizer_start: int
    ready_time: int
    weight: float


class Data:
    def __init__(self):
        self.config = json.loads((DATA_DIR / "config.json").read_text())
        self.cases = {}
        for row in read_csv(DATA_DIR / "cases.csv"):
            row["priority"] = int(row["priority"])
            row["release_min"] = int(row["release_min"])
            row["target_start_min"] = int(row["target_start_min"])
            row["latest_start_min"] = int(row["latest_start_min"])
            row["duration_min"] = int(row["duration_min"])
            row["turnover_min"] = int(row["turnover_min"])
            row["mandatory"] = int(row["mandatory"])
            row["robot_required"] = int(row["robot_required"])
            row["implant_case"] = int(row["implant_case"])
            row["cancel_penalty"] = float(row["cancel_penalty"])
            row["delay_penalty_per_min"] = float(row["delay_penalty_per_min"])
            self.cases[row["case_id"]] = row

        self.rooms = {}
        for row in read_csv(DATA_DIR / "rooms.csv"):
            row["open_min"] = int(row["open_min"])
            row["close_min"] = int(row["close_min"])
            row["turnover_buffer_min"] = int(row["turnover_buffer_min"])
            row["robot_ready"] = int(row["robot_ready"])
            row["implant_storage"] = int(row["implant_storage"])
            self.rooms[row["room_id"]] = row

        self.trays = {}
        for row in read_csv(DATA_DIR / "tray_types.csv"):
            for key in [
                "owned_sets",
                "initial_sterile_sets",
                "initial_dirty_sets",
                "wash_minutes",
                "assembly_minutes",
                "dry_minutes",
                "inspection_minutes",
                "loaner_allowed",
                "emergency_allowed",
                "implant_sensitive",
                "robot_pack_eligible",
            ]:
                row[key] = int(row[key])
            for key in ["weight_kg", "collision_risk"]:
                row[key] = float(row[key])
            self.trays[row["tray_type"]] = row

        self.requirements = defaultdict(dict)
        self.total_case_trays = defaultdict(int)
        for row in read_csv(DATA_DIR / "case_tray_requirements.csv"):
            qty = int(row["quantity"])
            self.requirements[row["case_id"]][row["tray_type"]] = qty
            self.total_case_trays[row["case_id"]] += qty

        self.washers = {}
        for row in read_csv(DATA_DIR / "washers.csv"):
            row["open_min"] = int(row["open_min"])
            row["close_min"] = int(row["close_min"])
            row["capacity_kg"] = float(row["capacity_kg"])
            row["cycle_minutes"] = int(row["cycle_minutes"])
            row["supports_heavy"] = int(row["supports_heavy"])
            self.washers[row["washer_id"]] = row

        self.sterilizers = {}
        for row in read_csv(DATA_DIR / "sterilizers.csv"):
            row["open_min"] = int(row["open_min"])
            row["close_min"] = int(row["close_min"])
            row["capacity_kg"] = float(row["capacity_kg"])
            row["cycle_minutes"] = int(row["cycle_minutes"])
            row["implant_quarantine_min"] = int(row["implant_quarantine_min"])
            row["wet_pack_sensitive"] = int(row["wet_pack_sensitive"])
            self.sterilizers[row["sterilizer_id"]] = row

        self.stations = {}
        for row in read_csv(DATA_DIR / "assembly_stations.csv"):
            row["open_min"] = int(row["open_min"])
            row["close_min"] = int(row["close_min"])
            row["parallel_techs"] = int(row["parallel_techs"])
            row["robot_cell"] = int(row["robot_cell"])
            row["heavy_tray_daily_limit"] = int(row["heavy_tray_daily_limit"])
            self.stations[row["station_id"]] = row

        self.scenarios = {}
        for row in read_csv(DATA_DIR / "scenarios.csv"):
            row["probability"] = float(row["probability"])
            row["wash_multiplier"] = float(row["wash_multiplier"])
            row["assembly_multiplier"] = float(row["assembly_multiplier"])
            row["sterilizer_multiplier"] = float(row["sterilizer_multiplier"])
            row["initial_sterile_multiplier"] = float(row["initial_sterile_multiplier"])
            row["loaner_delay_min"] = int(row["loaner_delay_min"])
            row["max_emergency_cycles"] = int(row["max_emergency_cycles"])
            row["wet_pack_risk_multiplier"] = float(row["wet_pack_risk_multiplier"])
            row["staff_capacity_factor"] = float(row["staff_capacity_factor"])
            self.scenarios[row["scenario_id"]] = row

        self.vendors_by_tray = defaultdict(list)
        self.vendor_map = {}
        for row in read_csv(DATA_DIR / "loaner_vendors.csv"):
            row["arrival_min"] = int(row["arrival_min"])
            row["max_sets"] = int(row["max_sets"])
            row["checkin_minutes_per_set"] = int(row["checkin_minutes_per_set"])
            row["cost_per_set"] = float(row["cost_per_set"])
            row["credentialed_implant"] = int(row["credentialed_implant"])
            self.vendors_by_tray[row["tray_type"]].append(row)
            self.vendor_map[(row["vendor_id"], row["tray_type"])] = row

        self.specialty_rules = {}
        for row in read_csv(DATA_DIR / "specialty_rules.csv"):
            row["allowed_service_lines"] = [x.strip() for x in row["allowed_service_lines"].split(",")]
            row["requires_bi_indicator"] = int(row["requires_bi_indicator"])
            row["max_emergency_cases"] = int(row["max_emergency_cases"])
            row["case_mix_weight"] = float(row["case_mix_weight"])
            self.specialty_rules[row["specialty"]] = row

        self.routes = {}
        for row in read_csv(DATA_DIR / "cart_delivery_routes.csv"):
            for key in [
                "travel_minutes",
                "cart_load_minutes",
                "room_settle_minutes",
                "handling_cost_per_tray",
            ]:
                row[key] = float(row[key])
            for key in [
                "min_room_setup_buffer_min",
                "max_staging_lead_min",
                "max_tray_sets_per_cart",
                "robot_cart_approved",
                "implant_approved",
            ]:
                row[key] = int(row[key])
            self.routes[row["room_id"]] = row

        self.windows_by_scenario = defaultdict(list)
        for row in read_csv(DATA_DIR / "cart_delivery_windows.csv"):
            row["window_start_min"] = int(row["window_start_min"])
            row["window_end_min"] = int(row["window_end_min"])
            row["max_cart_trips"] = int(row["max_cart_trips"])
            row["max_tray_sets"] = int(row["max_tray_sets"])
            self.windows_by_scenario[row["scenario_id"]].append(row)
        for rows in self.windows_by_scenario.values():
            rows.sort(key=lambda r: (r["elevator_id"], r["window_start_min"], r["window_id"]))
        self.effective_window_minutes = defaultdict(list)
        self.time_to_effective_window = {}
        for scenario_id, rows in self.windows_by_scenario.items():
            by_elevator = defaultdict(list)
            for row in rows:
                by_elevator[row["elevator_id"]].append(row)
            for elevator_id, group in by_elevator.items():
                lo = min(r["window_start_min"] for r in group)
                hi = max(r["window_end_min"] for r in group)
                ordered = sorted(group, key=lambda r: (r["window_start_min"], r["window_end_min"], r["window_id"]))
                for minute in range(lo, hi + 1):
                    assigned = None
                    for row in ordered:
                        if row["window_start_min"] <= minute <= row["window_end_min"]:
                            assigned = row["window_id"]
                            break
                    if assigned is not None:
                        self.effective_window_minutes[(scenario_id, elevator_id, assigned)].append(minute)
                        self.time_to_effective_window[(scenario_id, elevator_id, minute)] = assigned

        self.nominal_end = int(self.config["nominal_end_min"])
        self.max_room_ot = int(self.config["max_room_overtime_min"])
        self.max_spd_ot = int(self.config["max_spd_overtime_min"])
        self.max_case_delay = int(self.config["max_open_case_delay_min"])
        self.step = int(self.config["case_start_granularity_min"])
        self.horizon = max(r["close_min"] for r in self.rooms.values()) + self.max_room_ot

        self.eligible_rooms = {}
        for case_id, case in self.cases.items():
            eligible = []
            for room_id, room in self.rooms.items():
                if room["service_line"] not in self.specialty_rules[case["specialty"]]["allowed_service_lines"]:
                    continue
                if case["robot_required"] and not room["robot_ready"]:
                    continue
                if case["implant_case"] and not room["implant_storage"]:
                    continue
                route = self.routes[room_id]
                if case["robot_required"] and not route["robot_cart_approved"]:
                    continue
                if case["implant_case"] and not route["implant_approved"]:
                    continue
                if self.total_case_trays[case_id] > route["max_tray_sets_per_cart"]:
                    continue
                eligible.append(room_id)
            self.eligible_rooms[case_id] = eligible

        self.candidates = {}
        self.candidates_by_case = defaultdict(list)
        self.candidates_by_room_time = defaultdict(list)
        self.candidates_by_tray = defaultdict(list)
        self.audit_times = list(range(0, self.horizon + 1, self.step))
        for case_id, case in self.cases.items():
            for room_id in self.eligible_rooms[case_id]:
                room = self.rooms[room_id]
                route = self.routes[room_id]
                occ = case["duration_min"] + max(case["turnover_min"], room["turnover_buffer_min"])
                start_lb = ceil_int(max(case["release_min"], room["open_min"]) / self.step) * self.step
                start_ub = min(
                    room["close_min"] + self.max_room_ot - occ,
                    case["target_start_min"] + self.max_case_delay,
                )
                for start in range(start_lb, start_ub + 1, self.step):
                    latest_dep = start - ceil_int(
                        route["travel_minutes"] + route["room_settle_minutes"] + route["min_room_setup_buffer_min"]
                    )
                    earliest_dep = start - route["max_staging_lead_min"]
                    has_cart_window = True
                    for scen_id, windows in self.windows_by_scenario.items():
                        feasible = False
                        for w in windows:
                            if w["elevator_id"] != route["elevator_id"]:
                                continue
                            if min(latest_dep, w["window_end_min"]) >= max(earliest_dep, w["window_start_min"]):
                                feasible = True
                                break
                        if not feasible:
                            has_cart_window = False
                            break
                    if not has_cart_window:
                        continue
                    cand = Candidate(
                        case_id=case_id,
                        room_id=room_id,
                        start=start,
                        occ_end=start + occ,
                        surg_end=start + case["duration_min"],
                        delay=max(0, start - case["target_start_min"]),
                        latest_dep=latest_dep,
                    )
                    key = (case_id, room_id, start)
                    self.candidates[key] = cand
                    self.candidates_by_case[case_id].append(key)
                    for tau in range(start, cand.occ_end, self.step):
                        self.candidates_by_room_time[(room_id, tau)].append(key)
                    for tray_type in self.requirements[case_id]:
                        self.candidates_by_tray[tray_type].append(key)

        self.compat_washers = defaultdict(list)
        self.compat_stations = defaultdict(list)
        self.compat_sterilizers = defaultdict(list)
        for tray_type, tray in self.trays.items():
            heavy = tray["weight_kg"] > 12.0
            for washer in self.washers.values():
                if tray["weight_kg"] > washer["capacity_kg"] + 1e-9:
                    continue
                if heavy and not washer["supports_heavy"]:
                    continue
                self.compat_washers[tray_type].append(washer["washer_id"])
            skill = self.specialty_rules[tray["specialty"]]["assembly_skill_group"]
            for station in self.stations.values():
                if station["skill_group"] != skill:
                    continue
                if tray["specialty"] == "robotics" and not station["robot_cell"]:
                    continue
                self.compat_stations[tray_type].append(station["station_id"])
            for sterilizer in self.sterilizers.values():
                if tray["cycle_type"] != sterilizer["cycle_type"]:
                    continue
                if tray["weight_kg"] > sterilizer["capacity_kg"] + 1e-9:
                    continue
                self.compat_sterilizers[tray_type].append(sterilizer["sterilizer_id"])

        self.proc_time = defaultdict(dict)
        for tray_type in self.trays:
            for scen_id, scen in self.scenarios.items():
                self.proc_time[tray_type][scen_id] = self.compute_process_time(tray_type, scen_id)

    def compute_process_time(self, tray_type: str, scenario_id: str) -> int:
        scen = self.scenarios[scenario_id]
        tray = self.trays[tray_type]
        wash = min(
            max(ceil_int(tray["wash_minutes"] * scen["wash_multiplier"]), self.washers[w]["cycle_minutes"])
            for w in self.compat_washers[tray_type]
        )
        assembly = min(
            ceil_int(tray["assembly_minutes"] * scen["assembly_multiplier"] / max(scen["staff_capacity_factor"], 1e-6))
            for _ in self.compat_stations[tray_type]
        )
        post = min(
            self.sterilizers[z]["cycle_minutes"]
            + ceil_int((tray["dry_minutes"] + tray["inspection_minutes"]) * scen["sterilizer_multiplier"])
            + (self.sterilizers[z]["implant_quarantine_min"] if tray["implant_sensitive"] else 0)
            for z in self.compat_sterilizers[tray_type]
        )
        return wash + assembly + post


def solve_master(
    data: Data,
    banned_cases: set,
    time_limit: float,
    incumbent_schedule: Optional[Dict[str, Dict[str, int]]] = None,
) -> Tuple[Optional[Dict[str, Dict[str, int]]], Dict[str, object]]:
    model = Model("master_schedule")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", max(1.0, time_limit))
    model.setParam("randomization/randomseedshift", 0)

    z = {}
    y = {}
    overtime = {}

    for case_id in data.cases:
        y[case_id] = model.addVar(vtype="B", name=f"y[{case_id}]")
    for room_id in data.rooms:
        overtime[room_id] = model.addVar(lb=0.0, ub=data.max_room_ot, name=f"ot[{room_id}]")
    for key, cand in data.candidates.items():
        if cand.case_id in banned_cases:
            continue
        z[key] = model.addVar(vtype="B", name=f"z[{cand.case_id},{cand.room_id},{cand.start}]")

    for case_id in data.cases:
        rel = [z[key] for key in data.candidates_by_case[case_id] if key in z]
        if rel:
            model.addCons(quicksum(rel) == y[case_id], name=f"case_decision[{case_id}]")
        else:
            model.addCons(y[case_id] == 0, name=f"case_forced_off[{case_id}]")

    for room_id in data.rooms:
        for tau in range(0, data.horizon + 1, data.step):
            rel = [z[key] for key in data.candidates_by_room_time[(room_id, tau)] if key in z]
            if rel:
                model.addCons(quicksum(rel) <= 1, name=f"room[{room_id},{tau}]")

    for key, var in z.items():
        cand = data.candidates[key]
        room_close = data.rooms[cand.room_id]["close_min"]
        model.addCons(
            overtime[cand.room_id] >= cand.occ_end - room_close - data.max_room_ot * (1 - var),
            name=f"ot_lb[{cand.case_id},{cand.room_id},{cand.start}]",
        )

    for tray_type in data.trays:
        req_keys = [key for key in data.candidates_by_tray[tray_type] if key in z]
        if not req_keys:
            continue
        for scen_id, scen in data.scenarios.items():
            init_sterile = floor_int(data.trays[tray_type]["initial_sterile_sets"] * scen["initial_sterile_multiplier"])
            init_dirty = data.trays[tray_type]["initial_dirty_sets"]
            proc = data.proc_time[tray_type][scen_id]
            for tau in data.audit_times:
                demand_terms = []
                return_terms = []
                for key in req_keys:
                    cand = data.candidates[key]
                    qty = data.requirements[cand.case_id][tray_type]
                    if cand.latest_dep <= tau:
                        demand_terms.append(qty * z[key])
                    if cand.surg_end + proc <= tau:
                        return_terms.append(qty * z[key])
                if not demand_terms:
                    continue
                loan_avail = 0
                for vendor in data.vendors_by_tray[tray_type]:
                    if vendor["arrival_min"] + scen["loaner_delay_min"] <= tau:
                        loan_avail += vendor["max_sets"]
                rhs = init_sterile + loan_avail + (init_dirty if tau >= proc else 0)
                model.addCons(
                    quicksum(demand_terms) <= rhs + quicksum(return_terms),
                    name=f"tray_prefix[{tray_type},{scen_id},{tau}]",
                )

    objective = quicksum(
        data.cases[c]["cancel_penalty"] * (1 - y[c]) for c in data.cases
    ) + quicksum(
        data.cases[cand.case_id]["delay_penalty_per_min"] * cand.delay * var
        for key, var in z.items()
        for cand in [data.candidates[key]]
    ) + data.config["room_overtime_weight"] * quicksum(overtime[r] for r in data.rooms)

    model.setObjective(objective, "minimize")
    if incumbent_schedule is not None:
        start_sol = model.createSol()
        for case_id in data.cases:
            sched = incumbent_schedule.get(case_id, {"scheduled": 0, "room_id": "", "start_min": None})
            selected = None
            if sched["scheduled"]:
                selected = (case_id, sched["room_id"], sched["start_min"])
            model.setSolVal(start_sol, y[case_id], 1.0 if selected in z else 0.0)
            for key in data.candidates_by_case[case_id]:
                if key in z:
                    model.setSolVal(start_sol, z[key], 1.0 if key == selected else 0.0)
        for room_id in data.rooms:
            room_end = max(
                [
                    incumbent_schedule[cid]["start_min"]
                    + data.cases[cid]["duration_min"]
                    + max(data.cases[cid]["turnover_min"], data.rooms[room_id]["turnover_buffer_min"])
                    for cid in data.cases
                    if incumbent_schedule.get(cid, {}).get("scheduled") and incumbent_schedule[cid]["room_id"] == room_id
                ]
                or [data.rooms[room_id]["close_min"]]
            )
            model.setSolVal(start_sol, overtime[room_id], max(0, room_end - data.rooms[room_id]["close_min"]))
        model.addSol(start_sol, free=True)
    model.optimize()

    has_solution = model.getNSols() > 0
    info = {
        "status": str(model.getStatus()),
        "has_solution": has_solution,
        "obj": float(model.getObjVal()) if has_solution else None,
        "best_bound": float(model.getDualbound()) if has_solution else None,
        "gap": float(model.getGap()) if has_solution else None,
    }
    if not has_solution:
        return None, info

    sol = model.getBestSol()
    schedule = {}
    for case_id in data.cases:
        scheduled = 0
        room_id = ""
        start = None
        for key in data.candidates_by_case[case_id]:
            if key not in z:
                continue
            if model.getSolVal(sol, z[key]) > 0.5:
                cand = data.candidates[key]
                scheduled = 1
                room_id = cand.room_id
                start = cand.start
                break
        schedule[case_id] = {"scheduled": scheduled, "room_id": room_id, "start_min": start}
    return schedule, info


def assign_carts_for_scenario(
    data: Data, schedule: Dict[str, Dict[str, int]], scenario_id: str, time_limit: float
) -> Tuple[bool, Dict[str, Dict[str, object]], Dict[str, object]]:
    cases = [case_id for case_id, row in schedule.items() if row["scheduled"]]
    if not cases:
        return True, {}, {"status": "trivial", "obj": 0.0}

    model = Model(f"cart_{scenario_id}")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", max(1.0, time_limit))
    model.setParam("randomization/randomseedshift", 0)

    x = {}
    eligible = defaultdict(list)
    window_map = {}
    departure_choice = {}
    windows = data.windows_by_scenario[scenario_id]
    for w in windows:
        window_map[w["window_id"]] = w

    for case_id in cases:
        room_id = schedule[case_id]["room_id"]
        start = schedule[case_id]["start_min"]
        route = data.routes[room_id]
        latest_dep = start - ceil_int(
            route["travel_minutes"] + route["room_settle_minutes"] + route["min_room_setup_buffer_min"]
        )
        earliest_dep = start - route["max_staging_lead_min"]
        qty = data.total_case_trays[case_id]
        for w in windows:
            if w["elevator_id"] != route["elevator_id"]:
                continue
            feasible_minutes = [
                minute
                for minute in data.effective_window_minutes[(scenario_id, route["elevator_id"], w["window_id"])]
                if earliest_dep <= minute <= latest_dep
            ]
            if not feasible_minutes:
                continue
            var = model.addVar(vtype="B", name=f"x[{case_id},{w['window_id']}]")
            x[(case_id, w["window_id"])] = var
            eligible[case_id].append(w["window_id"])
            departure_choice[(case_id, w["window_id"])] = max(feasible_minutes)
        if not eligible[case_id]:
            return False, {}, {"status": "infeasible", "reason": f"no_cart_window:{case_id}", "case_ids": [case_id]}
        model.addCons(quicksum(x[(case_id, wid)] for wid in eligible[case_id]) == 1, name=f"assign[{case_id}]")

    for w in windows:
        users = [x[(case_id, w["window_id"])] for case_id in cases if (case_id, w["window_id"]) in x]
        if users:
            model.addCons(quicksum(users) <= w["max_cart_trips"], name=f"trip[{w['window_id']}]")
            model.addCons(
                quicksum(data.total_case_trays[case_id] * x[(case_id, w["window_id"])] for case_id in cases if (case_id, w["window_id"]) in x)
                <= w["max_tray_sets"],
                name=f"tray[{w['window_id']}]",
            )

    model.setObjective(
        quicksum(
            data.total_case_trays[case_id]
            * (
                schedule[case_id]["start_min"]
                - ceil_int(
                    data.routes[schedule[case_id]["room_id"]]["travel_minutes"]
                    + data.routes[schedule[case_id]["room_id"]]["room_settle_minutes"]
                    + data.routes[schedule[case_id]["room_id"]]["min_room_setup_buffer_min"]
                )
                - departure_choice[(case_id, wid)]
            )
            * x[(case_id, wid)]
            for case_id in cases
            for wid in eligible[case_id]
        ),
        "minimize",
    )
    model.optimize()
    if model.getNSols() == 0:
        dropped = diagnose_cart_subset(data, schedule, scenario_id, cases, eligible, windows, time_limit)
        return False, {}, {"status": str(model.getStatus()), "reason": "cart_mip_no_solution", "case_ids": dropped}

    sol = model.getBestSol()
    dep_plan = {}
    for case_id in cases:
        room_id = schedule[case_id]["room_id"]
        start = schedule[case_id]["start_min"]
        route = data.routes[room_id]
        latest_dep = start - ceil_int(route["travel_minutes"] + route["room_settle_minutes"] + route["min_room_setup_buffer_min"])
        earliest_dep = start - route["max_staging_lead_min"]
        chosen = None
        for wid in eligible[case_id]:
            if model.getSolVal(sol, x[(case_id, wid)]) > 0.5:
                chosen = window_map[wid]
                break
        if chosen is None:
            return False, {}, {"status": "infeasible", "reason": f"missing_window_assignment:{case_id}"}
        departure = departure_choice[(case_id, chosen["window_id"])]
        if departure < max(earliest_dep, chosen["window_start_min"]):
            return False, {}, {"status": "infeasible", "reason": f"invalid_departure:{case_id}"}
        dep_plan[case_id] = {
            "window_id": chosen["window_id"],
            "departure_min": departure,
            "route_id": route["route_id"],
            "elevator_id": chosen["elevator_id"],
            "latest_dep": latest_dep,
            "earliest_dep": earliest_dep,
        }
    return True, dep_plan, {"status": str(model.getStatus()), "obj": float(model.getObjVal())}


def diagnose_cart_subset(
    data: Data,
    schedule: Dict[str, Dict[str, int]],
    scenario_id: str,
    cases: List[str],
    eligible: Dict[str, List[str]],
    windows: List[Dict[str, object]],
    time_limit: float,
) -> List[str]:
    model = Model(f"cart_subset_{scenario_id}")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", max(1.0, min(20.0, time_limit)))
    model.setParam("randomization/randomseedshift", 0)

    keep = {case_id: model.addVar(vtype="B", name=f"keep[{case_id}]") for case_id in cases}
    x = {}
    for case_id in cases:
        for wid in eligible[case_id]:
            x[(case_id, wid)] = model.addVar(vtype="B", name=f"x[{case_id},{wid}]")
        model.addCons(quicksum(x[(case_id, wid)] for wid in eligible[case_id]) == keep[case_id], name=f"keep_assign[{case_id}]")

    for w in windows:
        users = [x[(case_id, w["window_id"])] for case_id in cases if (case_id, w["window_id"]) in x]
        if users:
            model.addCons(quicksum(users) <= w["max_cart_trips"], name=f"trip[{w['window_id']}]")
            model.addCons(
                quicksum(data.total_case_trays[case_id] * x[(case_id, w["window_id"])] for case_id in cases if (case_id, w["window_id"]) in x)
                <= w["max_tray_sets"],
                name=f"tray[{w['window_id']}]",
            )

    model.setObjective(
        quicksum(
            (data.cases[case_id]["cancel_penalty"] + 1000.0 * data.cases[case_id]["mandatory"]) * keep[case_id]
            for case_id in cases
        ),
        "maximize",
    )
    model.optimize()
    if model.getNSols() == 0:
        return cases
    sol = model.getBestSol()
    dropped = [case_id for case_id in cases if model.getSolVal(sol, keep[case_id]) < 0.5]
    return dropped


class ScenarioRecourseBuilder:
    def __init__(self, data: Data, schedule: Dict[str, Dict[str, int]], scenario_id: str, departures: Dict[str, Dict[str, object]]):
        self.data = data
        self.schedule = schedule
        self.scenario_id = scenario_id
        self.scenario = data.scenarios[scenario_id]
        self.departures = departures
        self.tray_rows = []
        self.batch_rows = []
        self.case_cost = 0.0
        self.loaner_cost = 0.0
        self.emergency_cost = 0.0
        self.cart_dwell_cost = 0.0
        self.handling_cost = 0.0
        self.spd_overtime = 0
        self.batch_counter = 0
        self.fail_reason = ""
        self.fail_case_ids = []

        self.ready_units = defaultdict(list)
        self.pending_dirty = defaultdict(list)
        self.loaner_used = defaultdict(int)
        self.emergency_used = 0
        self.emergency_cases = defaultdict(set)
        self.source_case_batch_use = defaultdict(int)
        self.init_dirty_batch_use = defaultdict(int)

        self.washer_next = {wid: 0 for wid in data.washers}
        self.ster_next = {sid: 0 for sid in data.sterilizers}
        self.station_slots = {
            aid: [0 for _ in range(data.stations[aid]["parallel_techs"])] for aid in data.stations
        }
        self.station_heavy_used = {aid: 0 for aid in data.stations}

        for tray_type, tray in data.trays.items():
            count = floor_int(tray["initial_sterile_sets"] * self.scenario["initial_sterile_multiplier"])
            for _ in range(count):
                self.ready_units[tray_type].append(
                    SterileUnit(tray_type=tray_type, ready_time=0, source="initial")
                )
            for _ in range(tray["initial_dirty_sets"]):
                self.pending_dirty[tray_type].append(
                    DirtyUnit(tray_type=tray_type, source_case_id="INIT", available_time=0)
                )

        for tray_type in self.ready_units:
            self.ready_units[tray_type].sort(key=lambda u: (u.ready_time, u.source, u.batch_id, u.loaner_vendor))
        for tray_type in self.pending_dirty:
            self.pending_dirty[tray_type].sort(key=lambda d: (d.available_time, d.source_case_id))

    def build(self) -> Tuple[bool, Dict[str, object]]:
        scheduled_cases = [cid for cid, row in self.schedule.items() if row["scheduled"]]
        ordered = sorted(
            scheduled_cases,
            key=lambda cid: (self.departures[cid]["departure_min"], self.schedule[cid]["start_min"], cid),
        )
        for case_id in ordered:
            case = self.data.cases[case_id]
            dep = self.departures[case_id]["departure_min"]
            start = self.schedule[case_id]["start_min"]
            self.case_cost += case["delay_penalty_per_min"] * max(0, start - case["target_start_min"])
            latest_dep = self.departures[case_id]["latest_dep"]
            self.cart_dwell_cost += (
                self.data.config["cart_early_dwell_penalty_per_tray_min"]
                * self.data.total_case_trays[case_id]
                * max(0, latest_dep - dep)
            )
            route = self.data.routes[self.schedule[case_id]["room_id"]]
            self.handling_cost += route["handling_cost_per_tray"] * self.data.total_case_trays[case_id]

            for tray_type, qty in self.data.requirements[case_id].items():
                ok, source_rows = self.allocate_case_tray(case_id, tray_type, qty, dep)
                if not ok:
                    self.fail_reason = f"tray_shortage:{case_id}:{tray_type}"
                    self.fail_case_ids = [case_id]
                    return False, {"reason": self.fail_reason, "case_ids": self.fail_case_ids}
                for row in source_rows:
                    self.tray_rows.append(row)

            self.release_case_dirty_units(case_id)

        last_finish = 0
        for batch in self.batch_rows:
            last_finish = max(last_finish, batch.ready_time)
        self.spd_overtime = max(0, last_finish - self.data.nominal_end)

        cost = (
            self.case_cost
            + self.loaner_cost
            + self.emergency_cost
            + self.cart_dwell_cost
            + self.handling_cost
            + self.data.config["spd_overtime_weight"] * self.spd_overtime
        )
        return True, {
            "tray_rows": self.tray_rows,
            "batch_rows": self.batch_rows,
            "scenario_cost": cost,
            "spd_overtime": self.spd_overtime,
        }

    def release_case_dirty_units(self, case_id: str):
        end_time = self.schedule[case_id]["start_min"] + self.data.cases[case_id]["duration_min"]
        for tray_type, qty in self.data.requirements[case_id].items():
            for _ in range(qty):
                self.pending_dirty[tray_type].append(
                    DirtyUnit(tray_type=tray_type, source_case_id=case_id, available_time=end_time)
                )
        for tray_type in self.data.requirements[case_id]:
            self.pending_dirty[tray_type].sort(key=lambda d: (d.available_time, d.source_case_id))

    def pop_ready_unit(self, tray_type: str, dep: int) -> Optional[SterileUnit]:
        ready = self.ready_units[tray_type]
        ready.sort(key=lambda u: (u.ready_time, u.source, u.batch_id, u.loaner_vendor))
        for idx, unit in enumerate(ready):
            if unit.ready_time <= dep:
                return ready.pop(idx)
        return None

    def pop_dirty_unit(self, tray_type: str) -> Optional[DirtyUnit]:
        dirty = self.pending_dirty[tray_type]
        dirty.sort(key=lambda d: (d.available_time, d.source_case_id))
        if dirty:
            return dirty.pop(0)
        return None

    def allocate_case_tray(self, case_id: str, tray_type: str, qty: int, dep: int) -> Tuple[bool, List[Dict[str, object]]]:
        rows = []
        allocations = []
        for _ in range(qty):
            unit = self.pop_ready_unit(tray_type, dep)
            if unit is not None:
                allocations.append(unit)
                continue

            while True:
                dirty = self.pop_dirty_unit(tray_type)
                if dirty is None:
                    break
                batch = self.schedule_regular_batch(tray_type, dirty, dep)
                if batch is None:
                    self.pending_dirty[tray_type].append(dirty)
                    self.pending_dirty[tray_type].sort(key=lambda d: (d.available_time, d.source_case_id))
                    break
                self.batch_rows.append(batch)
                self.ready_units[tray_type].append(
                    SterileUnit(
                        tray_type=tray_type,
                        ready_time=batch.ready_time,
                        source="reprocessed",
                        batch_id=batch.batch_id,
                    )
                )
                unit = self.pop_ready_unit(tray_type, dep)
                if unit is not None:
                    allocations.append(unit)
                    break
            else:
                pass
            if len(allocations) == _ + 1:
                continue

            unit = self.allocate_loaner_unit(case_id, tray_type, dep)
            if unit is not None:
                allocations.append(unit)
                continue

            unit = self.allocate_emergency_unit(case_id, tray_type, dep)
            if unit is not None:
                allocations.append(unit)
                continue

            return False, []

        agg = defaultdict(int)
        for unit in allocations:
            key = (unit.source, unit.batch_id, unit.loaner_vendor)
            agg[key] += 1
        for (source, batch_id, loaner_vendor), used_qty in sorted(agg.items()):
            row = {
                "row_type": "tray",
                "scenario_id": self.scenario_id,
                "case_id": case_id,
                "room_id": "",
                "start_min": "",
                "scheduled": "",
                "tray_type": tray_type,
                "quantity": used_qty,
                "source": source,
                "source_case_id": "",
                "batch_id": batch_id if source == "reprocessed" else "",
                "washer_id": "",
                "wash_start": "",
                "assembly_station": "",
                "assembly_start": "",
                "sterilizer_id": "",
                "sterilizer_start": "",
                "loaner_vendor": loaner_vendor if source == "loaner" else "",
                "emergency_flag": 1 if source == "emergency" else "",
                "notes": "",
            }
            rows.append(row)
        return True, rows

    def allocate_loaner_unit(self, case_id: str, tray_type: str, dep: int) -> Optional[SterileUnit]:
        tray = self.data.trays[tray_type]
        if not tray["loaner_allowed"]:
            return None
        case = self.data.cases[case_id]
        for vendor in sorted(self.data.vendors_by_tray[tray_type], key=lambda v: (v["cost_per_set"], v["arrival_min"], v["vendor_id"])):
            if self.loaner_used[(vendor["vendor_id"], tray_type)] >= vendor["max_sets"]:
                continue
            if vendor["arrival_min"] + self.scenario["loaner_delay_min"] > dep:
                continue
            if tray["implant_sensitive"] and case["implant_case"] and not vendor["credentialed_implant"]:
                continue
            self.loaner_used[(vendor["vendor_id"], tray_type)] += 1
            self.loaner_cost += vendor["cost_per_set"] + self.data.config["loaner_checkin_penalty"] * vendor["checkin_minutes_per_set"]
            return SterileUnit(
                tray_type=tray_type,
                ready_time=vendor["arrival_min"] + self.scenario["loaner_delay_min"],
                source="loaner",
                loaner_vendor=vendor["vendor_id"],
            )
        return None

    def allocate_emergency_unit(self, case_id: str, tray_type: str, dep: int) -> Optional[SterileUnit]:
        tray = self.data.trays[tray_type]
        if not tray["emergency_allowed"]:
            return None
        if self.emergency_used >= self.scenario["max_emergency_cycles"]:
            return None
        case = self.data.cases[case_id]
        specialty_cap = self.data.specialty_rules[case["specialty"]]["max_emergency_cases"]
        if case_id not in self.emergency_cases[case["specialty"]] and len(self.emergency_cases[case["specialty"]]) >= specialty_cap:
            return None
        self.emergency_used += 1
        self.emergency_cases[case["specialty"]].add(case_id)
        self.emergency_cost += self.data.config["emergency_cycle_penalty"]
        if tray["implant_sensitive"]:
            self.emergency_cost += self.data.config["implant_emergency_penalty"]
        return SterileUnit(tray_type=tray_type, ready_time=dep, source="emergency")

    def schedule_regular_batch(self, tray_type: str, dirty: DirtyUnit, dep: int) -> Optional[BatchJob]:
        tray = self.data.trays[tray_type]
        heavy = tray["weight_kg"] > 12.0

        best = None
        for washer_id in self.data.compat_washers[tray_type]:
            washer = self.data.washers[washer_id]
            wash_dur = max(ceil_int(tray["wash_minutes"] * self.scenario["wash_multiplier"]), washer["cycle_minutes"])
            wash_start = max(dirty.available_time, washer["open_min"], self.washer_next[washer_id])
            wash_finish = wash_start + wash_dur
            if wash_finish > washer["close_min"] + self.data.max_spd_ot:
                continue

            for station_id in self.data.compat_stations[tray_type]:
                station = self.data.stations[station_id]
                if heavy and self.station_heavy_used[station_id] >= station["heavy_tray_daily_limit"]:
                    continue
                asm_dur = ceil_int(
                    tray["assembly_minutes"] * self.scenario["assembly_multiplier"] / max(self.scenario["staff_capacity_factor"], 1e-6)
                )
                for slot_idx, slot_avail in enumerate(self.station_slots[station_id]):
                    asm_start = max(wash_finish, station["open_min"], slot_avail)
                    asm_finish = asm_start + asm_dur
                    if asm_finish > station["close_min"] + self.data.max_spd_ot:
                        continue

                    for sterilizer_id in self.data.compat_sterilizers[tray_type]:
                        sterilizer = self.data.sterilizers[sterilizer_id]
                        ster_dur = (
                            sterilizer["cycle_minutes"]
                            + ceil_int((tray["dry_minutes"] + tray["inspection_minutes"]) * self.scenario["sterilizer_multiplier"])
                            + (sterilizer["implant_quarantine_min"] if tray["implant_sensitive"] else 0)
                        )
                        ster_start = max(asm_finish, sterilizer["open_min"], self.ster_next[sterilizer_id])
                        ready = ster_start + ster_dur
                        if ready > sterilizer["close_min"] + self.data.max_spd_ot:
                            continue
                        if ready > dep:
                            continue
                        candidate = (
                            ready,
                            wash_start,
                            asm_start,
                            ster_start,
                            washer_id,
                            station_id,
                            slot_idx,
                            sterilizer_id,
                        )
                        if best is None or candidate < best:
                            best = candidate

        if best is None:
            return None

        ready, wash_start, asm_start, ster_start, washer_id, station_id, slot_idx, sterilizer_id = best
        wash_dur = max(
            ceil_int(tray["wash_minutes"] * self.scenario["wash_multiplier"]),
            self.data.washers[washer_id]["cycle_minutes"],
        )
        asm_dur = ceil_int(
            tray["assembly_minutes"] * self.scenario["assembly_multiplier"] / max(self.scenario["staff_capacity_factor"], 1e-6)
        )
        ster_dur = (
            self.data.sterilizers[sterilizer_id]["cycle_minutes"]
            + ceil_int((tray["dry_minutes"] + tray["inspection_minutes"]) * self.scenario["sterilizer_multiplier"])
            + (self.data.sterilizers[sterilizer_id]["implant_quarantine_min"] if tray["implant_sensitive"] else 0)
        )

        self.washer_next[washer_id] = wash_start + wash_dur
        self.station_slots[station_id][slot_idx] = asm_start + asm_dur
        self.ster_next[sterilizer_id] = ster_start + ster_dur
        if heavy:
            self.station_heavy_used[station_id] += 1

        self.batch_counter += 1
        batch_id = f"B_{self.scenario_id}_{self.batch_counter:04d}"
        if dirty.source_case_id == "INIT":
            self.init_dirty_batch_use[tray_type] += 1
        else:
            self.source_case_batch_use[(dirty.source_case_id, tray_type)] += 1

        return BatchJob(
            scenario_id=self.scenario_id,
            batch_id=batch_id,
            tray_type=tray_type,
            quantity=1,
            source_case_id=dirty.source_case_id,
            washer_id=washer_id,
            wash_start=wash_start,
            assembly_station=station_id,
            assembly_start=asm_start,
            sterilizer_id=sterilizer_id,
            sterilizer_start=ster_start,
            ready_time=ready,
            weight=tray["weight_kg"],
        )


def build_full_solution(data: Data, schedule: Dict[str, Dict[str, int]], total_time_limit: float) -> Tuple[bool, Dict[str, object]]:
    case_rows = []
    for case_id in sorted(data.cases):
        sched = schedule[case_id]
        case_rows.append(
            {
                "row_type": "case",
                "scenario_id": "",
                "case_id": case_id,
                "room_id": sched["room_id"] if sched["scheduled"] else "",
                "start_min": sched["start_min"] if sched["scheduled"] else "",
                "scheduled": sched["scheduled"],
                "tray_type": "",
                "quantity": "",
                "source": "",
                "source_case_id": "",
                "batch_id": "",
                "washer_id": "",
                "wash_start": "",
                "assembly_station": "",
                "assembly_start": "",
                "sterilizer_id": "",
                "sterilizer_start": "",
                "loaner_vendor": "",
                "emergency_flag": "",
                "notes": "",
            }
        )

    common_room_ot = 0
    for room_id in data.rooms:
        ends = [
            schedule[cid]["start_min"]
            + data.cases[cid]["duration_min"]
            + max(data.cases[cid]["turnover_min"], data.rooms[room_id]["turnover_buffer_min"])
            for cid in data.cases
            if schedule[cid]["scheduled"] and schedule[cid]["room_id"] == room_id
        ]
        room_ot = max(0, max(ends) - data.rooms[room_id]["close_min"]) if ends else 0
        common_room_ot += room_ot

    cart_rows = []
    tray_rows = []
    batch_rows = []
    scenario_costs = {}
    remaining = max(10.0, total_time_limit)
    per_scenario_limit = max(5.0, remaining / max(1, len(data.scenarios)))
    for scen_id in data.scenarios:
        ok, dep_plan, cart_info = assign_carts_for_scenario(data, schedule, scen_id, per_scenario_limit)
        if not ok:
            return False, {"reason": cart_info.get("reason", "cart_assignment_failed"), "case_ids": []}
        for case_id, plan in dep_plan.items():
            room_id = schedule[case_id]["room_id"]
            cart_rows.append(
                {
                    "row_type": "cart",
                    "scenario_id": scen_id,
                    "case_id": case_id,
                    "room_id": room_id,
                    "start_min": plan["departure_min"],
                    "scheduled": "",
                    "tray_type": "",
                    "quantity": data.total_case_trays[case_id],
                    "source": plan["route_id"],
                    "source_case_id": "",
                    "batch_id": f"CART_{scen_id}_{case_id}",
                    "washer_id": "",
                    "wash_start": "",
                    "assembly_station": "",
                    "assembly_start": "",
                    "sterilizer_id": "",
                    "sterilizer_start": "",
                    "loaner_vendor": "",
                    "emergency_flag": "",
                    "notes": "",
                }
            )
        builder = ScenarioRecourseBuilder(data, schedule, scen_id, dep_plan)
        ok, info = builder.build()
        if not ok:
            return False, {"reason": info["reason"], "case_ids": info["case_ids"]}
        for row in info["tray_rows"]:
            tray_rows.append(row)
        for batch in info["batch_rows"]:
            batch_rows.append(
                {
                    "row_type": "batch",
                    "scenario_id": batch.scenario_id,
                    "case_id": "",
                    "room_id": "",
                    "start_min": "",
                    "scheduled": "",
                    "tray_type": batch.tray_type,
                    "quantity": batch.quantity,
                    "source": "",
                    "source_case_id": batch.source_case_id,
                    "batch_id": batch.batch_id,
                    "washer_id": batch.washer_id,
                    "wash_start": batch.wash_start,
                    "assembly_station": batch.assembly_station,
                    "assembly_start": batch.assembly_start,
                    "sterilizer_id": batch.sterilizer_id,
                    "sterilizer_start": batch.sterilizer_start,
                    "loaner_vendor": "",
                    "emergency_flag": "",
                    "notes": "",
                }
            )
        scenario_costs[scen_id] = info["scenario_cost"]

    expected = sum(data.scenarios[s]["probability"] * scenario_costs[s] for s in data.scenarios)
    tail = data.config["scenario_tail_weight"] * max(scenario_costs.values() or [0.0])
    cancel_cost = sum(
        data.cases[cid]["cancel_penalty"] * (1 - schedule[cid]["scheduled"]) for cid in data.cases
    )
    room_ot_cost = data.config["room_overtime_weight"] * common_room_ot
    total_cost = cancel_cost + room_ot_cost + expected + tail
    rows = case_rows + cart_rows + tray_rows + batch_rows
    return True, {
        "rows": rows,
        "scenario_costs": scenario_costs,
        "expected_cost": expected,
        "tail_cost": tail,
        "cancel_cost": cancel_cost,
        "room_ot_cost": room_ot_cost,
        "total_cost": total_cost,
    }


def validate_solution(data: Data, rows: List[Dict[str, object]]) -> Tuple[bool, List[str], Dict[str, object]]:
    errors = []
    metrics = {"violated_groups": 0, "theta": 0.0}
    required_columns = [
        "row_type",
        "scenario_id",
        "case_id",
        "room_id",
        "start_min",
        "scheduled",
        "tray_type",
        "quantity",
        "source",
        "source_case_id",
        "batch_id",
        "washer_id",
        "wash_start",
        "assembly_station",
        "assembly_start",
        "sterilizer_id",
        "sterilizer_start",
        "loaner_vendor",
        "emergency_flag",
        "notes",
    ]

    for row in rows:
        if set(row.keys()) != set(required_columns):
            errors.append("schema_columns_mismatch")
            break

    case_rows = [r for r in rows if r["row_type"] == "case"]
    tray_rows = [r for r in rows if r["row_type"] == "tray"]
    cart_rows = [r for r in rows if r["row_type"] == "cart"]
    batch_rows = [r for r in rows if r["row_type"] == "batch"]

    if len(case_rows) != len(data.cases):
        errors.append("case_row_count")
    seen_case_rows = {r["case_id"] for r in case_rows}
    if seen_case_rows != set(data.cases):
        errors.append("case_row_coverage")

    schedule = {}
    for row in case_rows:
        case_id = row["case_id"]
        if row["scheduled"] not in (0, 1, "0", "1"):
            errors.append(f"bad_case_flag:{case_id}")
            continue
        scheduled = int(row["scheduled"])
        if scheduled:
            if row["room_id"] == "" or row["start_min"] == "":
                errors.append(f"scheduled_missing_room_start:{case_id}")
                continue
            room_id = str(row["room_id"])
            start = int(float(row["start_min"]))
            schedule[case_id] = {"scheduled": 1, "room_id": room_id, "start_min": start}
        else:
            if row["room_id"] != "" or row["start_min"] != "":
                errors.append(f"unscheduled_nonblank:{case_id}")
            schedule[case_id] = {"scheduled": 0, "room_id": "", "start_min": None}

    for case_id, sched in schedule.items():
        if not sched["scheduled"]:
            continue
        case = data.cases[case_id]
        room = data.rooms[sched["room_id"]]
        if sched["room_id"] not in data.eligible_rooms[case_id]:
            errors.append(f"room_ineligible:{case_id}")
        if sched["start_min"] < max(case["release_min"], room["open_min"]):
            errors.append(f"release_violation:{case_id}")
        occ_end = sched["start_min"] + case["duration_min"] + max(case["turnover_min"], room["turnover_buffer_min"])
        if occ_end > room["close_min"] + data.max_room_ot:
            errors.append(f"room_overtime_limit:{case_id}")

    for room_id in data.rooms:
        room_cases = [
            (schedule[cid]["start_min"], schedule[cid]["start_min"] + data.cases[cid]["duration_min"] + max(data.cases[cid]["turnover_min"], data.rooms[room_id]["turnover_buffer_min"]), cid)
            for cid in data.cases
            if schedule[cid]["scheduled"] and schedule[cid]["room_id"] == room_id
        ]
        room_cases.sort()
        for idx in range(1, len(room_cases)):
            if room_cases[idx][0] < room_cases[idx - 1][1]:
                errors.append(f"room_overlap:{room_id}:{room_cases[idx - 1][2]}:{room_cases[idx][2]}")

    batch_map = {}
    for row in batch_rows:
        batch_id = row["batch_id"]
        scen_id = row["scenario_id"]
        tray_type = row["tray_type"]
        qty = int(float(row["quantity"]))
        washer_id = row["washer_id"]
        assembly_station = row["assembly_station"]
        sterilizer_id = row["sterilizer_id"]
        wash_start = int(float(row["wash_start"]))
        assembly_start = int(float(row["assembly_start"]))
        sterilizer_start = int(float(row["sterilizer_start"]))
        batch_map[(scen_id, batch_id)] = row

        tray = data.trays[tray_type]
        if washer_id not in data.compat_washers[tray_type]:
            errors.append(f"batch_washer_compat:{batch_id}")
        if assembly_station not in data.compat_stations[tray_type]:
            errors.append(f"batch_station_compat:{batch_id}")
        if sterilizer_id not in data.compat_sterilizers[tray_type]:
            errors.append(f"batch_ster_compat:{batch_id}")

        scen = data.scenarios[scen_id]
        washer = data.washers[washer_id]
        station = data.stations[assembly_station]
        sterilizer = data.sterilizers[sterilizer_id]
        wash_dur = max(ceil_int(tray["wash_minutes"] * scen["wash_multiplier"]), washer["cycle_minutes"])
        asm_dur = ceil_int(tray["assembly_minutes"] * scen["assembly_multiplier"] / max(scen["staff_capacity_factor"], 1e-6))
        ster_dur = sterilizer["cycle_minutes"] + ceil_int((tray["dry_minutes"] + tray["inspection_minutes"]) * scen["sterilizer_multiplier"]) + (sterilizer["implant_quarantine_min"] if tray["implant_sensitive"] else 0)
        if wash_start < washer["open_min"]:
            errors.append(f"batch_washer_open:{batch_id}")
        if assembly_start < max(station["open_min"], wash_start + wash_dur):
            errors.append(f"batch_assembly_timing:{batch_id}")
        if sterilizer_start < max(sterilizer["open_min"], assembly_start + asm_dur):
            errors.append(f"batch_ster_timing:{batch_id}")
        if sterilizer_start + ster_dur > sterilizer["close_min"] + data.max_spd_ot:
            errors.append(f"batch_spd_overtime_limit:{batch_id}")
        if row["source_case_id"] == "INIT":
            if qty > data.trays[tray_type]["initial_dirty_sets"]:
                errors.append(f"batch_init_dirty_excess:{batch_id}")
        elif row["source_case_id"]:
            src_case = row["source_case_id"]
            if not schedule.get(src_case, {}).get("scheduled"):
                errors.append(f"batch_source_unscheduled:{batch_id}")
            else:
                src_end = schedule[src_case]["start_min"] + data.cases[src_case]["duration_min"]
                if wash_start < src_end:
                    errors.append(f"batch_source_before_case_end:{batch_id}")

    batch_usage = defaultdict(int)
    tray_qty = defaultdict(int)
    loaner_qty = defaultdict(int)
    emergency_by_scenario = defaultdict(int)
    emergency_cases = defaultdict(lambda: defaultdict(set))
    dep_map = {}
    for row in cart_rows:
        dep_map[(row["scenario_id"], row["case_id"])] = int(float(row["start_min"]))

    for row in tray_rows:
        scen_id = row["scenario_id"]
        case_id = row["case_id"]
        tray_type = row["tray_type"]
        qty = int(float(row["quantity"]))
        tray_qty[(scen_id, case_id, tray_type)] += qty
        source = row["source"]
        if not schedule.get(case_id, {}).get("scheduled"):
            errors.append(f"tray_for_unscheduled_case:{case_id}")
            continue
        dep = dep_map.get((scen_id, case_id))
        if dep is None:
            errors.append(f"missing_cart_for_tray:{scen_id}:{case_id}")
            continue
        if source == "initial":
            pass
        elif source == "loaner":
            vendor = row["loaner_vendor"]
            if (vendor, tray_type) not in data.vendor_map:
                errors.append(f"unknown_loaner:{vendor}:{tray_type}")
            else:
                info = data.vendor_map[(vendor, tray_type)]
                if info["arrival_min"] + data.scenarios[scen_id]["loaner_delay_min"] > dep:
                    errors.append(f"loaner_late:{vendor}:{case_id}:{tray_type}")
                loaner_qty[(scen_id, vendor, tray_type)] += qty
        elif source == "emergency":
            if not data.trays[tray_type]["emergency_allowed"]:
                errors.append(f"emergency_not_allowed:{case_id}:{tray_type}")
            emergency_by_scenario[scen_id] += qty
            emergency_cases[scen_id][data.cases[case_id]["specialty"]].add(case_id)
        elif source == "reprocessed":
            batch_id = row["batch_id"]
            batch = batch_map.get((scen_id, batch_id))
            if batch is None:
                errors.append(f"unknown_batch_ref:{scen_id}:{batch_id}")
            else:
                batch_usage[(scen_id, batch_id)] += qty
                tray = data.trays[tray_type]
                scen = data.scenarios[scen_id]
                sterilizer = data.sterilizers[batch["sterilizer_id"]]
                ready = int(float(batch["sterilizer_start"])) + sterilizer["cycle_minutes"] + ceil_int((tray["dry_minutes"] + tray["inspection_minutes"]) * scen["sterilizer_multiplier"]) + (sterilizer["implant_quarantine_min"] if tray["implant_sensitive"] else 0)
                if ready > dep:
                    errors.append(f"batch_ready_late:{batch_id}:{case_id}")
        else:
            errors.append(f"bad_tray_source:{source}:{case_id}:{tray_type}")

    for case_id, reqs in data.requirements.items():
        if not schedule[case_id]["scheduled"]:
            continue
        for scen_id in data.scenarios:
            for tray_type, qty in reqs.items():
                if tray_qty[(scen_id, case_id, tray_type)] != qty:
                    errors.append(f"tray_balance:{scen_id}:{case_id}:{tray_type}")

    for (scen_id, batch_id), used in batch_usage.items():
        batch = batch_map[(scen_id, batch_id)]
        if used > int(float(batch["quantity"])):
            errors.append(f"batch_overused:{batch_id}")

    for scen_id in data.scenarios:
        for tray_type, tray in data.trays.items():
            init_used = sum(
                int(float(r["quantity"]))
                for r in tray_rows
                if r["scenario_id"] == scen_id and r["tray_type"] == tray_type and r["source"] == "initial"
            )
            init_cap = floor_int(tray["initial_sterile_sets"] * data.scenarios[scen_id]["initial_sterile_multiplier"])
            if init_used > init_cap:
                errors.append(f"initial_overuse:{scen_id}:{tray_type}")

    for (scen_id, vendor, tray_type), used in loaner_qty.items():
        if used > data.vendor_map[(vendor, tray_type)]["max_sets"]:
            errors.append(f"loaner_overuse:{scen_id}:{vendor}:{tray_type}")

    for scen_id, used in emergency_by_scenario.items():
        if used > data.scenarios[scen_id]["max_emergency_cycles"]:
            errors.append(f"emergency_overuse:{scen_id}")
        for specialty, cases_used in emergency_cases[scen_id].items():
            if len(cases_used) > data.specialty_rules[specialty]["max_emergency_cases"]:
                errors.append(f"emergency_specialty_overuse:{scen_id}:{specialty}")

    carts_per_scenario_case = defaultdict(int)
    for row in cart_rows:
        scen_id = row["scenario_id"]
        case_id = row["case_id"]
        carts_per_scenario_case[(scen_id, case_id)] += 1
        if not schedule.get(case_id, {}).get("scheduled"):
            errors.append(f"cart_unscheduled_case:{case_id}")
            continue
        room_id = row["room_id"]
        if room_id != schedule[case_id]["room_id"]:
            errors.append(f"cart_room_mismatch:{case_id}")
        route = data.routes[room_id]
        if row["source"] != route["route_id"]:
            errors.append(f"cart_route_mismatch:{case_id}")
        dep = int(float(row["start_min"]))
        start = schedule[case_id]["start_min"]
        latest_dep = start - ceil_int(route["travel_minutes"] + route["room_settle_minutes"] + route["min_room_setup_buffer_min"])
        earliest_dep = start - route["max_staging_lead_min"]
        if dep < earliest_dep or dep > latest_dep:
            errors.append(f"cart_departure_window:{case_id}:{scen_id}")

    for case_id in data.cases:
        if not schedule[case_id]["scheduled"]:
            continue
        for scen_id in data.scenarios:
            if carts_per_scenario_case[(scen_id, case_id)] != 1:
                errors.append(f"cart_count:{scen_id}:{case_id}")

    for scen_id, windows in data.windows_by_scenario.items():
        trips = defaultdict(int)
        trays = defaultdict(int)
        for row in cart_rows:
            if row["scenario_id"] != scen_id:
                continue
            dep = int(float(row["start_min"]))
            room_id = row["room_id"]
            route = data.routes[room_id]
            matched = data.time_to_effective_window.get((scen_id, route["elevator_id"], dep))
            if matched is None:
                errors.append(f"cart_no_window:{scen_id}:{row['case_id']}")
                continue
            trips[matched] += 1
            trays[matched] += int(float(row["quantity"]))
        for w in windows:
            if trips[w["window_id"]] > w["max_cart_trips"]:
                errors.append(f"cart_trip_capacity:{scen_id}:{w['window_id']}")
            if trays[w["window_id"]] > w["max_tray_sets"]:
                errors.append(f"cart_tray_capacity:{scen_id}:{w['window_id']}")

    for scen_id in data.scenarios:
        washer_events = defaultdict(list)
        ster_events = defaultdict(list)
        station_events = defaultdict(list)
        heavy_count = defaultdict(int)
        for row in batch_rows:
            if row["scenario_id"] != scen_id:
                continue
            tray = data.trays[row["tray_type"]]
            washer = data.washers[row["washer_id"]]
            station = data.stations[row["assembly_station"]]
            sterilizer = data.sterilizers[row["sterilizer_id"]]
            scen = data.scenarios[scen_id]
            wash_start = int(float(row["wash_start"]))
            asm_start = int(float(row["assembly_start"]))
            ster_start = int(float(row["sterilizer_start"]))
            wash_dur = max(ceil_int(tray["wash_minutes"] * scen["wash_multiplier"]), washer["cycle_minutes"])
            asm_dur = ceil_int(tray["assembly_minutes"] * scen["assembly_multiplier"] / max(scen["staff_capacity_factor"], 1e-6))
            ster_dur = sterilizer["cycle_minutes"] + ceil_int((tray["dry_minutes"] + tray["inspection_minutes"]) * scen["sterilizer_multiplier"]) + (sterilizer["implant_quarantine_min"] if tray["implant_sensitive"] else 0)
            washer_events[row["washer_id"]].append((wash_start, 1))
            washer_events[row["washer_id"]].append((wash_start + wash_dur, -1))
            station_events[row["assembly_station"]].append((asm_start, 1))
            station_events[row["assembly_station"]].append((asm_start + asm_dur, -1))
            ster_events[row["sterilizer_id"]].append((ster_start, 1))
            ster_events[row["sterilizer_id"]].append((ster_start + ster_dur, -1))
            if tray["weight_kg"] > 12.0:
                heavy_count[row["assembly_station"]] += int(float(row["quantity"]))

        for washer_id, events in washer_events.items():
            level = 0
            for _, delta in sorted(events, key=lambda x: (x[0], x[1])):
                level += delta
                if level > 1:
                    errors.append(f"washer_overlap:{scen_id}:{washer_id}")
                    break
        for sterilizer_id, events in ster_events.items():
            level = 0
            for _, delta in sorted(events, key=lambda x: (x[0], x[1])):
                level += delta
                if level > 1:
                    errors.append(f"ster_overlap:{scen_id}:{sterilizer_id}")
                    break
        for station_id, events in station_events.items():
            level = 0
            for _, delta in sorted(events, key=lambda x: (x[0], x[1])):
                level += delta
                if level > data.stations[station_id]["parallel_techs"]:
                    errors.append(f"station_overlap:{scen_id}:{station_id}")
                    break
        for station_id, count in heavy_count.items():
            if count > data.stations[station_id]["heavy_tray_daily_limit"]:
                errors.append(f"station_heavy_limit:{scen_id}:{station_id}")

    if errors:
        metrics["violated_groups"] = len(set(e.split(":")[0] for e in errors))
        metrics["theta"] = 1.0
    return not errors, errors, metrics


def write_solution(rows: List[Dict[str, object]]):
    columns = [
        "row_type",
        "scenario_id",
        "case_id",
        "room_id",
        "start_min",
        "scheduled",
        "tray_type",
        "quantity",
        "source",
        "source_case_id",
        "batch_id",
        "washer_id",
        "wash_start",
        "assembly_station",
        "assembly_start",
        "sterilizer_id",
        "sterilizer_start",
        "loaner_vendor",
        "emergency_flag",
        "notes",
    ]
    with SOLUTION_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: format_num(row.get(col, "")) for col in columns})


def write_log(lines: List[str]):
    LOG_PATH.write_text("\n".join(lines) + "\n")


def empty_schedule(data: Data) -> Dict[str, Dict[str, int]]:
    return {case_id: {"scheduled": 0, "room_id": "", "start_min": None} for case_id in data.cases}


def choose_case_to_drop(data: Data, fail_case_ids: List[str], banned_cases: set) -> Optional[str]:
    candidates = [cid for cid in fail_case_ids if cid not in banned_cases]
    if not candidates:
        return None
    return min(candidates, key=lambda cid: (data.cases[cid]["mandatory"], data.cases[cid]["cancel_penalty"], cid))


def copy_schedule(schedule: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
    return {case_id: dict(values) for case_id, values in schedule.items()}


def room_overlap_with_schedule(data: Data, schedule: Dict[str, Dict[str, int]], case_id: str, room_id: str, start: int) -> bool:
    room = data.rooms[room_id]
    case = data.cases[case_id]
    occ_end = start + case["duration_min"] + max(case["turnover_min"], room["turnover_buffer_min"])
    for other_id, other in schedule.items():
        if other_id == case_id or not other["scheduled"] or other["room_id"] != room_id:
            continue
        other_case = data.cases[other_id]
        other_end = other["start_min"] + other_case["duration_min"] + max(
            other_case["turnover_min"], room["turnover_buffer_min"]
        )
        if not (occ_end <= other["start_min"] or other_end <= start):
            return True
    return False


def candidate_keys_for_case(
    data: Data, schedule: Dict[str, Dict[str, int]], case_id: str, limit: int = 20
) -> List[Tuple[str, str, int]]:
    keys = []
    current = schedule.get(case_id, {"scheduled": 0, "room_id": "", "start_min": None})
    for key in data.candidates_by_case[case_id]:
        cand = data.candidates[key]
        if current["scheduled"] and key == (case_id, current["room_id"], current["start_min"]):
            continue
        if room_overlap_with_schedule(data, schedule, case_id, cand.room_id, cand.start):
            continue
        keys.append(key)
    keys.sort(
        key=lambda key: (
            data.candidates[key].delay,
            data.candidates[key].start,
            key[1],
        )
    )
    return keys[:limit]


def load_solution_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def schedule_from_rows(data: Data, rows: List[Dict[str, object]]) -> Dict[str, Dict[str, int]]:
    schedule = empty_schedule(data)
    for row in rows:
        if row.get("row_type") != "case":
            continue
        case_id = row["case_id"]
        scheduled = int(row["scheduled"]) if str(row["scheduled"]).strip() != "" else 0
        if scheduled:
            schedule[case_id] = {
                "scheduled": 1,
                "room_id": row["room_id"],
                "start_min": int(float(row["start_min"])),
            }
    return schedule


def recompute_objective_from_rows(data: Data, rows: List[Dict[str, object]]) -> Dict[str, object]:
    schedule = schedule_from_rows(data, rows)
    case_rows = [r for r in rows if r["row_type"] == "case"]
    tray_rows = [r for r in rows if r["row_type"] == "tray"]
    cart_rows = [r for r in rows if r["row_type"] == "cart"]
    batch_rows = [r for r in rows if r["row_type"] == "batch"]

    cancel_cost = 0.0
    room_ot_cost = 0.0
    room_ot_total = 0
    for case_id, sched in schedule.items():
        if not sched["scheduled"]:
            cancel_cost += data.cases[case_id]["cancel_penalty"]
    for room_id in data.rooms:
        room_end = max(
            [
                schedule[cid]["start_min"]
                + data.cases[cid]["duration_min"]
                + max(data.cases[cid]["turnover_min"], data.rooms[room_id]["turnover_buffer_min"])
                for cid in data.cases
                if schedule[cid]["scheduled"] and schedule[cid]["room_id"] == room_id
            ]
            or [data.rooms[room_id]["close_min"]]
        )
        room_ot = max(0, room_end - data.rooms[room_id]["close_min"])
        room_ot_total += room_ot
    room_ot_cost = data.config["room_overtime_weight"] * room_ot_total

    carts_by_scenario_case = {}
    for row in cart_rows:
        carts_by_scenario_case[(row["scenario_id"], row["case_id"])] = row

    scenario_costs = {}
    for scenario_id, scenario in data.scenarios.items():
        delay_cost = 0.0
        handling_cost = 0.0
        dwell_cost = 0.0
        for case_id, sched in schedule.items():
            if not sched["scheduled"]:
                continue
            delay_cost += data.cases[case_id]["delay_penalty_per_min"] * max(
                0, sched["start_min"] - data.cases[case_id]["target_start_min"]
            )
            cart = carts_by_scenario_case.get((scenario_id, case_id))
            if cart is None:
                continue
            route = data.routes[sched["room_id"]]
            qty = int(float(cart["quantity"]))
            dep = int(float(cart["start_min"]))
            latest_dep = sched["start_min"] - ceil_int(
                route["travel_minutes"] + route["room_settle_minutes"] + route["min_room_setup_buffer_min"]
            )
            handling_cost += route["handling_cost_per_tray"] * qty
            dwell_cost += data.config["cart_early_dwell_penalty_per_tray_min"] * qty * max(0, latest_dep - dep)

        loaner_cost = 0.0
        emergency_cost = 0.0
        for row in tray_rows:
            if row["scenario_id"] != scenario_id:
                continue
            tray_type = row["tray_type"]
            qty = int(float(row["quantity"]))
            if row["source"] == "loaner":
                vendor = data.vendor_map[(row["loaner_vendor"], tray_type)]
                loaner_cost += qty * (
                    vendor["cost_per_set"] + data.config["loaner_checkin_penalty"] * vendor["checkin_minutes_per_set"]
                )
            elif row["source"] == "emergency":
                emergency_cost += qty * data.config["emergency_cycle_penalty"]
                if data.trays[tray_type]["implant_sensitive"]:
                    emergency_cost += qty * data.config["implant_emergency_penalty"]

        spd_end = data.nominal_end
        for row in batch_rows:
            if row["scenario_id"] != scenario_id:
                continue
            tray = data.trays[row["tray_type"]]
            sterilizer = data.sterilizers[row["sterilizer_id"]]
            ready = int(float(row["sterilizer_start"])) + sterilizer["cycle_minutes"] + ceil_int(
                (tray["dry_minutes"] + tray["inspection_minutes"]) * scenario["sterilizer_multiplier"]
            ) + (sterilizer["implant_quarantine_min"] if tray["implant_sensitive"] else 0)
            spd_end = max(spd_end, ready)
        spd_ot = max(0, spd_end - data.nominal_end)
        scenario_costs[scenario_id] = (
            delay_cost
            + handling_cost
            + dwell_cost
            + loaner_cost
            + emergency_cost
            + data.config["spd_overtime_weight"] * spd_ot
        )

    expected_cost = sum(data.scenarios[s]["probability"] * scenario_costs.get(s, 0.0) for s in data.scenarios)
    tail_cost = data.config["scenario_tail_weight"] * max(scenario_costs.values() or [0.0])
    total_cost = cancel_cost + room_ot_cost + expected_cost + tail_cost
    return {
        "cancel_cost": cancel_cost,
        "room_ot_cost": room_ot_cost,
        "scenario_costs": scenario_costs,
        "expected_cost": expected_cost,
        "tail_cost": tail_cost,
        "total_cost": total_cost,
    }


def evaluate_schedule(
    data: Data, schedule: Dict[str, Dict[str, int]], time_limit: float
) -> Tuple[bool, Dict[str, object]]:
    build_ok, build_info = build_full_solution(data, schedule, time_limit)
    if not build_ok:
        return False, {"reason": build_info["reason"], "case_ids": build_info.get("case_ids", [])}
    rows = build_info["rows"]
    valid, errors, metrics = validate_solution(data, rows)
    if not valid:
        return False, {"reason": "validation_failed", "errors": errors, "metrics": metrics}
    objective = recompute_objective_from_rows(data, rows)
    return True, {"rows": rows, "objective": objective, "metrics": metrics}


def greedy_repair_and_improve(
    data: Data,
    base_schedule: Dict[str, Dict[str, int]],
    base_obj: float,
    time_limit: float,
) -> Tuple[Dict[str, Dict[str, int]], Optional[Dict[str, object]], List[str]]:
    start = time.time()
    current_schedule = copy_schedule(base_schedule)
    current_obj = base_obj
    current_eval = None
    move_log = []

    order = sorted(
        data.cases,
        key=lambda cid: (
            data.cases[cid]["mandatory"] == 0,
            -data.cases[cid]["cancel_penalty"],
            data.cases[cid]["target_start_min"],
            cid,
        ),
    )

    improved = True
    while improved and time.time() - start < time_limit:
        improved = False
        best_move = None
        for case_id in order:
            if current_schedule[case_id]["scheduled"]:
                continue
            for key in candidate_keys_for_case(data, current_schedule, case_id, limit=20):
                trial = copy_schedule(current_schedule)
                trial[case_id] = {"scheduled": 1, "room_id": key[1], "start_min": key[2]}
                ok, info = evaluate_schedule(data, trial, 15.0)
                if not ok:
                    continue
                obj = info["objective"]["total_cost"]
                if obj < current_obj - 1e-6 and (best_move is None or obj < best_move[0]):
                    best_move = (obj, case_id, key, info)
                if time.time() - start >= time_limit:
                    break
            if time.time() - start >= time_limit:
                break
        if best_move is not None:
            current_obj, case_id, key, current_eval = best_move
            current_schedule[case_id] = {"scheduled": 1, "room_id": key[1], "start_min": key[2]}
            move_log.append(f"add:{case_id}:{key[1]}:{key[2]}:{current_obj}")
            improved = True

    improved = True
    while improved and time.time() - start < time_limit:
        improved = False
        best_move = None
        for case_id in order:
            if not current_schedule[case_id]["scheduled"]:
                continue
            original = dict(current_schedule[case_id])
            current_schedule[case_id] = {"scheduled": 0, "room_id": "", "start_min": None}
            for key in candidate_keys_for_case(data, current_schedule, case_id, limit=12):
                trial = copy_schedule(current_schedule)
                trial[case_id] = {"scheduled": 1, "room_id": key[1], "start_min": key[2]}
                ok, info = evaluate_schedule(data, trial, 15.0)
                if not ok:
                    continue
                obj = info["objective"]["total_cost"]
                if obj < current_obj - 1e-6 and (best_move is None or obj < best_move[0]):
                    best_move = (obj, case_id, key, info)
                if time.time() - start >= time_limit:
                    break
            current_schedule[case_id] = original
            if time.time() - start >= time_limit:
                break
        if best_move is not None:
            current_obj, case_id, key, current_eval = best_move
            current_schedule[case_id] = {"scheduled": 1, "room_id": key[1], "start_min": key[2]}
            move_log.append(f"move:{case_id}:{key[1]}:{key[2]}:{current_obj}")
            improved = True

    if current_eval is None:
        ok, info = evaluate_schedule(data, current_schedule, 20.0)
        if ok:
            current_eval = info
    return current_schedule, current_eval, move_log


def main():
    start_wall = time.time()
    SUB_DIR.mkdir(exist_ok=True)
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Required model file missing: {MODEL_PATH}")

    total_limit = 240.0
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit:
        total_limit = min(total_limit, float(env_limit))

    data = Data()
    log_lines = []
    log_lines.append("# Solve Log")
    log_lines.append("")
    log_lines.append("## Commands")
    log_lines.append("- `PYTHONPATH=/opt/or-deps python -c \"import pyscipopt; print(pyscipopt.__version__)\"`")
    log_lines.append("- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`")
    log_lines.append("")
    log_lines.append("## Incumbent Audit")

    incumbent_rows = load_solution_rows(SOLUTION_PATH)
    incumbent_valid, incumbent_errors, incumbent_metrics = validate_solution(data, incumbent_rows)
    incumbent_objective = recompute_objective_from_rows(data, incumbent_rows) if incumbent_valid else None
    incumbent_schedule = schedule_from_rows(data, incumbent_rows) if incumbent_valid else empty_schedule(data)
    incumbent_total = incumbent_objective["total_cost"] if incumbent_objective is not None else float("inf")
    incumbent_scheduled = sum(s["scheduled"] for s in incumbent_schedule.values())
    log_lines.append(
        f"- Incumbent rows `{len(incumbent_rows)}`, scheduled cases `{incumbent_scheduled}`, valid `{incumbent_valid}`, theta `{incumbent_metrics['theta']}`, violated groups `{incumbent_metrics['violated_groups']}`, objective `{incumbent_total if incumbent_objective is not None else 'invalid'}`."
    )
    if incumbent_errors:
        log_lines.append(f"- Incumbent audit errors: `{'; '.join(incumbent_errors[:12])}`.")

    mandatory_probe = None
    for case_id in sorted([cid for cid, case in data.cases.items() if case["mandatory"]]):
        probe_schedule = empty_schedule(data)
        for key in candidate_keys_for_case(data, probe_schedule, case_id, limit=5):
            probe_schedule[case_id] = {"scheduled": 1, "room_id": key[1], "start_min": key[2]}
            ok, info = evaluate_schedule(data, probe_schedule, 10.0)
            if ok:
                mandatory_probe = (case_id, key[1], key[2], info["objective"]["total_cost"])
                break
            probe_schedule[case_id] = {"scheduled": 0, "room_id": "", "start_min": None}
        if mandatory_probe is not None:
            break
    log_lines.append(f"- Mandatory feasibility probe: `{mandatory_probe}`.")

    log_lines.append("")
    log_lines.append("## Repair And Search")

    elapsed = time.time() - start_wall
    remaining = max(5.0, total_limit - elapsed)
    heuristic_limit = min(120.0, max(30.0, remaining * 0.55))
    heuristic_schedule, heuristic_eval, move_log = greedy_repair_and_improve(
        data, incumbent_schedule, incumbent_total, heuristic_limit
    )
    if heuristic_eval is None:
        heuristic_eval = {"rows": incumbent_rows, "objective": incumbent_objective, "metrics": incumbent_metrics}
    heuristic_total = heuristic_eval["objective"]["total_cost"] if heuristic_eval["objective"] is not None else float("inf")
    log_lines.append(
        f"- Greedy/LNS candidate objective `{heuristic_total}`, scheduled `{sum(v['scheduled'] for v in heuristic_schedule.values())}`, moves `{len(move_log)}`."
    )
    if move_log:
        log_lines.append(f"- Accepted moves: `{'; '.join(move_log[:20])}`.")

    elapsed = time.time() - start_wall
    remaining = max(5.0, total_limit - elapsed)
    master_limit = min(45.0, max(10.0, remaining * 0.35))
    master_schedule, master_info = solve_master(data, set(), master_limit, heuristic_schedule)
    log_lines.append(
        f"- Bounded master with MIP start: status `{master_info['status']}`, has_solution `{master_info['has_solution']}`, obj `{master_info['obj']}`, bound `{master_info['best_bound']}`, gap `{master_info['gap']}`."
    )
    master_eval = None
    if master_schedule is not None:
        ok, info = evaluate_schedule(data, master_schedule, max(10.0, remaining * 0.25))
        if ok:
            master_eval = info
            log_lines.append(
                f"- Master-derived audited candidate objective `{info['objective']['total_cost']}`, scheduled `{sum(v['scheduled'] for v in master_schedule.values())}`."
            )
        else:
            log_lines.append(f"- Master-derived candidate rejected: `{info}`.")

    candidate_rows = None
    candidate_objective = None
    candidate_source = "incumbent"
    candidate_metrics = incumbent_metrics
    candidate_errors = incumbent_errors
    if heuristic_eval is not None and heuristic_total < incumbent_total - 1e-6:
        candidate_rows = heuristic_eval["rows"]
        candidate_objective = heuristic_eval["objective"]
        candidate_source = "heuristic"
        candidate_metrics = heuristic_eval["metrics"]
        candidate_errors = []
    if master_eval is not None and master_eval["objective"]["total_cost"] < (
        candidate_objective["total_cost"] if candidate_objective is not None else incumbent_total
    ) - 1e-6:
        candidate_rows = master_eval["rows"]
        candidate_objective = master_eval["objective"]
        candidate_source = "master"
        candidate_metrics = master_eval["metrics"]
        candidate_errors = []

    published = False
    final_rows = incumbent_rows
    final_objective = incumbent_objective
    final_valid = incumbent_valid
    final_errors = incumbent_errors
    final_metrics = incumbent_metrics
    if candidate_rows is not None and candidate_objective is not None:
        write_solution(candidate_rows)
        reparsed_rows = load_solution_rows(SOLUTION_PATH)
        reparsed_valid, reparsed_errors, reparsed_metrics = validate_solution(data, reparsed_rows)
        reparsed_objective = recompute_objective_from_rows(data, reparsed_rows) if reparsed_valid else None
        log_lines.append(
            f"- Published candidate source `{candidate_source}` reparsed valid `{reparsed_valid}`, theta `{reparsed_metrics['theta']}`, violated groups `{reparsed_metrics['violated_groups']}`, objective `{reparsed_objective['total_cost'] if reparsed_objective is not None else 'invalid'}`."
        )
        if reparsed_valid and reparsed_objective is not None and reparsed_objective["total_cost"] < incumbent_total - 1e-6:
            published = True
            final_rows = reparsed_rows
            final_objective = reparsed_objective
            final_valid = reparsed_valid
            final_errors = reparsed_errors
            final_metrics = reparsed_metrics
        else:
            if incumbent_rows:
                write_solution(incumbent_rows)
            final_rows = incumbent_rows
            final_objective = incumbent_objective
            final_valid = incumbent_valid
            final_errors = incumbent_errors
            final_metrics = incumbent_metrics
    else:
        log_lines.append("- No audited candidate beat the incumbent; incumbent kept unchanged.")

    log_lines.append("")
    log_lines.append("## Final")
    log_lines.append(f"- Incumbent objective: `{incumbent_total if incumbent_objective is not None else 'invalid'}`.")
    log_lines.append(
        f"- Best candidate objective: `{candidate_objective['total_cost'] if candidate_objective is not None else 'none'}` from `{candidate_source}`."
    )
    log_lines.append(f"- Published new artifact: `{published}`.")
    log_lines.append(f"- Final validation valid: `{final_valid}`.")
    log_lines.append(f"- Final validation theta: `{final_metrics['theta']}`.")
    log_lines.append(f"- Final violated groups: `{final_metrics['violated_groups']}`.")
    if final_errors:
        log_lines.append(f"- Final validation errors: `{'; '.join(final_errors[:12])}`.")
    if final_objective is not None:
        log_lines.append(f"- Final total cost: `{final_objective['total_cost']}`.")
        log_lines.append(f"- Final cancellation cost: `{final_objective['cancel_cost']}`.")
        log_lines.append(f"- Final room overtime cost: `{final_objective['room_ot_cost']}`.")
        log_lines.append(f"- Final expected scenario cost: `{final_objective['expected_cost']}`.")
        log_lines.append(f"- Final tail component: `{final_objective['tail_cost']}`.")
        log_lines.append(f"- Final scenario costs: `{final_objective['scenario_costs']}`.")
    log_lines.append(f"- Elapsed seconds: `{round(time.time() - start_wall, 3)}`.")
    write_log(log_lines)


if __name__ == "__main__":
    main()
