#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import solve_initial as base
import solve_step2 as step2


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
PREV_PLAN_PATH = SUBMISSIONS_DIR / "step2_replan.csv"
PLAN_PATH = SUBMISSIONS_DIR / "step3_replan.csv"
CANDIDATE_PLAN_PATH = SUBMISSIONS_DIR / "step3_replan_candidate.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log_step3.md"
MODEL_PATH = SUBMISSIONS_DIR / "model_step3.md"

STEP3_DEFAULT_TIME_LIMIT = 295.0
base.DEFAULT_TIME_LIMIT = STEP3_DEFAULT_TIME_LIMIT


@dataclass(frozen=True)
class SpecialBookingRule:
    rule_id: str
    booking_id: str
    require_accept: bool
    max_legs: int | None
    description: str


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
        step2.ProtectedSlotRule(
            rule_id=row["rule_id"],
            leg_id=row["leg_id"],
            priority=row["priority"],
            min_teu=float(row["min_teu"]),
            description=row["description"],
        )
        for row in step2.read_optional_csv(DATA_DIR / "protected_slots.csv")
    ]
    instance["transfer_limits"] = [
        step2.TransferLimitRule(
            rule_id=row["rule_id"],
            port_id=row["port_id"],
            day=int(row["day"]),
            max_reefer_transfer_teu=float(row["max_reefer_transfer_teu"]),
            max_dg1_transfer_teu=float(row["max_dg1_transfer_teu"]),
            description=row["description"],
        )
        for row in step2.read_optional_csv(DATA_DIR / "transfer_limits.csv")
    ]

    special_rules: Dict[str, SpecialBookingRule] = {}
    for row in step2.read_optional_csv(DATA_DIR / "special_booking_rules.csv"):
        booking_id = row["booking_id"]
        rule = SpecialBookingRule(
            rule_id=row["rule_id"],
            booking_id=booking_id,
            require_accept=row["require_accept"] == "1",
            max_legs=int(row["max_legs"]) if row["max_legs"] else None,
            description=row["description"],
        )
        existing = special_rules.get(booking_id)
        if existing is None:
            special_rules[booking_id] = rule
            continue
        merged_max_legs = existing.max_legs
        if rule.max_legs is not None:
            merged_max_legs = rule.max_legs if merged_max_legs is None else min(merged_max_legs, rule.max_legs)
        special_rules[booking_id] = SpecialBookingRule(
            rule_id=f"{existing.rule_id}|{rule.rule_id}",
            booking_id=booking_id,
            require_accept=existing.require_accept or rule.require_accept,
            max_legs=merged_max_legs,
            description=f"{existing.description} {rule.description}".strip(),
        )
    instance["special_booking_rules"] = special_rules

    if special_rules:
        instance["effective_stage_name"] = "step3"
    elif instance["protected_slots"] or instance["transfer_limits"]:
        instance["effective_stage_name"] = "step2"
    else:
        instance["effective_stage_name"] = instance["current_step_name"]
    instance["stage_label_mismatch"] = instance["effective_stage_name"] != instance["current_step_name"]

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


def enumerate_paths(instance) -> Dict[str, List[base.PathOption]]:
    legs = instance["legs"]
    bookings = instance["bookings"]
    path_rules = instance["path_rules"]
    customs_windows = instance["customs_windows"]
    special_rules = instance["special_booking_rules"]

    legs_from = defaultdict(list)
    for leg in legs.values():
        legs_from[leg.from_port].append(leg)
    for leg_list in legs_from.values():
        leg_list.sort(key=lambda leg: (leg.depart_day, leg.arrive_day, leg.leg_id))

    booking_paths: Dict[str, List[base.PathOption]] = {}
    for booking_id, booking in bookings.items():
        rule = path_rules[booking_id]
        special_rule = special_rules.get(booking_id)
        max_legs = 1 if rule.requires_direct else rule.max_transship_count + 1
        if special_rule is not None and special_rule.max_legs is not None:
            max_legs = min(max_legs, special_rule.max_legs)
        allowed_services = set(rule.allowed_services)
        options: List[base.PathOption] = []

        if max_legs <= 0:
            booking_paths[booking_id] = options
            continue

        def build_option(path: List[base.Leg]) -> base.PathOption | None:
            customs_group = rule.customs_group
            customs_events: List[Tuple[str, int, str]] = [(booking.origin_port, path[0].depart_day, customs_group)]
            for next_leg in path[1:]:
                customs_events.append((next_leg.from_port, next_leg.depart_day, customs_group))
            customs_events.append((booking.destination_port, path[-1].arrive_day, customs_group))
            customs_cost = 0.0
            for event in customs_events:
                window = customs_windows.get(event)
                if window is None:
                    return None
                customs_cost += window[2] * booking.teu
            return base.PathOption(
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

        def dfs(port_id: str, min_depart_day: int, path: List[base.Leg], used_ports: set[str]) -> None:
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
                    dfs(leg.to_port, leg.arrive_day, new_path, used_ports | {leg.to_port})

        dfs(booking.origin_port, booking.earliest_load_day, [], {booking.origin_port})
        booking_paths[booking_id] = options
    return booking_paths


def build_model(instance, booking_paths):
    bundle = step2.build_model(instance, booking_paths)
    model = bundle["model"]
    for bid, rule in instance["special_booking_rules"].items():
        if rule.require_accept:
            model.addCons(bundle["reject_vars"][bid] == 0, name=f"special_accept[{bid}]")
    return bundle


def validate_plan(instance, booking_paths, rows: List[Dict[str, str]]):
    base_audit = step2.validate_plan(instance, booking_paths, rows)
    errors = list(base_audit["errors"])
    residuals = defaultdict(float, base_audit["residuals"])

    def register_violation(group: str, violation: float, scale: float = 1.0, message: str | None = None) -> None:
        if violation <= base.EPS:
            return
        residual = violation / max(1.0, abs(scale))
        residuals[group] = max(residuals[group], residual)
        if message:
            errors.append(message)

    decisions = base_audit["parsed_solution"]["booking_decisions"]
    selected_paths = {
        bid: option
        for bid, option in base_audit["parsed_solution"]["booking_paths"].items()
        if option is not None
    }

    for bid, rule in instance["special_booking_rules"].items():
        if rule.require_accept and decisions.get(bid) != "accept":
            register_violation(
                "special_booking",
                1.0,
                1.0,
                f"Special booking rule {rule.rule_id} requires accepting {bid}.",
            )
        option = selected_paths.get(bid)
        if option is not None and rule.max_legs is not None:
            register_violation(
                "special_booking",
                max(0.0, len(option.leg_ids) - rule.max_legs),
                rule.max_legs,
                f"Special booking rule {rule.rule_id} limits {bid} to {rule.max_legs} legs.",
            )

    result = dict(base_audit)
    result.update(
        {
            "ok": not errors,
            "errors": errors,
            "theta": max(residuals.values(), default=0.0),
            "violated_groups": sum(1 for value in residuals.values() if value > base.EPS),
            "residuals": dict(residuals),
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
            return incumbent_audit, "step3_incumbent"
    baseline_rows = build_baseline_rows(instance)
    baseline_audit = validate_plan(instance, booking_paths, baseline_rows)
    baseline_audit["rows"] = baseline_rows
    baseline_audit["path"] = "extended_step2_state"
    return baseline_audit, "extended_step2_state"


def summarize_leg_priority_usage(instance, parsed_solution):
    usage = defaultdict(float)
    for bid, option in parsed_solution["booking_paths"].items():
        if option is None:
            continue
        booking = instance["bookings"][bid]
        for leg_id in option.leg_ids:
            usage[(leg_id, booking.priority)] += booking.teu
    return usage


def audit_prior_commitment_changes(instance, parsed_solution):
    prior_paths = instance["prior_state"]["accepted_paths"]
    current_decisions = parsed_solution["booking_decisions"]
    current_paths = parsed_solution["booking_paths"]
    changes = []
    frozen_window_changes = []

    for bid, prior_path in sorted(prior_paths.items()):
        current_option = current_paths.get(bid)
        current_decision = current_decisions.get(bid, "reject")
        current_path = tuple(current_option.leg_ids) if current_option is not None else tuple()
        if current_decision == "accept" and current_path == prior_path:
            continue
        record = {
            "booking_id": bid,
            "prior_path": prior_path,
            "current_decision": current_decision,
            "current_path": current_path,
        }
        changes.append(record)
        prefix = instance["frozen_prefixes"].get(bid, tuple())
        if prefix:
            if current_decision != "accept" or not step2.startswith_tuple(current_path, prefix):
                frozen_window_changes.append(record)

    return {
        "all_changes": changes,
        "frozen_window_changes": frozen_window_changes,
    }


def write_model_summary(instance, booking_paths, final_audit) -> None:
    parsed_solution = final_audit["parsed_solution"]
    decisions = parsed_solution["booking_decisions"]
    usage = summarize_leg_priority_usage(instance, parsed_solution)
    prior_change_audit = audit_prior_commitment_changes(instance, parsed_solution)
    rejected = [bid for bid in instance["booking_order"] if decisions.get(bid) == "reject"]
    mandatory_special = sorted(
        bid for bid, rule in instance["special_booking_rules"].items() if rule.require_accept
    )
    impossible = [bid for bid, paths in booking_paths.items() if not paths]
    n_paths = sum(len(paths) for paths in booking_paths.values())

    rows = [
        "# Step 3 Final Replan Model",
        "",
        "This stage-3 model replans from the approved `step2_replan.csv` using the final visible booking packet, the final event notice, and all current tables under `/app/data`. It preserves every frozen commitment implied by `freeze_until_day`, enforces the updated protected-slot, transfer-cap, and special-booking tables as hard constraints, and charges change penalties against prior accepted step-2 commitments whose decision or full path changes outside the frozen window.",
        "",
        "## Formulation",
        "",
        "Sets and indices:",
        "",
        "- `b in B`: all currently visible bookings.",
        "- `p in P_b`: enumerated time-feasible loaded-leg paths for booking `b` under booking-path and special-booking limits.",
        "- `l in L`: vessel legs.",
        "- `n in N`: ports.",
        "- `d in D`: days in the finite horizon.",
        "- `c in {STD, REEFER}`: empty-container types.",
        "",
        "Decision variables:",
        "",
        "- `x[b,p] in {0,1}`: booking `b` is accepted on path `p`.",
        "- `r[b] in {0,1}`: booking `b` is rejected.",
        "- `m[l,c] in Z_+`: empty repositioning TEU on leg `l`.",
        "- `u[n,d,c] in Z_+`: leased empties at port `n`, day `d`.",
        "- `s[n,d,c] >= 0`: end-of-day empty stock.",
        "- `z[b] in {0,1}` for prior accepted bookings: 1 if the stage-3 plan differs from the approved step-2 path for booking `b`.",
        "",
        "Path generation:",
        "",
        "- Paths use only public vessel legs and must satisfy booking time windows.",
        "- Transshipment count is `number_of_loaded_legs - 1`.",
        "- Paths respect `max_transship_count`, `requires_direct`, allowed services, and any `special_booking_rules.csv` maximum-leg override.",
        "- Customs events are counted at export, each intermediate transfer departure port, and import.",
        "",
        "Objective:",
        "",
        "Maximize accepted booking revenue minus rejection penalties, empty reposition and carbon costs, lease cost, customs inspection cost, empty holding cost, and step-3 change penalties against the accepted step-2 plan.",
        "",
        "Hard constraints:",
        "",
        "- Exactly one accept/reject decision per visible booking.",
        "- Mandatory acceptance for visible medical reefer bookings and any `special_booking_rules.csv` `require_accept=1` booking.",
        "- Priority-group minimum acceptance floors from `service_rules.csv`.",
        "- Vessel TEU, reefer-plug, and DG1 segregation limits.",
        "- Empty inventory balance, nonnegativity, yard capacity, reefer-yard capacity, and gate limits.",
        "- Lease upper bounds.",
        "- Customs capacity and medical cold-chain sublimits.",
        "- Protected-slot floors from `protected_slots.csv`.",
        "- Reefer and DG1 transfer caps from `transfer_limits.csv`.",
        "- Frozen prefixes, empty moves, and leases inherited from the approved step-2 plan inside the public frozen window.",
        "",
        "## Stage-3 Notes",
        "",
        f"- `current_step.json` still reports `current_step={instance['current_step_name']}` and `freeze_until_day={instance['freeze_until_day']}`, but the visible data clearly represent `{instance['effective_stage_name']}`; the solver keeps the file's numeric frozen window as the hard boundary and treats the visible step-3 tables as active rules.",
        f"- Active special-booking rules: `{len(instance['special_booking_rules'])}`. Mandatory special accepts: `{', '.join(mandatory_special) if mandatory_special else 'none'}`.",
        f"- Frozen accepted booking prefixes implied by the public window: `{len(instance['frozen_prefixes'])}`.",
        "",
        "## Solved Outcome",
        "",
        f"The final audited artifact `step3_replan.csv` is feasible with zero independent hard-constraint residual and audited objective `{final_audit['objective']:.8f}`.",
        "",
        f"- Accepted bookings: `{sum(1 for bid in instance['booking_order'] if decisions.get(bid) == 'accept')}`",
        f"- Rejected bookings: `{', '.join(rejected) if rejected else 'none'}`",
        f"- Enumerated feasible booking paths: `{n_paths}`",
        f"- Bookings with no feasible path: `{', '.join(impossible) if impossible else 'none'}`",
        f"- Prior accepted step-2 commitments changed: `{len(prior_change_audit['all_changes'])}`",
        f"- Frozen-window violations among those changes under public `freeze_until_day={instance['freeze_until_day']}`: `{len(prior_change_audit['frozen_window_changes'])}`",
    ]

    for rule in instance["protected_slots"]:
        rows.append(
            f"- Protected-slot usage `{rule.leg_id}/{rule.priority}`: `{base.fmt_qty(usage[(rule.leg_id, rule.priority)])}`"
        )

    with MODEL_PATH.open("w") as handle:
        handle.write("\n".join(rows) + "\n")


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
    mandatory_medical = sorted(
        bid
        for bid, booking in instance["bookings"].items()
        if booking.medical_flag and booking.container_type == "REEFER"
    )
    rollback_changes = audit_prior_commitment_changes(instance, rollback_audit["parsed_solution"])
    candidate_changes = audit_prior_commitment_changes(instance, candidate_audit["parsed_solution"])
    final_changes = audit_prior_commitment_changes(instance, final_audit["parsed_solution"])
    rollback_changed_ids = ", ".join(change["booking_id"] for change in rollback_changes["all_changes"]) or "none"
    final_changed_ids = ", ".join(change["booking_id"] for change in final_changes["all_changes"]) or "none"

    rows = [
        "# Solve Log: Step 3 Final Replan",
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
        f"- Rollback prior-commitment changes vs step 2: `{len(rollback_changes['all_changes'])}` ({rollback_changed_ids})",
        f"- Rollback frozen-window violations under public `freeze_until_day={instance['freeze_until_day']}`: `{len(rollback_changes['frozen_window_changes'])}`",
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
        f"- Special-booking rules: `{len(instance['special_booking_rules'])}`",
        "",
        "## Guarded Comparison",
        "",
        f"- Rollback audited objective: `{rollback_audit['objective']:.8f}`",
        f"- Candidate audited objective: `{candidate_audit['objective']:.8f}`",
        f"- Candidate audit ok: `{candidate_audit['ok']}`",
        f"- Candidate minus rollback: `{improvement:.8f}`",
        f"- Candidate prior-commitment changes vs step 2: `{len(candidate_changes['all_changes'])}`",
        f"- Candidate frozen-window violations under public `freeze_until_day={instance['freeze_until_day']}`: `{len(candidate_changes['frozen_window_changes'])}`",
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
        f"- Mandatory medical reefer bookings: `{', '.join(mandatory_medical)}`",
        f"- Final prior-commitment changes vs step 2: `{len(final_changes['all_changes'])}` ({final_changed_ids})",
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
        "- Stage-3 audit adds protected-slot floors, transfer caps, frozen commitments, special-booking mandates, and change-penalty recomputation on top of the accepted step-1 checker.",
        f"- Legacy step-2 commitment edits remain publicly identifiable, but with `freeze_until_day={instance['freeze_until_day']}` the public frozen-prefix audit reports `{len(final_changes['frozen_window_changes'])}` violations.",
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
        if not any(step2.startswith_tuple(option.leg_ids, prefix) for option in booking_paths[bid]):
            raise RuntimeError(f"Frozen booking {bid} has no feasible current path that preserves prefix {prefix}.")

    for bid, booking in instance["bookings"].items():
        if booking.medical_flag and booking.container_type == "REEFER" and not booking_paths[bid]:
            raise RuntimeError(f"Mandatory medical booking {bid} has no feasible path.")

    for bid, rule in instance["special_booking_rules"].items():
        if rule.require_accept and not booking_paths[bid]:
            raise RuntimeError(f"Special booking {bid} requires acceptance but has no feasible path.")

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

    existing_step3_incumbent_ok = PLAN_PATH.exists()
    incumbent_objective = rollback_audit["objective"] if rollback_audit["ok"] and warm_start_source == "step3_incumbent" else float("-inf")
    if warm_start_source != "step3_incumbent":
        existing_step3_incumbent_ok = False

    published_candidate = candidate_audit["objective"] > incumbent_objective + base.EPS
    if published_candidate:
        CANDIDATE_PLAN_PATH.replace(PLAN_PATH)
    else:
        CANDIDATE_PLAN_PATH.unlink(missing_ok=True)
        if not existing_step3_incumbent_ok:
            base.write_plan(candidate_rows, PLAN_PATH)
            published_candidate = True

    final_audit = audit_plan_file(instance, booking_paths, PLAN_PATH)
    if not final_audit["ok"]:
        raise RuntimeError("Final published plan failed independent audit:\n" + "\n".join(final_audit["errors"][:20]))

    negative_audit_ok = run_negative_audit(instance, booking_paths, final_audit["rows"])
    if not negative_audit_ok:
        raise RuntimeError("Negative audit did not reject a corrupted artifact.")

    elapsed = time.perf_counter() - start
    write_model_summary(instance, booking_paths, final_audit)
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
