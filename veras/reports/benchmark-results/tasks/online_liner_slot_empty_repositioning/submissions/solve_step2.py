#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from pyscipopt import Model, quicksum

import solve_initial as base


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
PREV_PLAN_PATH = SUBMISSIONS_DIR / "initial_plan.csv"
PLAN_PATH = SUBMISSIONS_DIR / "step2_replan.csv"
CANDIDATE_PLAN_PATH = SUBMISSIONS_DIR / "step2_replan_candidate.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log_step2.md"


@dataclass(frozen=True)
class ProtectedSlotRule:
    rule_id: str
    leg_id: str
    priority: str
    min_teu: float
    description: str


@dataclass(frozen=True)
class TransferLimitRule:
    rule_id: str
    port_id: str
    day: int
    max_reefer_transfer_teu: float
    max_dg1_transfer_teu: float
    description: str


def read_optional_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    return base.read_csv(path)


def startswith_tuple(full: Tuple[str, ...], prefix: Tuple[str, ...]) -> bool:
    return full[: len(prefix)] == prefix


def load_previous_plan_state(instance) -> Dict[str, object]:
    rows = base.read_plan_rows(PREV_PLAN_PATH)
    bookings = instance["bookings"]
    legs = instance["legs"]

    booking_decisions: Dict[str, str] = {}
    booking_legs = defaultdict(list)
    move_qty: Dict[Tuple[str, str], float] = {}
    lease_qty: Dict[Tuple[str, int, str], float] = {}

    for row in rows:
        row_type = row["row_type"]
        if row_type == "booking_decision" and row["item_id"] in bookings:
            booking_decisions[row["item_id"]] = row["decision"]
        elif row_type == "booking_leg" and row["item_id"] in bookings:
            booking_legs[row["item_id"]].append(row["leg_id"])
        elif row_type == "empty_move" and row["leg_id"] in legs:
            qty = float(row["quantity_teu"] or "0")
            move_qty[(row["leg_id"], row["container_type"])] = qty
        elif row_type == "lease":
            qty = float(row["quantity_teu"] or "0")
            lease_qty[(row["port_id"], int(row["day"]), row["container_type"])] = qty

    accepted_paths = {
        bid: tuple(leg_ids)
        for bid, leg_ids in booking_legs.items()
        if booking_decisions.get(bid) == "accept"
    }

    rejected_bookings = {
        bid
        for bid, decision in booking_decisions.items()
        if decision == "reject"
    }

    return {
        "rows": rows,
        "booking_decisions": booking_decisions,
        "accepted_paths": accepted_paths,
        "rejected_bookings": rejected_bookings,
        "moves": move_qty,
        "leases": lease_qty,
    }


def build_baseline_rows(instance) -> List[Dict[str, str]]:
    prior_rows = [dict(row) for row in instance["prior_state"]["rows"]]
    seen_decisions = {
        row["item_id"]
        for row in prior_rows
        if row["row_type"] == "booking_decision"
    }
    rows = []
    for row in prior_rows:
        if row["row_type"] in {"booking_decision", "booking_leg", "empty_move", "lease"}:
            rows.append(row)
    for bid in instance["booking_order"]:
        if bid in seen_decisions:
            continue
        rows.append(
            {
                "row_type": "booking_decision",
                "item_id": bid,
                "decision": "reject",
                "leg_id": "",
                "port_id": "",
                "day": "",
                "container_type": "",
                "quantity_teu": "",
            }
        )
    return rows


def load_instance():
    instance = base.load_instance()

    current_step_info = json.loads((DATA_DIR / "current_step.json").read_text())
    instance["current_step_info"] = current_step_info
    instance["current_step_name"] = current_step_info.get("current_step", "unknown")
    instance["current_day"] = int(current_step_info.get("current_day", 0))
    instance["freeze_until_day"] = int(current_step_info.get("freeze_until_day", 0))

    instance["protected_slots"] = [
        ProtectedSlotRule(
            rule_id=row["rule_id"],
            leg_id=row["leg_id"],
            priority=row["priority"],
            min_teu=float(row["min_teu"]),
            description=row["description"],
        )
        for row in read_optional_csv(DATA_DIR / "protected_slots.csv")
    ]
    instance["transfer_limits"] = [
        TransferLimitRule(
            rule_id=row["rule_id"],
            port_id=row["port_id"],
            day=int(row["day"]),
            max_reefer_transfer_teu=float(row["max_reefer_transfer_teu"]),
            max_dg1_transfer_teu=float(row["max_dg1_transfer_teu"]),
            description=row["description"],
        )
        for row in read_optional_csv(DATA_DIR / "transfer_limits.csv")
    ]

    has_replan_evidence = bool(instance["protected_slots"] or instance["transfer_limits"])
    instance["effective_stage_name"] = "step2" if has_replan_evidence else instance["current_step_name"]
    instance["stage_label_mismatch"] = instance["effective_stage_name"] != instance["current_step_name"]

    special_rules_path = DATA_DIR / "special_booking_rules.csv"
    if special_rules_path.exists():
        raise RuntimeError("special_booking_rules.csv is present but this stage-2 solver does not yet parse its schema.")

    instance["prior_state"] = load_previous_plan_state(instance)

    frozen_prefixes = {}
    for bid, prior_path in instance["prior_state"]["accepted_paths"].items():
        prefix = tuple(
            leg_id
            for leg_id in prior_path
            if instance["legs"][leg_id].depart_day <= instance["freeze_until_day"]
        )
        if prefix:
            frozen_prefixes[bid] = prefix
    instance["frozen_prefixes"] = frozen_prefixes

    return instance


def enumerate_paths(instance):
    return base.enumerate_paths(instance)


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

    model = Model("step2_online_liner_replan")
    time_limit_env = base.os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if time_limit_env:
        time_limit = min(base.DEFAULT_TIME_LIMIT, float(time_limit_env))
    else:
        time_limit = base.DEFAULT_TIME_LIMIT
    model.setParam("limits/gap", base.SCIP_REL_GAP)
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
            path_vars[(bid, option.path_id)] = model.addVar(vtype="B", name=f"path[{option.path_id}]")

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
    leg_priority_loaded = defaultdict(list)
    transfer_reefer_usage = defaultdict(list)
    transfer_dg1_usage = defaultdict(list)

    for bid, option_map in options_by_booking.items():
        booking = bookings[bid]
        for option in option_map.values():
            var = path_vars[(bid, option.path_id)]
            first_leg = legs[option.leg_ids[0]]
            booking_start[(booking.origin_port, booking.container_type, first_leg.depart_day)].append((var, booking.teu))
            gate_out_bookings[(booking.origin_port, first_leg.depart_day)].append((var, booking.teu))
            booking_release[(booking.destination_port, booking.container_type, option.release_day)].append((var, booking.teu))

            event_counter = Counter(option.customs_events)
            for event, count in event_counter.items():
                customs_usage[event].append((var, booking.teu * count))
                if booking.medical_flag and booking.container_type == "REEFER":
                    medical_customs_usage[event].append((var, booking.teu * count))

            for leg_id in option.leg_ids:
                leg_loaded[leg_id].append((var, booking.teu))
                leg_priority_loaded[(leg_id, booking.priority)].append((var, booking.teu))
                if booking.container_type == "REEFER":
                    leg_reefer[leg_id].append((var, booking.teu))
                if booking.hazmat_class == "DG1":
                    leg_dg1[leg_id].append((var, booking.teu))

            for leg_id in option.leg_ids[1:]:
                transfer_port = legs[leg_id].from_port
                transfer_day = legs[leg_id].depart_day
                if booking.container_type == "REEFER":
                    transfer_reefer_usage[(transfer_port, transfer_day)].append((var, booking.teu))
                if booking.hazmat_class == "DG1":
                    transfer_dg1_usage[(transfer_port, transfer_day)].append((var, booking.teu))

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
            model.addCons(medical_used <= max_medical, name=f"medical_customs[{event[0]},{event[1]}]")

    for rule in instance["protected_slots"]:
        protected_expr = quicksum(var * coeff for var, coeff in leg_priority_loaded[(rule.leg_id, rule.priority)])
        model.addCons(protected_expr >= rule.min_teu, name=f"protected_slot[{rule.rule_id}]")

    for rule in instance["transfer_limits"]:
        reefer_transfer = quicksum(var * coeff for var, coeff in transfer_reefer_usage[(rule.port_id, rule.day)])
        dg1_transfer = quicksum(var * coeff for var, coeff in transfer_dg1_usage[(rule.port_id, rule.day)])
        model.addCons(reefer_transfer <= rule.max_reefer_transfer_teu, name=f"reefer_transfer[{rule.rule_id}]")
        model.addCons(dg1_transfer <= rule.max_dg1_transfer_teu, name=f"dg1_transfer[{rule.rule_id}]")

    prior_moves = instance["prior_state"]["moves"]
    for leg in legs.values():
        if leg.depart_day > instance["freeze_until_day"]:
            continue
        for container_type in ("STD", "REEFER"):
            model.addCons(
                move_vars[(leg.leg_id, container_type)] == prior_moves.get((leg.leg_id, container_type), 0.0),
                name=f"freeze_move[{leg.leg_id},{container_type}]",
            )

    prior_leases = instance["prior_state"]["leases"]
    for key, var in lease_vars.items():
        port_id, day, container_type = key
        if day > instance["freeze_until_day"]:
            continue
        model.addCons(
            var == prior_leases.get((port_id, day, container_type), 0.0),
            name=f"freeze_lease[{port_id},{day},{container_type}]",
        )

    for bid, prefix in instance["frozen_prefixes"].items():
        model.addCons(reject_vars[bid] == 0, name=f"freeze_accept[{bid}]")
        for option in booking_paths[bid]:
            if not startswith_tuple(option.leg_ids, prefix):
                model.addCons(path_vars[(bid, option.path_id)] == 0, name=f"freeze_prefix[{bid},{option.path_id}]")

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
            model.addCons(stock_std + stock_reefer <= limit["yard_capacity"], name=f"yard_cap[{port_id},{day}]")
            model.addCons(stock_reefer <= limit["reefer_plug_capacity"], name=f"yard_reefer[{port_id},{day}]")

            gate_out_expr = quicksum(var * coeff for var, coeff in gate_out_bookings[(port_id, day)]) + quicksum(
                gate_out_moves[(port_id, day)]
            )
            model.addCons(gate_out_expr <= limit["gate_out_limit"], name=f"gate_out[{port_id},{day}]")

            gate_in_expr = quicksum(var * coeff for var, coeff in booking_release[(port_id, "STD", day)]) + quicksum(
                var * coeff for var, coeff in booking_release[(port_id, "REEFER", day)]
            )
            gate_in_expr += quicksum(gate_in_moves[(port_id, day)])
            gate_in_expr += quicksum(gate_in_leases[(port_id, day)])
            model.addCons(gate_in_expr <= limit["gate_in_limit"], name=f"gate_in[{port_id},{day}]")

    change_vars = {}
    for bid, prior_path in instance["prior_state"]["accepted_paths"].items():
        if bid not in bookings:
            continue
        matching_path = None
        for option in booking_paths[bid]:
            if option.leg_ids == prior_path:
                matching_path = option.path_id
                break
        if matching_path is None:
            change_vars[bid] = model.addVar(lb=1.0, ub=1.0, vtype="B", name=f"change[{bid}]")
        else:
            change_vars[bid] = model.addVar(vtype="B", name=f"change[{bid}]")
            model.addCons(
                change_vars[bid] >= 1 - path_vars[(bid, matching_path)],
                name=f"change_link[{bid}]",
            )

    objective = quicksum(
        path_vars[(bid, option.path_id)] * (bookings[bid].revenue - option.customs_cost)
        for bid, options in booking_paths.items()
        for option in options
    )
    objective -= quicksum(reject_vars[bid] * bookings[bid].reject_penalty for bid in bookings)
    objective -= quicksum(
        move_vars[(leg_id, container_type)] * (cost + base.CARBON_COST_PER_KG * carbon)
        for (leg_id, container_type), (cost, carbon) in empty_costs.items()
    )
    objective -= quicksum(lease_vars[key] * lease_options[key][1] for key in lease_vars)
    objective -= quicksum(
        stock_vars[(port_id, "STD", day)] * ports[port_id].storage_cost_std
        + stock_vars[(port_id, "REEFER", day)] * ports[port_id].storage_cost_reefer
        for port_id in ports
        for day in days
    )
    objective -= quicksum(change_vars[bid] * bookings[bid].change_penalty for bid in change_vars)
    model.setObjective(objective, "maximize")

    return {
        "model": model,
        "time_limit": time_limit,
        "reject_vars": reject_vars,
        "path_vars": path_vars,
        "move_vars": move_vars,
        "lease_vars": lease_vars,
        "stock_vars": stock_vars,
        "change_vars": change_vars,
        "options_by_booking": options_by_booking,
    }


def validate_plan(instance, booking_paths, rows: List[Dict[str, str]]):
    base_audit = base.validate_plan(instance, booking_paths, rows)

    errors = list(base_audit["errors"])
    residuals = defaultdict(float, base_audit["residuals"])

    def register_violation(group: str, violation: float, scale: float = 1.0, message: str | None = None) -> None:
        if violation <= base.EPS:
            return
        residual = violation / max(1.0, abs(scale))
        residuals[group] = max(residuals[group], residual)
        if message:
            errors.append(message)

    bookings = instance["bookings"]
    legs = instance["legs"]
    decisions = base_audit["parsed_solution"]["booking_decisions"]
    selected_paths = {
        bid: option
        for bid, option in base_audit["parsed_solution"]["booking_paths"].items()
        if option is not None
    }
    moves = base_audit["parsed_solution"]["moves"]
    leases = base_audit["parsed_solution"]["leases"]

    leg_priority_load = defaultdict(float)
    transfer_reefer_usage = defaultdict(float)
    transfer_dg1_usage = defaultdict(float)
    change_penalty_total = 0.0

    for bid, option in selected_paths.items():
        booking = bookings[bid]
        for leg_id in option.leg_ids:
            leg_priority_load[(leg_id, booking.priority)] += booking.teu
        for leg_id in option.leg_ids[1:]:
            transfer_port = legs[leg_id].from_port
            transfer_day = legs[leg_id].depart_day
            if booking.container_type == "REEFER":
                transfer_reefer_usage[(transfer_port, transfer_day)] += booking.teu
            if booking.hazmat_class == "DG1":
                transfer_dg1_usage[(transfer_port, transfer_day)] += booking.teu

    for rule in instance["protected_slots"]:
        used = leg_priority_load[(rule.leg_id, rule.priority)]
        register_violation(
            "protected_slot",
            max(0.0, rule.min_teu - used),
            rule.min_teu,
            f"Protected slot {rule.rule_id} uses {used} below required {rule.min_teu}.",
        )

    for rule in instance["transfer_limits"]:
        reefer_used = transfer_reefer_usage[(rule.port_id, rule.day)]
        dg1_used = transfer_dg1_usage[(rule.port_id, rule.day)]
        register_violation(
            "transfer_limit_reefer",
            max(0.0, reefer_used - rule.max_reefer_transfer_teu),
            rule.max_reefer_transfer_teu,
            f"Reefer transfer cap {rule.rule_id} uses {reefer_used} above {rule.max_reefer_transfer_teu}.",
        )
        register_violation(
            "transfer_limit_dg1",
            max(0.0, dg1_used - rule.max_dg1_transfer_teu),
            rule.max_dg1_transfer_teu,
            f"DG1 transfer cap {rule.rule_id} uses {dg1_used} above {rule.max_dg1_transfer_teu}.",
        )

    for bid, prefix in instance["frozen_prefixes"].items():
        if decisions.get(bid) != "accept":
            register_violation(
                "frozen_booking",
                1.0,
                1.0,
                f"Frozen accepted booking {bid} cannot be rejected in the replanning window.",
            )
            continue
        option = selected_paths.get(bid)
        if option is None or not startswith_tuple(option.leg_ids, prefix):
            register_violation(
                "frozen_booking",
                1.0,
                1.0,
                f"Booking {bid} does not preserve frozen path prefix {prefix}.",
            )

    prior_moves = instance["prior_state"]["moves"]
    for leg in instance["legs"].values():
        if leg.depart_day > instance["freeze_until_day"]:
            continue
        for container_type in ("STD", "REEFER"):
            key = (leg.leg_id, container_type)
            expected = prior_moves.get(key, 0.0)
            actual = moves.get(key, 0.0)
            register_violation(
                "frozen_move",
                abs(actual - expected),
                max(1.0, abs(expected)),
                f"Frozen move {key} changed from {expected} to {actual}.",
            )

    prior_leases = instance["prior_state"]["leases"]
    for key in instance["lease_options"]:
        port_id, day, container_type = key
        if day > instance["freeze_until_day"]:
            continue
        expected = prior_leases.get((port_id, day, container_type), 0.0)
        actual = leases.get((port_id, day, container_type), 0.0)
        register_violation(
            "frozen_lease",
            abs(actual - expected),
            max(1.0, abs(expected)),
            f"Frozen lease {(port_id, day, container_type)} changed from {expected} to {actual}.",
        )

    for bid, prior_path in instance["prior_state"]["accepted_paths"].items():
        current_option = selected_paths.get(bid)
        if current_option is None or current_option.leg_ids != prior_path:
            change_penalty_total += bookings[bid].change_penalty

    objective = base_audit["objective"] - change_penalty_total
    theta = max(residuals.values(), default=0.0)

    result = dict(base_audit)
    result.update(
        {
            "ok": not errors,
            "errors": errors,
            "theta": theta,
            "violated_groups": sum(1 for value in residuals.values() if value > base.EPS),
            "residuals": dict(residuals),
            "objective": objective,
            "change_penalty_total": change_penalty_total,
        }
    )
    return result


def audit_plan_file(instance, booking_paths, path: Path):
    rows = base.read_plan_rows(path)
    audit = validate_plan(instance, booking_paths, rows)
    audit["rows"] = rows
    audit["path"] = str(path)
    return audit


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


def choose_warm_start(instance, booking_paths):
    if PLAN_PATH.exists():
        incumbent_audit = audit_plan_file(instance, booking_paths, PLAN_PATH)
        if incumbent_audit["ok"]:
            return incumbent_audit, "step2_incumbent"
    baseline_rows = build_baseline_rows(instance)
    baseline_audit = validate_plan(instance, booking_paths, baseline_rows)
    baseline_audit["rows"] = baseline_rows
    baseline_audit["path"] = "extended_initial_state"
    return baseline_audit, "extended_initial_state"


def write_log(
    instance,
    booking_paths,
    model_bundle,
    warm_start_source: str,
    warm_start_added: bool,
    rollback_audit,
    candidate_audit,
    final_audit,
    elapsed: float,
    negative_audit_ok: bool,
    published_candidate: bool,
) -> None:
    model = model_bundle["model"]
    n_feasible_paths = sum(len(paths) for paths in booking_paths.values())
    impossible = [bid for bid, paths in booking_paths.items() if not paths]
    status = str(model.getStatus())
    candidate_objective = model.getObjVal() if model.getNSols() > 0 else float("nan")
    best_bound = model.getDualbound() if model.getNSols() > 0 else float("nan")
    gap = model.getGap() if model.getNSols() > 0 else float("nan")
    improvement = candidate_audit["objective"] - rollback_audit["objective"]
    local_branching_attempted = False
    exact_proof_available = model.getNSols() > 0 and abs(gap) <= base.EPS

    rows = [
        "# Solve Log: Step 2 Replan",
        "",
        "## Repair Phase",
        "",
        f"- Rollback source: `{warm_start_source}`",
        f"- Rollback artifact: `{rollback_audit.get('path', PLAN_PATH)}`",
        f"- Rollback audited objective: `{rollback_audit['objective']:.8f}`",
        f"- Rollback audit ok: `{rollback_audit['ok']}`",
        f"- Rollback theta: `{rollback_audit['theta']:.8f}`",
        f"- Rollback violated groups: `{rollback_audit['violated_groups']}`",
        f"- Effective visible stage: `{instance['effective_stage_name']}`",
        f"- `current_step.json` label: `{instance['current_step_name']}`",
        f"- Stage-label mismatch noted: `{instance['stage_label_mismatch']}`",
        "",
        "## Performance Phase",
        "",
        f"- Strategy: `bounded exact MIP with rollback incumbent as MIP start`",
        f"- Warm start added: `{warm_start_added}`",
        f"- Local branching attempted: `{local_branching_attempted}`",
        f"- Status: `{status}`",
        f"- Time limit seconds: `{model_bundle['time_limit']}`",
        f"- Elapsed seconds: `{elapsed:.4f}`",
        f"- Incumbent count: `{model.getNSols()}`",
        f"- SCIP candidate objective: `{candidate_objective:.8f}`",
        f"- Dual bound: `{best_bound:.8f}`",
        f"- Relative gap: `{gap:.8f}`",
        f"- Exact proof available: `{exact_proof_available}`",
        "",
        "## Replan State",
        "",
        f"- Current day from file: `{instance['current_day']}`",
        f"- Freeze-until day from file: `{instance['freeze_until_day']}`",
        f"- Frozen accepted booking prefixes: `{len(instance['frozen_prefixes'])}`",
        f"- Protected-slot rules: `{len(instance['protected_slots'])}`",
        f"- Transfer-cap rules: `{len(instance['transfer_limits'])}`",
        "",
        "## Guarded Comparison",
        "",
        f"- Rollback audited objective: `{rollback_audit['objective']:.8f}`",
        f"- Candidate audited objective: `{candidate_audit['objective']:.8f}`",
        f"- Candidate audit ok: `{candidate_audit['ok']}`",
        f"- Candidate minus rollback: `{improvement:.8f}`",
        f"- Final published audited objective: `{final_audit['objective']:.8f}`",
        f"- Final artifact audit ok: `{final_audit['ok']}`",
        f"- Published candidate: `{published_candidate}`",
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
        f"- Replanning change penalty: `{final_audit['change_penalty_total']:.8f}`",
        "",
        "## Audit",
        "",
        "- The rollback incumbent was reparsed from disk and audited before optimization.",
        "- Candidate publication was gated on a fresh audit of the written candidate CSV.",
        "- Independent validation was rerun from the exported CSV, not from model variables.",
        f"- Corrupted-copy negative audit rejected artifact: `{negative_audit_ok}`",
        "- Stage-2 audit adds protected-slot floors, transfer caps, frozen commitments, and change-penalty recomputation on top of the accepted step-1 checker.",
        "- Gate handling assumption: accepted bookings consume gate-out at the origin first-loaded day; empty releases, empty arrivals, and leases consume gate-in.",
        "- Carbon assumption: `carbon_kg_per_teu` is monetized with unit coefficient `1.0` because no separate carbon-price table is provided.",
    ]

    with LOG_PATH.open("w") as handle:
        handle.write("\n".join(rows) + "\n")


def main():
    start = time.perf_counter()
    instance = load_instance()
    booking_paths = enumerate_paths(instance)

    for bid, prefix in instance["frozen_prefixes"].items():
        if not any(startswith_tuple(option.leg_ids, prefix) for option in booking_paths[bid]):
            raise RuntimeError(f"Frozen booking {bid} has no feasible current path that preserves prefix {prefix}.")

    for bid, booking in instance["bookings"].items():
        if booking.medical_flag and booking.container_type == "REEFER" and not booking_paths[bid]:
            raise RuntimeError(f"Mandatory medical booking {bid} has no feasible path.")

    if CANDIDATE_PLAN_PATH.exists():
        CANDIDATE_PLAN_PATH.unlink()

    rollback_audit, warm_start_source = choose_warm_start(instance, booking_paths)

    model_bundle = build_model(instance, booking_paths)
    warm_start_added = False
    if rollback_audit.get("parsed_solution") is not None:
        warm_start_added = base.apply_mip_start(instance, model_bundle, rollback_audit["parsed_solution"])

    model = model_bundle["model"]
    model.optimize()

    solution = base.extract_solution(instance, model_bundle)
    candidate_rows = base.build_plan_rows(instance, solution)
    base.write_plan(candidate_rows, CANDIDATE_PLAN_PATH)
    candidate_audit = audit_plan_file(instance, booking_paths, CANDIDATE_PLAN_PATH)
    if not candidate_audit["ok"]:
        raise RuntimeError("Candidate plan failed independent audit:\n" + "\n".join(candidate_audit["errors"][:20]))

    existing_step2_incumbent_ok = PLAN_PATH.exists()
    incumbent_objective = rollback_audit["objective"] if rollback_audit["ok"] and warm_start_source == "step2_incumbent" else float("-inf")
    if warm_start_source != "step2_incumbent":
        existing_step2_incumbent_ok = False

    published_candidate = candidate_audit["objective"] > incumbent_objective + base.EPS
    if published_candidate:
        CANDIDATE_PLAN_PATH.replace(PLAN_PATH)
    else:
        CANDIDATE_PLAN_PATH.unlink(missing_ok=True)
        if not existing_step2_incumbent_ok:
            base.write_plan(candidate_rows, PLAN_PATH)
            published_candidate = True

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
        warm_start_source,
        warm_start_added,
        rollback_audit,
        candidate_audit,
        final_audit,
        elapsed,
        negative_audit_ok,
        published_candidate,
    )


if __name__ == "__main__":
    main()
