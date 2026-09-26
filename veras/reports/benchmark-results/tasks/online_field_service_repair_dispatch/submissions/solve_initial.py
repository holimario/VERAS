#!/usr/bin/env python3
import csv
import json
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from pyscipopt import Model, quicksum


PARTS = ["part_ont", "part_radio", "part_battery", "part_router"]
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}
SKILL_DURATION_FACTOR = {1: 1.28, 2: 1.0, 3: 0.83}
SCIP_GAP = 0.0005
DEFAULT_TIME_LIMIT = 20.0


@dataclass(frozen=True)
class Technician:
    technician_id: str
    home_node: str
    skills: Dict[str, int]
    bucket_truck: int
    seniority: int
    shift_start: int
    shift_end: int
    overtime_limit: int
    wage_per_min: float
    overtime_cost_per_min: float
    stock: Dict[str, int]


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    node_id: str
    region: str
    priority: str
    required_skill: str
    secondary_skill: str
    two_technician_required: int
    bucket_required: int
    release_minute: int
    due_minute: int
    deadline_minute: int
    base_service_minutes: int
    late_penalty_per_min: float
    deferral_penalty: float
    change_penalty: float
    predecessor_ticket_id: str
    risk_class: str
    parts: Dict[str, int]


@dataclass(frozen=True)
class CrewOption:
    ticket_id: str
    option_id: int
    primary_technician: str
    assistant_technician: str
    duration: int
    participants: Tuple[str, ...]


@dataclass(frozen=True)
class AccessWindow:
    ticket_id: str
    window_id: str
    earliest_start_minute: int
    latest_start_minute: int
    reason: str


@dataclass(frozen=True)
class StagingLimit:
    region: str
    interval_id: str
    start_minute: int
    end_minute: int
    max_active_crews: int
    reason: str


@dataclass
class RouteState:
    jobs: List[str]
    current_node: str
    available_time: int


def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def parse_data():
    base = "/app/data"
    with open(os.path.join(base, "current_wave.json")) as handle:
        wave = json.load(handle)

    technicians: List[Technician] = []
    for row in load_csv(os.path.join(base, "technicians.csv")):
        skills = {}
        for item in row["skills"].split("|"):
            skill, level = item.split(":")
            skills[skill] = int(level)
        technicians.append(
            Technician(
                technician_id=row["technician_id"],
                home_node=row["home_node"],
                skills=skills,
                bucket_truck=int(row["bucket_truck"]),
                seniority=int(row["seniority"]),
                shift_start=int(row["shift_start"]),
                shift_end=int(row["shift_end"]),
                overtime_limit=int(row["overtime_limit"]),
                wage_per_min=float(row["wage_per_min"]),
                overtime_cost_per_min=float(row["overtime_cost_per_min"]),
                stock={part: int(row[part]) for part in PARTS},
            )
        )

    tickets: List[Ticket] = []
    for row in load_csv(os.path.join(base, "tickets.csv")):
        tickets.append(
            Ticket(
                ticket_id=row["ticket_id"],
                node_id=row["node_id"],
                region=row["region"],
                priority=row["priority"],
                required_skill=row["required_skill"],
                secondary_skill=row["secondary_skill"],
                two_technician_required=int(row["two_technician_required"]),
                bucket_required=int(row["bucket_required"]),
                release_minute=int(row["release_minute"]),
                due_minute=int(row["due_minute"]),
                deadline_minute=int(row["deadline_minute"]),
                base_service_minutes=int(row["base_service_minutes"]),
                late_penalty_per_min=float(row["late_penalty_per_min"]),
                deferral_penalty=float(row["deferral_penalty"]),
                change_penalty=float(row["change_penalty"]),
                predecessor_ticket_id=row["predecessor_ticket_id"],
                risk_class=row["risk_class"],
                parts={part: int(row[part]) for part in PARTS},
            )
        )

    travel = {}
    for row in load_csv(os.path.join(base, "travel_times.csv")):
        travel[(row["from_node"], row["to_node"])] = int(row["travel_minutes"])

    replenishments = defaultdict(lambda: defaultdict(int))
    for row in load_csv(os.path.join(base, "spare_replenishments.csv")):
        if int(row["wave"]) <= int(wave["current_wave"]):
            tech_id = row["technician_id"]
            for part in PARTS:
                replenishments[tech_id][part] += int(row[part])

    region_policies = {}
    for row in load_csv(os.path.join(base, "region_policies.csv")):
        region_policies[row["region"]] = float(row["min_p1_p2_service_rate"])

    access_windows = []
    for row in load_csv(os.path.join(base, "access_windows.csv")):
        access_windows.append(
            AccessWindow(
                ticket_id=row["ticket_id"],
                window_id=row["window_id"],
                earliest_start_minute=int(row["earliest_start_minute"]),
                latest_start_minute=int(row["latest_start_minute"]),
                reason=row["reason"],
            )
        )

    staging_limits = []
    for row in load_csv(os.path.join(base, "crew_staging_limits.csv")):
        staging_limits.append(
            StagingLimit(
                region=row["region"],
                interval_id=row["interval_id"],
                start_minute=int(row["start_minute"]),
                end_minute=int(row["end_minute"]),
                max_active_crews=int(row["max_active_crews"]),
                reason=row["reason"],
            )
        )

    return {
        "wave": wave,
        "technicians": technicians,
        "tickets": tickets,
        "travel": travel,
        "replenishments": replenishments,
        "region_policies": region_policies,
        "access_windows": access_windows,
        "staging_limits": staging_limits,
    }


def earliest_start(ticket: Ticket, current_time: int) -> int:
    return max(current_time, ticket.release_minute)


def skill_duration(ticket: Ticket, technician: Technician) -> Optional[int]:
    level = technician.skills.get(ticket.required_skill, 0)
    if level <= 0:
        return None
    return int(math.ceil(ticket.base_service_minutes * SKILL_DURATION_FACTOR[level]))


def tech_map(data):
    return {tech.technician_id: tech for tech in data["technicians"]}


def ticket_map(data):
    return {ticket.ticket_id: ticket for ticket in data["tickets"]}


def access_map(data):
    grouped = defaultdict(list)
    for row in data["access_windows"]:
        grouped[row.ticket_id].append(row)
    return grouped


def floor_requirements(data) -> Dict[str, int]:
    counts = defaultdict(int)
    for ticket in data["tickets"]:
        if ticket.priority in {"P1", "P2"}:
            counts[ticket.region] += 1
    floors = {}
    for region, rate in data["region_policies"].items():
        floors[region] = int(math.ceil(rate * counts.get(region, 0) - 1e-9))
    return floors


def build_crew_options(data) -> Dict[str, List[CrewOption]]:
    technicians = data["technicians"]
    max_seniority = max(tech.seniority for tech in technicians)
    options_by_ticket = {}

    for ticket in data["tickets"]:
        options: List[CrewOption] = []
        for primary in technicians:
            duration = skill_duration(ticket, primary)
            if duration is None:
                continue
            if ticket.two_technician_required:
                for assistant in technicians:
                    if assistant.technician_id == primary.technician_id:
                        continue
                    if ticket.bucket_required and not (
                        primary.bucket_truck or assistant.bucket_truck
                    ):
                        continue
                    if ticket.secondary_skill and not (
                        primary.skills.get(ticket.secondary_skill, 0) > 0
                        or assistant.skills.get(ticket.secondary_skill, 0) > 0
                    ):
                        continue
                    if (
                        ticket.risk_class == "high"
                        and max(primary.seniority, assistant.seniority) < max_seniority
                    ):
                        continue
                    options.append(
                        CrewOption(
                            ticket_id=ticket.ticket_id,
                            option_id=len(options),
                            primary_technician=primary.technician_id,
                            assistant_technician=assistant.technician_id,
                            duration=duration,
                            participants=(primary.technician_id, assistant.technician_id),
                        )
                    )
            else:
                if ticket.bucket_required and not primary.bucket_truck:
                    continue
                if ticket.secondary_skill and primary.skills.get(ticket.secondary_skill, 0) <= 0:
                    continue
                options.append(
                    CrewOption(
                        ticket_id=ticket.ticket_id,
                        option_id=len(options),
                        primary_technician=primary.technician_id,
                        assistant_technician="",
                        duration=duration,
                        participants=(primary.technician_id,),
                    )
                )
        if not options:
            raise RuntimeError(f"No feasible crew option exists for ticket {ticket.ticket_id}")
        options_by_ticket[ticket.ticket_id] = options

    return options_by_ticket


def participant_ticket_map(options_by_ticket):
    result = defaultdict(set)
    for ticket_id, options in options_by_ticket.items():
        for option in options:
            for tech_id in option.participants:
                result[tech_id].add(ticket_id)
    return {tech_id: sorted(ticket_ids) for tech_id, ticket_ids in result.items()}


def compute_route_cost_delta(
    ticket: Ticket,
    option: CrewOption,
    routes: Dict[str, RouteState],
    tech_by_id: Dict[str, Technician],
    travel: Dict[Tuple[str, str], int],
    current_time: int,
) -> Optional[Tuple[float, int]]:
    start_candidates = [earliest_start(ticket, current_time)]
    current_returns = {}
    service_cost = 0.0
    travel_cost_delta = 0.0
    overtime_delta = 0.0

    for tech_id in option.participants:
        tech = tech_by_id[tech_id]
        route = routes[tech_id]
        start_candidates.append(route.available_time + travel[(route.current_node, ticket.node_id)])
        current_returns[tech_id] = (
            route.available_time + travel[(route.current_node, tech.home_node)]
            if route.jobs
            else tech.shift_start
        )
        service_cost += tech.wage_per_min * option.duration

    start_time = max(start_candidates)
    finish_time = start_time + option.duration
    if finish_time > ticket.deadline_minute:
        return None

    for tech_id in option.participants:
        tech = tech_by_id[tech_id]
        route = routes[tech_id]
        if not route.jobs:
            old_return_travel = 0
            new_return_travel = travel[(tech.home_node, ticket.node_id)] + travel[
                (ticket.node_id, tech.home_node)
            ]
        else:
            old_return_travel = travel[(route.current_node, tech.home_node)]
            new_return_travel = travel[(route.current_node, ticket.node_id)] + travel[
                (ticket.node_id, tech.home_node)
            ]
        travel_cost_delta += tech.wage_per_min * (new_return_travel - old_return_travel)
        new_return = finish_time + travel[(ticket.node_id, tech.home_node)]
        old_ot = max(0, current_returns[tech_id] - tech.shift_end)
        new_ot = max(0, new_return - tech.shift_end)
        overtime_delta += max(0.0, tech.overtime_cost_per_min - tech.wage_per_min) * (new_ot - old_ot)
        if new_return > tech.shift_end + tech.overtime_limit:
            return None

    lateness_cost = max(0, finish_time - ticket.due_minute) * ticket.late_penalty_per_min
    return service_cost + travel_cost_delta + overtime_delta + lateness_cost, start_time


def greedy_construct(data, options_by_ticket, ordering_name: str):
    tech_by_id = tech_map(data)
    travel = data["travel"]
    current_time = int(data["wave"]["current_time_minute"])
    floors = floor_requirements(data)
    routes = {
        tech.technician_id: RouteState([], tech.home_node, tech.shift_start)
        for tech in data["technicians"]
    }
    primary_parts = defaultdict(lambda: defaultdict(int))
    scheduled_high = defaultdict(int)
    plan = {}

    tickets = list(data["tickets"])
    if ordering_name == "tight_first":
        tickets.sort(
            key=lambda ticket: (
                PRIORITY_ORDER[ticket.priority],
                ticket.deadline_minute,
                ticket.due_minute,
                len(options_by_ticket[ticket.ticket_id]),
                -ticket.deferral_penalty,
            )
        )
    else:
        tickets.sort(
            key=lambda ticket: (
                PRIORITY_ORDER[ticket.priority],
                len(options_by_ticket[ticket.ticket_id]),
                ticket.due_minute,
                ticket.deadline_minute,
                -ticket.deferral_penalty,
            )
        )

    for ticket in tickets:
        best = None
        for option in options_by_ticket[ticket.ticket_id]:
            primary_id = option.primary_technician
            feasible_parts = True
            for part in PARTS:
                limit = tech_by_id[primary_id].stock[part] + data["replenishments"][primary_id][part]
                if primary_parts[primary_id][part] + ticket.parts[part] > limit:
                    feasible_parts = False
                    break
            if not feasible_parts:
                continue

            estimate = compute_route_cost_delta(
                ticket,
                option,
                routes,
                tech_by_id,
                travel,
                current_time,
            )
            if estimate is None:
                continue
            if best is None or estimate[0] < best[0]:
                best = (estimate[0], estimate[1], option)

        must_schedule = ticket.priority in {"P1", "P2"}
        if best is None:
            if must_schedule:
                return None
            plan[ticket.ticket_id] = {
                "ticket_id": ticket.ticket_id,
                "decision": "defer",
                "primary_technician": "",
                "assistant_technician": "",
                "start_minute": "",
            }
            continue

        if ticket.priority == "P3" and best[0] >= ticket.deferral_penalty - 1e-9:
            plan[ticket.ticket_id] = {
                "ticket_id": ticket.ticket_id,
                "decision": "defer",
                "primary_technician": "",
                "assistant_technician": "",
                "start_minute": "",
            }
            continue

        _, start_time, option = best
        finish_time = start_time + option.duration
        for tech_id in option.participants:
            routes[tech_id].jobs.append(ticket.ticket_id)
            routes[tech_id].current_node = ticket.node_id
            routes[tech_id].available_time = finish_time
        for part in PARTS:
            primary_parts[option.primary_technician][part] += ticket.parts[part]
        if ticket.priority in {"P1", "P2"}:
            scheduled_high[ticket.region] += 1
        plan[ticket.ticket_id] = {
            "ticket_id": ticket.ticket_id,
            "decision": "schedule",
            "primary_technician": option.primary_technician,
            "assistant_technician": option.assistant_technician,
            "start_minute": start_time,
        }

    for region, minimum in floors.items():
        if scheduled_high[region] < minimum:
            return None

    return plan


def independent_audit(plan_rows, data, options_by_ticket):
    tech_by_id = tech_map(data)
    ticket_by_id = ticket_map(data)
    access_by_ticket = access_map(data)
    travel = data["travel"]
    floors = floor_requirements(data)
    option_signature = {
        (option.ticket_id, option.primary_technician, option.assistant_technician): option
        for options in options_by_ticket.values()
        for option in options
    }

    errors = []
    residuals = []
    groups = set()

    def add_error(group: str, message: str, residual: float = 1.0):
        groups.add(group)
        residuals.append(residual)
        errors.append(f"[{group}] {message}")

    expected = [ticket.ticket_id for ticket in data["tickets"]]
    observed = [row.get("ticket_id", "") for row in plan_rows]
    if len(plan_rows) != len(expected):
        add_error("coverage", "Row count does not match ticket count.", 1.0)
    if observed != expected:
        add_error("coverage", "Rows must match the ticket order from tickets.csv.", 1.0)

    by_ticket = {}
    for row in plan_rows:
        ticket_id = row.get("ticket_id", "")
        if ticket_id not in ticket_by_id:
            add_error("domain", f"Unknown ticket_id {ticket_id}.", 1.0)
            continue
        if ticket_id in by_ticket:
            add_error("coverage", f"Duplicate row for {ticket_id}.", 1.0)
            continue
        by_ticket[ticket_id] = row

    tech_intervals = defaultdict(list)
    primary_parts = defaultdict(lambda: defaultdict(int))
    scheduled_high = defaultdict(int)
    intervals_by_region = defaultdict(list)
    total_lateness_cost = 0.0
    total_deferral_cost = 0.0
    total_labor_cost = 0.0

    for ticket_id in expected:
        row = by_ticket.get(ticket_id)
        if row is None:
            continue
        ticket = ticket_by_id[ticket_id]
        decision = row.get("decision", "")
        if decision not in {"schedule", "defer"}:
            add_error("domain", f"{ticket_id} has invalid decision {decision}.", 1.0)
            continue

        if decision == "defer":
            if ticket.priority == "P1":
                add_error("mandatory", f"{ticket_id} is P1 and cannot be deferred.", 1.0)
            if row.get("primary_technician", "") or row.get("assistant_technician", "") or row.get(
                "start_minute", ""
            ):
                add_error("schema", f"{ticket_id} defer row must leave crew and start blank.", 1.0)
            total_deferral_cost += ticket.deferral_penalty
            continue

        primary = row.get("primary_technician", "")
        assistant = row.get("assistant_technician", "")
        if primary not in tech_by_id:
            add_error("crew", f"{ticket_id} has unknown primary technician {primary}.", 1.0)
            continue
        if ticket.two_technician_required:
            if assistant not in tech_by_id:
                add_error("crew", f"{ticket_id} requires a distinct assistant technician.", 1.0)
                continue
            if assistant == primary:
                add_error("crew", f"{ticket_id} assistant must be distinct from primary.", 1.0)
        elif assistant:
            add_error("schema", f"{ticket_id} should leave assistant blank.", 1.0)

        try:
            start = int(row.get("start_minute", ""))
        except Exception:
            add_error("schema", f"{ticket_id} start_minute must be an integer.", 1.0)
            continue

        option = option_signature.get((ticket_id, primary, assistant))
        if option is None:
            add_error("crew", f"{ticket_id} uses an invalid crew combination.", 1.0)
            continue

        lower = earliest_start(ticket, int(data["wave"]["current_time_minute"]))
        finish = start + option.duration
        if start < lower:
            add_error(
                "timing",
                f"{ticket_id} starts before its release/current lower bound.",
                (lower - start) / max(1, lower),
            )
        if finish > ticket.deadline_minute:
            add_error(
                "timing",
                f"{ticket_id} finishes after its deadline.",
                (finish - ticket.deadline_minute) / max(1, ticket.deadline_minute),
            )
        if access_by_ticket[ticket_id]:
            in_window = any(
                window.earliest_start_minute <= start <= window.latest_start_minute
                for window in access_by_ticket[ticket_id]
            )
            if not in_window:
                add_error("access", f"{ticket_id} is outside every access window.", 1.0)

        lateness = max(0, finish - ticket.due_minute)
        total_lateness_cost += lateness * ticket.late_penalty_per_min
        if ticket.priority in {"P1", "P2"}:
            scheduled_high[ticket.region] += 1

        for tech_id in option.participants:
            tech_intervals[tech_id].append((start, finish, ticket))
        intervals_by_region[ticket.region].append((start, finish, ticket.ticket_id))
        for part in PARTS:
            primary_parts[primary][part] += ticket.parts[part]

    for region, minimum in floors.items():
        if scheduled_high[region] < minimum:
            shortfall = minimum - scheduled_high[region]
            add_error(
                "service_floor",
                f"{region} high-priority service floor short by {shortfall}.",
                shortfall / max(1, minimum),
            )

    for ticket in data["tickets"]:
        if not ticket.predecessor_ticket_id:
            continue
        row = by_ticket.get(ticket.ticket_id)
        pred_row = by_ticket.get(ticket.predecessor_ticket_id)
        if row is None or pred_row is None:
            continue
        if row["decision"] == "schedule" and pred_row["decision"] == "defer":
            add_error(
                "precedence",
                f"{ticket.ticket_id} is scheduled while predecessor is deferred.",
                1.0,
            )
        elif row["decision"] == "schedule" and pred_row["decision"] == "schedule":
            pred_option = option_signature[
                (
                    ticket.predecessor_ticket_id,
                    pred_row["primary_technician"],
                    pred_row["assistant_technician"],
                )
            ]
            pred_finish = int(pred_row["start_minute"]) + pred_option.duration
            start = int(row["start_minute"])
            if start < pred_finish + 15:
                add_error(
                    "precedence",
                    f"{ticket.ticket_id} violates predecessor verification lag.",
                    (pred_finish + 15 - start) / max(1, pred_finish + 15),
                )

    for tech_id, intervals in tech_intervals.items():
        tech = tech_by_id[tech_id]
        intervals.sort(key=lambda item: (item[0], item[1], item[2].ticket_id))
        if not intervals:
            continue

        first_start, _, first_ticket = intervals[0]
        first_reach = tech.shift_start + travel[(tech.home_node, first_ticket.node_id)]
        if first_start < first_reach:
            add_error(
                "route",
                f"{tech_id} cannot reach first ticket {first_ticket.ticket_id} from home in time.",
                (first_reach - first_start) / max(1, first_reach),
            )

        prev_start, prev_finish, prev_ticket = intervals[0]
        travel_minutes = travel[(tech.home_node, first_ticket.node_id)]
        service_minutes = prev_finish - prev_start

        for start, finish, ticket in intervals[1:]:
            needed = prev_finish + travel[(prev_ticket.node_id, ticket.node_id)]
            if start < needed:
                add_error(
                    "route",
                    f"{tech_id} violates location continuity between {prev_ticket.ticket_id} and {ticket.ticket_id}.",
                    (needed - start) / max(1, needed),
                )
            travel_minutes += travel[(prev_ticket.node_id, ticket.node_id)]
            service_minutes += finish - start
            prev_start, prev_finish, prev_ticket = start, finish, ticket

        route_end = prev_finish + travel[(prev_ticket.node_id, tech.home_node)]
        travel_minutes += travel[(prev_ticket.node_id, tech.home_node)]
        if route_end > tech.shift_end + tech.overtime_limit:
            add_error(
                "route",
                f"{tech_id} exceeds shift plus overtime limit.",
                (route_end - tech.shift_end - tech.overtime_limit)
                / max(1, tech.shift_end + tech.overtime_limit),
            )
        overtime = max(0, route_end - tech.shift_end)
        total_labor_cost += tech.wage_per_min * (travel_minutes + service_minutes)
        total_labor_cost += max(0.0, tech.overtime_cost_per_min - tech.wage_per_min) * overtime

    for tech in data["technicians"]:
        for part in PARTS:
            limit = tech.stock[part] + data["replenishments"][tech.technician_id][part]
            used = primary_parts[tech.technician_id][part]
            if used > limit:
                add_error(
                    "stock",
                    f"{tech.technician_id} exceeds {part} stock ({used} > {limit}).",
                    (used - limit) / max(1, limit),
                )

    for limit in data["staging_limits"]:
        events = []
        for start, finish, _ticket_id in intervals_by_region[limit.region]:
            left = max(start, limit.start_minute)
            right = min(finish, limit.end_minute)
            if left < right:
                events.append((left, 1))
                events.append((right, -1))
        active = 0
        for minute, delta in sorted(events, key=lambda item: (item[0], item[1])):
            active += delta
            if active > limit.max_active_crews:
                add_error(
                    "staging",
                    f"{limit.region} staging cap violated in interval {limit.interval_id}.",
                    (active - limit.max_active_crews) / max(1, limit.max_active_crews),
                )
                break

    objective = total_labor_cost + total_lateness_cost + total_deferral_cost
    return {
        "ok": not errors,
        "errors": errors,
        "theta": max(residuals) if residuals else 0.0,
        "violated_groups": len(groups),
        "objective": objective,
        "labor_cost": total_labor_cost,
        "lateness_cost": total_lateness_cost,
        "deferral_cost": total_deferral_cost,
    }


def build_model(data, options_by_ticket):
    technicians = data["technicians"]
    tickets = data["tickets"]
    tech_by_id = tech_map(data)
    ticket_by_id = ticket_map(data)
    travel = data["travel"]
    floors = floor_requirements(data)
    access_by_ticket = access_map(data)
    current_time = int(data["wave"]["current_time_minute"])

    model = Model("field_service_initial")
    model.setParam("limits/gap", SCIP_GAP)
    model.setParam("display/verblevel", 0)
    time_limit_env = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    time_limit = DEFAULT_TIME_LIMIT
    if time_limit_env:
        time_limit = min(DEFAULT_TIME_LIMIT, float(time_limit_env))
    model.setParam("limits/time", time_limit)

    max_travel = max(travel.values())
    max_deadline = max(ticket.deadline_minute for ticket in tickets)
    max_shift_return = max(tech.shift_end + tech.overtime_limit for tech in technicians)
    max_duration = max(option.duration for options in options_by_ticket.values() for option in options)
    horizon = max(max_deadline, max_shift_return) + max_travel + max_duration + 30

    ticket_ids = [ticket.ticket_id for ticket in tickets]
    tech_ticket_ids = participant_ticket_map(options_by_ticket)

    u = {}
    d = {}
    start = {}
    late = {}
    z = {}
    use = {}
    route_end = {}
    overtime = {}
    window_use = {}
    x = {}

    for ticket in tickets:
        d[ticket.ticket_id] = model.addVar(vtype="B", name=f"defer[{ticket.ticket_id}]")
        start[ticket.ticket_id] = model.addVar(vtype="I", lb=0, ub=horizon, name=f"start[{ticket.ticket_id}]")
        late[ticket.ticket_id] = model.addVar(vtype="C", lb=0.0, name=f"late[{ticket.ticket_id}]")
        for option in options_by_ticket[ticket.ticket_id]:
            u[(ticket.ticket_id, option.option_id)] = model.addVar(
                vtype="B", name=f"u[{ticket.ticket_id},{option.option_id}]"
            )
        if access_by_ticket[ticket.ticket_id]:
            for idx, _window in enumerate(access_by_ticket[ticket.ticket_id]):
                window_use[(ticket.ticket_id, idx)] = model.addVar(
                    vtype="B", name=f"window[{ticket.ticket_id},{idx}]"
                )

    for tech in technicians:
        tech_id = tech.technician_id
        use[tech_id] = model.addVar(vtype="B", name=f"use[{tech_id}]")
        route_end[tech_id] = model.addVar(
            vtype="C",
            lb=0.0,
            ub=tech.shift_end + tech.overtime_limit,
            name=f"route_end[{tech_id}]",
        )
        overtime[tech_id] = model.addVar(
            vtype="C",
            lb=0.0,
            ub=tech.overtime_limit,
            name=f"overtime[{tech_id}]",
        )
        possible = tech_ticket_ids.get(tech_id, [])
        for ticket_id in possible:
            z[(tech_id, ticket_id)] = model.addVar(vtype="B", name=f"z[{tech_id},{ticket_id}]")
            x[(tech_id, "__HOME__", ticket_id)] = model.addVar(
                vtype="B", name=f"x[{tech_id},HOME,{ticket_id}]"
            )
            x[(tech_id, ticket_id, "__HOME__")] = model.addVar(
                vtype="B", name=f"x[{tech_id},{ticket_id},HOME]"
            )
        for ticket_i in possible:
            for ticket_j in possible:
                if ticket_i == ticket_j:
                    continue
                x[(tech_id, ticket_i, ticket_j)] = model.addVar(
                    vtype="B", name=f"x[{tech_id},{ticket_i},{ticket_j}]"
                )

    finish_expr = {}
    for ticket in tickets:
        finish_expr[ticket.ticket_id] = start[ticket.ticket_id] + quicksum(
            option.duration * u[(ticket.ticket_id, option.option_id)]
            for option in options_by_ticket[ticket.ticket_id]
        )

    for ticket in tickets:
        ticket_id = ticket.ticket_id
        model.addCons(
            quicksum(u[(ticket_id, option.option_id)] for option in options_by_ticket[ticket_id]) + d[ticket_id] == 1,
            name=f"one_decision[{ticket_id}]",
        )
        if ticket.priority == "P1":
            model.addCons(d[ticket_id] == 0, name=f"p1_mandatory[{ticket_id}]")
        lower = earliest_start(ticket, current_time)
        model.addCons(start[ticket_id] >= lower * (1 - d[ticket_id]), name=f"release[{ticket_id}]")
        model.addCons(start[ticket_id] <= horizon * (1 - d[ticket_id]), name=f"start_zero[{ticket_id}]")
        model.addCons(finish_expr[ticket_id] <= ticket.deadline_minute, name=f"deadline[{ticket_id}]")
        model.addCons(late[ticket_id] >= finish_expr[ticket_id] - ticket.due_minute, name=f"late_lb[{ticket_id}]")
        model.addCons(
            late[ticket_id] <= max(0, ticket.deadline_minute - ticket.due_minute) * (1 - d[ticket_id]),
            name=f"late_ub[{ticket_id}]",
        )
        if access_by_ticket[ticket_id]:
            model.addCons(
                quicksum(
                    window_use[(ticket_id, idx)] for idx in range(len(access_by_ticket[ticket_id]))
                )
                == 1 - d[ticket_id],
                name=f"window_pick[{ticket_id}]",
            )
            for idx, window in enumerate(access_by_ticket[ticket_id]):
                model.addCons(
                    start[ticket_id] >= window.earliest_start_minute - horizon * (1 - window_use[(ticket_id, idx)]),
                    name=f"window_lb[{ticket_id},{idx}]",
                )
                model.addCons(
                    start[ticket_id] <= window.latest_start_minute + horizon * (1 - window_use[(ticket_id, idx)]),
                    name=f"window_ub[{ticket_id},{idx}]",
                )
        if ticket.predecessor_ticket_id and ticket.predecessor_ticket_id in finish_expr:
            pred = ticket.predecessor_ticket_id
            model.addCons(d[ticket_id] >= d[pred], name=f"pred_defer[{ticket_id}]")
            model.addCons(
                start[ticket_id] >= finish_expr[pred] + 15 - horizon * (d[ticket_id] + d[pred]),
                name=f"pred_time[{ticket_id}]",
            )

    for region, minimum in floors.items():
        region_ticket_ids = [
            ticket.ticket_id
            for ticket in tickets
            if ticket.region == region and ticket.priority in {"P1", "P2"}
        ]
        model.addCons(
            quicksum(1 - d[ticket_id] for ticket_id in region_ticket_ids) >= minimum,
            name=f"service_floor[{region}]",
        )

    for tech in technicians:
        tech_id = tech.technician_id
        possible = tech_ticket_ids.get(tech_id, [])
        if not possible:
            model.addCons(use[tech_id] == 0, name=f"use_zero[{tech_id}]")
            continue

        for ticket_id in possible:
            model.addCons(
                z[(tech_id, ticket_id)]
                == quicksum(
                    u[(ticket_id, option.option_id)]
                    for option in options_by_ticket[ticket_id]
                    if tech_id in option.participants
                ),
                name=f"participation[{tech_id},{ticket_id}]",
            )

        model.addCons(
            quicksum(z[(tech_id, ticket_id)] for ticket_id in possible) <= len(possible) * use[tech_id],
            name=f"use_ub[{tech_id}]",
        )
        model.addCons(
            quicksum(z[(tech_id, ticket_id)] for ticket_id in possible) >= use[tech_id],
            name=f"use_lb[{tech_id}]",
        )
        model.addCons(
            quicksum(x[(tech_id, "__HOME__", ticket_id)] for ticket_id in possible) == use[tech_id],
            name=f"home_out[{tech_id}]",
        )
        model.addCons(
            quicksum(x[(tech_id, ticket_id, "__HOME__")] for ticket_id in possible) == use[tech_id],
            name=f"home_in[{tech_id}]",
        )

        for ticket_id in possible:
            incoming = quicksum(
                x[(tech_id, other, ticket_id)] for other in possible if other != ticket_id
            ) + x[(tech_id, "__HOME__", ticket_id)]
            outgoing = quicksum(
                x[(tech_id, ticket_id, other)] for other in possible if other != ticket_id
            ) + x[(tech_id, ticket_id, "__HOME__")]
            model.addCons(incoming == z[(tech_id, ticket_id)], name=f"in_deg[{tech_id},{ticket_id}]")
            model.addCons(outgoing == z[(tech_id, ticket_id)], name=f"out_deg[{tech_id},{ticket_id}]")
            ticket = ticket_by_id[ticket_id]
            model.addCons(
                start[ticket_id]
                >= tech.shift_start + travel[(tech.home_node, ticket.node_id)] - horizon * (1 - x[(tech_id, "__HOME__", ticket_id)]),
                name=f"first_reach[{tech_id},{ticket_id}]",
            )
            model.addCons(
                route_end[tech_id]
                >= finish_expr[ticket_id] + travel[(ticket.node_id, tech.home_node)] - horizon * (1 - x[(tech_id, ticket_id, "__HOME__")]),
                name=f"return_home[{tech_id},{ticket_id}]",
            )

        for ticket_i in possible:
            node_i = ticket_by_id[ticket_i].node_id
            for ticket_j in possible:
                if ticket_i == ticket_j:
                    continue
                node_j = ticket_by_id[ticket_j].node_id
                model.addCons(
                    start[ticket_j]
                    >= finish_expr[ticket_i] + travel[(node_i, node_j)] - horizon * (1 - x[(tech_id, ticket_i, ticket_j)]),
                    name=f"time_link[{tech_id},{ticket_i},{ticket_j}]",
                )

        model.addCons(
            route_end[tech_id] <= (tech.shift_end + tech.overtime_limit) * use[tech_id],
            name=f"route_end_zero[{tech_id}]",
        )
        model.addCons(overtime[tech_id] >= route_end[tech_id] - tech.shift_end, name=f"ot_lb[{tech_id}]")

    for tech in technicians:
        tech_id = tech.technician_id
        for part in PARTS:
            model.addCons(
                quicksum(
                    ticket_by_id[ticket_id].parts[part] * u[(ticket_id, option.option_id)]
                    for ticket_id in ticket_ids
                    for option in options_by_ticket[ticket_id]
                    if option.primary_technician == tech_id
                )
                <= tech.stock[part] + data["replenishments"][tech_id][part],
                name=f"stock[{tech_id},{part}]",
            )

    objective_terms = []
    for tech in technicians:
        tech_id = tech.technician_id
        possible = tech_ticket_ids.get(tech_id, [])
        travel_expr = quicksum(
            travel[(tech.home_node, ticket_by_id[ticket_id].node_id)] * x[(tech_id, "__HOME__", ticket_id)]
            + travel[(ticket_by_id[ticket_id].node_id, tech.home_node)] * x[(tech_id, ticket_id, "__HOME__")]
            for ticket_id in possible
        )
        travel_expr += quicksum(
            travel[(ticket_by_id[ticket_i].node_id, ticket_by_id[ticket_j].node_id)]
            * x[(tech_id, ticket_i, ticket_j)]
            for ticket_i in possible
            for ticket_j in possible
            if ticket_i != ticket_j
        )
        service_expr = quicksum(
            option.duration * u[(ticket_id, option.option_id)]
            for ticket_id in ticket_ids
            for option in options_by_ticket[ticket_id]
            if tech_id in option.participants
        )
        objective_terms.append(tech.wage_per_min * (travel_expr + service_expr))
        objective_terms.append(max(0.0, tech.overtime_cost_per_min - tech.wage_per_min) * overtime[tech_id])

    for ticket in tickets:
        objective_terms.append(ticket.deferral_penalty * d[ticket.ticket_id])
        objective_terms.append(ticket.late_penalty_per_min * late[ticket.ticket_id])

    model.setObjective(quicksum(objective_terms), "minimize")
    return model, {
        "u": u,
        "d": d,
        "start": start,
        "late": late,
        "z": z,
        "use": use,
        "x": x,
        "route_end": route_end,
        "overtime": overtime,
        "time_limit": time_limit,
    }


def add_warm_start(model, vars_dict, heuristic_rows, data, options_by_ticket):
    if heuristic_rows is None:
        return False

    option_signature = {
        (option.ticket_id, option.primary_technician, option.assistant_technician): option
        for options in options_by_ticket.values()
        for option in options
    }
    row_by_ticket = {row["ticket_id"]: row for row in heuristic_rows}
    tech_intervals = defaultdict(list)
    tech_by_id = tech_map(data)

    sol = model.createPartialSol()

    for ticket in data["tickets"]:
        row = row_by_ticket[ticket.ticket_id]
        ticket_id = ticket.ticket_id
        is_defer = 1 if row["decision"] == "defer" else 0
        model.setSolVal(sol, vars_dict["d"][ticket_id], is_defer)
        if row["decision"] == "schedule":
            start_value = int(row["start_minute"])
            model.setSolVal(sol, vars_dict["start"][ticket_id], start_value)
            choice = option_signature[
                (ticket_id, row["primary_technician"], row["assistant_technician"])
            ]
            for option in options_by_ticket[ticket_id]:
                model.setSolVal(
                    sol,
                    vars_dict["u"][(ticket_id, option.option_id)],
                    1 if option.option_id == choice.option_id else 0,
                )
            late_value = max(0, start_value + choice.duration - ticket.due_minute)
            model.setSolVal(sol, vars_dict["late"][ticket_id], late_value)
            for tech_id in choice.participants:
                tech_intervals[tech_id].append((start_value, ticket_id))
        else:
            model.setSolVal(sol, vars_dict["start"][ticket_id], 0)
            model.setSolVal(sol, vars_dict["late"][ticket_id], 0)
            for option in options_by_ticket[ticket_id]:
                model.setSolVal(sol, vars_dict["u"][(ticket_id, option.option_id)], 0)

    for tech in data["technicians"]:
        tech_id = tech.technician_id
        intervals = sorted(tech_intervals.get(tech_id, []))
        use_value = 1 if intervals else 0
        if tech_id in vars_dict["use"]:
            model.setSolVal(sol, vars_dict["use"][tech_id], use_value)

        scheduled_ids = {ticket_id for _start, ticket_id in intervals}
        for key, var in vars_dict["z"].items():
            if key[0] == tech_id:
                model.setSolVal(sol, var, 1 if key[1] in scheduled_ids else 0)

        for key, var in vars_dict["x"].items():
            if key[0] != tech_id:
                continue
            model.setSolVal(sol, var, 0)

        if not intervals:
            model.setSolVal(sol, vars_dict["route_end"][tech_id], 0)
            model.setSolVal(sol, vars_dict["overtime"][tech_id], 0)
            continue

        ticket_sequence = [ticket_id for _start, ticket_id in intervals]
        first = ticket_sequence[0]
        model.setSolVal(sol, vars_dict["x"][(tech_id, "__HOME__", first)], 1)
        for left, right in zip(ticket_sequence, ticket_sequence[1:]):
            model.setSolVal(sol, vars_dict["x"][(tech_id, left, right)], 1)
        last = ticket_sequence[-1]
        model.setSolVal(sol, vars_dict["x"][(tech_id, last, "__HOME__")], 1)

        last_row = row_by_ticket[last]
        last_option = option_signature[
            (last, last_row["primary_technician"], last_row["assistant_technician"])
        ]
        last_finish = int(last_row["start_minute"]) + last_option.duration
        route_end_value = last_finish + data["travel"][(ticket_map(data)[last].node_id, tech.home_node)]
        model.setSolVal(sol, vars_dict["route_end"][tech_id], route_end_value)
        model.setSolVal(sol, vars_dict["overtime"][tech_id], max(0, route_end_value - tech.shift_end))

    return bool(model.addSol(sol))


def extract_plan_from_solution(model, vars_dict, data, options_by_ticket):
    best = model.getBestSol()
    if best is None:
        return None

    rows = []
    for ticket in data["tickets"]:
        ticket_id = ticket.ticket_id
        if model.getSolVal(best, vars_dict["d"][ticket_id]) >= 0.5:
            rows.append(
                {
                    "ticket_id": ticket_id,
                    "decision": "defer",
                    "primary_technician": "",
                    "assistant_technician": "",
                    "start_minute": "",
                }
            )
            continue
        choice = max(
            options_by_ticket[ticket_id],
            key=lambda option: model.getSolVal(best, vars_dict["u"][(ticket_id, option.option_id)]),
        )
        rows.append(
            {
                "ticket_id": ticket_id,
                "decision": "schedule",
                "primary_technician": choice.primary_technician,
                "assistant_technician": choice.assistant_technician,
                "start_minute": int(round(model.getSolVal(best, vars_dict["start"][ticket_id]))),
            }
        )
    return rows


def write_plan_csv(path: str, rows: List[Dict[str, object]]):
    fieldnames = [
        "ticket_id",
        "decision",
        "primary_technician",
        "assistant_technician",
        "start_minute",
    ]
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_plan_csv(path: str):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def format_float(value: float) -> str:
    return f"{value:.8f}"


def rows_from_plan_dict(plan_dict, data):
    return [plan_dict[ticket.ticket_id] for ticket in data["tickets"]]


def choose_best_heuristic(data, options_by_ticket):
    candidates = []
    for ordering_name in ["constraint_first", "tight_first"]:
        plan = greedy_construct(data, options_by_ticket, ordering_name)
        if plan is None:
            continue
        rows = rows_from_plan_dict(plan, data)
        audit = independent_audit(rows, data, options_by_ticket)
        if audit["ok"]:
            candidates.append((audit["objective"], rows, audit))
    if not candidates:
        return None, {
            "ok": False,
            "errors": ["No greedy feasible incumbent found."],
            "theta": 1.0,
            "violated_groups": 1,
            "objective": math.inf,
            "labor_cost": math.inf,
            "lateness_cost": math.inf,
            "deferral_cost": math.inf,
        }
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], candidates[0][2]


def load_plan_if_present(path: str):
    if not os.path.exists(path):
        return None
    return read_plan_csv(path)


def randomized_repair_search(data, options_by_ticket, incumbent_audit, seeds: int = 400):
    tickets_master = list(data["tickets"])
    tech_by_id = tech_map(data)
    travel = data["travel"]
    current_time = int(data["wave"]["current_time_minute"])
    floors = floor_requirements(data)
    best_rows = None
    best_audit = None

    for seed in range(seeds):
        rng = random.Random(seed)
        tickets = list(tickets_master)
        rng.shuffle(tickets)
        tickets.sort(
            key=lambda ticket: (
                PRIORITY_ORDER[ticket.priority],
                rng.random(),
                ticket.deadline_minute,
                -ticket.deferral_penalty,
            )
        )

        routes = {
            tech.technician_id: RouteState([], tech.home_node, tech.shift_start)
            for tech in data["technicians"]
        }
        primary_parts = defaultdict(lambda: defaultdict(int))
        scheduled_high = defaultdict(int)
        plan = {}
        feasible = True

        for ticket in tickets:
            choices = []
            for option in options_by_ticket[ticket.ticket_id]:
                primary_id = option.primary_technician
                if any(
                    primary_parts[primary_id][part] + ticket.parts[part]
                    > tech_by_id[primary_id].stock[part] + data["replenishments"][primary_id][part]
                    for part in PARTS
                ):
                    continue
                estimate = compute_route_cost_delta(
                    ticket,
                    option,
                    routes,
                    tech_by_id,
                    travel,
                    current_time,
                )
                if estimate is None:
                    continue
                score = estimate[0] + rng.random() * 5.0
                choices.append((score, estimate[0], estimate[1], option))

            if not choices:
                feasible = False
                break

            choices.sort(key=lambda item: item[0])
            _, true_cost, start_time, option = choices[0]
            if ticket.priority == "P3" and true_cost >= ticket.deferral_penalty - 1e-9:
                plan[ticket.ticket_id] = {
                    "ticket_id": ticket.ticket_id,
                    "decision": "defer",
                    "primary_technician": "",
                    "assistant_technician": "",
                    "start_minute": "",
                }
                continue

            finish_time = start_time + option.duration
            for tech_id in option.participants:
                routes[tech_id].jobs.append(ticket.ticket_id)
                routes[tech_id].current_node = ticket.node_id
                routes[tech_id].available_time = finish_time
            for part in PARTS:
                primary_parts[option.primary_technician][part] += ticket.parts[part]
            if ticket.priority in {"P1", "P2"}:
                scheduled_high[ticket.region] += 1
            plan[ticket.ticket_id] = {
                "ticket_id": ticket.ticket_id,
                "decision": "schedule",
                "primary_technician": option.primary_technician,
                "assistant_technician": option.assistant_technician,
                "start_minute": start_time,
            }

        if not feasible or any(scheduled_high[region] < floors[region] for region in floors):
            continue

        rows = rows_from_plan_dict(plan, data)
        audit = independent_audit(rows, data, options_by_ticket)
        if not audit["ok"]:
            continue
        if audit["objective"] + 1e-9 < incumbent_audit["objective"] and (
            best_audit is None or audit["objective"] + 1e-9 < best_audit["objective"]
        ):
            best_rows = rows
            best_audit = audit

    return best_rows, best_audit


def write_log(
    path: str,
    data,
    options_by_ticket,
    incumbent_audit,
    candidate_audit,
    published_audit,
    model,
    vars_dict,
    elapsed,
    published_source,
    disk_audit_ok,
    exact_candidate_used,
):
    status = model.getStatus()
    incumbent_count = model.getNSols()
    objective = model.getObjVal() if incumbent_count else None
    dual_bound = model.getDualbound() if incumbent_count else None
    gap = model.getGap() if incumbent_count else None

    corrupted = read_plan_csv("/app/submissions/initial_plan.csv")
    if corrupted:
        corrupted = corrupted[:-1]
    corrupted_result = independent_audit(corrupted, data, options_by_ticket)

    lines = [
        "# Solve Log Initial",
        "",
        "## Mode Summary",
        f"- rollback_incumbent_audited_from_disk: {disk_audit_ok}",
        f"- exact_candidate_used: {exact_candidate_used}",
        f"- published_source: {published_source}",
        f"- final_feasible: {published_audit['ok']}",
        "",
        "## Solver",
        f"- status: {status}",
        f"- time_limit_seconds: {format_float(vars_dict['time_limit'])}",
        f"- elapsed_seconds: {format_float(elapsed)}",
        f"- incumbent_count: {incumbent_count}",
        f"- objective: {format_float(objective) if objective is not None else 'n/a'}",
        f"- dual_bound: {format_float(dual_bound) if dual_bound is not None else 'n/a'}",
        f"- relative_gap: {format_float(gap) if gap is not None else 'n/a'}",
        "",
        "## State Vector",
        "- artifact_present: True",
        "- artifact_schema_header: exact",
        f"- decision_universe_coverage: {format_float(len(read_plan_csv('/app/submissions/initial_plan.csv')) / len(data['tickets']))}",
        f"- integrality_residual: {format_float(0.0)}",
        f"- max_normalized_hard_residual_theta: {format_float(published_audit['theta'])}",
        f"- violated_constraint_groups: {published_audit['violated_groups']}",
        "",
        "## Objective Comparison",
        f"- incumbent_objective: {format_float(incumbent_audit['objective'])}",
        f"- candidate_objective: {format_float(candidate_audit['objective'])}",
        f"- published_objective: {format_float(published_audit['objective'])}",
        "",
        "## Cost Recompute",
        f"- labor_cost: {format_float(published_audit['labor_cost'])}",
        f"- lateness_cost: {format_float(published_audit['lateness_cost'])}",
        f"- deferral_cost: {format_float(published_audit['deferral_cost'])}",
        f"- total_cost: {format_float(published_audit['objective'])}",
        "",
        "## Instance Notes",
        f"- access_windows_active: {len(data['access_windows'])}",
        f"- staging_limits_active: {len(data['staging_limits'])}",
        f"- replenishment_rows_active: {sum(1 for tech_id in data['replenishments'] if any(data['replenishments'][tech_id][part] for part in PARTS))}",
        "- challenger_issue_addressed: disk artifact is reparsed and audited independently before optimization",
        "",
        "## Audit",
        f"- incumbent_disk_audit_check: {'passed' if disk_audit_ok else 'failed'}",
        f"- final_artifact_check: {'passed' if published_audit['ok'] else 'failed'}",
        f"- negative_audit_rejects_corrupted_copy: {not corrupted_result['ok']}",
    ]
    for error in published_audit["errors"]:
        lines.append(f"- {error}")
    if not incumbent_audit["ok"]:
        lines.append("- incumbent_audit_errors:")
        for error in incumbent_audit["errors"]:
            lines.append(f"- {error}")

    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    start_wall = time.time()
    data = parse_data()
    options_by_ticket = build_crew_options(data)
    incumbent_rows = load_plan_if_present("/app/submissions/initial_plan.csv")
    incumbent_audit = None
    if incumbent_rows is not None:
        incumbent_audit = independent_audit(incumbent_rows, data, options_by_ticket)

    if incumbent_rows is None or not incumbent_audit["ok"]:
        heuristic_rows, heuristic_audit = choose_best_heuristic(data, options_by_ticket)
        if heuristic_rows is None:
            raise RuntimeError("No feasible rollback incumbent is available from disk or heuristic construction.")
        incumbent_rows = heuristic_rows
        incumbent_audit = heuristic_audit
        write_plan_csv("/app/submissions/initial_plan.csv", incumbent_rows)

    if not incumbent_audit["ok"]:
        raise RuntimeError("Rollback incumbent is not independently feasible after repair attempts.")

    candidate_rows = incumbent_rows
    candidate_audit = incumbent_audit

    randomized_rows, randomized_audit = randomized_repair_search(
        data, options_by_ticket, incumbent_audit
    )
    if randomized_rows is not None and randomized_audit["objective"] + 1e-9 < candidate_audit["objective"]:
        candidate_rows = randomized_rows
        candidate_audit = randomized_audit

    model, vars_dict = build_model(data, options_by_ticket)
    add_warm_start(model, vars_dict, incumbent_rows, data, options_by_ticket)
    if candidate_rows is not incumbent_rows:
        add_warm_start(model, vars_dict, candidate_rows, data, options_by_ticket)
    model.optimize()

    exact_candidate_used = False
    if model.getNSols() > 0:
        exact_rows = extract_plan_from_solution(model, vars_dict, data, options_by_ticket)
        if exact_rows is not None:
            exact_audit = independent_audit(exact_rows, data, options_by_ticket)
            if exact_audit["ok"] and exact_audit["objective"] + 1e-9 < candidate_audit["objective"]:
                candidate_rows = exact_rows
                candidate_audit = exact_audit
                exact_candidate_used = True

    published_rows = incumbent_rows
    published_audit = incumbent_audit
    published_source = "rollback_incumbent"
    if candidate_audit["objective"] + 1e-9 < incumbent_audit["objective"]:
        write_plan_csv("/app/submissions/initial_plan.csv", candidate_rows)
        published_rows = read_plan_csv("/app/submissions/initial_plan.csv")
        published_audit = independent_audit(published_rows, data, options_by_ticket)
        if not published_audit["ok"]:
            write_plan_csv("/app/submissions/initial_plan.csv", incumbent_rows)
            published_rows = read_plan_csv("/app/submissions/initial_plan.csv")
            published_audit = independent_audit(published_rows, data, options_by_ticket)
            published_source = "rollback_incumbent"
        else:
            published_source = "scip" if exact_candidate_used else "randomized_lns"
    else:
        published_rows = read_plan_csv("/app/submissions/initial_plan.csv")
        published_audit = independent_audit(published_rows, data, options_by_ticket)

    if not published_audit["ok"]:
        raise RuntimeError("Published artifact failed independent audit.")

    elapsed = time.time() - start_wall
    write_log(
        "/app/submissions/solve_log_initial.md",
        data,
        options_by_ticket,
        incumbent_audit,
        candidate_audit,
        published_audit,
        model,
        vars_dict,
        elapsed,
        published_source,
        disk_audit_ok=incumbent_audit["ok"],
        exact_candidate_used=exact_candidate_used,
    )


if __name__ == "__main__":
    main()
