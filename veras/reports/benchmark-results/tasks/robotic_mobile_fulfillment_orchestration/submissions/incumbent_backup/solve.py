#!/usr/bin/env python3
import csv
import json
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import product
from typing import Dict, List, Optional, Tuple

from pyscipopt import Model, quicksum


DATA_DIR = "/app/data"
SUBMISSIONS_DIR = "/app/submissions"
SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.csv")
LOG_PATH = os.path.join(SUBMISSIONS_DIR, "solve_log.md")

SCIP_GAP = 0.0005
MAX_SINGLE_ATTEMPT_SECONDS = 300.0
TRAVEL_UNITS_PER_SLOT = 10.0
EPS = 1e-9


@dataclass(frozen=True)
class VisitTemplate:
    pod_id: str
    picks: Tuple[Tuple[str, int], ...]
    units: int
    service_minutes: int
    travel_distance: int
    travel_slots: int
    duration: int
    hazmat: bool
    aisle_group: str


@dataclass(frozen=True)
class Pattern:
    station_id: str
    line_to_pod: Tuple[Tuple[str, str], ...]
    visits: Tuple[VisitTemplate, ...]
    pods_used: Tuple[str, ...]
    split_extra: int
    travel_distance: int
    energy_used: float
    total_duration: int
    usage_by_pod_sku: Tuple[Tuple[str, str, int], ...]
    local_cost: float


@dataclass
class RobotState:
    available_time: int
    location_type: str
    location_id: str
    battery: float
    last_visit_hazmat: bool


@dataclass
class ScheduledVisit:
    visit_id: str
    order_id: str
    pod_id: str
    robot_id: str
    station_id: str
    start_slot: int
    end_slot: int
    duration: int
    travel_distance: int
    energy_used: float
    hazmat: bool
    aisle_group: str
    picks: Tuple[Tuple[str, int], ...]


@dataclass
class AttemptResult:
    imbalance_multiplier: float
    assignment_status: str
    assignment_gap: Optional[float]
    schedule_status: str
    schedule_gap: Optional[float]
    used_schedule_fallback: bool
    feasible: bool
    validation_errors: List[str]
    score: Optional[float]
    metrics: Dict[str, float]
    scheduled_visits: List[ScheduledVisit]
    order_completion: Dict[str, int]
    selected_patterns: Dict[str, Pattern]


def read_csv(name: str) -> List[Dict[str, str]]:
    path = os.path.join(DATA_DIR, name)
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_solution(scheduled_visits: List[ScheduledVisit]) -> None:
    rows: List[Dict[str, str]] = []
    for visit in sorted(scheduled_visits, key=lambda v: (v.start_slot, v.station_id, v.robot_id, v.visit_id)):
        rows.append(
            {
                "row_type": "visit",
                "visit_id": visit.visit_id,
                "order_id": "",
                "pod_id": visit.pod_id,
                "sku_id": "",
                "robot_id": visit.robot_id,
                "station_id": visit.station_id,
                "start_slot": str(visit.start_slot),
                "quantity": "",
            }
        )
        for sku_id, qty in visit.picks:
            rows.append(
                {
                    "row_type": "pick",
                    "visit_id": visit.visit_id,
                    "order_id": visit.order_id,
                    "pod_id": "",
                    "sku_id": sku_id,
                    "robot_id": "",
                    "station_id": "",
                    "start_slot": "",
                    "quantity": str(qty),
                }
            )

    with open(SOLUTION_PATH, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "row_type",
                "visit_id",
                "order_id",
                "pod_id",
                "sku_id",
                "robot_id",
                "station_id",
                "start_slot",
                "quantity",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


class Instance:
    def __init__(self) -> None:
        self.config = json.load(open(os.path.join(DATA_DIR, "config.json")))
        self.slot_minutes = int(self.config["slot_minutes"])
        self.horizon = int(self.config["planning_horizon_slots"])
        self.cleaning_buffer = int(self.config["cleaning_buffer_slots"])
        self.priority_lateness_cap = int(self.config["max_order_lateness_slots_for_priority_orders"])
        self.weights = self.config["objective_weights"]

        self.orders = {r["order_id"]: r for r in read_csv("orders.csv")}
        self.stations = {r["station_id"]: r for r in read_csv("stations.csv")}
        self.pods = {r["pod_id"]: r for r in read_csv("pods.csv")}
        self.robots = {r["robot_id"]: r for r in read_csv("robots.csv")}
        self.skus = {r["sku_id"]: r for r in read_csv("skus.csv")}
        self.zones = {r["zone_id"]: r for r in read_csv("zones.csv")}

        self.order_lines: Dict[str, List[Dict[str, int]]] = defaultdict(list)
        for row in read_csv("order_lines.csv"):
            self.order_lines[row["order_id"]].append({"sku_id": row["sku_id"], "quantity": int(row["quantity"])})

        self.pod_inventory: Dict[str, Dict[str, int]] = defaultdict(dict)
        for row in read_csv("pod_inventory.csv"):
            self.pod_inventory[row["pod_id"]][row["sku_id"]] = int(row["quantity"])

        self.promise_groups: Dict[str, List[str]] = defaultdict(list)
        self.promise_group_target: Dict[str, int] = {}
        for row in read_csv("promise_groups.csv"):
            self.promise_groups[row["group_id"]].append(row["order_id"])
            self.promise_group_target[row["group_id"]] = int(row["target_spread_slots"])

        self.robot_maintenance: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        for row in read_csv("robot_maintenance_windows.csv"):
            self.robot_maintenance[row["robot_id"]].append((int(row["start_slot"]), int(row["end_slot"])))
        for robot_id in self.robot_maintenance:
            self.robot_maintenance[robot_id].sort()

        self.station_windows: Dict[str, List[Tuple[int, int, int]]] = defaultdict(list)
        for row in read_csv("station_windows.csv"):
            self.station_windows[row["station_id"]].append(
                (int(row["start_slot"]), int(row["end_slot"]), int(row["max_active_orders"]))
            )
        for station_id in self.stations:
            if station_id not in self.station_windows:
                self.station_windows[station_id] = [(0, self.horizon, int(self.stations[station_id]["open_order_capacity"]))]
            self.station_windows[station_id].sort()

        self.aisle_capacity: Dict[Tuple[str, int], int] = {}
        self.peak_slots = set()
        for row in read_csv("aisle_capacity.csv"):
            key = (row["aisle_group"], int(row["slot"]))
            self.aisle_capacity[key] = int(row["capacity"])
            if int(row["peak"]) == 1:
                self.peak_slots.add(int(row["slot"]))

        self.station_xy = {sid: (int(r["x"]), int(r["y"])) for sid, r in self.stations.items()}
        self.zone_xy = {zid: (int(r["x"]), int(r["y"])) for zid, r in self.zones.items()}

        self.line_candidates: Dict[Tuple[str, str], List[str]] = {}
        for order_id, lines in self.order_lines.items():
            for line in lines:
                sku_id = line["sku_id"]
                qty = line["quantity"]
                candidates = []
                for pod_id, pod_row in self.pods.items():
                    if self.pod_inventory[pod_id].get(sku_id, 0) < qty:
                        continue
                    if self.skus[sku_id]["fragile"] == "1" and pod_row["weight_class"] == "heavy":
                        continue
                    candidates.append(pod_id)
                self.line_candidates[(order_id, sku_id)] = sorted(candidates)

    @staticmethod
    def manhattan(a: Tuple[int, int], b: Tuple[int, int]) -> int:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def loaded_travel_distance(self, pod_id: str, station_id: str) -> int:
        zone_id = self.pods[pod_id]["zone_id"]
        return self.manhattan(self.zone_xy[zone_id], self.station_xy[station_id])

    def loaded_travel_slots(self, pod_id: str, station_id: str) -> int:
        return max(1, math.ceil(self.loaded_travel_distance(pod_id, station_id) / TRAVEL_UNITS_PER_SLOT))

    def deadhead_slots(self, location_type: str, location_id: str, pod_id: str) -> int:
        pod_zone_id = self.pods[pod_id]["zone_id"]
        if location_type == "zone":
            distance = self.manhattan(self.zone_xy[location_id], self.zone_xy[pod_zone_id])
        else:
            distance = self.manhattan(self.station_xy[location_id], self.zone_xy[pod_zone_id])
        return math.ceil(distance / TRAVEL_UNITS_PER_SLOT)

    def energy_for_visit(self, robot_id: str, pod_id: str, station_id: str) -> float:
        distance = 2 * self.loaded_travel_distance(pod_id, station_id)
        return distance * float(self.robots[robot_id]["energy_per_distance"])

    def eligible_stations(self, order_id: str) -> List[str]:
        order = self.orders[order_id]
        stations = []
        for station_id, station in self.stations.items():
            if order["temperature_class"] == "cold" and station["allows_cold"] != "1":
                continue
            if order["hazmat"] == "1" and station["allows_hazmat"] != "1":
                continue
            stations.append(station_id)
        return stations


def split_items_into_visits(inst: Instance, station_id: str, pod_id: str, items: List[Tuple[str, int]], hazmat: bool) -> Tuple[VisitTemplate, ...]:
    max_units = int(inst.stations[station_id]["max_units_per_visit"])
    expanded: List[str] = []
    for sku_id, qty in items:
        expanded.extend([sku_id] * qty)
    expanded.sort(key=lambda sku: (-int(inst.skus[sku]["unit_pick_time"]), sku))

    chunks: List[List[str]] = []
    while expanded:
        chunk = expanded[:max_units]
        expanded = expanded[max_units:]
        chunks.append(chunk)

    travel_distance = 2 * inst.loaded_travel_distance(pod_id, station_id)
    travel_slots = inst.loaded_travel_slots(pod_id, station_id)
    aisle_group = inst.zones[inst.pods[pod_id]["zone_id"]]["aisle_group"]
    visits: List[VisitTemplate] = []
    for chunk in chunks:
        counter = Counter(chunk)
        picks = tuple(sorted(counter.items()))
        units = sum(counter.values())
        service_minutes = sum(counter[sku] * int(inst.skus[sku]["unit_pick_time"]) for sku in counter)
        duration = 2 * travel_slots + max(1, math.ceil(service_minutes / inst.slot_minutes))
        visits.append(
            VisitTemplate(
                pod_id=pod_id,
                picks=picks,
                units=units,
                service_minutes=service_minutes,
                travel_distance=travel_distance,
                travel_slots=travel_slots,
                duration=duration,
                hazmat=hazmat,
                aisle_group=aisle_group,
            )
        )
    return tuple(visits)


def enumerate_best_pattern(inst: Instance, order_id: str, station_id: str) -> Pattern:
    lines = sorted(inst.order_lines[order_id], key=lambda row: (len(inst.line_candidates[(order_id, row["sku_id"])]), row["sku_id"]))
    candidate_lists = [inst.line_candidates[(order_id, row["sku_id"])] for row in lines]
    hazmat = inst.orders[order_id]["hazmat"] == "1"

    best_pattern: Optional[Pattern] = None
    best_key: Optional[Tuple[float, int, int, Tuple[str, ...]]] = None

    for assigned_pods in product(*candidate_lists):
        used_pods = sorted(set(assigned_pods))
        if len(used_pods) > int(inst.orders[order_id]["max_split_pods"]):
            continue

        by_pod: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        usage_counter: Counter = Counter()
        for row, pod_id in zip(lines, assigned_pods):
            by_pod[pod_id].append((row["sku_id"], row["quantity"]))
            usage_counter[(pod_id, row["sku_id"])] += row["quantity"]

        visits: List[VisitTemplate] = []
        for pod_id, items in by_pod.items():
            visits.extend(split_items_into_visits(inst, station_id, pod_id, items, hazmat))

        visits_tuple = tuple(visits)
        split_extra = len(used_pods) - 1
        travel_distance = sum(v.travel_distance for v in visits_tuple)
        energy_used = sum(v.travel_distance * float(inst.robots[next(iter(inst.robots))]["energy_per_distance"]) for v in visits_tuple)
        total_duration = sum(v.duration for v in visits_tuple)
        usage_by_pod_sku = tuple(sorted((pod_id, sku_id, qty) for (pod_id, sku_id), qty in usage_counter.items()))
        local_cost = (
            inst.weights["split_order"] * split_extra
            + inst.weights["visit_travel"] * travel_distance
            + inst.weights["energy_used"] * energy_used
        )
        line_to_pod = tuple(sorted((row["sku_id"], pod_id) for row, pod_id in zip(lines, assigned_pods)))
        pattern = Pattern(
            station_id=station_id,
            line_to_pod=line_to_pod,
            visits=visits_tuple,
            pods_used=tuple(used_pods),
            split_extra=split_extra,
            travel_distance=travel_distance,
            energy_used=energy_used,
            total_duration=total_duration,
            usage_by_pod_sku=usage_by_pod_sku,
            local_cost=local_cost,
        )
        key = (local_cost, total_duration, len(visits_tuple), tuple(used_pods))
        if best_key is None or key < best_key:
            best_key = key
            best_pattern = pattern

    if best_pattern is None:
        raise RuntimeError(f"No feasible local pattern for order {order_id} at station {station_id}")
    return best_pattern


def generate_patterns(inst: Instance) -> Dict[str, List[Pattern]]:
    patterns: Dict[str, List[Pattern]] = {}
    for order_id in sorted(inst.orders):
        station_patterns = [enumerate_best_pattern(inst, order_id, station_id) for station_id in inst.eligible_stations(order_id)]
        patterns[order_id] = station_patterns
    return patterns


def safe_gap(model: Model) -> Optional[float]:
    try:
        if model.getNSols() > 0:
            return float(model.getGap())
    except Exception:
        return None
    return None


def configure_scip(model: Model, time_limit: float) -> None:
    model.setParam("limits/gap", SCIP_GAP)
    model.setParam("display/verblevel", 0)
    model.setParam("limits/time", max(1.0, time_limit))


def solve_assignment_master(inst: Instance, patterns: Dict[str, List[Pattern]], imbalance_multiplier: float, time_limit: float) -> Tuple[Dict[str, Pattern], str, Optional[float]]:
    model = Model("assignment_master")
    configure_scip(model, time_limit)

    y = {}
    for order_id, plist in patterns.items():
        for idx, _pattern in enumerate(plist):
            y[(order_id, idx)] = model.addVar(vtype="B", name=f"y[{order_id},{idx}]")

    station_pairs = [("ST_A", "ST_B"), ("ST_A", "ST_C"), ("ST_B", "ST_C")]
    load_expr = {}
    for station_id in inst.stations:
        load_expr[station_id] = quicksum(
            pattern.total_duration * y[(order_id, idx)]
            for order_id, plist in patterns.items()
            for idx, pattern in enumerate(plist)
            if pattern.station_id == station_id
        )
    diff = {
        pair: model.addVar(lb=0.0, vtype="C", name=f"diff[{pair[0]},{pair[1]}]")
        for pair in station_pairs
    }

    for order_id, plist in patterns.items():
        model.addCons(quicksum(y[(order_id, idx)] for idx in range(len(plist))) == 1, name=f"assign[{order_id}]")

    inventory_keys = set()
    for plist in patterns.values():
        for pattern in plist:
            for pod_id, sku_id, _qty in pattern.usage_by_pod_sku:
                inventory_keys.add((pod_id, sku_id))
    for pod_id, sku_id in sorted(inventory_keys):
        expr = quicksum(
            qty * y[(order_id, idx)]
            for order_id, plist in patterns.items()
            for idx, pattern in enumerate(plist)
            for p2, sku2, qty in pattern.usage_by_pod_sku
            if p2 == pod_id and sku2 == sku_id
        )
        model.addCons(expr <= inst.pod_inventory[pod_id].get(sku_id, 0), name=f"inv[{pod_id},{sku_id}]")

    for s1, s2 in station_pairs:
        model.addCons(diff[(s1, s2)] >= load_expr[s1] - load_expr[s2], name=f"diff_pos[{s1},{s2}]")
        model.addCons(diff[(s1, s2)] >= load_expr[s2] - load_expr[s1], name=f"diff_neg[{s1},{s2}]")

    objective = quicksum(
        pattern.local_cost * y[(order_id, idx)]
        for order_id, plist in patterns.items()
        for idx, pattern in enumerate(plist)
    ) + imbalance_multiplier * inst.weights["station_imbalance"] * quicksum(diff[pair] for pair in station_pairs)
    model.setObjective(objective, "minimize")
    model.optimize()

    if model.getNSols() == 0:
        selected = {order_id: min(plist, key=lambda p: p.local_cost) for order_id, plist in patterns.items()}
        return selected, str(model.getStatus()), safe_gap(model)

    selected_patterns: Dict[str, Pattern] = {}
    for order_id, plist in patterns.items():
        chosen_idx = max(range(len(plist)), key=lambda idx: model.getVal(y[(order_id, idx)]))
        selected_patterns[order_id] = plist[chosen_idx]
    return selected_patterns, str(model.getStatus()), safe_gap(model)


def solve_order_starts(inst: Instance, selected_patterns: Dict[str, Pattern], time_limit: float) -> Tuple[Dict[str, int], str, Optional[float], bool]:
    durations = {order_id: pattern.total_duration for order_id, pattern in selected_patterns.items()}
    feasible_times = {
        order_id: list(range(int(inst.orders[order_id]["release_slot"]), inst.horizon - durations[order_id] + 1))
        for order_id in selected_patterns
    }

    if any(not slots for slots in feasible_times.values()):
        return greedy_order_starts(inst, selected_patterns), "duration_infeasible", None, True

    model = Model("order_start")
    configure_scip(model, time_limit)

    x = {}
    for order_id, slots in feasible_times.items():
        for t in slots:
            x[(order_id, t)] = model.addVar(vtype="B", name=f"x[{order_id},{t}]")

    completion = {order_id: model.addVar(lb=0.0, vtype="C", name=f"C[{order_id}]") for order_id in selected_patterns}
    tardiness = {order_id: model.addVar(lb=0.0, vtype="C", name=f"T[{order_id}]") for order_id in selected_patterns}
    priority_excess = {
        order_id: model.addVar(lb=0.0, vtype="C", name=f"P[{order_id}]")
        for order_id, order in inst.orders.items()
        if order["priority"] == "priority"
    }
    group_max = {gid: model.addVar(lb=0.0, vtype="C", name=f"gmax[{gid}]") for gid in inst.promise_groups}
    group_min = {gid: model.addVar(lb=0.0, vtype="C", name=f"gmin[{gid}]") for gid in inst.promise_groups}
    group_spread = {gid: model.addVar(lb=0.0, vtype="C", name=f"gspread[{gid}]") for gid in inst.promise_groups}

    for order_id, slots in feasible_times.items():
        model.addCons(quicksum(x[(order_id, t)] for t in slots) == 1, name=f"start[{order_id}]")

    for station_id, station in inst.stations.items():
        capacity = int(station["pod_visits_per_slot"])
        station_orders = [order_id for order_id, pattern in selected_patterns.items() if pattern.station_id == station_id]
        for tau in range(inst.horizon):
            expr = quicksum(
                x[(order_id, t)]
                for order_id in station_orders
                for t in feasible_times[order_id]
                if t <= tau < t + durations[order_id]
            )
            model.addCons(expr <= capacity, name=f"cap[{station_id},{tau}]")

    for order_id, slots in feasible_times.items():
        model.addCons(
            completion[order_id]
            == quicksum((t + durations[order_id]) * x[(order_id, t)] for t in slots),
            name=f"completion[{order_id}]",
        )
        model.addCons(tardiness[order_id] >= completion[order_id] - int(inst.orders[order_id]["due_slot"]), name=f"late[{order_id}]")

    for order_id, pvar in priority_excess.items():
        model.addCons(pvar >= tardiness[order_id] - inst.priority_lateness_cap, name=f"prio_excess[{order_id}]")

    for group_id, order_ids in inst.promise_groups.items():
        for order_id in order_ids:
            model.addCons(group_max[group_id] >= completion[order_id], name=f"gmax[{group_id},{order_id}]")
            model.addCons(group_min[group_id] <= completion[order_id], name=f"gmin[{group_id},{order_id}]")
        model.addCons(
            group_spread[group_id] >= group_max[group_id] - group_min[group_id] - inst.promise_group_target[group_id],
            name=f"spread[{group_id}]",
        )

    objective = (
        inst.weights["weighted_tardiness"] * quicksum(tardiness[order_id] for order_id in selected_patterns)
        + inst.weights["priority_late"] * quicksum(priority_excess.values())
        + inst.weights["promise_group_spread"] * quicksum(group_spread.values())
        + 0.001 * quicksum(completion.values())
    )
    model.setObjective(objective, "minimize")
    model.optimize()

    if model.getNSols() == 0:
        return greedy_order_starts(inst, selected_patterns), str(model.getStatus()), safe_gap(model), True

    starts = {}
    for order_id, slots in feasible_times.items():
        starts[order_id] = max(slots, key=lambda t: model.getVal(x[(order_id, t)]))
    return starts, str(model.getStatus()), safe_gap(model), False


def greedy_order_starts(inst: Instance, selected_patterns: Dict[str, Pattern]) -> Dict[str, int]:
    starts: Dict[str, int] = {}
    for station_id in inst.stations:
        lane_end = [0] * int(inst.stations[station_id]["pod_visits_per_slot"])
        orders = [oid for oid, pattern in selected_patterns.items() if pattern.station_id == station_id]
        orders.sort(
            key=lambda oid: (
                inst.orders[oid]["priority"] != "priority",
                int(inst.orders[oid]["due_slot"]),
                int(inst.orders[oid]["release_slot"]),
                oid,
            )
        )
        for order_id in orders:
            lane = min(range(len(lane_end)), key=lambda i: lane_end[i])
            start = max(lane_end[lane], int(inst.orders[order_id]["release_slot"]))
            starts[order_id] = start
            lane_end[lane] = start + selected_patterns[order_id].total_duration
    return starts


def derive_lane_sequences(inst: Instance, selected_patterns: Dict[str, Pattern], planned_starts: Dict[str, int]) -> Dict[str, List[List[str]]]:
    lane_sequences: Dict[str, List[List[str]]] = {}
    for station_id in inst.stations:
        capacity = int(inst.stations[station_id]["pod_visits_per_slot"])
        lane_end = [0] * capacity
        lanes = [[] for _ in range(capacity)]
        orders = [oid for oid, pattern in selected_patterns.items() if pattern.station_id == station_id]
        orders.sort(key=lambda oid: (planned_starts[oid], int(inst.orders[oid]["due_slot"]), oid))
        for order_id in orders:
            eligible_lanes = [i for i in range(capacity) if lane_end[i] <= planned_starts[order_id]]
            if eligible_lanes:
                lane = max(eligible_lanes, key=lambda i: lane_end[i])
            else:
                lane = min(range(capacity), key=lambda i: lane_end[i])
            lanes[lane].append(order_id)
            lane_end[lane] = max(lane_end[lane], planned_starts[order_id]) + selected_patterns[order_id].total_duration
        lane_sequences[station_id] = lanes
    return lane_sequences


def order_visit_sequences(selected_patterns: Dict[str, Pattern]) -> Dict[str, Tuple[VisitTemplate, ...]]:
    pod_counts = Counter()
    for pattern in selected_patterns.values():
        for visit in pattern.visits:
            pod_counts[visit.pod_id] += 1

    sequences = {}
    for order_id, pattern in selected_patterns.items():
        visits = sorted(
            pattern.visits,
            key=lambda v: (-pod_counts[v.pod_id], -v.duration, v.pod_id, v.picks),
        )
        sequences[order_id] = tuple(visits)
    return sequences


def earliest_robot_start(inst: Instance, robot_id: str, robot_state: RobotState, visit: VisitTemplate, station_id: str, earliest: int) -> Optional[int]:
    ready = robot_state.available_time
    if robot_state.last_visit_hazmat and not visit.hazmat:
        ready += inst.cleaning_buffer
    ready += inst.deadhead_slots(robot_state.location_type, robot_state.location_id, visit.pod_id)
    t = max(earliest, ready)

    while True:
        shifted = False
        for start, end in inst.robot_maintenance.get(robot_id, []):
            if not (t + visit.duration <= start or t >= end):
                t = end
                shifted = True
                break
        if not shifted:
            break

    energy_used = inst.energy_for_visit(robot_id, visit.pod_id, station_id)
    min_battery = float(inst.robots[robot_id]["min_battery"])
    if robot_state.battery - energy_used < min_battery - EPS:
        return None
    if t + visit.duration > int(inst.robots[robot_id]["shift_end"]):
        return None
    return t


def find_earliest_visit_start(
    inst: Instance,
    visit: VisitTemplate,
    station_id: str,
    earliest: int,
    pod_available: Dict[str, int],
    aisle_usage: Dict[Tuple[str, int], int],
    robot_states: Dict[str, RobotState],
) -> Optional[Tuple[int, str]]:
    t = max(earliest, pod_available[visit.pod_id])
    latest_start = inst.horizon - visit.duration

    while t <= latest_start:
        violating_slot = None
        for tau in range(t, t + visit.duration):
            if aisle_usage[(visit.aisle_group, tau)] + 1 > inst.aisle_capacity[(visit.aisle_group, tau)]:
                violating_slot = tau
                break
        if violating_slot is not None:
            t = violating_slot + 1
            continue

        candidates = []
        for robot_id, state in robot_states.items():
            robot_t = earliest_robot_start(inst, robot_id, state, visit, station_id, t)
            if robot_t is None:
                continue
            battery_left = state.battery - inst.energy_for_visit(robot_id, visit.pod_id, station_id)
            candidates.append((robot_t, -battery_left, robot_id))

        if not candidates:
            return None

        candidates.sort()
        best_t, _neg_battery, best_robot = candidates[0]
        if best_t > t:
            t = best_t
            continue
        return best_t, best_robot
    return None


def realize_schedule(
    inst: Instance,
    selected_patterns: Dict[str, Pattern],
    planned_starts: Dict[str, int],
) -> Tuple[List[ScheduledVisit], Dict[str, int], List[str]]:
    lane_sequences = derive_lane_sequences(inst, selected_patterns, planned_starts)
    visit_sequences = order_visit_sequences(selected_patterns)

    robot_states = {
        robot_id: RobotState(
            available_time=int(robot["shift_start"]),
            location_type="zone",
            location_id=robot["start_zone"],
            battery=float(robot["initial_battery"]),
            last_visit_hazmat=False,
        )
        for robot_id, robot in inst.robots.items()
    }
    pod_available = {pod_id: 0 for pod_id in inst.pods}
    aisle_usage: Dict[Tuple[str, int], int] = Counter()
    scheduled_visits: List[ScheduledVisit] = []
    order_completion: Dict[str, int] = {}
    errors: List[str] = []

    visit_counter = 1
    lane_frontier = []
    for station_id, lanes in lane_sequences.items():
        for lane_idx, order_ids in enumerate(lanes):
            lane_frontier.append(
                {
                    "station_id": station_id,
                    "lane_idx": lane_idx,
                    "order_ids": order_ids,
                    "position": 0,
                    "available": 0,
                }
            )

    while True:
        candidates = []
        for frontier in lane_frontier:
            if frontier["position"] >= len(frontier["order_ids"]):
                continue
            order_id = frontier["order_ids"][frontier["position"]]
            ready = max(frontier["available"], planned_starts[order_id], int(inst.orders[order_id]["release_slot"]))
            candidates.append((ready, int(inst.orders[order_id]["due_slot"]), frontier["station_id"], frontier["lane_idx"], frontier))
        if not candidates:
            break

        _ready, _due, station_id, lane_idx, frontier = min(candidates)
        order_id = frontier["order_ids"][frontier["position"]]
        current = max(frontier["available"], planned_starts[order_id], int(inst.orders[order_id]["release_slot"]))

        for visit in visit_sequences[order_id]:
            found = find_earliest_visit_start(inst, visit, station_id, current, pod_available, aisle_usage, robot_states)
            if found is None:
                errors.append(f"No feasible robot/time assignment for order {order_id}, pod {visit.pod_id}")
                return scheduled_visits, order_completion, errors

            start_slot, robot_id = found
            end_slot = start_slot + visit.duration
            visit_id = f"V{visit_counter:03d}"
            visit_counter += 1

            for tau in range(start_slot, end_slot):
                aisle_usage[(visit.aisle_group, tau)] += 1
            pod_available[visit.pod_id] = end_slot + int(inst.pods[visit.pod_id]["return_buffer_slots"])

            robot_state = robot_states[robot_id]
            energy_used = inst.energy_for_visit(robot_id, visit.pod_id, station_id)
            robot_state.available_time = end_slot
            robot_state.location_type = "zone"
            robot_state.location_id = inst.pods[visit.pod_id]["zone_id"]
            robot_state.battery -= energy_used
            robot_state.last_visit_hazmat = visit.hazmat

            scheduled_visits.append(
                ScheduledVisit(
                    visit_id=visit_id,
                    order_id=order_id,
                    pod_id=visit.pod_id,
                    robot_id=robot_id,
                    station_id=station_id,
                    start_slot=start_slot,
                    end_slot=end_slot,
                    duration=visit.duration,
                    travel_distance=visit.travel_distance,
                    energy_used=energy_used,
                    hazmat=visit.hazmat,
                    aisle_group=visit.aisle_group,
                    picks=visit.picks,
                )
            )
            current = end_slot

        frontier["available"] = current
        frontier["position"] += 1
        order_completion[order_id] = current

    return scheduled_visits, order_completion, errors


def compute_metrics(inst: Instance, scheduled_visits: List[ScheduledVisit], order_completion: Dict[str, int], selected_patterns: Dict[str, Pattern]) -> Dict[str, float]:
    tardiness = sum(max(0, order_completion[order_id] - int(inst.orders[order_id]["due_slot"])) for order_id in inst.orders)
    priority_late = sum(
        max(0, order_completion[order_id] - int(inst.orders[order_id]["due_slot"]) - inst.priority_lateness_cap)
        for order_id, order in inst.orders.items()
        if order["priority"] == "priority"
    )
    promise_group_spread = 0
    for group_id, order_ids in inst.promise_groups.items():
        completions = [order_completion[order_id] for order_id in order_ids]
        promise_group_spread += max(0, max(completions) - min(completions) - inst.promise_group_target[group_id])

    station_load = Counter()
    visit_travel = 0
    energy_used = 0.0
    peak_congestion = 0
    for visit in scheduled_visits:
        station_load[visit.station_id] += visit.duration
        visit_travel += visit.travel_distance
        energy_used += visit.energy_used
        for tau in range(visit.start_slot, visit.end_slot):
            if tau in inst.peak_slots:
                peak_congestion += 1

    avg_station_load = sum(station_load[sid] for sid in inst.stations) / len(inst.stations)
    station_imbalance = sum(abs(station_load[sid] - avg_station_load) for sid in inst.stations)
    split_order = sum(pattern.split_extra for pattern in selected_patterns.values())
    charge_time = 0

    score = (
        inst.weights["weighted_tardiness"] * tardiness
        + inst.weights["priority_late"] * priority_late
        + inst.weights["promise_group_spread"] * promise_group_spread
        + inst.weights["visit_travel"] * visit_travel
        + inst.weights["energy_used"] * energy_used
        + inst.weights["split_order"] * split_order
        + inst.weights["station_imbalance"] * station_imbalance
        + inst.weights["peak_congestion"] * peak_congestion
        + inst.weights["charge_time"] * charge_time
    )
    return {
        "weighted_tardiness": float(tardiness),
        "priority_late": float(priority_late),
        "promise_group_spread": float(promise_group_spread),
        "visit_travel": float(visit_travel),
        "energy_used": float(energy_used),
        "split_order": float(split_order),
        "station_imbalance": float(station_imbalance),
        "peak_congestion": float(peak_congestion),
        "charge_time": float(charge_time),
        "approx_score": float(score),
    }


def validate_solution(inst: Instance, solution_path: str) -> Tuple[List[str], Dict[str, float]]:
    rows = read_solution_rows(solution_path)
    errors: List[str] = []

    expected_columns = [
        "row_type",
        "visit_id",
        "order_id",
        "pod_id",
        "sku_id",
        "robot_id",
        "station_id",
        "start_slot",
        "quantity",
    ]
    if rows["columns"] != expected_columns:
        errors.append("Solution columns do not match the required schema exactly")

    visits = rows["visits"]
    picks_by_visit = rows["picks_by_visit"]
    charges = rows["charges"]
    if charges:
        errors.append("Charge rows are not expected in this solver output")

    demand = Counter()
    for order_id, lines in inst.order_lines.items():
        for line in lines:
            demand[(order_id, line["sku_id"])] += line["quantity"]

    picked = Counter()
    pod_sku_used = Counter()
    order_stations = defaultdict(set)
    order_pods = defaultdict(set)
    order_first = {}
    order_last = {}
    station_usage = Counter()
    aisle_usage = Counter()
    pod_block_usage = Counter()
    robot_intervals = defaultdict(list)

    for visit_id in picks_by_visit:
        if visit_id not in visits:
            errors.append(f"Pick rows reference unknown visit_id {visit_id}")

    scheduled_visits: List[ScheduledVisit] = []
    for visit_id, visit in visits.items():
        pod_id = visit["pod_id"]
        station_id = visit["station_id"]
        robot_id = visit["robot_id"]
        start_slot = int(visit["start_slot"])
        picks = picks_by_visit.get(visit_id, [])
        if visit["pod_id"] == "" or visit["robot_id"] == "" or visit["station_id"] == "":
            errors.append(f"Visit row {visit_id} has blank required fields")
            continue
        if start_slot < 0:
            errors.append(f"Visit {visit_id} has negative start slot")
        if pod_id not in inst.pods or station_id not in inst.stations or robot_id not in inst.robots:
            errors.append(f"Visit {visit_id} references an unknown pod/station/robot")
            continue

        hazmat = False
        pick_counter = Counter()
        for pick in picks:
            order_id = pick["order_id"]
            sku_id = pick["sku_id"]
            qty = int(pick["quantity"])
            picked[(order_id, sku_id)] += qty
            pod_sku_used[(pod_id, sku_id)] += qty
            order_stations[order_id].add(station_id)
            order_pods[order_id].add(pod_id)
            if order_id not in inst.orders:
                errors.append(f"Pick on visit {visit_id} references unknown order {order_id}")
                continue
            if sku_id not in inst.skus:
                errors.append(f"Pick on visit {visit_id} references unknown SKU {sku_id}")
                continue
            if inst.skus[sku_id]["fragile"] == "1" and inst.pods[pod_id]["weight_class"] == "heavy":
                errors.append(f"Fragile SKU {sku_id} picked from heavy pod {pod_id}")
            if inst.orders[order_id]["hazmat"] == "1":
                hazmat = True
            pick_counter[sku_id] += qty

        service_minutes = sum(qty * int(inst.skus[sku_id]["unit_pick_time"]) for sku_id, qty in pick_counter.items())
        travel_distance = 2 * inst.loaded_travel_distance(pod_id, station_id)
        travel_slots = inst.loaded_travel_slots(pod_id, station_id)
        duration = 2 * travel_slots + max(1, math.ceil(service_minutes / inst.slot_minutes))
        end_slot = start_slot + duration
        aisle_group = inst.zones[inst.pods[pod_id]["zone_id"]]["aisle_group"]
        energy_used = inst.energy_for_visit(robot_id, pod_id, station_id)

        scheduled_visits.append(
            ScheduledVisit(
                visit_id=visit_id,
                order_id=picks[0]["order_id"] if picks else "",
                pod_id=pod_id,
                robot_id=robot_id,
                station_id=station_id,
                start_slot=start_slot,
                end_slot=end_slot,
                duration=duration,
                travel_distance=travel_distance,
                energy_used=energy_used,
                hazmat=hazmat,
                aisle_group=aisle_group,
                picks=tuple((pick["sku_id"], int(pick["quantity"])) for pick in picks),
            )
        )

        for tau in range(start_slot, end_slot):
            if tau < 0 or tau >= inst.horizon:
                errors.append(f"Visit {visit_id} extends outside the planning horizon")
                continue
            station_usage[(station_id, tau)] += 1
            aisle_usage[(aisle_group, tau)] += 1
        block_end = end_slot + int(inst.pods[pod_id]["return_buffer_slots"])
        for tau in range(start_slot, min(inst.horizon, block_end)):
            pod_block_usage[(pod_id, tau)] += 1
        robot_intervals[robot_id].append((start_slot, end_slot, visit_id, pod_id, station_id, hazmat))

        order_ids = {pick["order_id"] for pick in picks}
        for order_id in order_ids:
            order_first[order_id] = min(order_first.get(order_id, start_slot), start_slot)
            order_last[order_id] = max(order_last.get(order_id, end_slot), end_slot)

    for key, qty in demand.items():
        if picked[key] != qty:
            errors.append(f"Demand mismatch for {key[0]} / {key[1]}: picked {picked[key]} but required {qty}")

    for (pod_id, sku_id), qty in pod_sku_used.items():
        if qty > inst.pod_inventory[pod_id].get(sku_id, 0):
            errors.append(f"Pod inventory exceeded on {pod_id} / {sku_id}")

    for order_id, stations in order_stations.items():
        if len(stations) != 1:
            errors.append(f"Order {order_id} uses {len(stations)} stations")
        if len(order_pods[order_id]) > int(inst.orders[order_id]["max_split_pods"]):
            errors.append(f"Order {order_id} exceeds max split pods")
        if order_first.get(order_id, inst.horizon + 1) < int(inst.orders[order_id]["release_slot"]):
            errors.append(f"Order {order_id} begins before release")
        station_id = next(iter(stations))
        if inst.orders[order_id]["temperature_class"] == "cold" and inst.stations[station_id]["allows_cold"] != "1":
            errors.append(f"Cold order {order_id} uses non-cold station {station_id}")
        if inst.orders[order_id]["hazmat"] == "1" and inst.stations[station_id]["allows_hazmat"] != "1":
            errors.append(f"Hazmat order {order_id} uses non-hazmat station {station_id}")

    for station_id, station in inst.stations.items():
        cap = int(station["pod_visits_per_slot"])
        for tau in range(inst.horizon):
            if station_usage[(station_id, tau)] > cap:
                errors.append(f"Station capacity exceeded at {station_id}, slot {tau}")
        open_count = Counter()
        for order_id, order_station_set in order_stations.items():
            if station_id not in order_station_set:
                continue
            if order_id not in order_first or order_id not in order_last:
                continue
            for tau in range(order_first[order_id], order_last[order_id]):
                open_count[tau] += 1
        for start, end, cap_orders in inst.station_windows[station_id]:
            for tau in range(start, end):
                if open_count[tau] > cap_orders:
                    errors.append(f"Open-order window exceeded at {station_id}, slot {tau}")

    for (pod_id, tau), count in pod_block_usage.items():
        if count > 1:
            errors.append(f"Pod overlap/return buffer violated for {pod_id} at slot {tau}")

    for (aisle_group, tau), count in aisle_usage.items():
        if count > inst.aisle_capacity[(aisle_group, tau)]:
            errors.append(f"Aisle capacity exceeded for {aisle_group} at slot {tau}")

    for robot_id, intervals in robot_intervals.items():
        intervals.sort()
        robot = inst.robots[robot_id]
        current_battery = float(robot["initial_battery"])
        previous_end = int(robot["shift_start"])
        previous_loc_type = "zone"
        previous_loc_id = robot["start_zone"]
        previous_hazmat = False

        for start_slot, end_slot, visit_id, pod_id, station_id, hazmat in intervals:
            if start_slot < int(robot["shift_start"]) or end_slot > int(robot["shift_end"]):
                errors.append(f"Robot {robot_id} visit {visit_id} is outside shift")
            for maint_start, maint_end in inst.robot_maintenance.get(robot_id, []):
                if not (end_slot <= maint_start or start_slot >= maint_end):
                    errors.append(f"Robot {robot_id} visit {visit_id} overlaps maintenance")

            ready = previous_end
            if previous_hazmat and not hazmat:
                ready += inst.cleaning_buffer
            ready += inst.deadhead_slots(previous_loc_type, previous_loc_id, pod_id)
            if start_slot < ready:
                errors.append(f"Robot {robot_id} violates location continuity/cleaning before visit {visit_id}")

            energy_used = inst.energy_for_visit(robot_id, pod_id, station_id)
            current_battery -= energy_used
            if current_battery < float(robot["min_battery"]) - EPS:
                errors.append(f"Robot {robot_id} drops below reserve after visit {visit_id}")

            previous_end = end_slot
            previous_loc_type = "zone"
            previous_loc_id = inst.pods[pod_id]["zone_id"]
            previous_hazmat = hazmat

    completion = {order_id: order_last.get(order_id, inst.horizon) for order_id in inst.orders}
    selected_patterns_proxy = {}
    for order_id in inst.orders:
        stations = order_stations.get(order_id, set())
        station_id = next(iter(stations)) if stations else inst.eligible_stations(order_id)[0]
        selected_patterns_proxy[order_id] = Pattern(
            station_id=station_id,
            line_to_pod=tuple(),
            visits=tuple(),
            pods_used=tuple(sorted(order_pods.get(order_id, set()))),
            split_extra=max(0, len(order_pods.get(order_id, set())) - 1),
            travel_distance=0,
            energy_used=0.0,
            total_duration=max(0, completion[order_id] - order_first.get(order_id, completion[order_id])),
            usage_by_pod_sku=tuple(),
            local_cost=0.0,
        )

    metrics = compute_metrics(inst, scheduled_visits, completion, selected_patterns_proxy)
    return errors, metrics


def read_solution_rows(solution_path: str) -> Dict[str, object]:
    with open(solution_path, newline="") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        visits: Dict[str, Dict[str, str]] = {}
        picks_by_visit: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        charges: List[Dict[str, str]] = []
        for row in reader:
            row_type = row["row_type"]
            if row_type == "visit":
                visit_id = row["visit_id"]
                if visit_id in visits:
                    raise ValueError(f"Duplicate visit_id {visit_id}")
                visits[visit_id] = row
            elif row_type == "pick":
                picks_by_visit[row["visit_id"]].append(row)
            elif row_type == "charge":
                charges.append(row)
            else:
                raise ValueError(f"Unknown row_type {row_type}")
    return {"columns": columns, "visits": visits, "picks_by_visit": picks_by_visit, "charges": charges}


def summarize_attempts(attempts: List[AttemptResult], final_validation_errors: List[str], final_metrics: Dict[str, float], elapsed: float) -> str:
    lines = ["# Solve Log", "", "## Commands", "", "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`", ""]
    lines.extend(["## Attempts", ""])
    for idx, attempt in enumerate(attempts, start=1):
        lines.append(f"### Attempt {idx}")
        lines.append("")
        lines.append(f"- imbalance multiplier: `{attempt.imbalance_multiplier}`")
        lines.append(f"- assignment status: `{attempt.assignment_status}`")
        lines.append(f"- assignment gap: `{attempt.assignment_gap}`")
        lines.append(f"- order-start status: `{attempt.schedule_status}`")
        lines.append(f"- order-start gap: `{attempt.schedule_gap}`")
        lines.append(f"- schedule fallback used: `{attempt.used_schedule_fallback}`")
        lines.append(f"- feasible artifact: `{attempt.feasible}`")
        if attempt.score is not None:
            lines.append(f"- approximate score: `{attempt.score:.6f}`")
        if attempt.validation_errors:
            lines.append("- validation errors:")
            for err in attempt.validation_errors:
                lines.append(f"  - {err}")
        lines.append("")
    lines.extend(
        [
            "## Final Validation",
            "",
            f"- elapsed seconds: `{elapsed:.2f}`",
            f"- hard-constraint errors: `{len(final_validation_errors)}`",
            f"- weighted tardiness: `{final_metrics.get('weighted_tardiness', float('nan'))}`",
            f"- priority late excess: `{final_metrics.get('priority_late', float('nan'))}`",
            f"- promise group spread excess: `{final_metrics.get('promise_group_spread', float('nan'))}`",
            f"- visit travel: `{final_metrics.get('visit_travel', float('nan'))}`",
            f"- energy used: `{final_metrics.get('energy_used', float('nan'))}`",
            f"- split order count: `{final_metrics.get('split_order', float('nan'))}`",
            f"- station imbalance: `{final_metrics.get('station_imbalance', float('nan'))}`",
            f"- peak congestion proxy: `{final_metrics.get('peak_congestion', float('nan'))}`",
            f"- approximate score: `{final_metrics.get('approx_score', float('nan'))}`",
            "",
            "## Notes",
            "",
            "- The public prompt does not specify an exact soft-score formula for every term; the reported score is the solver's consistent public-data proxy.",
            "- Validation is recomputed from the exported `solution.csv`, not from in-memory solver variables.",
            "",
        ]
    )
    if final_validation_errors:
        lines.append("### Final Errors")
        lines.append("")
        for err in final_validation_errors:
            lines.append(f"- {err}")
        lines.append("")
    return "\n".join(lines)


def available_time(deadline: float) -> float:
    return max(1.0, deadline - time.time())


def solve_instance() -> None:
    start_time = time.time()
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit is not None:
        total_limit = min(MAX_SINGLE_ATTEMPT_SECONDS, float(env_limit))
    else:
        total_limit = MAX_SINGLE_ATTEMPT_SECONDS
    deadline = start_time + total_limit - 2.0

    inst = Instance()
    patterns = generate_patterns(inst)

    attempts: List[AttemptResult] = []
    best_attempt: Optional[AttemptResult] = None

    for imbalance_multiplier in [1.0, 2.0, 4.0]:
        if available_time(deadline) < 10:
            break

        assignment_limit = min(20.0, available_time(deadline) * 0.3)
        selected_patterns, assignment_status, assignment_gap = solve_assignment_master(
            inst, patterns, imbalance_multiplier, assignment_limit
        )

        schedule_limit = min(40.0, available_time(deadline) * 0.5)
        planned_starts, schedule_status, schedule_gap, schedule_fallback = solve_order_starts(
            inst, selected_patterns, schedule_limit
        )

        scheduled_visits, order_completion, schedule_errors = realize_schedule(inst, selected_patterns, planned_starts)
        feasible = len(schedule_errors) == 0
        metrics = {}
        score = None
        validation_errors: List[str] = list(schedule_errors)

        if feasible:
            write_solution(scheduled_visits)
            validation_errors, metrics = validate_solution(inst, SOLUTION_PATH)
            feasible = len(validation_errors) == 0
            if feasible:
                metrics = compute_metrics(inst, scheduled_visits, order_completion, selected_patterns)
                score = metrics["approx_score"]

        attempt = AttemptResult(
            imbalance_multiplier=imbalance_multiplier,
            assignment_status=assignment_status,
            assignment_gap=assignment_gap,
            schedule_status=schedule_status,
            schedule_gap=schedule_gap,
            used_schedule_fallback=schedule_fallback,
            feasible=feasible,
            validation_errors=validation_errors,
            score=score,
            metrics=metrics,
            scheduled_visits=scheduled_visits,
            order_completion=order_completion,
            selected_patterns=selected_patterns,
        )
        attempts.append(attempt)

        if feasible and (best_attempt is None or (score is not None and score < (best_attempt.score or float("inf")))):
            best_attempt = attempt

        if feasible and imbalance_multiplier >= 2.0:
            break

    if best_attempt is None:
        best_attempt = min(attempts, key=lambda a: (len(a.validation_errors), a.score if a.score is not None else float("inf")))
        if best_attempt.scheduled_visits:
            write_solution(best_attempt.scheduled_visits)
        else:
            with open(SOLUTION_PATH, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "row_type",
                        "visit_id",
                        "order_id",
                        "pod_id",
                        "sku_id",
                        "robot_id",
                        "station_id",
                        "start_slot",
                        "quantity",
                    ]
                )

    final_validation_errors, final_metrics = validate_solution(inst, SOLUTION_PATH)
    with open(LOG_PATH, "w") as f:
        f.write(summarize_attempts(attempts, final_validation_errors, final_metrics, time.time() - start_time))

    if final_validation_errors:
        raise RuntimeError("\n".join(final_validation_errors[:10]))


if __name__ == "__main__":
    solve_instance()
