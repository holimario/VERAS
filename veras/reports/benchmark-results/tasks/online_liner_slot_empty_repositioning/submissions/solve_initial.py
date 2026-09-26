#!/usr/bin/env python3
from __future__ import annotations

import csv
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
PLAN_PATH = SUBMISSIONS_DIR / "initial_plan.csv"
CANDIDATE_PLAN_PATH = SUBMISSIONS_DIR / "initial_plan_candidate.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log_initial.md"

SCIP_REL_GAP = 0.0005
DEFAULT_TIME_LIMIT = 60.0
EPS = 1e-8
CARBON_COST_PER_KG = 1.0


@dataclass(frozen=True)
class Port:
    port_id: str
    region: str
    initial_std: float
    initial_reefer: float
    yard_capacity: float
    reefer_plug_capacity: float
    gate_out_default: float
    gate_in_default: float
    storage_cost_std: float
    storage_cost_reefer: float


@dataclass(frozen=True)
class Leg:
    leg_id: str
    service: str
    from_port: str
    to_port: str
    depart_day: int
    arrive_day: int
    teu_capacity: float
    reefer_plug_capacity: float
    dg1_limit: float


@dataclass(frozen=True)
class Booking:
    booking_id: str
    customer: str
    origin_port: str
    destination_port: str
    container_type: str
    teu: float
    earliest_load_day: int
    due_arrival_day: int
    revenue: float
    reject_penalty: float
    priority: str
    hazmat_class: str
    medical_flag: bool
    change_penalty: float


@dataclass(frozen=True)
class BookingPathRule:
    booking_id: str
    max_transship_count: int
    allowed_services: Tuple[str, ...]
    requires_direct: bool
    customs_group: str
    cold_chain_preclear: bool


@dataclass(frozen=True)
class ServiceRule:
    rule_id: str
    priority: str
    min_accept_rate: float


@dataclass(frozen=True)
class PathOption:
    path_id: str
    booking_id: str
    leg_ids: Tuple[str, ...]
    first_depart_day: int
    final_arrive_day: int
    release_day: int
    transship_count: int
    customs_events: Tuple[Tuple[str, int, str], ...]
    customs_cost: float


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def fmt_qty(value: float) -> str:
    rounded = round(float(value), 8)
    if abs(rounded - round(rounded)) <= EPS:
        return str(int(round(rounded)))
    text = f"{rounded:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def ceil_with_tol(value: float) -> int:
    return int(math.ceil(value - 1e-9))


def load_instance():
    ports = {
        row["port_id"]: Port(
            port_id=row["port_id"],
            region=row["region"],
            initial_std=float(row["initial_std"]),
            initial_reefer=float(row["initial_reefer"]),
            yard_capacity=float(row["yard_capacity"]),
            reefer_plug_capacity=float(row["reefer_plug_capacity"]),
            gate_out_default=float(row["gate_out_default"]),
            gate_in_default=float(row["gate_in_default"]),
            storage_cost_std=float(row["storage_cost_std"]),
            storage_cost_reefer=float(row["storage_cost_reefer"]),
        )
        for row in read_csv(DATA_DIR / "ports.csv")
    }

    legs = {
        row["leg_id"]: Leg(
            leg_id=row["leg_id"],
            service=row["service"],
            from_port=row["from_port"],
            to_port=row["to_port"],
            depart_day=int(row["depart_day"]),
            arrive_day=int(row["arrive_day"]),
            teu_capacity=float(row["teu_capacity"]),
            reefer_plug_capacity=float(row["reefer_plug_capacity"]),
            dg1_limit=float(row["dg1_limit"]),
        )
        for row in read_csv(DATA_DIR / "vessel_legs.csv")
    }

    bookings = {
        row["booking_id"]: Booking(
            booking_id=row["booking_id"],
            customer=row["customer"],
            origin_port=row["origin_port"],
            destination_port=row["destination_port"],
            container_type=row["container_type"],
            teu=float(row["teu"]),
            earliest_load_day=int(row["earliest_load_day"]),
            due_arrival_day=int(row["due_arrival_day"]),
            revenue=float(row["revenue"]),
            reject_penalty=float(row["reject_penalty"]),
            priority=row["priority"],
            hazmat_class=row["hazmat_class"],
            medical_flag=row["medical_flag"] == "1",
            change_penalty=float(row["change_penalty"]),
        )
        for row in read_csv(DATA_DIR / "bookings.csv")
    }

    booking_order = [row["booking_id"] for row in read_csv(DATA_DIR / "bookings.csv")]

    path_rules = {
        row["booking_id"]: BookingPathRule(
            booking_id=row["booking_id"],
            max_transship_count=int(row["max_transship_count"]),
            allowed_services=tuple(s for s in row["allowed_services"].split("|") if s),
            requires_direct=row["requires_direct"] == "1",
            customs_group=row["customs_group"],
            cold_chain_preclear=row["cold_chain_preclear"] == "1",
        )
        for row in read_csv(DATA_DIR / "booking_path_rules.csv")
    }

    service_rules = [
        ServiceRule(
            rule_id=row["rule_id"],
            priority=row["priority"],
            min_accept_rate=float(row["min_accept_rate"]),
        )
        for row in read_csv(DATA_DIR / "service_rules.csv")
    ]

    empty_costs = {
        (row["leg_id"], row["container_type"]): (
            float(row["cost_per_teu"]),
            float(row["carbon_kg_per_teu"]),
        )
        for row in read_csv(DATA_DIR / "empty_reposition_costs.csv")
    }

    lease_options = {
        (row["port_id"], int(row["day"]), row["container_type"]): (
            float(row["max_teu"]),
            float(row["lease_cost_per_teu"]),
        )
        for row in read_csv(DATA_DIR / "lease_options.csv")
    }

    port_day_limits = {}
    for row in read_csv(DATA_DIR / "port_day_limits.csv"):
        port_day_limits[(row["port_id"], int(row["day"]))] = {
            "gate_out_limit": float(row["gate_out_limit"]),
            "gate_in_limit": float(row["gate_in_limit"]),
            "yard_capacity": float(row["yard_capacity"]),
            "reefer_plug_capacity": float(row["reefer_plug_capacity"]),
        }

    customs_windows = {
        (row["port_id"], int(row["day"]), row["customs_group"]): (
            float(row["max_teu"]),
            float(row["max_medical_reefer_teu"]),
            float(row["inspection_cost_per_teu"]),
        )
        for row in read_csv(DATA_DIR / "customs_inspection_windows.csv")
    }

    max_day = max(
        max(day for _, day in port_day_limits),
        max(day for _, day, _ in lease_options),
        max(leg.arrive_day for leg in legs.values()) + 1,
    )
    days = list(range(0, max_day + 1))

    limits = {}
    for port_id, port in ports.items():
        for day in days:
            key = (port_id, day)
            limits[key] = {
                "gate_out_limit": port.gate_out_default,
                "gate_in_limit": port.gate_in_default,
                "yard_capacity": port.yard_capacity,
                "reefer_plug_capacity": port.reefer_plug_capacity,
            }
            if key in port_day_limits:
                limits[key].update(port_day_limits[key])

    priority_counts = Counter(bookings[bid].priority for bid in booking_order)
    min_accept_by_priority = {
        rule.priority: ceil_with_tol(rule.min_accept_rate * priority_counts[rule.priority])
        for rule in service_rules
    }

    return {
        "ports": ports,
        "legs": legs,
        "bookings": bookings,
        "booking_order": booking_order,
        "path_rules": path_rules,
        "service_rules": service_rules,
        "empty_costs": empty_costs,
        "lease_options": lease_options,
        "limits": limits,
        "customs_windows": customs_windows,
        "days": days,
        "min_accept_by_priority": min_accept_by_priority,
    }


def enumerate_paths(instance) -> Dict[str, List[PathOption]]:
    legs = instance["legs"]
    bookings = instance["bookings"]
    path_rules = instance["path_rules"]
    customs_windows = instance["customs_windows"]

    legs_from = defaultdict(list)
    for leg in legs.values():
        legs_from[leg.from_port].append(leg)
    for leg_list in legs_from.values():
        leg_list.sort(key=lambda leg: (leg.depart_day, leg.arrive_day, leg.leg_id))

    booking_paths: Dict[str, List[PathOption]] = {}
    for booking_id, booking in bookings.items():
        rule = path_rules[booking_id]
        max_legs = 1 if rule.requires_direct else rule.max_transship_count + 1
        allowed_services = set(rule.allowed_services)
        options: List[PathOption] = []

        def build_option(path: List[Leg]) -> PathOption | None:
            customs_events: List[Tuple[str, int, str]] = []
            customs_group = rule.customs_group
            customs_events.append((booking.origin_port, path[0].depart_day, customs_group))
            for next_leg in path[1:]:
                customs_events.append((next_leg.from_port, next_leg.depart_day, customs_group))
            customs_events.append((booking.destination_port, path[-1].arrive_day, customs_group))
            customs_cost = 0.0
            for event in customs_events:
                window = customs_windows.get(event)
                if window is None:
                    return None
                customs_cost += window[2] * booking.teu
            return PathOption(
                path_id=f"{booking_id}_P{len(options) + 1:02d}",
                booking_id=booking_id,
                leg_ids=tuple(leg.leg_id for leg in path),
                first_depart_day=path[0].depart_day,
                final_arrive_day=path[-1].arrive_day,
                release_day=path[-1].arrive_day + (1 if booking.container_type == "REEFER" else 0),
                transship_count=len(path) - 1,
                customs_events=tuple(customs_events),
                customs_cost=customs_cost,
            )

        def dfs(port_id: str, min_depart_day: int, path: List[Leg], used_ports: set[str]) -> None:
            if len(path) >= max_legs:
                return
            for leg in legs_from[port_id]:
                if leg.service not in allowed_services:
                    continue
                if leg.depart_day < min_depart_day:
                    continue
                if leg.arrive_day > booking.due_arrival_day:
                    continue
                if leg.to_port in used_ports:
                    continue
                new_path = path + [leg]
                if leg.to_port == booking.destination_port:
                    option = build_option(new_path)
                    if option is not None:
                        options.append(option)
                elif len(new_path) < max_legs:
                    dfs(
                        leg.to_port,
                        leg.arrive_day,
                        new_path,
                        used_ports | {leg.to_port},
                    )

        dfs(booking.origin_port, booking.earliest_load_day, [], {booking.origin_port})
        booking_paths[booking_id] = options
    return booking_paths


def build_model(instance, booking_paths):
    ports = instance["ports"]
    legs = instance["legs"]
    bookings = instance["bookings"]
    lease_options = instance["lease_options"]
    limits = instance["limits"]
    empty_costs = instance["empty_costs"]
    customs_windows = instance["customs_windows"]
    days = instance["days"]
    min_accept_by_priority = instance["min_accept_by_priority"]

    model = Model("initial_online_liner")
    time_limit_env = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if time_limit_env:
        time_limit = min(DEFAULT_TIME_LIMIT, float(time_limit_env))
    else:
        time_limit = DEFAULT_TIME_LIMIT
    model.setParam("limits/gap", SCIP_REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)
    model.setParam("parallel/maxnthreads", 1)

    reject_vars = {
        bid: model.addVar(vtype="B", name=f"reject[{bid}]")
        for bid in bookings
    }
    path_vars = {}
    for bid, options in booking_paths.items():
        for option in options:
            path_vars[(bid, option.path_id)] = model.addVar(
                vtype="B",
                name=f"path[{option.path_id}]",
            )

    move_vars = {}
    for leg in legs.values():
        for container_type in ("STD", "REEFER"):
            move_vars[(leg.leg_id, container_type)] = model.addVar(
                lb=0.0,
                ub=leg.teu_capacity,
                vtype="I",
                name=f"move[{leg.leg_id},{container_type}]",
            )

    lease_vars = {}
    for key, (max_teu, _) in lease_options.items():
        lease_vars[key] = model.addVar(
            lb=0.0,
            ub=max_teu,
            vtype="I",
            name=f"lease[{key[0]},{key[1]},{key[2]}]",
        )

    stock_vars = {}
    for port_id in ports:
        for container_type in ("STD", "REEFER"):
            for day in days:
                stock_vars[(port_id, container_type, day)] = model.addVar(
                    lb=0.0,
                    vtype="C",
                    name=f"stock[{port_id},{container_type},{day}]",
                )

    options_by_booking = {
        bid: {option.path_id: option for option in options}
        for bid, options in booking_paths.items()
    }

    booking_start = defaultdict(list)
    booking_release = defaultdict(list)
    gate_out_bookings = defaultdict(list)
    customs_usage = defaultdict(list)
    medical_customs_usage = defaultdict(list)
    leg_loaded = defaultdict(list)
    leg_reefer = defaultdict(list)
    leg_dg1 = defaultdict(list)

    for bid, option_map in options_by_booking.items():
        booking = bookings[bid]
        for option in option_map.values():
            var = path_vars[(bid, option.path_id)]
            first_leg = legs[option.leg_ids[0]]
            booking_start[(booking.origin_port, booking.container_type, first_leg.depart_day)].append(
                (var, booking.teu)
            )
            gate_out_bookings[(booking.origin_port, first_leg.depart_day)].append((var, booking.teu))
            booking_release[(booking.destination_port, booking.container_type, option.release_day)].append(
                (var, booking.teu)
            )
            event_counter = Counter(option.customs_events)
            for event, count in event_counter.items():
                customs_usage[event].append((var, booking.teu * count))
                if booking.medical_flag and booking.container_type == "REEFER":
                    medical_customs_usage[event].append((var, booking.teu * count))
            for leg_id in option.leg_ids:
                leg_loaded[leg_id].append((var, booking.teu))
                if booking.container_type == "REEFER":
                    leg_reefer[leg_id].append((var, booking.teu))
                if booking.hazmat_class == "DG1":
                    leg_dg1[leg_id].append((var, booking.teu))

    move_depart = defaultdict(list)
    move_arrive = defaultdict(list)
    gate_out_moves = defaultdict(list)
    gate_in_moves = defaultdict(list)
    for leg in legs.values():
        for container_type in ("STD", "REEFER"):
            var = move_vars[(leg.leg_id, container_type)]
            move_depart[(leg.from_port, container_type, leg.depart_day)].append(var)
            move_arrive[(leg.to_port, container_type, leg.arrive_day)].append(var)
            gate_out_moves[(leg.from_port, leg.depart_day)].append(var)
            gate_in_moves[(leg.to_port, leg.arrive_day)].append(var)

    lease_by_port_day_type = defaultdict(list)
    gate_in_leases = defaultdict(list)
    for key, var in lease_vars.items():
        port_id, day, container_type = key
        lease_by_port_day_type[(port_id, container_type, day)].append(var)
        gate_in_leases[(port_id, day)].append(var)

    for bid, booking in bookings.items():
        assignment_expr = quicksum(path_vars[(bid, option.path_id)] for option in booking_paths[bid]) + reject_vars[bid]
        model.addCons(assignment_expr == 1, name=f"booking_decision[{bid}]")
        if booking.medical_flag and booking.container_type == "REEFER":
            model.addCons(reject_vars[bid] == 0, name=f"medical_mandate[{bid}]")
        if not booking_paths[bid]:
            model.addCons(reject_vars[bid] == 1, name=f"force_reject_no_path[{bid}]")

    for priority, floor in min_accept_by_priority.items():
        accepted = quicksum(
            path_vars[(bid, option.path_id)]
            for bid, booking in bookings.items()
            if booking.priority == priority
            for option in booking_paths[bid]
        )
        model.addCons(accepted >= floor, name=f"service_floor[{priority}]")

    for leg_id, leg in legs.items():
        loaded_expr = quicksum(var * coeff for var, coeff in leg_loaded[leg_id])
        move_expr = move_vars[(leg_id, "STD")] + move_vars[(leg_id, "REEFER")]
        model.addCons(loaded_expr + move_expr <= leg.teu_capacity, name=f"leg_teu[{leg_id}]")
        reefer_expr = quicksum(var * coeff for var, coeff in leg_reefer[leg_id]) + move_vars[(leg_id, "REEFER")]
        model.addCons(reefer_expr <= leg.reefer_plug_capacity, name=f"leg_reefer[{leg_id}]")
        dg1_expr = quicksum(var * coeff for var, coeff in leg_dg1[leg_id])
        model.addCons(dg1_expr <= leg.dg1_limit, name=f"leg_dg1[{leg_id}]")

    for event, (max_teu, max_medical, _) in customs_windows.items():
        used = quicksum(var * coeff for var, coeff in customs_usage[event])
        model.addCons(used <= max_teu, name=f"customs[{event[0]},{event[1]},{event[2]}]")
        if event[2] == "cold_chain" and max_medical >= 0:
            medical_used = quicksum(var * coeff for var, coeff in medical_customs_usage[event])
            model.addCons(
                medical_used <= max_medical,
                name=f"medical_customs[{event[0]},{event[1]},{event[2]}]",
            )

    for port_id in ports:
        for container_type in ("STD", "REEFER"):
            initial_stock = ports[port_id].initial_std if container_type == "STD" else ports[port_id].initial_reefer
            for day in days:
                inflow_terms = []
                inflow_terms.extend(booking_release[(port_id, container_type, day)])
                inflow_terms.extend((var, 1.0) for var in move_arrive[(port_id, container_type, day)])
                inflow_terms.extend((var, 1.0) for var in lease_by_port_day_type[(port_id, container_type, day)])
                outflow_terms = []
                outflow_terms.extend(booking_start[(port_id, container_type, day)])
                outflow_terms.extend((var, 1.0) for var in move_depart[(port_id, container_type, day)])
                inflow_expr = quicksum(var * coeff for var, coeff in inflow_terms)
                outflow_expr = quicksum(var * coeff for var, coeff in outflow_terms)
                if day == 0:
                    model.addCons(
                        stock_vars[(port_id, container_type, day)] == initial_stock + inflow_expr - outflow_expr,
                        name=f"stock_balance[{port_id},{container_type},{day}]",
                    )
                else:
                    model.addCons(
                        stock_vars[(port_id, container_type, day)]
                        == stock_vars[(port_id, container_type, day - 1)] + inflow_expr - outflow_expr,
                        name=f"stock_balance[{port_id},{container_type},{day}]",
                    )

    for port_id in ports:
        for day in days:
            limit = limits[(port_id, day)]
            stock_std = stock_vars[(port_id, "STD", day)]
            stock_reefer = stock_vars[(port_id, "REEFER", day)]
            model.addCons(
                stock_std + stock_reefer <= limit["yard_capacity"],
                name=f"yard_cap[{port_id},{day}]",
            )
            model.addCons(
                stock_reefer <= limit["reefer_plug_capacity"],
                name=f"yard_reefer[{port_id},{day}]",
            )
            gate_out_expr = quicksum(var * coeff for var, coeff in gate_out_bookings[(port_id, day)]) + quicksum(
                gate_out_moves[(port_id, day)]
            )
            model.addCons(
                gate_out_expr <= limit["gate_out_limit"],
                name=f"gate_out[{port_id},{day}]",
            )
            gate_in_expr = quicksum(var * coeff for var, coeff in booking_release[(port_id, "STD", day)]) + quicksum(
                var * coeff for var, coeff in booking_release[(port_id, "REEFER", day)]
            )
            gate_in_expr += quicksum(gate_in_moves[(port_id, day)])
            gate_in_expr += quicksum(gate_in_leases[(port_id, day)])
            model.addCons(
                gate_in_expr <= limit["gate_in_limit"],
                name=f"gate_in[{port_id},{day}]",
            )

    objective = quicksum(
        path_vars[(bid, option.path_id)] * (bookings[bid].revenue - option.customs_cost)
        for bid, options in booking_paths.items()
        for option in options
    )
    objective -= quicksum(reject_vars[bid] * bookings[bid].reject_penalty for bid in bookings)
    objective -= quicksum(
        move_vars[(leg_id, container_type)] * (cost + CARBON_COST_PER_KG * carbon)
        for (leg_id, container_type), (cost, carbon) in empty_costs.items()
    )
    objective -= quicksum(
        lease_vars[key] * lease_options[key][1]
        for key in lease_vars
    )
    objective -= quicksum(
        stock_vars[(port_id, "STD", day)] * ports[port_id].storage_cost_std
        + stock_vars[(port_id, "REEFER", day)] * ports[port_id].storage_cost_reefer
        for port_id in ports
        for day in days
    )
    model.setObjective(objective, "maximize")

    return {
        "model": model,
        "time_limit": time_limit,
        "reject_vars": reject_vars,
        "path_vars": path_vars,
        "move_vars": move_vars,
        "lease_vars": lease_vars,
        "stock_vars": stock_vars,
        "options_by_booking": options_by_booking,
    }


def extract_solution(instance, model_bundle):
    model = model_bundle["model"]
    if model.getNSols() <= 0:
        raise RuntimeError(f"SCIP finished with status {model.getStatus()} and no incumbent.")
    sol = model.getBestSol()

    booking_paths = {}
    booking_decisions = {}
    for bid in instance["booking_order"]:
        chosen = None
        for option in model_bundle["options_by_booking"][bid].values():
            value = model.getSolVal(sol, model_bundle["path_vars"][(bid, option.path_id)])
            if value > 0.5:
                chosen = option
                break
        booking_paths[bid] = chosen
        booking_decisions[bid] = "accept" if chosen is not None else "reject"

    moves = {}
    for key, var in model_bundle["move_vars"].items():
        value = model.getSolVal(sol, var)
        if value > EPS:
            moves[key] = round(value, 8)

    leases = {}
    for key, var in model_bundle["lease_vars"].items():
        value = model.getSolVal(sol, var)
        if value > EPS:
            leases[key] = round(value, 8)

    return {
        "booking_decisions": booking_decisions,
        "booking_paths": booking_paths,
        "moves": moves,
        "leases": leases,
    }


def build_plan_rows(instance, solution):
    legs = instance["legs"]
    rows: List[Dict[str, str]] = []
    for bid in instance["booking_order"]:
        rows.append(
            {
                "row_type": "booking_decision",
                "item_id": bid,
                "decision": solution["booking_decisions"][bid],
                "leg_id": "",
                "port_id": "",
                "day": "",
                "container_type": "",
                "quantity_teu": "",
            }
        )
        option = solution["booking_paths"][bid]
        if option is None:
            continue
        booking = instance["bookings"][bid]
        for leg_id in option.leg_ids:
            rows.append(
                {
                    "row_type": "booking_leg",
                    "item_id": bid,
                    "decision": "load",
                    "leg_id": leg_id,
                    "port_id": "",
                    "day": "",
                    "container_type": "",
                    "quantity_teu": fmt_qty(booking.teu),
                }
            )

    for (leg_id, container_type), quantity in sorted(solution["moves"].items()):
        rows.append(
            {
                "row_type": "empty_move",
                "item_id": f"EM_{leg_id}_{container_type}",
                "decision": "move",
                "leg_id": leg_id,
                "port_id": "",
                "day": str(legs[leg_id].depart_day),
                "container_type": container_type,
                "quantity_teu": fmt_qty(quantity),
            }
        )

    for (port_id, day, container_type), quantity in sorted(solution["leases"].items()):
        rows.append(
            {
                "row_type": "lease",
                "item_id": f"LS_{port_id}_{day}_{container_type}",
                "decision": "lease",
                "leg_id": "",
                "port_id": port_id,
                "day": str(day),
                "container_type": container_type,
                "quantity_teu": fmt_qty(quantity),
            }
        )
    return rows


def write_plan(rows: List[Dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "row_type",
        "item_id",
        "decision",
        "leg_id",
        "port_id",
        "day",
        "container_type",
        "quantity_teu",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_plan_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def audit_plan_file(instance, booking_paths, path: Path):
    rows = read_plan_rows(path)
    audit = validate_plan(instance, booking_paths, rows)
    audit["rows"] = rows
    audit["path"] = str(path)
    return audit


def validate_plan(instance, booking_paths, rows: List[Dict[str, str]]):
    ports = instance["ports"]
    legs = instance["legs"]
    bookings = instance["bookings"]
    limits = instance["limits"]
    customs_windows = instance["customs_windows"]
    empty_costs = instance["empty_costs"]
    lease_options = instance["lease_options"]
    days = instance["days"]
    min_accept_by_priority = instance["min_accept_by_priority"]

    errors: List[str] = []
    residuals = defaultdict(float)

    def register_violation(group: str, violation: float, scale: float = 1.0, message: str | None = None) -> None:
        if violation <= EPS:
            return
        residual = violation / max(1.0, abs(scale))
        residuals[group] = max(residuals[group], residual)
        if message:
            errors.append(message)

    def parse_int_field(
        value: str,
        group: str,
        message: str,
        *,
        allow_blank: bool = False,
    ) -> int | None:
        if value == "":
            if allow_blank:
                return None
            register_violation(group, 1.0, 1.0, message)
            return None
        try:
            return int(value)
        except ValueError:
            register_violation(group, 1.0, 1.0, message)
            return None

    def parse_float_field(
        value: str,
        group: str,
        message: str,
        *,
        allow_blank: bool = False,
    ) -> float | None:
        if value == "":
            if allow_blank:
                return None
            register_violation(group, 1.0, 1.0, message)
            return None
        try:
            return float(value)
        except ValueError:
            register_violation(group, 1.0, 1.0, message)
            return None

    def require_blank(row: Dict[str, str], field: str, group: str, message: str) -> None:
        if row[field] != "":
            register_violation(group, 1.0, 1.0, message)

    decisions = {}
    booking_leg_rows = defaultdict(list)
    empty_moves = {}
    leases = {}
    seen_item_ids = set()

    required_fields = {
        "row_type",
        "item_id",
        "decision",
        "leg_id",
        "port_id",
        "day",
        "container_type",
        "quantity_teu",
    }
    for row in rows:
        if set(row) != required_fields:
            register_violation("schema", 1.0, 1.0, "CSV schema mismatch.")
            break

    for row in rows:
        row_type = row["row_type"]
        item_id = row["item_id"]
        if row_type == "booking_decision":
            require_blank(row, "leg_id", "schema", f"booking_decision {item_id} must have blank leg_id.")
            require_blank(row, "port_id", "schema", f"booking_decision {item_id} must have blank port_id.")
            require_blank(row, "day", "schema", f"booking_decision {item_id} must have blank day.")
            require_blank(row, "container_type", "schema", f"booking_decision {item_id} must have blank container_type.")
            require_blank(row, "quantity_teu", "schema", f"booking_decision {item_id} must have blank quantity_teu.")
            if item_id in decisions:
                register_violation("booking_decision", 1.0, 1.0, f"Duplicate booking decision for {item_id}.")
                continue
            if item_id not in bookings:
                register_violation("booking_decision", 1.0, 1.0, f"Unknown booking id {item_id}.")
                continue
            if row["decision"] not in {"accept", "reject"}:
                register_violation("booking_decision", 1.0, 1.0, f"Invalid decision {row['decision']} for {item_id}.")
                continue
            decisions[item_id] = row["decision"]
        elif row_type == "booking_leg":
            if item_id not in bookings:
                register_violation("booking_leg", 1.0, 1.0, f"Unknown booking id on booking_leg row {item_id}.")
                continue
            if row["decision"] != "load":
                register_violation("booking_leg", 1.0, 1.0, f"Booking leg for {item_id} must use decision=load.")
            if row["leg_id"] not in legs:
                register_violation("booking_leg", 1.0, 1.0, f"Booking leg for {item_id} uses unknown leg {row['leg_id']}.")
            require_blank(row, "port_id", "schema", f"booking_leg {item_id} must have blank port_id.")
            require_blank(row, "day", "schema", f"booking_leg {item_id} must have blank day.")
            require_blank(row, "container_type", "schema", f"booking_leg {item_id} must have blank container_type.")
            parse_float_field(
                row["quantity_teu"],
                "booking_leg",
                f"Booking leg for {item_id} has non-numeric quantity_teu.",
            )
            booking_leg_rows[item_id].append(row)
        elif row_type == "empty_move":
            if row["decision"] != "move":
                register_violation("empty_move", 1.0, 1.0, f"Empty move {item_id} must use decision=move.")
            require_blank(row, "port_id", "schema", f"empty_move {item_id} must have blank port_id.")
            if row["leg_id"] not in legs:
                register_violation("empty_move", 1.0, 1.0, f"Unknown empty-move leg {row['leg_id']}.")
            if row["container_type"] not in {"STD", "REEFER"}:
                register_violation(
                    "empty_move",
                    1.0,
                    1.0,
                    f"Empty move {item_id} has invalid container type {row['container_type']}.",
                )
            if item_id in seen_item_ids:
                register_violation("empty_move", 1.0, 1.0, f"Duplicate empty_move item_id {item_id}.")
            seen_item_ids.add(item_id)
            key = (row["leg_id"], row["container_type"])
            if key in empty_moves:
                register_violation("empty_move", 1.0, 1.0, f"Duplicate empty move for {key}.")
            qty = parse_float_field(
                row["quantity_teu"],
                "empty_move",
                f"Empty move {item_id} has non-numeric quantity_teu.",
            )
            if qty is None:
                qty = 0.0
            empty_moves[key] = qty
            declared_day = parse_int_field(
                row["day"],
                "empty-move-departure-day",
                f"Empty move {item_id} has non-integer departure day.",
            )
            if row["leg_id"] in legs and declared_day is not None:
                required_day = legs[row["leg_id"]].depart_day
                register_violation(
                    "empty-move-departure-day",
                    abs(declared_day - required_day),
                    1.0,
                    f"Empty move {item_id} day {declared_day} does not match leg departure day {required_day}.",
                )
        elif row_type == "lease":
            if row["decision"] != "lease":
                register_violation("lease", 1.0, 1.0, f"Lease {item_id} must use decision=lease.")
            require_blank(row, "leg_id", "schema", f"lease {item_id} must have blank leg_id.")
            if row["port_id"] not in ports:
                register_violation("lease", 1.0, 1.0, f"Lease {item_id} uses unknown port {row['port_id']}.")
            if row["container_type"] not in {"STD", "REEFER"}:
                register_violation("lease", 1.0, 1.0, f"Lease {item_id} has invalid container type {row['container_type']}.")
            if item_id in seen_item_ids:
                register_violation("lease", 1.0, 1.0, f"Duplicate lease item_id {item_id}.")
            seen_item_ids.add(item_id)
            lease_day = parse_int_field(
                row["day"],
                "lease",
                f"Lease {item_id} has non-integer day.",
            )
            if lease_day is None:
                continue
            key = (row["port_id"], lease_day, row["container_type"])
            if key in leases:
                register_violation("lease", 1.0, 1.0, f"Duplicate lease for {key}.")
            qty = parse_float_field(
                row["quantity_teu"],
                "lease",
                f"Lease {item_id} has non-numeric quantity_teu.",
            )
            if qty is None:
                qty = 0.0
            leases[key] = qty
        else:
            register_violation("schema", 1.0, 1.0, f"Unknown row type {row_type}.")

    for bid in instance["booking_order"]:
        if bid not in decisions:
            register_violation("booking_decision", 1.0, 1.0, f"Missing booking decision for {bid}.")

    selected_paths = {}
    accepted_count = Counter()
    objective = 0.0
    customs_cost_total = 0.0
    move_cost_total = 0.0
    lease_cost_total = 0.0
    hold_cost_total = 0.0
    reject_penalty_total = 0.0
    revenue_total = 0.0

    for bid, booking in bookings.items():
        decision = decisions.get(bid)
        leg_rows = booking_leg_rows.get(bid, [])
        if decision == "reject":
            if leg_rows:
                register_violation("booking_leg", 1.0, 1.0, f"Rejected booking {bid} has booking_leg rows.")
            reject_penalty_total += booking.reject_penalty
            objective -= booking.reject_penalty
            continue
        if decision != "accept":
            continue
        accepted_count[booking.priority] += 1
        leg_ids = tuple(row["leg_id"] for row in leg_rows)
        path_map = {option.leg_ids: option for option in booking_paths[bid]}
        if leg_ids not in path_map:
            register_violation("booking_path", 1.0, 1.0, f"Booking {bid} has an invalid leg sequence {leg_ids}.")
            continue
        option = path_map[leg_ids]
        selected_paths[bid] = option
        revenue_total += booking.revenue
        customs_cost_total += option.customs_cost
        objective += booking.revenue
        for row in leg_rows:
            if row["decision"] != "load":
                register_violation("booking_leg", 1.0, 1.0, f"Booking leg for {bid} has invalid decision.")
            qty = float(row["quantity_teu"] or "0")
            register_violation(
                "booking_leg",
                abs(qty - booking.teu),
                booking.teu,
                f"Booking {bid} leg quantity {qty} does not equal booking TEU {booking.teu}.",
            )

    for priority, floor in min_accept_by_priority.items():
        register_violation(
            "service_floor",
            max(0.0, floor - accepted_count[priority]),
            floor,
            f"Priority {priority} accepted {accepted_count[priority]} below floor {floor}.",
        )

    leg_load = defaultdict(float)
    leg_reefer = defaultdict(float)
    leg_dg1 = defaultdict(float)
    booking_start = defaultdict(float)
    booking_release = defaultdict(float)
    customs_use = defaultdict(float)
    medical_customs_use = defaultdict(float)

    for bid, option in selected_paths.items():
        booking = bookings[bid]
        first_leg = legs[option.leg_ids[0]]
        booking_start[(booking.origin_port, booking.container_type, first_leg.depart_day)] += booking.teu
        booking_release[(booking.destination_port, booking.container_type, option.release_day)] += booking.teu
        for event in option.customs_events:
            customs_use[event] += booking.teu
            if booking.medical_flag and booking.container_type == "REEFER":
                medical_customs_use[event] += booking.teu
        for leg_id in option.leg_ids:
            leg_load[leg_id] += booking.teu
            if booking.container_type == "REEFER":
                leg_reefer[leg_id] += booking.teu
            if booking.hazmat_class == "DG1":
                leg_dg1[leg_id] += booking.teu

    move_depart = defaultdict(float)
    move_arrive = defaultdict(float)
    gate_out = defaultdict(float)
    gate_in = defaultdict(float)

    for (leg_id, container_type), qty in empty_moves.items():
        if leg_id not in legs:
            register_violation("empty_move", 1.0, 1.0, f"Unknown empty-move leg {leg_id}.")
            continue
        if container_type not in {"STD", "REEFER"}:
            register_violation("empty_move", 1.0, 1.0, f"Invalid empty-move container type {container_type}.")
            continue
        if qty < -EPS:
            register_violation("empty_move", -qty, 1.0, f"Negative empty-move quantity on {leg_id}/{container_type}.")
            continue
        leg = legs[leg_id]
        move_depart[(leg.from_port, container_type, leg.depart_day)] += qty
        move_arrive[(leg.to_port, container_type, leg.arrive_day)] += qty
        leg_load[leg_id] += qty
        if container_type == "REEFER":
            leg_reefer[leg_id] += qty
        gate_out[(leg.from_port, leg.depart_day)] += qty
        gate_in[(leg.to_port, leg.arrive_day)] += qty
        cost, carbon = empty_costs[(leg_id, container_type)]
        move_cost_total += qty * (cost + CARBON_COST_PER_KG * carbon)
        objective -= qty * (cost + CARBON_COST_PER_KG * carbon)

    for key, qty in leases.items():
        if key not in lease_options:
            register_violation("lease", 1.0, 1.0, f"Lease key {key} is not available.")
            continue
        max_teu, lease_cost = lease_options[key]
        register_violation(
            "lease_limit",
            max(0.0, qty - max_teu),
            max_teu,
            f"Lease {key} exceeds limit {max_teu}.",
        )
        if qty < -EPS:
            register_violation("lease", -qty, 1.0, f"Negative lease quantity {key}.")
        gate_in[(key[0], key[1])] += qty
        lease_cost_total += qty * lease_cost
        objective -= qty * lease_cost

    for bid, option in selected_paths.items():
        booking = bookings[bid]
        first_leg = legs[option.leg_ids[0]]
        gate_out[(booking.origin_port, first_leg.depart_day)] += booking.teu

    for (port_id, container_type, day), qty in booking_release.items():
        gate_in[(port_id, day)] += qty

    for leg_id, leg in legs.items():
        register_violation(
            "leg_teu",
            max(0.0, leg_load[leg_id] - leg.teu_capacity),
            leg.teu_capacity,
            f"Leg {leg_id} TEU load {leg_load[leg_id]} exceeds {leg.teu_capacity}.",
        )
        register_violation(
            "leg_reefer",
            max(0.0, leg_reefer[leg_id] - leg.reefer_plug_capacity),
            leg.reefer_plug_capacity,
            f"Leg {leg_id} reefer load {leg_reefer[leg_id]} exceeds {leg.reefer_plug_capacity}.",
        )
        register_violation(
            "leg_dg1",
            max(0.0, leg_dg1[leg_id] - leg.dg1_limit),
            leg.dg1_limit,
            f"Leg {leg_id} DG1 load {leg_dg1[leg_id]} exceeds {leg.dg1_limit}.",
        )

    for event, (max_teu, max_medical, cost) in customs_windows.items():
        if customs_use[event] > 0:
            objective -= customs_use[event] * cost
        register_violation(
            "customs",
            max(0.0, customs_use[event] - max_teu),
            max_teu,
            f"Customs window {event} uses {customs_use[event]} above {max_teu}.",
        )
        if event[2] == "cold_chain":
            register_violation(
                "medical_customs",
                max(0.0, medical_customs_use[event] - max_medical),
                max_medical,
                f"Medical cold-chain sublimit {event} uses {medical_customs_use[event]} above {max_medical}.",
            )

    stock = defaultdict(float)
    for port_id, port in ports.items():
        for day in days:
            std_prev = port.initial_std if day == 0 else stock[(port_id, "STD", day - 1)]
            reefer_prev = port.initial_reefer if day == 0 else stock[(port_id, "REEFER", day - 1)]
            std = (
                std_prev
                + booking_release[(port_id, "STD", day)]
                + move_arrive[(port_id, "STD", day)]
                + leases.get((port_id, day, "STD"), 0.0)
                - booking_start[(port_id, "STD", day)]
                - move_depart[(port_id, "STD", day)]
            )
            reefer = (
                reefer_prev
                + booking_release[(port_id, "REEFER", day)]
                + move_arrive[(port_id, "REEFER", day)]
                + leases.get((port_id, day, "REEFER"), 0.0)
                - booking_start[(port_id, "REEFER", day)]
                - move_depart[(port_id, "REEFER", day)]
            )
            stock[(port_id, "STD", day)] = std
            stock[(port_id, "REEFER", day)] = reefer
            hold_cost_total += std * port.storage_cost_std + reefer * port.storage_cost_reefer
            register_violation(
                "stock_nonnegative",
                max(0.0, -std),
                max(port.initial_std, 1.0),
                f"Negative STD stock at {port_id} day {day}: {std}.",
            )
            register_violation(
                "stock_nonnegative",
                max(0.0, -reefer),
                max(port.initial_reefer, 1.0),
                f"Negative REEFER stock at {port_id} day {day}: {reefer}.",
            )
            limit = limits[(port_id, day)]
            register_violation(
                "yard_capacity",
                max(0.0, std + reefer - limit["yard_capacity"]),
                limit["yard_capacity"],
                f"Yard capacity exceeded at {port_id} day {day}.",
            )
            register_violation(
                "reefer_yard_capacity",
                max(0.0, reefer - limit["reefer_plug_capacity"]),
                limit["reefer_plug_capacity"],
                f"Reefer yard capacity exceeded at {port_id} day {day}.",
            )
            register_violation(
                "gate_out",
                max(0.0, gate_out[(port_id, day)] - limit["gate_out_limit"]),
                limit["gate_out_limit"],
                f"Gate-out limit exceeded at {port_id} day {day}.",
            )
            register_violation(
                "gate_in",
                max(0.0, gate_in[(port_id, day)] - limit["gate_in_limit"]),
                limit["gate_in_limit"],
                f"Gate-in limit exceeded at {port_id} day {day}.",
            )

    objective -= hold_cost_total

    integrality_residual = 0.0
    for quantity in empty_moves.values():
        integrality_residual = max(integrality_residual, abs(quantity - round(quantity)))
    for quantity in leases.values():
        integrality_residual = max(integrality_residual, abs(quantity - round(quantity)))

    theta = max(residuals.values(), default=0.0)
    return {
        "ok": not errors,
        "errors": errors,
        "theta": theta,
        "violated_groups": sum(1 for value in residuals.values() if value > EPS),
        "residuals": dict(residuals),
        "objective": objective,
        "coverage": len(decisions),
        "integrality_residual": integrality_residual,
        "accepted_count": dict(accepted_count),
        "move_cost_total": move_cost_total,
        "lease_cost_total": lease_cost_total,
        "hold_cost_total": hold_cost_total,
        "customs_cost_total": customs_cost_total,
        "reject_penalty_total": reject_penalty_total,
        "revenue_total": revenue_total,
        "parsed_solution": {
            "booking_decisions": {
                bid: decisions.get(bid, "reject")
                for bid in instance["booking_order"]
            },
            "booking_paths": {
                bid: selected_paths.get(bid)
                for bid in instance["booking_order"]
            },
            "moves": dict(empty_moves),
            "leases": dict(leases),
            "stock_levels": dict(stock),
        },
    }


def apply_mip_start(instance, model_bundle, incumbent_solution) -> bool:
    model = model_bundle["model"]
    start_sol = model.createSol()

    for bid in instance["booking_order"]:
        chosen = incumbent_solution["booking_paths"].get(bid)
        if chosen is None:
            model.setSolVal(start_sol, model_bundle["reject_vars"][bid], 1.0)
        else:
            model.setSolVal(start_sol, model_bundle["reject_vars"][bid], 0.0)
        for option in model_bundle["options_by_booking"][bid].values():
            value = 1.0 if chosen is not None and option.path_id == chosen.path_id else 0.0
            model.setSolVal(start_sol, model_bundle["path_vars"][(bid, option.path_id)], value)

    for key, var in model_bundle["move_vars"].items():
        model.setSolVal(start_sol, var, incumbent_solution["moves"].get(key, 0.0))

    for key, var in model_bundle["lease_vars"].items():
        model.setSolVal(start_sol, var, incumbent_solution["leases"].get(key, 0.0))

    for key, var in model_bundle["stock_vars"].items():
        model.setSolVal(start_sol, var, incumbent_solution["stock_levels"].get(key, 0.0))

    return bool(model.addSol(start_sol, free=True))


def write_log(
    instance,
    booking_paths,
    model_bundle,
    incumbent_audit,
    candidate_audit,
    final_audit,
    elapsed: float,
    negative_audit_ok: bool,
    warm_start_added: bool,
    published_candidate: bool,
) -> None:
    model = model_bundle["model"]
    n_feasible_paths = sum(len(paths) for paths in booking_paths.values())
    impossible = [bid for bid, paths in booking_paths.items() if not paths]
    status = str(model.getStatus())
    best_bound = model.getDualbound() if model.getNSols() > 0 else float("nan")
    gap = model.getGap() if model.getNSols() > 0 else float("nan")
    candidate_objective = model.getObjVal() if model.getNSols() > 0 else float("nan")

    rows = [
        "# Solve Log: Initial Stage",
        "",
        "## Solver",
        "",
        f"- Status: `{status}`",
        f"- Time limit seconds: `{model_bundle['time_limit']}`",
        f"- Elapsed seconds: `{elapsed:.4f}`",
        f"- Warm start added from rollback incumbent: `{warm_start_added}`",
        f"- Incumbent count: `{model.getNSols()}`",
        f"- SCIP candidate objective: `{candidate_objective:.8f}`",
        f"- Dual bound: `{best_bound:.8f}`",
        f"- Relative gap: `{gap:.8f}`",
        f"- Published candidate: `{published_candidate}`",
        "",
        "## Guarded Comparison",
        "",
        f"- Rollback incumbent audited objective: `{incumbent_audit['objective']:.8f}`",
        f"- Candidate audited objective: `{candidate_audit['objective']:.8f}`",
        f"- Final published audited objective: `{final_audit['objective']:.8f}`",
        f"- Rollback incumbent audit ok: `{incumbent_audit['ok']}`",
        f"- Candidate audit ok: `{candidate_audit['ok']}`",
        f"- Final artifact audit ok: `{final_audit['ok']}`",
        "",
        "## State Vector",
        "",
        f"- Artifact present: `{PLAN_PATH.exists()}`",
        f"- Decision coverage: `{final_audit['coverage']}/{len(instance['booking_order'])}`",
        f"- Maximum normalized residual theta: `{final_audit['theta']:.8f}`",
        f"- Violated hard-constraint groups: `{final_audit['violated_groups']}`",
        f"- Export integrality residual: `{final_audit['integrality_residual']:.8f}`",
        f"- Independent audited objective: `{final_audit['objective']:.8f}`",
        "",
        "## Instance Summary",
        "",
        f"- Visible bookings: `{len(instance['booking_order'])}`",
        f"- Enumerated feasible booking paths: `{n_feasible_paths}`",
        f"- Bookings with no feasible path: `{', '.join(impossible) if impossible else 'none'}`",
        f"- Minimum acceptance floors: `{instance['min_accept_by_priority']}`",
        "",
        "## Objective Components",
        "",
        f"- Accepted booking revenue: `{final_audit['revenue_total']:.8f}`",
        f"- Rejection penalties: `{final_audit['reject_penalty_total']:.8f}`",
        f"- Empty reposition and carbon cost: `{final_audit['move_cost_total']:.8f}`",
        f"- Lease cost: `{final_audit['lease_cost_total']:.8f}`",
        f"- Customs inspection cost: `{final_audit['customs_cost_total']:.8f}`",
        f"- Empty holding cost: `{final_audit['hold_cost_total']:.8f}`",
        "",
        "## Audit",
        "",
        "- Independent validation was rerun from the exported CSV, not from model variables.",
        f"- Corrupted-copy negative audit rejected artifact: `{negative_audit_ok}`",
        "- Restoration event repaired: `empty_move.day` is now exported and checked against `vessel_legs.depart_day`.",
        "- Gate handling assumption: accepted bookings consume gate-out at the origin first-loaded day; empty releases, empty arrivals, and leases consume gate-in.",
        "- Carbon assumption: `carbon_kg_per_teu` is monetized with unit coefficient `1.0` because no separate carbon-price table is provided.",
    ]

    with LOG_PATH.open("w") as handle:
        handle.write("\n".join(rows) + "\n")


def run_negative_audit(instance, booking_paths, rows):
    corrupted = [dict(row) for row in rows]
    for row in corrupted:
        if row["row_type"] == "booking_leg":
            row["quantity_teu"] = "0"
            break
    else:
        return False
    audit = validate_plan(instance, booking_paths, corrupted)
    return not audit["ok"]


def main():
    start = time.perf_counter()
    instance = load_instance()
    booking_paths = enumerate_paths(instance)
    if CANDIDATE_PLAN_PATH.exists():
        CANDIDATE_PLAN_PATH.unlink()

    for bid, booking in instance["bookings"].items():
        if booking.medical_flag and booking.container_type == "REEFER" and not booking_paths[bid]:
            raise RuntimeError(f"Mandatory medical booking {bid} has no feasible path.")

    incumbent_audit = audit_plan_file(instance, booking_paths, PLAN_PATH)
    if not incumbent_audit["ok"]:
        raise RuntimeError("Rollback incumbent failed independent audit:\n" + "\n".join(incumbent_audit["errors"][:20]))

    model_bundle = build_model(instance, booking_paths)
    model = model_bundle["model"]
    warm_start_added = apply_mip_start(
        instance,
        model_bundle,
        incumbent_audit["parsed_solution"],
    )
    model.optimize()

    solution = extract_solution(instance, model_bundle)
    candidate_rows = build_plan_rows(instance, solution)
    write_plan(candidate_rows, CANDIDATE_PLAN_PATH)
    candidate_audit = audit_plan_file(instance, booking_paths, CANDIDATE_PLAN_PATH)
    if not candidate_audit["ok"]:
        raise RuntimeError("Candidate plan failed independent audit:\n" + "\n".join(candidate_audit["errors"][:20]))

    published_candidate = candidate_audit["objective"] > incumbent_audit["objective"] + EPS
    if published_candidate:
        CANDIDATE_PLAN_PATH.replace(PLAN_PATH)
    else:
        CANDIDATE_PLAN_PATH.unlink(missing_ok=True)

    final_audit = audit_plan_file(instance, booking_paths, PLAN_PATH)
    if not final_audit["ok"]:
        raise RuntimeError("Final published plan failed independent audit:\n" + "\n".join(final_audit["errors"][:20]))

    negative_audit_ok = run_negative_audit(instance, booking_paths, final_audit["rows"])
    if not negative_audit_ok:
        raise RuntimeError("Negative audit did not reject a corrupted artifact.")

    elapsed = time.perf_counter() - start
    write_log(
        instance,
        booking_paths,
        model_bundle,
        incumbent_audit,
        candidate_audit,
        final_audit,
        elapsed,
        negative_audit_ok,
        warm_start_added,
        published_candidate,
    )


if __name__ == "__main__":
    main()
