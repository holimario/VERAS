import argparse
import csv
import json
import math
import os
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyscipopt import Model, quicksum


DATA_DIR = Path("/app/data")
SUBMISSIONS_DIR = Path("/app/submissions")
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.json"
MODEL_PATH = SUBMISSIONS_DIR / "model.md"
SOLVE_LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"


def day_of(minute: int) -> int:
    return minute // 1440 + 1


def as_bool(value: str) -> int:
    return int(value)


@dataclass(frozen=True)
class NominalPath:
    order_id: str
    index: int
    lanes: Tuple[str, ...]
    lane_set: frozenset
    booking_cost: float
    nonroad_teu_legs: float
    total_teu_legs: float
    contract_teu: Dict[Tuple[str, int], float]


@dataclass(frozen=True)
class ScenarioPath:
    order_id: str
    scenario_id: str
    index: int
    lanes: Tuple[str, ...]
    lane_set: frozenset
    base_pair_cost: float
    nonroad_teu_legs: float
    total_teu_legs: float
    emissions: float
    handling: Dict[Tuple[str, int], float]
    resources: Dict[Tuple[str, int, str], float]


def read_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def read_csv_rows(path: Path) -> List[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_instance(data_dir: Path) -> dict:
    config = read_json(data_dir / "config.json")

    terminals = {}
    for row in read_csv_rows(data_dir / "terminals.csv"):
        terminals[row["terminal_id"]] = {
            **row,
            "reefer_supported": as_bool(row["reefer_supported"]),
            "hazmat_allowed": as_bool(row["hazmat_allowed"]),
        }

    transfer_rules = {}
    for row in read_csv_rows(data_dir / "transfer_rules.csv"):
        transfer_rules[row["terminal_id"]] = {
            "base_transfer_minutes": int(row["base_transfer_minutes"]),
            "reefer_extra_minutes": int(row["reefer_extra_minutes"]),
            "hazmat_extra_minutes": int(row["hazmat_extra_minutes"]),
            "transfer_cost_per_teu": float(row["transfer_cost_per_teu"]),
        }

    lanes = []
    lane_by_id = {}
    outgoing = defaultdict(list)
    for row in read_csv_rows(data_dir / "lanes.csv"):
        lane = {
            **row,
            "depart_minute": int(row["depart_minute"]),
            "arrive_minute": int(row["arrive_minute"]),
            "booking_capacity_teu": int(row["booking_capacity_teu"]),
            "scenario_capacity_teu": int(row["scenario_capacity_teu"]),
            "booking_cost_per_teu": float(row["booking_cost_per_teu"]),
            "move_cost_per_teu": float(row["move_cost_per_teu"]),
            "emissions_kg_per_teu": float(row["emissions_kg_per_teu"]),
            "reefer_allowed": as_bool(row["reefer_allowed"]),
            "hazmat_allowed": as_bool(row["hazmat_allowed"]),
            "day": day_of(int(row["depart_minute"])),
            "is_nonroad": 1 if row["mode"] in {"rail", "barge"} else 0,
        }
        lanes.append(lane)
        lane_by_id[lane["lane_id"]] = lane
        outgoing[lane["origin"]].append(lane["lane_id"])

    for terminal_id, lane_ids in outgoing.items():
        lane_ids.sort(key=lambda lid: lane_by_id[lid]["depart_minute"])

    orders = []
    order_by_id = {}
    for row in read_csv_rows(data_dir / "orders.csv"):
        order = {
            **row,
            "ready_minute": int(row["ready_minute"]),
            "due_minute": int(row["due_minute"]),
            "teu": int(row["teu"]),
            "reefer": as_bool(row["reefer"]),
            "hazmat": as_bool(row["hazmat"]),
            "max_transfers": int(row["max_transfers"]),
            "reject_penalty": float(row["reject_penalty"]),
            "late_cost_per_teu_hour": float(row["late_cost_per_teu_hour"]),
        }
        orders.append(order)
        order_by_id[order["order_id"]] = order

    scenarios = []
    scenario_prob = {}
    for row in read_csv_rows(data_dir / "scenarios.csv"):
        scenarios.append(row["scenario_id"])
        scenario_prob[row["scenario_id"]] = float(row["probability"])

    scenario_lane_adjustments = defaultdict(dict)
    for row in read_csv_rows(data_dir / "scenario_lane_adjustments.csv"):
        scenario_lane_adjustments[row["scenario_id"]][row["lane_id"]] = {
            "capacity_teu": int(row["capacity_teu"]),
            "departure_delay_minutes": int(row["departure_delay_minutes"]),
            "arrival_delay_minutes": int(row["arrival_delay_minutes"]),
            "spot_surcharge_per_teu": float(row["spot_surcharge_per_teu"]),
            "cancelled": as_bool(row["cancelled"]),
        }

    throughput_limits = {}
    for row in read_csv_rows(data_dir / "scenario_terminal_limits.csv"):
        throughput_limits[(row["scenario_id"], row["terminal_id"], int(row["day"]))] = float(
            row["throughput_teu"]
        )

    resource_rows = {}
    for row in read_csv_rows(data_dir / "terminal_resource_limits.csv"):
        key = (row["scenario_id"], row["terminal_id"], int(row["day"]), row["resource_type"])
        resource_rows[key] = {
            "capacity_teu": float(row["capacity_teu"]),
            "overflow_cost_per_teu": float(row["overflow_cost_per_teu"]),
        }

    contracts = {}
    for row in read_csv_rows(data_dir / "operator_contracts.csv"):
        key = (row["operator"], int(row["day"]))
        contracts[key] = {
            "min_booking_teu": float(row["min_booking_teu"]),
            "max_booking_teu": float(row["max_booking_teu"]),
            "activation_cost": float(row["activation_cost"]),
            "shortfall_cost_per_teu": float(row["shortfall_cost_per_teu"]),
        }

    service_requirements = {}
    for row in read_csv_rows(data_dir / "service_requirements.csv"):
        service_requirements[row["priority"]] = float(row["min_accept_teu"])

    return {
        "config": config,
        "terminals": terminals,
        "transfer_rules": transfer_rules,
        "lanes": lanes,
        "lane_by_id": lane_by_id,
        "outgoing": outgoing,
        "orders": orders,
        "order_by_id": order_by_id,
        "scenarios": scenarios,
        "scenario_prob": scenario_prob,
        "scenario_lane_adjustments": scenario_lane_adjustments,
        "throughput_limits": throughput_limits,
        "resource_rows": resource_rows,
        "contracts": contracts,
        "service_requirements": service_requirements,
    }


def lane_state(instance: dict, lane_id: str, scenario_id: Optional[str]) -> Optional[dict]:
    lane = instance["lane_by_id"][lane_id]
    if scenario_id is None:
        return {
            "depart_minute": lane["depart_minute"],
            "arrive_minute": lane["arrive_minute"],
            "capacity_teu": float(lane["scenario_capacity_teu"]),
            "spot_surcharge_per_teu": 0.0,
            "cancelled": 0,
        }
    adjustment = instance["scenario_lane_adjustments"].get(scenario_id, {}).get(lane_id)
    if adjustment and adjustment["cancelled"]:
        return None
    if adjustment:
        return {
            "depart_minute": lane["depart_minute"] + adjustment["departure_delay_minutes"],
            "arrive_minute": lane["arrive_minute"] + adjustment["arrival_delay_minutes"],
            "capacity_teu": float(adjustment["capacity_teu"]),
            "spot_surcharge_per_teu": float(adjustment["spot_surcharge_per_teu"]),
            "cancelled": 0,
        }
    return {
        "depart_minute": lane["depart_minute"],
        "arrive_minute": lane["arrive_minute"],
        "capacity_teu": float(lane["scenario_capacity_teu"]),
        "spot_surcharge_per_teu": 0.0,
        "cancelled": 0,
    }


def transfer_minutes(instance: dict, order: dict, terminal_id: str) -> int:
    rule = instance["transfer_rules"][terminal_id]
    minutes = rule["base_transfer_minutes"]
    if order["reefer"]:
        minutes += rule["reefer_extra_minutes"]
    if order["hazmat"]:
        minutes += rule["hazmat_extra_minutes"]
    return minutes


def lane_compatible(instance: dict, order: dict, lane: dict) -> bool:
    origin_terminal = instance["terminals"][lane["origin"]]
    destination_terminal = instance["terminals"][lane["destination"]]
    if order["reefer"]:
        if not lane["reefer_allowed"]:
            return False
        if not origin_terminal["reefer_supported"] or not destination_terminal["reefer_supported"]:
            return False
    if order["hazmat"]:
        if not lane["hazmat_allowed"]:
            return False
        if not origin_terminal["hazmat_allowed"] or not destination_terminal["hazmat_allowed"]:
            return False
    return True


def enumerate_paths(instance: dict, order: dict, scenario_id: Optional[str]) -> List[Tuple[str, ...]]:
    max_legs = min(instance["config"]["max_path_legs"], order["max_transfers"] + 1)
    latest_arrival = order["due_minute"] + instance["config"]["max_lateness_minutes"]
    paths: List[Tuple[str, ...]] = []

    def dfs(node: str, current_arrival: Optional[int], path: List[str], visited: set) -> None:
        if node == order["destination"] and path:
            paths.append(tuple(path))
            return
        if len(path) >= max_legs:
            return
        for lane_id in instance["outgoing"].get(node, []):
            lane = instance["lane_by_id"][lane_id]
            if not lane_compatible(instance, order, lane):
                continue
            state = lane_state(instance, lane_id, scenario_id)
            if state is None:
                continue
            depart = state["depart_minute"]
            arrive = state["arrive_minute"]
            if not path:
                if depart < order["ready_minute"]:
                    continue
            else:
                required_depart = current_arrival + transfer_minutes(instance, order, node)
                if depart < required_depart:
                    continue
            if arrive > latest_arrival:
                continue
            if lane["destination"] in visited and lane["destination"] != order["destination"]:
                continue
            path.append(lane_id)
            dfs(lane["destination"], arrive, path, visited | {lane["destination"]})
            path.pop()

    dfs(order["origin"], None, [], {order["origin"]})
    paths.sort()
    return paths


def build_path_data(instance: dict) -> dict:
    nominal_paths: Dict[str, List[NominalPath]] = defaultdict(list)
    scenario_paths: Dict[Tuple[str, str], List[ScenarioPath]] = defaultdict(list)
    nominal_lookup: Dict[Tuple[str, Tuple[str, ...]], NominalPath] = {}
    scenario_lookup: Dict[Tuple[str, str, Tuple[str, ...]], ScenarioPath] = {}
    forced_reject = set()

    for order in instance["orders"]:
        nominal_lane_paths = enumerate_paths(instance, order, None)
        if not nominal_lane_paths:
            forced_reject.add(order["order_id"])
            continue

        order_scenario_lists = {}
        scenario_infeasible = False
        for scenario_id in instance["scenarios"]:
            lane_paths = enumerate_paths(instance, order, scenario_id)
            if not lane_paths:
                scenario_infeasible = True
                break
            order_scenario_lists[scenario_id] = lane_paths
        if scenario_infeasible:
            forced_reject.add(order["order_id"])
            continue

        for idx, lane_path in enumerate(nominal_lane_paths):
            contract_teu = defaultdict(float)
            booking_cost = 0.0
            nonroad_teu_legs = 0.0
            total_teu_legs = float(order["teu"] * len(lane_path))
            for lane_id in lane_path:
                lane = instance["lane_by_id"][lane_id]
                booking_cost += order["teu"] * lane["booking_cost_per_teu"]
                nonroad_teu_legs += order["teu"] * lane["is_nonroad"]
                contract_key = (lane["operator"], lane["day"])
                if contract_key in instance["contracts"]:
                    contract_teu[contract_key] += order["teu"]
            path = NominalPath(
                order_id=order["order_id"],
                index=idx,
                lanes=lane_path,
                lane_set=frozenset(lane_path),
                booking_cost=booking_cost,
                nonroad_teu_legs=nonroad_teu_legs,
                total_teu_legs=total_teu_legs,
                contract_teu=dict(contract_teu),
            )
            nominal_paths[order["order_id"]].append(path)
            nominal_lookup[(order["order_id"], lane_path)] = path

        for scenario_id, lane_paths in order_scenario_lists.items():
            for idx, lane_path in enumerate(lane_paths):
                move_cost = 0.0
                transfer_cost = 0.0
                emissions = 0.0
                handling = defaultdict(float)
                resources = defaultdict(float)
                nonroad_teu_legs = 0.0
                total_teu_legs = float(order["teu"] * len(lane_path))
                last_arrival = None
                for leg_idx, lane_id in enumerate(lane_path):
                    lane = instance["lane_by_id"][lane_id]
                    state = lane_state(instance, lane_id, scenario_id)
                    if state is None:
                        raise RuntimeError(f"Unexpected cancelled lane in enumerated path {lane_path}")
                    move_cost += order["teu"] * lane["move_cost_per_teu"]
                    emissions += order["teu"] * lane["emissions_kg_per_teu"]
                    nonroad_teu_legs += order["teu"] * lane["is_nonroad"]
                    handling[(lane["origin"], day_of(state["depart_minute"]))] += order["teu"]
                    if order["reefer"]:
                        key = (lane["origin"], day_of(state["depart_minute"]), "reefer_plug")
                        resources[key] += order["teu"]
                    if order["hazmat"]:
                        key = (lane["origin"], day_of(state["depart_minute"]), "hazmat_inspection")
                        resources[key] += order["teu"]
                    if leg_idx >= 1:
                        transfer_cost += (
                            order["teu"]
                            * instance["transfer_rules"][lane["origin"]]["transfer_cost_per_teu"]
                        )
                    last_arrival = state["arrive_minute"]
                lateness_hours = max(0.0, (last_arrival - order["due_minute"]) / 60.0)
                late_cost = order["teu"] * order["late_cost_per_teu_hour"] * lateness_hours
                path = ScenarioPath(
                    order_id=order["order_id"],
                    scenario_id=scenario_id,
                    index=idx,
                    lanes=lane_path,
                    lane_set=frozenset(lane_path),
                    base_pair_cost=move_cost + transfer_cost + late_cost,
                    nonroad_teu_legs=nonroad_teu_legs,
                    total_teu_legs=total_teu_legs,
                    emissions=emissions,
                    handling=dict(handling),
                    resources=dict(resources),
                )
                scenario_paths[(order["order_id"], scenario_id)].append(path)
                scenario_lookup[(order["order_id"], scenario_id, lane_path)] = path

    return {
        "nominal_paths": nominal_paths,
        "scenario_paths": scenario_paths,
        "nominal_lookup": nominal_lookup,
        "scenario_lookup": scenario_lookup,
        "forced_reject": forced_reject,
    }


def pair_spot_cost(instance: dict, order: dict, nominal_path: NominalPath, scenario_path: ScenarioPath) -> float:
    base_spot = float(instance["config"]["spot_surcharge_per_teu"])
    unreserved_penalty = float(instance["config"]["unreserved_lane_penalty_per_teu"])
    total = 0.0
    for lane_id in scenario_path.lanes:
        if lane_id in nominal_path.lane_set:
            continue
        state = lane_state(instance, lane_id, scenario_path.scenario_id)
        total += order["teu"] * (
            base_spot + unreserved_penalty + state["spot_surcharge_per_teu"]
        )
    return total


def pair_spot_cost_from_paths(
    instance: dict, order: dict, nominal_lane_set: frozenset, scenario_id: str, scenario_lanes: Tuple[str, ...]
) -> float:
    base_spot = float(instance["config"]["spot_surcharge_per_teu"])
    unreserved_penalty = float(instance["config"]["unreserved_lane_penalty_per_teu"])
    total = 0.0
    for lane_id in scenario_lanes:
        if lane_id in nominal_lane_set:
            continue
        state = lane_state(instance, lane_id, scenario_id)
        if state is None:
            raise RuntimeError(f"Cancelled lane {lane_id} encountered in audited scenario path")
        total += order["teu"] * (
            base_spot + unreserved_penalty + state["spot_surcharge_per_teu"]
        )
    return total


def strip_internal_metrics(metrics: dict) -> dict:
    return {
        key: value
        for key, value in metrics.items()
        if not key.startswith("_")
    }


def analyze_path(
    instance: dict,
    order: dict,
    raw_lane_path,
    scenario_id: Optional[str],
    context: str,
) -> Tuple[List[str], Optional[dict]]:
    issues: List[str] = []
    if not isinstance(raw_lane_path, list):
        issues.append(f"{context}: path is not a JSON array")
        return issues, None
    lane_path = tuple(raw_lane_path)
    max_legs = min(instance["config"]["max_path_legs"], order["max_transfers"] + 1)
    if not lane_path:
        issues.append(f"{context}: path is empty")
        return issues, None
    if len(lane_path) > max_legs:
        issues.append(
            f"{context}: path has {len(lane_path)} legs, exceeding limit {max_legs}"
        )

    booking_cost = 0.0
    move_cost = 0.0
    transfer_cost = 0.0
    emissions = 0.0
    nonroad_teu_legs = 0.0
    handling = defaultdict(float)
    resources = defaultdict(float)
    contract_teu = defaultdict(float)
    total_teu_legs = float(order["teu"] * len(lane_path))

    prev_lane = None
    prev_arrival = None
    for leg_idx, lane_id in enumerate(lane_path):
        lane = instance["lane_by_id"].get(lane_id)
        if lane is None:
            issues.append(f"{context}: lane {lane_id} is not listed in lanes.csv")
            return issues, None
        if not lane_compatible(instance, order, lane):
            issues.append(f"{context}: lane {lane_id} violates cargo compatibility")
        state = lane_state(instance, lane_id, scenario_id)
        if state is None:
            issues.append(f"{context}: lane {lane_id} is cancelled in scenario {scenario_id}")
            return issues, None

        if leg_idx == 0:
            if lane["origin"] != order["origin"]:
                issues.append(
                    f"{context}: first lane origin {lane['origin']} does not match order origin {order['origin']}"
                )
            if state["depart_minute"] < order["ready_minute"]:
                issues.append(
                    f"{context}: first departure {state['depart_minute']} precedes ready time {order['ready_minute']}"
                )
        else:
            transfer_terminal = prev_lane["destination"]
            if lane["origin"] != transfer_terminal:
                issues.append(
                    f"{context}: lane {lane_id} does not connect from terminal {transfer_terminal}"
                )
            min_depart = prev_arrival + transfer_minutes(instance, order, transfer_terminal)
            if state["depart_minute"] < min_depart:
                issues.append(
                    f"{context}: lane {lane_id} departs at {state['depart_minute']} before minimum transfer-ready time {min_depart}"
                )
            transfer_cost += order["teu"] * instance["transfer_rules"][transfer_terminal][
                "transfer_cost_per_teu"
            ]

        booking_cost += order["teu"] * lane["booking_cost_per_teu"]
        move_cost += order["teu"] * lane["move_cost_per_teu"]
        emissions += order["teu"] * lane["emissions_kg_per_teu"]
        nonroad_teu_legs += order["teu"] * lane["is_nonroad"]

        if scenario_id is not None:
            dep_day = day_of(state["depart_minute"])
            handling[(lane["origin"], dep_day)] += order["teu"]
            if order["reefer"]:
                resources[(lane["origin"], dep_day, "reefer_plug")] += order["teu"]
            if order["hazmat"]:
                resources[(lane["origin"], dep_day, "hazmat_inspection")] += order["teu"]
        else:
            contract_key = (lane["operator"], lane["day"])
            if contract_key in instance["contracts"]:
                contract_teu[contract_key] += order["teu"]

        prev_lane = lane
        prev_arrival = state["arrive_minute"]

    if prev_lane["destination"] != order["destination"]:
        issues.append(
            f"{context}: final lane destination {prev_lane['destination']} does not match order destination {order['destination']}"
        )
    latest_allowed = order["due_minute"] + instance["config"]["max_lateness_minutes"]
    if prev_arrival > latest_allowed:
        issues.append(
            f"{context}: arrival {prev_arrival} exceeds latest allowed {latest_allowed}"
        )

    lateness_hours = max(0.0, (prev_arrival - order["due_minute"]) / 60.0)
    late_cost = order["teu"] * order["late_cost_per_teu_hour"] * lateness_hours
    return issues, {
        "lanes": lane_path,
        "lane_set": frozenset(lane_path),
        "booking_cost": booking_cost,
        "move_cost": move_cost,
        "transfer_cost": transfer_cost,
        "late_cost": late_cost,
        "emissions": emissions,
        "nonroad_teu_legs": nonroad_teu_legs,
        "total_teu_legs": total_teu_legs,
        "handling": dict(handling),
        "resources": dict(resources),
        "contract_teu": dict(contract_teu),
    }


def choose_best_eta(scenario_cost: dict, scenario_prob: dict, alpha: float) -> Tuple[float, float]:
    candidate_etas = sorted({0.0, *scenario_cost.values()})
    best_eta = 0.0
    best_cvar = float("inf")
    for eta in candidate_etas:
        cvar = eta + (1.0 / (1.0 - alpha)) * sum(
            scenario_prob[scenario_id] * max(0.0, cost - eta)
            for scenario_id, cost in scenario_cost.items()
        )
        if cvar < best_cvar - 1e-12:
            best_cvar = cvar
            best_eta = eta
    return best_eta, best_cvar


def build_model(instance: dict, path_data: dict, time_limit: float) -> Tuple[Model, dict]:
    model = Model("synchromodal_freight_recovery")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", float(time_limit))

    nominal_vars = {}
    pair_vars = {}
    contract_active = {}
    contract_shortfall = {}
    overflow_vars = {}
    emission_excess = {}
    scenario_cost_vars = {}
    cvar_excess = {}

    feasible_orders = [
        order for order in instance["orders"] if order["order_id"] not in path_data["forced_reject"]
    ]

    for order in feasible_orders:
        order_id = order["order_id"]
        for nominal_path in path_data["nominal_paths"][order_id]:
            nominal_vars[(order_id, nominal_path.index)] = model.addVar(
                vtype="B", name=f"x[{order_id},{nominal_path.index}]"
            )
        for scenario_id in instance["scenarios"]:
            for nominal_path in path_data["nominal_paths"][order_id]:
                for scenario_path in path_data["scenario_paths"][(order_id, scenario_id)]:
                    pair_vars[(order_id, scenario_id, nominal_path.index, scenario_path.index)] = (
                        model.addVar(
                            vtype="B",
                            name=f"z[{order_id},{scenario_id},{nominal_path.index},{scenario_path.index}]",
                        )
                    )

    for contract_key in instance["contracts"]:
        contract_active[contract_key] = model.addVar(
            vtype="B", name=f"y[{contract_key[0]},{contract_key[1]}]"
        )
        contract_shortfall[contract_key] = model.addVar(
            lb=0.0, vtype="C", name=f"short[{contract_key[0]},{contract_key[1]}]"
        )

    for resource_key in instance["resource_rows"]:
        overflow_vars[resource_key] = model.addVar(
            lb=0.0,
            vtype="C",
            name=f"overflow[{resource_key[0]},{resource_key[1]},{resource_key[2]},{resource_key[3]}]",
        )

    eta = model.addVar(lb=0.0, vtype="C", name="eta")
    for scenario_id in instance["scenarios"]:
        emission_excess[scenario_id] = model.addVar(
            lb=0.0, vtype="C", name=f"emission_excess[{scenario_id}]"
        )
        scenario_cost_vars[scenario_id] = model.addVar(
            lb=0.0, vtype="C", name=f"scenario_cost[{scenario_id}]"
        )
        cvar_excess[scenario_id] = model.addVar(
            lb=0.0, vtype="C", name=f"cvar_excess[{scenario_id}]"
        )

    for order in feasible_orders:
        order_id = order["order_id"]
        x_expr = quicksum(
            nominal_vars[(order_id, nominal_path.index)]
            for nominal_path in path_data["nominal_paths"][order_id]
        )
        model.addCons(x_expr <= 1.0, name=f"select_nominal[{order_id}]")
        for scenario_id in instance["scenarios"]:
            for nominal_path in path_data["nominal_paths"][order_id]:
                z_expr = quicksum(
                    pair_vars[(order_id, scenario_id, nominal_path.index, scenario_path.index)]
                    for scenario_path in path_data["scenario_paths"][(order_id, scenario_id)]
                )
                model.addCons(
                    z_expr == nominal_vars[(order_id, nominal_path.index)],
                    name=f"pair_link[{order_id},{scenario_id},{nominal_path.index}]",
                )

    for lane in instance["lanes"]:
        model.addCons(
            quicksum(
                order["teu"]
                * (1.0 if lane["lane_id"] in nominal_path.lanes else 0.0)
                * nominal_vars[(order["order_id"], nominal_path.index)]
                for order in feasible_orders
                for nominal_path in path_data["nominal_paths"][order["order_id"]]
            )
            <= lane["booking_capacity_teu"],
            name=f"booking_cap[{lane['lane_id']}]",
        )

    for scenario_id in instance["scenarios"]:
        for lane in instance["lanes"]:
            state = lane_state(instance, lane["lane_id"], scenario_id)
            capacity = 0.0 if state is None else state["capacity_teu"]
            model.addCons(
                quicksum(
                    order["teu"]
                    * (1.0 if lane["lane_id"] in scenario_path.lanes else 0.0)
                    * pair_vars[(order["order_id"], scenario_id, nominal_path.index, scenario_path.index)]
                    for order in feasible_orders
                    for nominal_path in path_data["nominal_paths"][order["order_id"]]
                    for scenario_path in path_data["scenario_paths"][(order["order_id"], scenario_id)]
                )
                <= capacity,
                name=f"scenario_cap[{scenario_id},{lane['lane_id']}]",
            )

    for contract_key, contract_data in instance["contracts"].items():
        booked_expr = quicksum(
            nominal_path.contract_teu.get(contract_key, 0.0)
            * nominal_vars[(order["order_id"], nominal_path.index)]
            for order in feasible_orders
            for nominal_path in path_data["nominal_paths"][order["order_id"]]
        )
        model.addCons(
            booked_expr <= contract_data["max_booking_teu"] * contract_active[contract_key],
            name=f"contract_max[{contract_key[0]},{contract_key[1]}]",
        )
        model.addCons(
            contract_shortfall[contract_key]
            >= contract_data["min_booking_teu"] * contract_active[contract_key] - booked_expr,
            name=f"contract_shortfall[{contract_key[0]},{contract_key[1]}]",
        )

    for throughput_key, limit in instance["throughput_limits"].items():
        scenario_id, terminal_id, day = throughput_key
        model.addCons(
            quicksum(
                scenario_path.handling.get((terminal_id, day), 0.0)
                * pair_vars[(order["order_id"], scenario_id, nominal_path.index, scenario_path.index)]
                for order in feasible_orders
                for nominal_path in path_data["nominal_paths"][order["order_id"]]
                for scenario_path in path_data["scenario_paths"][(order["order_id"], scenario_id)]
            )
            <= limit,
            name=f"throughput[{scenario_id},{terminal_id},{day}]",
        )

    for resource_key, resource_data in instance["resource_rows"].items():
        scenario_id, terminal_id, day, resource_type = resource_key
        model.addCons(
            quicksum(
                scenario_path.resources.get((terminal_id, day, resource_type), 0.0)
                * pair_vars[(order["order_id"], scenario_id, nominal_path.index, scenario_path.index)]
                for order in feasible_orders
                for nominal_path in path_data["nominal_paths"][order["order_id"]]
                for scenario_path in path_data["scenario_paths"][(order["order_id"], scenario_id)]
            )
            <= resource_data["capacity_teu"] + overflow_vars[resource_key],
            name=f"resource[{scenario_id},{terminal_id},{day},{resource_type}]",
        )

    for priority, min_accept_teu in instance["service_requirements"].items():
        model.addCons(
            quicksum(
                order["teu"] * nominal_vars[(order["order_id"], nominal_path.index)]
                for order in feasible_orders
                if order["priority"] == priority
                for nominal_path in path_data["nominal_paths"][order["order_id"]]
            )
            >= min_accept_teu,
            name=f"service[{priority}]",
        )

    rho = float(instance["config"]["min_nonroad_teu_share"])
    model.addCons(
        quicksum(
            (nominal_path.nonroad_teu_legs - rho * nominal_path.total_teu_legs)
            * nominal_vars[(order["order_id"], nominal_path.index)]
            for order in feasible_orders
            for nominal_path in path_data["nominal_paths"][order["order_id"]]
        )
        >= 0.0,
        name="modal_share_nominal",
    )

    for scenario_id in instance["scenarios"]:
        model.addCons(
            quicksum(
                (scenario_path.nonroad_teu_legs - rho * scenario_path.total_teu_legs)
                * pair_vars[(order["order_id"], scenario_id, nominal_path.index, scenario_path.index)]
                for order in feasible_orders
                for nominal_path in path_data["nominal_paths"][order["order_id"]]
                for scenario_path in path_data["scenario_paths"][(order["order_id"], scenario_id)]
            )
            >= 0.0,
            name=f"modal_share[{scenario_id}]",
        )

    emission_cap = float(instance["config"]["emission_cap_kg_per_scenario"])
    emission_penalty = float(instance["config"]["emission_penalty_per_kg"])
    for scenario_id in instance["scenarios"]:
        emissions_expr = quicksum(
            scenario_path.emissions
            * pair_vars[(order["order_id"], scenario_id, nominal_path.index, scenario_path.index)]
            for order in feasible_orders
            for nominal_path in path_data["nominal_paths"][order["order_id"]]
            for scenario_path in path_data["scenario_paths"][(order["order_id"], scenario_id)]
        )
        model.addCons(
            emission_excess[scenario_id] >= emissions_expr - emission_cap,
            name=f"emission_excess[{scenario_id}]",
        )

        scenario_pair_cost = quicksum(
            (
                scenario_path.base_pair_cost
                + pair_spot_cost(instance, order, nominal_path, scenario_path)
            )
            * pair_vars[(order["order_id"], scenario_id, nominal_path.index, scenario_path.index)]
            for order in feasible_orders
            for nominal_path in path_data["nominal_paths"][order["order_id"]]
            for scenario_path in path_data["scenario_paths"][(order["order_id"], scenario_id)]
        )
        overflow_cost = quicksum(
            resource_data["overflow_cost_per_teu"] * overflow_vars[resource_key]
            for resource_key, resource_data in instance["resource_rows"].items()
            if resource_key[0] == scenario_id
        )
        model.addCons(
            scenario_cost_vars[scenario_id]
            == scenario_pair_cost + overflow_cost + emission_penalty * emission_excess[scenario_id],
            name=f"scenario_cost_def[{scenario_id}]",
        )
        model.addCons(
            cvar_excess[scenario_id] >= scenario_cost_vars[scenario_id] - eta,
            name=f"cvar_link[{scenario_id}]",
        )

    total_reject_penalty = sum(order["reject_penalty"] for order in instance["orders"])
    objective = total_reject_penalty
    objective -= quicksum(
        order["reject_penalty"] * nominal_vars[(order["order_id"], nominal_path.index)]
        for order in feasible_orders
        for nominal_path in path_data["nominal_paths"][order["order_id"]]
    )
    objective += quicksum(
        nominal_path.booking_cost * nominal_vars[(order["order_id"], nominal_path.index)]
        for order in feasible_orders
        for nominal_path in path_data["nominal_paths"][order["order_id"]]
    )
    objective += quicksum(
        contract_data["activation_cost"] * contract_active[contract_key]
        + contract_data["shortfall_cost_per_teu"] * contract_shortfall[contract_key]
        for contract_key, contract_data in instance["contracts"].items()
    )
    objective += quicksum(
        instance["scenario_prob"][scenario_id] * scenario_cost_vars[scenario_id]
        for scenario_id in instance["scenarios"]
    )
    alpha = float(instance["config"]["tail_risk_alpha"])
    tail_weight = float(instance["config"]["tail_risk_weight"])
    objective += tail_weight * (
        eta
        + (1.0 / (1.0 - alpha))
        * quicksum(
            instance["scenario_prob"][scenario_id] * cvar_excess[scenario_id]
            for scenario_id in instance["scenarios"]
        )
    )
    model.setObjective(objective, "minimize")

    return model, {
        "feasible_orders": feasible_orders,
        "nominal_vars": nominal_vars,
        "pair_vars": pair_vars,
        "contract_active": contract_active,
        "contract_shortfall": contract_shortfall,
        "overflow_vars": overflow_vars,
        "emission_excess": emission_excess,
        "scenario_cost_vars": scenario_cost_vars,
        "cvar_excess": cvar_excess,
        "eta": eta,
    }


def extract_solution(instance: dict, path_data: dict, model: Model, vars_data: dict) -> dict:
    if model.getNSols() <= 0 or model.getBestSol() is None:
        raise RuntimeError("SCIP finished without an incumbent; no solution can be exported.")
    sol = model.getBestSol()

    accepted_nominal = {}
    selected_pairs = {}
    for order in vars_data["feasible_orders"]:
        order_id = order["order_id"]
        chosen_nominal = None
        for nominal_path in path_data["nominal_paths"][order_id]:
            value = model.getSolVal(sol, vars_data["nominal_vars"][(order_id, nominal_path.index)])
            if value > 0.5:
                chosen_nominal = nominal_path
                break
        if chosen_nominal is not None:
            accepted_nominal[order_id] = chosen_nominal
            for scenario_id in instance["scenarios"]:
                chosen_scenario = None
                for scenario_path in path_data["scenario_paths"][(order_id, scenario_id)]:
                    key = (order_id, scenario_id, chosen_nominal.index, scenario_path.index)
                    value = model.getSolVal(sol, vars_data["pair_vars"][key])
                    if value > 0.5:
                        chosen_scenario = scenario_path
                        break
                if chosen_scenario is None:
                    raise RuntimeError(f"Missing scenario path for {order_id} in {scenario_id}")
                selected_pairs[(order_id, scenario_id)] = chosen_scenario

    orders_payload = []
    for order in instance["orders"]:
        order_id = order["order_id"]
        if order_id not in accepted_nominal:
            orders_payload.append(
                {
                    "order_id": order_id,
                    "accept": False,
                    "reserved_path": [],
                    "scenario_plans": [],
                }
            )
            continue
        scenario_plans = []
        for scenario_id in instance["scenarios"]:
            scenario_plans.append(
                {
                    "scenario_id": scenario_id,
                    "path": list(selected_pairs[(order_id, scenario_id)].lanes),
                }
            )
        orders_payload.append(
            {
                "order_id": order_id,
                "accept": True,
                "reserved_path": list(accepted_nominal[order_id].lanes),
                "scenario_plans": scenario_plans,
            }
        )

    return {"orders": orders_payload}


def compute_solution_metrics(instance: dict, path_data: dict, solution: dict, include_internal: bool = False) -> dict:
    issues = []
    max_hard_residual = 0.0

    order_entries = solution.get("orders")
    if not isinstance(order_entries, list):
        issues.append("solution.json field 'orders' must be a JSON array")
        order_entries = []

    if len(order_entries) != len(instance["orders"]):
        issues.append("solution.json does not contain exactly one row per order")

    order_ids = [order["order_id"] for order in instance["orders"]]
    order_id_set = set(order_ids)
    scenario_ids = list(instance["scenarios"])
    scenario_id_set = set(scenario_ids)

    entry_map = {}
    for entry in order_entries:
        if not isinstance(entry, dict):
            issues.append("each orders[] entry must be a JSON object")
            continue
        order_id = entry.get("order_id")
        if order_id not in order_id_set:
            issues.append(f"unknown order_id {order_id} in solution")
            continue
        if order_id in entry_map:
            issues.append(f"duplicate order_id {order_id} in solution")
            continue
        entry_map[order_id] = entry

    missing_orders = [order_id for order_id in order_ids if order_id not in entry_map]
    if missing_orders:
        issues.append(f"missing orders in solution: {missing_orders}")

    booking_usage = defaultdict(float)
    scenario_usage = defaultdict(float)
    throughput_usage = defaultdict(float)
    resource_usage = defaultdict(float)
    contract_usage = defaultdict(float)
    accepted_teu_by_priority = defaultdict(float)
    nominal_modal_expr = 0.0
    scenario_modal_expr = defaultdict(float)
    scenario_emissions = defaultdict(float)
    scenario_pair_cost = defaultdict(float)

    total_reject_cost = 0.0
    total_booking_cost = 0.0
    selected_nominal_paths = {}
    selected_scenario_paths = {}

    for order in instance["orders"]:
        order_id = order["order_id"]
        entry = entry_map.get(order_id)
        if entry is None:
            total_reject_cost += order["reject_penalty"]
            continue

        accept = entry.get("accept")
        if not isinstance(accept, bool):
            issues.append(f"order {order_id}: field 'accept' must be boolean")
            accept = bool(accept)

        reserved_path_raw = entry.get("reserved_path", [])
        scenario_plans_raw = entry.get("scenario_plans", [])

        if not accept:
            if reserved_path_raw:
                issues.append(f"rejected order {order_id} has nonempty reserved_path")
            if scenario_plans_raw:
                issues.append(f"rejected order {order_id} has nonempty scenario_plans")
            total_reject_cost += order["reject_penalty"]
            continue

        reserved_issues, nominal_info = analyze_path(
            instance,
            order,
            reserved_path_raw,
            None,
            f"order {order_id} reserved_path",
        )
        issues.extend(reserved_issues)
        if nominal_info is None:
            continue

        selected_nominal_paths[order_id] = nominal_info["lanes"]
        accepted_teu_by_priority[order["priority"]] += order["teu"]
        total_booking_cost += nominal_info["booking_cost"]
        nominal_modal_expr += nominal_info["nonroad_teu_legs"] - (
            instance["config"]["min_nonroad_teu_share"] * nominal_info["total_teu_legs"]
        )
        for lane_id in nominal_info["lanes"]:
            booking_usage[lane_id] += order["teu"]
        for contract_key, amount in nominal_info["contract_teu"].items():
            contract_usage[contract_key] += amount

        if not isinstance(scenario_plans_raw, list):
            issues.append(f"accepted order {order_id}: scenario_plans must be a JSON array")
            continue

        scenario_entry_map = {}
        for scenario_entry in scenario_plans_raw:
            if not isinstance(scenario_entry, dict):
                issues.append(f"accepted order {order_id}: scenario_plans entries must be JSON objects")
                continue
            scenario_id = scenario_entry.get("scenario_id")
            if scenario_id not in scenario_id_set:
                issues.append(f"accepted order {order_id}: unknown scenario_id {scenario_id}")
                continue
            if scenario_id in scenario_entry_map:
                issues.append(f"accepted order {order_id}: duplicate scenario_id {scenario_id}")
                continue
            scenario_entry_map[scenario_id] = scenario_entry

        missing_scenarios = [scenario_id for scenario_id in scenario_ids if scenario_id not in scenario_entry_map]
        if missing_scenarios:
            issues.append(
                f"accepted order {order_id} does not cover every scenario exactly once"
            )
        extra_count = max(0, len(scenario_plans_raw) - len(scenario_entry_map))
        if extra_count > 0 and len(scenario_entry_map) == len(scenario_ids):
            issues.append(f"accepted order {order_id} has invalid extra scenario plan rows")

        for scenario_id in scenario_ids:
            if scenario_id not in scenario_entry_map:
                continue
            scenario_issues, scenario_info = analyze_path(
                instance,
                order,
                scenario_entry_map[scenario_id].get("path", []),
                scenario_id,
                f"order {order_id} scenario {scenario_id}",
            )
            issues.extend(scenario_issues)
            if scenario_info is None:
                continue
            selected_scenario_paths[(order_id, scenario_id)] = scenario_info["lanes"]
            scenario_modal_expr[scenario_id] += scenario_info["nonroad_teu_legs"] - (
                instance["config"]["min_nonroad_teu_share"] * scenario_info["total_teu_legs"]
            )
            scenario_emissions[scenario_id] += scenario_info["emissions"]
            scenario_pair_cost[scenario_id] += (
                scenario_info["move_cost"]
                + scenario_info["transfer_cost"]
                + scenario_info["late_cost"]
                + pair_spot_cost_from_paths(
                    instance,
                    order,
                    nominal_info["lane_set"],
                    scenario_id,
                    scenario_info["lanes"],
                )
            )
            for lane_id in scenario_info["lanes"]:
                scenario_usage[(scenario_id, lane_id)] += order["teu"]
            for handling_key, amount in scenario_info["handling"].items():
                throughput_usage[(scenario_id, handling_key[0], handling_key[1])] += amount
            for resource_key, amount in scenario_info["resources"].items():
                resource_usage[(scenario_id, resource_key[0], resource_key[1], resource_key[2])] += amount

    for lane in instance["lanes"]:
        used = booking_usage[lane["lane_id"]]
        excess = used - lane["booking_capacity_teu"]
        if excess > 1e-8:
            issues.append(
                f"booking capacity exceeded on {lane['lane_id']}: used {used:.8f}, cap {lane['booking_capacity_teu']:.8f}"
            )
        if lane["booking_capacity_teu"] > 0:
            max_hard_residual = max(max_hard_residual, max(0.0, excess) / lane["booking_capacity_teu"])

    for scenario_id in scenario_ids:
        for lane in instance["lanes"]:
            state = lane_state(instance, lane["lane_id"], scenario_id)
            cap = 0.0 if state is None else state["capacity_teu"]
            used = scenario_usage[(scenario_id, lane["lane_id"])]
            excess = used - cap
            if excess > 1e-8:
                issues.append(
                    f"scenario capacity exceeded on {scenario_id}/{lane['lane_id']}: used {used:.8f}, cap {cap:.8f}"
                )
            if cap > 0:
                max_hard_residual = max(max_hard_residual, max(0.0, excess) / cap)

    for throughput_key, cap in instance["throughput_limits"].items():
        used = throughput_usage[throughput_key]
        excess = used - cap
        if excess > 1e-8:
            issues.append(
                f"throughput exceeded on {throughput_key}: used {used:.8f}, cap {cap:.8f}"
            )
        if cap > 0:
            max_hard_residual = max(max_hard_residual, max(0.0, excess) / cap)

    for contract_key, contract_data in instance["contracts"].items():
        used = contract_usage[contract_key]
        excess = used - contract_data["max_booking_teu"]
        if excess > 1e-8:
            issues.append(
                f"contract max exceeded on {contract_key}: used {used:.8f}, max {contract_data['max_booking_teu']:.8f}"
            )
        if contract_data["max_booking_teu"] > 0:
            max_hard_residual = max(
                max_hard_residual, max(0.0, excess) / contract_data["max_booking_teu"]
            )

    for priority, min_accept_teu in instance["service_requirements"].items():
        achieved = accepted_teu_by_priority[priority]
        shortfall = min_accept_teu - achieved
        if shortfall > 1e-8:
            issues.append(
                f"service floor missed for {priority}: accepted {achieved:.8f}, min {min_accept_teu:.8f}"
            )
        if min_accept_teu > 0:
            max_hard_residual = max(max_hard_residual, max(0.0, shortfall) / min_accept_teu)

    if nominal_modal_expr < -1e-8:
        issues.append(f"nominal modal-share constraint violated by {-nominal_modal_expr:.8f}")
    total_nominal_teu_legs = sum(
        len(selected_nominal_paths[order_id]) * instance["order_by_id"][order_id]["teu"]
        for order_id in selected_nominal_paths
    )
    if total_nominal_teu_legs > 0:
        max_hard_residual = max(
            max_hard_residual, max(0.0, -nominal_modal_expr) / total_nominal_teu_legs
        )

    for scenario_id in scenario_ids:
        expr = scenario_modal_expr[scenario_id]
        if expr < -1e-8:
            issues.append(f"modal-share constraint violated in {scenario_id} by {-expr:.8f}")
        total_teu_legs = sum(
            len(path) * instance["order_by_id"][order_id]["teu"]
            for (order_id, sid), path in selected_scenario_paths.items()
            if sid == scenario_id
        )
        if total_teu_legs > 0:
            max_hard_residual = max(max_hard_residual, max(0.0, -expr) / total_teu_legs)

    activation_cost = 0.0
    shortfall_cost = 0.0
    contract_active = {}
    contract_shortfall = {}
    for contract_key, contract_data in instance["contracts"].items():
        used = contract_usage[contract_key]
        active = 1.0 if used > 1e-8 else 0.0
        shortfall = max(0.0, contract_data["min_booking_teu"] - used) if active > 0.5 else 0.0
        contract_active[contract_key] = active
        contract_shortfall[contract_key] = shortfall
        if active > 0.5:
            activation_cost += contract_data["activation_cost"]
            shortfall_cost += shortfall * contract_data["shortfall_cost_per_teu"]

    overflow_amount = {}
    overflow_cost = defaultdict(float)
    for resource_key, resource_data in instance["resource_rows"].items():
        overflow = max(0.0, resource_usage[resource_key] - resource_data["capacity_teu"])
        overflow_amount[resource_key] = overflow
        overflow_cost[resource_key[0]] += overflow * resource_data["overflow_cost_per_teu"]

    emission_excess = {}
    emission_penalty = defaultdict(float)
    for scenario_id in scenario_ids:
        excess = max(
            0.0,
            scenario_emissions[scenario_id]
            - float(instance["config"]["emission_cap_kg_per_scenario"]),
        )
        emission_excess[scenario_id] = excess
        emission_penalty[scenario_id] = excess * float(instance["config"]["emission_penalty_per_kg"])

    scenario_cost = {}
    for scenario_id in scenario_ids:
        scenario_cost[scenario_id] = (
            scenario_pair_cost[scenario_id]
            + overflow_cost[scenario_id]
            + emission_penalty[scenario_id]
        )

    expected_scenario_cost = sum(
        instance["scenario_prob"][scenario_id] * scenario_cost[scenario_id]
        for scenario_id in scenario_ids
    )
    alpha = float(instance["config"]["tail_risk_alpha"])
    best_eta, best_cvar = choose_best_eta(scenario_cost, instance["scenario_prob"], alpha)
    total_objective = (
        total_reject_cost
        + total_booking_cost
        + activation_cost
        + shortfall_cost
        + expected_scenario_cost
        + float(instance["config"]["tail_risk_weight"]) * best_cvar
    )

    metrics = {
        "issues": issues,
        "max_hard_residual": max(0.0, max_hard_residual),
        "accepted_orders": sorted(selected_nominal_paths),
        "rejected_orders": sorted(set(order_ids) - set(selected_nominal_paths)),
        "accepted_teu_by_priority": dict(accepted_teu_by_priority),
        "booking_usage": dict(booking_usage),
        "scenario_cost": scenario_cost,
        "expected_scenario_cost": expected_scenario_cost,
        "overflow_cost": dict(overflow_cost),
        "emission_penalty": dict(emission_penalty),
        "total_objective": total_objective,
        "incumbent_exists": len(selected_nominal_paths) > 0 or total_reject_cost > 0.0,
    }
    if include_internal:
        metrics.update(
            {
                "_selected_nominal_paths": selected_nominal_paths,
                "_selected_scenario_paths": selected_scenario_paths,
                "_contract_usage": dict(contract_usage),
                "_contract_active": contract_active,
                "_contract_shortfall": contract_shortfall,
                "_resource_usage": dict(resource_usage),
                "_overflow_amount": overflow_amount,
                "_scenario_emissions": dict(scenario_emissions),
                "_emission_excess": emission_excess,
                "_best_eta": best_eta,
            }
        )
    return metrics


def write_solution(solution: dict, path: Path) -> None:
    with path.open("w") as f:
        json.dump(solution, f, indent=2)
        f.write("\n")


def load_incumbent_audit(instance: dict, path_data: dict, solution_path: Path) -> Optional[dict]:
    if not solution_path.exists():
        return None
    try:
        solution = read_json(solution_path)
    except Exception:
        return None
    audit = compute_solution_metrics(instance, path_data, solution, include_internal=True)
    audit["_solution"] = solution
    audit["_path"] = solution_path
    return audit


def add_incumbent_mip_start(
    instance: dict,
    path_data: dict,
    model: Model,
    vars_data: dict,
    incumbent_audit: dict,
) -> bool:
    if incumbent_audit is None or incumbent_audit["issues"]:
        return False

    start = model.createSol()
    selected_nominal_paths = incumbent_audit["_selected_nominal_paths"]
    selected_scenario_paths = incumbent_audit["_selected_scenario_paths"]

    for order in vars_data["feasible_orders"]:
        order_id = order["order_id"]
        chosen_nominal = selected_nominal_paths.get(order_id)
        chosen_nominal_idx = None
        if chosen_nominal is not None:
            nominal_path = path_data["nominal_lookup"].get((order_id, chosen_nominal))
            if nominal_path is None:
                return False
            chosen_nominal_idx = nominal_path.index
        for nominal_path in path_data["nominal_paths"][order_id]:
            model.setSolVal(
                start,
                vars_data["nominal_vars"][(order_id, nominal_path.index)],
                1.0 if nominal_path.index == chosen_nominal_idx else 0.0,
            )
        for scenario_id in instance["scenarios"]:
            chosen_scenario = selected_scenario_paths.get((order_id, scenario_id))
            chosen_scenario_idx = None
            if chosen_nominal_idx is not None and chosen_scenario is not None:
                scenario_path = path_data["scenario_lookup"].get((order_id, scenario_id, chosen_scenario))
                if scenario_path is None:
                    return False
                chosen_scenario_idx = scenario_path.index
            for nominal_path in path_data["nominal_paths"][order_id]:
                for scenario_path in path_data["scenario_paths"][(order_id, scenario_id)]:
                    value = 1.0 if (
                        nominal_path.index == chosen_nominal_idx
                        and scenario_path.index == chosen_scenario_idx
                    ) else 0.0
                    model.setSolVal(
                        start,
                        vars_data["pair_vars"][(order_id, scenario_id, nominal_path.index, scenario_path.index)],
                        value,
                    )

    for contract_key, var in vars_data["contract_active"].items():
        model.setSolVal(start, var, incumbent_audit["_contract_active"][contract_key])
        model.setSolVal(start, vars_data["contract_shortfall"][contract_key], incumbent_audit["_contract_shortfall"][contract_key])

    for resource_key, var in vars_data["overflow_vars"].items():
        model.setSolVal(start, var, incumbent_audit["_overflow_amount"][resource_key])

    for scenario_id, var in vars_data["emission_excess"].items():
        model.setSolVal(start, var, incumbent_audit["_emission_excess"][scenario_id])
        model.setSolVal(start, vars_data["scenario_cost_vars"][scenario_id], incumbent_audit["scenario_cost"][scenario_id])
        model.setSolVal(start, vars_data["cvar_excess"][scenario_id], max(0.0, incumbent_audit["scenario_cost"][scenario_id] - incumbent_audit["_best_eta"]))
    model.setSolVal(start, vars_data["eta"], incumbent_audit["_best_eta"])

    return bool(model.addSol(start, free=True))


def solve_instance() -> dict:
    instance = load_instance(DATA_DIR)
    path_data = build_path_data(instance)

    config_limit = float(instance["config"].get("default_time_limit_seconds", 300))
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit is not None:
        time_limit = min(config_limit, float(env_limit))
    else:
        time_limit = config_limit

    incumbent_audit = load_incumbent_audit(instance, path_data, SOLUTION_PATH)

    start = time.time()
    model, vars_data = build_model(instance, path_data, time_limit)
    mip_start_loaded = add_incumbent_mip_start(instance, path_data, model, vars_data, incumbent_audit)
    model.optimize()
    elapsed = time.time() - start

    result = {
        "status": str(model.getStatus()),
        "time_limit_seconds": time_limit,
        "elapsed_seconds": elapsed,
        "forced_reject_orders": sorted(path_data["forced_reject"]),
        "n_solutions": int(model.getNSols()),
        "primal_bound": None,
        "dual_bound": None,
        "gap": None,
        "mip_start_loaded": mip_start_loaded,
        "incumbent_audit": None if incumbent_audit is None else {
            "issues": list(incumbent_audit["issues"]),
            "max_hard_residual": incumbent_audit["max_hard_residual"],
            "total_objective": incumbent_audit["total_objective"],
        },
        "candidate_metrics": None,
        "published_solution": False,
    }
    if model.getNSols() > 0 and model.getBestSol() is not None:
        result["primal_bound"] = float(model.getObjVal())
        result["dual_bound"] = float(model.getDualbound())
        try:
            result["gap"] = float(model.getGap())
        except Exception:
            result["gap"] = None
        candidate_solution = extract_solution(instance, path_data, model, vars_data)
        candidate_metrics = compute_solution_metrics(
            instance, path_data, candidate_solution, include_internal=True
        )
        result["candidate_metrics"] = strip_internal_metrics(candidate_metrics)

        incumbent_valid = incumbent_audit is not None and not incumbent_audit["issues"]
        candidate_valid = not candidate_metrics["issues"]
        publish = False
        if candidate_valid:
            if not incumbent_valid:
                publish = True
            else:
                publish = candidate_metrics["total_objective"] < incumbent_audit["total_objective"] - 1e-8

        if publish:
            if SOLUTION_PATH.exists():
                shutil.copy2(SOLUTION_PATH, SUBMISSIONS_DIR / "solution.rollback.json")
            write_solution(candidate_solution, SOLUTION_PATH)
            result["published_solution"] = True
        elif incumbent_audit is None and candidate_valid:
            write_solution(candidate_solution, SOLUTION_PATH)
            result["published_solution"] = True
    return result


def validate_only(solution_path: Path) -> dict:
    instance = load_instance(DATA_DIR)
    path_data = build_path_data(instance)
    solution = read_json(solution_path)
    return strip_internal_metrics(compute_solution_metrics(instance, path_data, solution, include_internal=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", type=Path, default=None)
    args = parser.parse_args()

    if args.validate_only is not None:
        metrics = validate_only(args.validate_only)
        print(json.dumps(metrics, indent=2, sort_keys=True))
        return

    if not MODEL_PATH.exists():
        raise RuntimeError(f"Required model file not found at {MODEL_PATH}")

    result = solve_instance()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
