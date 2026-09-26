from __future__ import annotations

import csv
import json
import math
import os
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"
MODEL_PATH = SUBMISSIONS_DIR / "model.md"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
ROLLBACK_PATH = SUBMISSIONS_DIR / "solution_rollback.csv"
CANDIDATE_PATH = SUBMISSIONS_DIR / "solution_candidate.csv"
START_SOL_PATH = SUBMISSIONS_DIR / "incumbent_start.sol"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"
SCRIPT_PATH = SUBMISSIONS_DIR / "solve.py"

REL_GAP = 0.0005
MAX_ATTEMPT_SECONDS = 295.0
INT_TOL = 1e-6
AUDIT_TOL = 1e-8


@dataclass(frozen=True)
class Data:
    config: dict
    slots: List[str]
    slot_index: Dict[str, int]
    classes: List[str]
    blocks: List[str]
    scenarios: List[str]
    probabilities: Dict[str, float]
    gate_multiplier: Dict[str, float]
    base_gate_capacity: Dict[str, int]
    gate_capacity: Dict[Tuple[str, str], int]
    queue_limit: Dict[str, int]
    reefer_limit: Dict[str, int]
    exposure_cap: Dict[str, float]
    min_quota: Dict[str, int]
    allowed_slots: Dict[str, set]
    latest_slot_idx: Dict[str, int]
    waiting_cost: Dict[str, float]
    shortage_penalty: Dict[str, float]
    exposure_units: Dict[str, float]
    reefer_flag: Dict[str, int]
    block_base_capacity: Dict[str, float]
    block_max_teams: Dict[str, int]
    productivity: Dict[Tuple[str, str, str], float]
    demand: Dict[Tuple[str, str, str], int]
    demand_max_by_ct: Dict[Tuple[str, str], int]
    eligible_pairs: List[Tuple[str, str]]
    eligible_blocks: Dict[str, List[str]]
    work_units: Dict[Tuple[str, str], float]
    handling_cost: Dict[Tuple[str, str], float]
    slot_mix_rules: Dict[str, List[Tuple[str, Tuple[str, ...], int]]]
    team_pools: Dict[str, List[Tuple[str, Tuple[str, ...], int]]]
    commitments: List[Tuple[str, str, str, int]]
    quota_admin_cost: float
    extra_team_cost: float
    team_work_unit_gain: float
    minimum_total_quota: int
    maximum_total_quota: int
    max_extra_teams_per_slot: int
    quota_ramp_limit: int
    tail_risk_alpha: float
    tail_risk_weight: float


@dataclass
class SolveResult:
    mode: str
    status: str
    elapsed_seconds: float
    objective: Optional[float]
    dual_bound: Optional[float]
    gap: Optional[float]
    solution_count: int
    exported: bool
    notes: List[str]


def load_csv(path: Path) -> List[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_data() -> Data:
    config = json.loads((DATA_DIR / "config.json").read_text())

    time_rows = load_csv(DATA_DIR / "time_slots.csv")
    class_rows = load_csv(DATA_DIR / "truck_classes.csv")
    block_rows = load_csv(DATA_DIR / "yard_blocks.csv")
    eligibility_rows = load_csv(DATA_DIR / "class_block_eligibility.csv")
    scenario_rows = load_csv(DATA_DIR / "scenarios.csv")
    demand_rows = load_csv(DATA_DIR / "scenario_demand.csv")
    capacity_rows = load_csv(DATA_DIR / "scenario_block_capacity.csv")
    mix_rows = load_csv(DATA_DIR / "slot_class_mix_limits.csv")
    pool_rows = load_csv(DATA_DIR / "team_pool_limits.csv")
    commitment_rows = load_csv(DATA_DIR / "scenario_service_commitments.csv")

    slots = [r["slot_id"] for r in sorted(time_rows, key=lambda r: int(r["slot_index"]))]
    slot_index = {r["slot_id"]: int(r["slot_index"]) for r in time_rows}
    classes = [r["class_id"] for r in class_rows]
    blocks = [r["block_id"] for r in block_rows]
    scenarios = [r["scenario_id"] for r in scenario_rows]

    probabilities = {r["scenario_id"]: float(r["probability"]) for r in scenario_rows}
    gate_multiplier = {r["scenario_id"]: float(r["gate_multiplier"]) for r in scenario_rows}

    base_gate_capacity = {r["slot_id"]: int(r["base_gate_capacity"]) for r in time_rows}
    gate_capacity = {}
    queue_limit = {r["slot_id"]: int(r["queue_limit_trucks"]) for r in time_rows}
    reefer_limit = {r["slot_id"]: int(r["reefer_inspection_limit"]) for r in time_rows}
    exposure_cap = {r["slot_id"]: float(r["drayage_exposure_cap"]) for r in time_rows}
    min_quota = {r["slot_id"]: int(r["min_quota"]) for r in time_rows}
    for s in scenarios:
        for t in slots:
            gate_capacity[(s, t)] = int(math.floor(base_gate_capacity[t] * gate_multiplier[s] + 1e-9))

    allowed_slots = {r["class_id"]: set(r["allowed_slots"].split("|")) for r in class_rows}
    latest_slot_idx = {r["class_id"]: int(r["latest_service_slot"]) for r in class_rows}
    waiting_cost = {r["class_id"]: float(r["waiting_cost_per_slot"]) for r in class_rows}
    shortage_penalty = {r["class_id"]: float(r["shortage_penalty"]) for r in class_rows}
    exposure_units = {r["class_id"]: float(r["exposure_units"]) for r in class_rows}
    reefer_flag = {r["class_id"]: int(r["reefer_flag"]) for r in class_rows}

    block_base_capacity = {r["block_id"]: float(r["base_work_units"]) for r in block_rows}
    block_max_teams = {r["block_id"]: int(r["max_extra_teams"]) for r in block_rows}
    productivity = {
        (r["scenario_id"], r["slot_id"], r["block_id"]): float(r["capacity_multiplier"])
        for r in capacity_rows
    }

    demand = {
        (r["scenario_id"], r["slot_id"], r["class_id"]): int(r["requested_trucks"])
        for r in demand_rows
    }
    demand_max_by_ct: Dict[Tuple[str, str], int] = defaultdict(int)
    for (s, t, c), value in demand.items():
        demand_max_by_ct[(c, t)] = max(demand_max_by_ct[(c, t)], value)

    eligible_pairs = [(r["class_id"], r["block_id"]) for r in eligibility_rows]
    eligible_blocks: Dict[str, List[str]] = defaultdict(list)
    work_units = {}
    handling_cost = {}
    for r in eligibility_rows:
        c = r["class_id"]
        b = r["block_id"]
        eligible_blocks[c].append(b)
        work_units[(c, b)] = float(r["work_units"])
        handling_cost[(c, b)] = float(r["handling_cost"])

    slot_mix_rules: Dict[str, List[Tuple[str, Tuple[str, ...], int]]] = defaultdict(list)
    for r in mix_rows:
        slot_mix_rules[r["slot_id"]].append(
            (r["rule_id"], tuple(r["classes"].split("|")), int(r["max_admissions"]))
        )

    team_pools: Dict[str, List[Tuple[str, Tuple[str, ...], int]]] = defaultdict(list)
    for r in pool_rows:
        team_pools[r["slot_id"]].append(
            (r["pool_id"], tuple(r["blocks"].split("|")), int(r["max_extra_teams"]))
        )

    commitments = [
        (r["scenario_id"], r["class_id"], r["by_slot_id"], int(r["min_served_trucks"]))
        for r in commitment_rows
    ]

    return Data(
        config=config,
        slots=slots,
        slot_index=slot_index,
        classes=classes,
        blocks=blocks,
        scenarios=scenarios,
        probabilities=probabilities,
        gate_multiplier=gate_multiplier,
        base_gate_capacity=base_gate_capacity,
        gate_capacity=gate_capacity,
        queue_limit=queue_limit,
        reefer_limit=reefer_limit,
        exposure_cap=exposure_cap,
        min_quota=min_quota,
        allowed_slots=allowed_slots,
        latest_slot_idx=latest_slot_idx,
        waiting_cost=waiting_cost,
        shortage_penalty=shortage_penalty,
        exposure_units=exposure_units,
        reefer_flag=reefer_flag,
        block_base_capacity=block_base_capacity,
        block_max_teams=block_max_teams,
        productivity=productivity,
        demand=demand,
        demand_max_by_ct=dict(demand_max_by_ct),
        eligible_pairs=eligible_pairs,
        eligible_blocks={c: sorted(bs) for c, bs in eligible_blocks.items()},
        work_units=work_units,
        handling_cost=handling_cost,
        slot_mix_rules=dict(slot_mix_rules),
        team_pools=dict(team_pools),
        commitments=commitments,
        quota_admin_cost=float(config["quota_admin_cost"]),
        extra_team_cost=float(config["extra_team_cost"]),
        team_work_unit_gain=float(config["team_work_unit_gain"]),
        minimum_total_quota=int(config["minimum_total_quota"]),
        maximum_total_quota=int(config["maximum_total_quota"]),
        max_extra_teams_per_slot=int(config["max_extra_teams_per_slot"]),
        quota_ramp_limit=int(config["quota_ramp_limit"]),
        tail_risk_alpha=float(config["tail_risk_alpha"]),
        tail_risk_weight=float(config["tail_risk_weight"]),
    )


def limit_from_env() -> float:
    raw = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if raw is None:
        return MAX_ATTEMPT_SECONDS
    try:
        return min(MAX_ATTEMPT_SECONDS, max(1.0, float(raw)))
    except ValueError:
        return MAX_ATTEMPT_SECONDS


def clean_int(value: float) -> int:
    rounded = int(round(value))
    if abs(value - rounded) > 1e-5:
        raise ValueError(f"Non-integer value encountered: {value}")
    if rounded < 0 and abs(rounded) <= 1:
        rounded = 0
    if rounded < 0:
        raise ValueError(f"Negative value encountered: {value}")
    return rounded


def safe_stat(getter) -> Optional[float]:
    try:
        return float(getter())
    except Exception:
        return None


class ModelBuilder:
    def __init__(self, data: Data, elastic: bool):
        self.data = data
        self.elastic = elastic
        self.model = Model("container_gate_dro")
        self.model.setParam("display/verblevel", 0)
        self.model.setParam("limits/gap", REL_GAP)
        self.total_norm = None
        self.theta = None
        self.slacks: List[Tuple[object, float]] = []
        if elastic:
            self.theta = self.model.addVar(lb=0.0, vtype="C", name="theta")
            self.total_norm = []

    def _register_slack(self, slack, scale: float, label: str) -> None:
        if self.total_norm is not None:
            self.total_norm.append(slack / scale)
        self.slacks.append((slack, scale))
        self.model.addCons(self.theta >= slack / scale, name=f"theta_link[{label}]")

    def add_le(self, expr, rhs: float, name: str, scale: float) -> None:
        if not self.elastic:
            self.model.addCons(expr <= rhs, name=name)
            return
        slack = self.model.addVar(lb=0.0, vtype="C", name=f"slack_pos[{name}]")
        self.model.addCons(expr <= rhs + slack, name=name)
        self._register_slack(slack, max(1.0, scale), name)

    def add_ge(self, expr, rhs: float, name: str, scale: float) -> None:
        if not self.elastic:
            self.model.addCons(expr >= rhs, name=name)
            return
        slack = self.model.addVar(lb=0.0, vtype="C", name=f"slack_neg[{name}]")
        self.model.addCons(expr + slack >= rhs, name=name)
        self._register_slack(slack, max(1.0, scale), name)

    def add_eq(self, expr, rhs: float, name: str, scale: float) -> None:
        if not self.elastic:
            self.model.addCons(expr == rhs, name=name)
            return
        slack_pos = self.model.addVar(lb=0.0, vtype="C", name=f"slack_pos[{name}]")
        slack_neg = self.model.addVar(lb=0.0, vtype="C", name=f"slack_neg[{name}]")
        self.model.addCons(expr <= rhs + slack_pos, name=f"{name}_ub")
        self.model.addCons(expr + slack_neg >= rhs, name=f"{name}_lb")
        self._register_slack(slack_pos, max(1.0, scale), f"{name}_pos")
        self._register_slack(slack_neg, max(1.0, scale), f"{name}_neg")


def build_model(
    data: Data,
    elastic: bool = False,
    candidate: Optional[dict] = None,
    local_branch_radius: Optional[int] = None,
) -> Tuple[Model, dict, dict]:
    builder = ModelBuilder(data, elastic=elastic)
    model = builder.model

    q = {}
    for c in data.classes:
        for t in data.slots:
            if t not in data.allowed_slots[c]:
                continue
            ub = min(data.maximum_total_quota, data.demand_max_by_ct.get((c, t), 0))
            q[(c, t)] = model.addVar(vtype="INTEGER", lb=0, ub=ub, name=f"quota[{c},{t}]")

    y = {
        (b, t): model.addVar(
            vtype="INTEGER",
            lb=0,
            ub=data.block_max_teams[b],
            name=f"team[{b},{t}]",
        )
        for b in data.blocks
        for t in data.slots
    }

    a = {}
    for s in data.scenarios:
        for c in data.classes:
            for t in data.slots:
                ub = data.demand[(s, t, c)] if t in data.allowed_slots[c] else 0
                a[(s, c, t)] = model.addVar(
                    vtype="INTEGER",
                    lb=0,
                    ub=ub,
                    name=f"admit[{s},{c},{t}]",
                )

    z = {}
    service_slots_by_class = {
        c: [t for t in data.slots if data.slot_index[t] <= data.latest_slot_idx[c]]
        for c in data.classes
    }
    total_demand_by_sc = defaultdict(int)
    for (s, t, c), value in data.demand.items():
        total_demand_by_sc[(s, c)] += value
    for s in data.scenarios:
        for c, b in data.eligible_pairs:
            ub = total_demand_by_sc[(s, c)]
            for t in service_slots_by_class[c]:
                z[(s, c, b, t)] = model.addVar(
                    vtype="INTEGER",
                    lb=0,
                    ub=ub,
                    name=f"service[{s},{c},{b},{t}]",
                )

    slot_total_quota_expr = {}
    for t in data.slots:
        slot_total_quota_expr[t] = quicksum(q[c, t] for c in data.classes if (c, t) in q)
        builder.add_ge(
            slot_total_quota_expr[t],
            data.min_quota[t],
            f"slot_min_quota[{t}]",
            data.min_quota[t],
        )

    day_quota = quicksum(var for var in q.values())
    builder.add_ge(day_quota, data.minimum_total_quota, "day_quota_lb", data.minimum_total_quota)
    builder.add_le(day_quota, data.maximum_total_quota, "day_quota_ub", data.maximum_total_quota)

    for idx in range(1, len(data.slots)):
        prev_t = data.slots[idx - 1]
        t = data.slots[idx]
        builder.add_le(
            slot_total_quota_expr[t] - slot_total_quota_expr[prev_t],
            data.quota_ramp_limit,
            f"quota_ramp_up[{prev_t},{t}]",
            data.quota_ramp_limit,
        )
        builder.add_le(
            slot_total_quota_expr[prev_t] - slot_total_quota_expr[t],
            data.quota_ramp_limit,
            f"quota_ramp_down[{prev_t},{t}]",
            data.quota_ramp_limit,
        )

    for b in data.blocks:
        for t in data.slots:
            builder.add_le(
                y[(b, t)],
                data.block_max_teams[b],
                f"team_block_cap[{b},{t}]",
                data.block_max_teams[b],
            )
    for t in data.slots:
        builder.add_le(
            quicksum(y[(b, t)] for b in data.blocks),
            data.max_extra_teams_per_slot,
            f"team_slot_cap[{t}]",
            data.max_extra_teams_per_slot,
        )
        for pool_id, blocks_in_pool, pool_cap in data.team_pools.get(t, []):
            builder.add_le(
                quicksum(y[(b, t)] for b in blocks_in_pool),
                pool_cap,
                f"team_pool_cap[{pool_id},{t}]",
                pool_cap,
            )

    for s in data.scenarios:
        for c in data.classes:
            for t in data.slots:
                quota_var = q.get((c, t))
                if quota_var is None:
                    builder.add_eq(a[(s, c, t)], 0.0, f"invalid_admit[{s},{c},{t}]", 1.0)
                else:
                    builder.add_le(
                        a[(s, c, t)],
                        quota_var,
                        f"quota_link[{s},{c},{t}]",
                        max(1.0, data.demand[(s, t, c)]),
                    )
                builder.add_le(
                    a[(s, c, t)],
                    data.demand[(s, t, c)],
                    f"demand_link[{s},{c},{t}]",
                    max(1.0, data.demand[(s, t, c)]),
                )

        for t in data.slots:
            builder.add_le(
                quicksum(a[(s, c, t)] for c in data.classes),
                data.gate_capacity[(s, t)],
                f"gate_cap[{s},{t}]",
                data.gate_capacity[(s, t)],
            )
            builder.add_le(
                quicksum(data.reefer_flag[c] * a[(s, c, t)] for c in data.classes),
                data.reefer_limit[t],
                f"reefer_cap[{s},{t}]",
                data.reefer_limit[t],
            )
            builder.add_le(
                quicksum(data.exposure_units[c] * a[(s, c, t)] for c in data.classes),
                data.exposure_cap[t],
                f"exposure_cap[{s},{t}]",
                data.exposure_cap[t],
            )
            for rule_id, classes_in_rule, max_admissions in data.slot_mix_rules.get(t, []):
                builder.add_le(
                    quicksum(a[(s, c, t)] for c in classes_in_rule),
                    max_admissions,
                    f"mix_cap[{s},{t},{rule_id}]",
                    max_admissions,
                )

        for c in data.classes:
            total_admit = quicksum(a[(s, c, t)] for t in data.slots)
            cum_admit = 0
            cum_service = 0
            for t in data.slots:
                cum_admit = cum_admit + a[(s, c, t)]
                service_now = quicksum(
                    z[(s, c, b, t)] for b in data.eligible_blocks.get(c, []) if (s, c, b, t) in z
                )
                cum_service = cum_service + service_now
                builder.add_le(
                    cum_service,
                    cum_admit,
                    f"prefix_service[{s},{c},{t}]",
                    max(1.0, total_demand_by_sc[(s, c)]),
                )
                builder.add_le(
                    cum_admit - cum_service,
                    data.queue_limit[t],
                    f"queue_limit[{s},{c},{t}]",
                    data.queue_limit[t],
                )
            builder.add_eq(
                cum_service,
                total_admit,
                f"full_service[{s},{c}]",
                max(1.0, total_demand_by_sc[(s, c)]),
            )

        for scenario_id, c, by_slot, min_served in data.commitments:
            if scenario_id != s:
                continue
            expr = quicksum(
                z[(s, c, b, t)]
                for b in data.eligible_blocks.get(c, [])
                for t in service_slots_by_class[c]
                if data.slot_index[t] <= data.slot_index[by_slot]
            )
            builder.add_ge(
                expr,
                min_served,
                f"commitment[{s},{c},{by_slot}]",
                min_served,
            )

        for b in data.blocks:
            for t in data.slots:
                lhs = quicksum(
                    data.work_units[(c, b)] * z[(s, c, b, t)]
                    for c in data.classes
                    if (c, b) in data.work_units and (s, c, b, t) in z
                )
                rhs = (
                    data.block_base_capacity[b] * data.productivity[(s, t, b)]
                    + data.team_work_unit_gain * y[(b, t)]
                )
                nominal = data.block_base_capacity[b] * data.productivity[(s, t, b)] + (
                    data.team_work_unit_gain * data.block_max_teams[b]
                )
                builder.add_le(lhs, rhs, f"block_capacity[{s},{b},{t}]", nominal)

    scenario_cost = {}
    for s in data.scenarios:
        handling = quicksum(
            data.handling_cost[(c, b)] * z[(s, c, b, t)]
            for (ss, c, b, t) in z
            if ss == s
        )
        shortage = quicksum(
            data.shortage_penalty[c] * (data.demand[(s, t, c)] - a[(s, c, t)])
            for c in data.classes
            for t in data.slots
        )
        waiting_terms = []
        for c in data.classes:
            cum_admit = 0
            cum_service = 0
            for t in data.slots:
                cum_admit = cum_admit + a[(s, c, t)]
                service_now = quicksum(
                    z[(s, c, b, t)] for b in data.eligible_blocks.get(c, []) if (s, c, b, t) in z
                )
                cum_service = cum_service + service_now
                waiting_terms.append(data.waiting_cost[c] * (cum_admit - cum_service))
        scenario_cost[s] = handling + shortage + quicksum(waiting_terms)

    vars_dict = {"q": q, "y": y, "a": a, "z": z}

    if local_branch_radius is not None and candidate is not None:
        distance_terms = []
        for key, var in q.items():
            target = candidate.get("q", {}).get(key, 0)
            up = model.addVar(lb=0.0, vtype="C", name=f"lb_up[q,{key}]")
            dn = model.addVar(lb=0.0, vtype="C", name=f"lb_dn[q,{key}]")
            model.addCons(var - target == up - dn, name=f"lb_link_q[{key}]")
            distance_terms.extend([up, dn])
        for key, var in y.items():
            target = candidate.get("y", {}).get(key, 0)
            up = model.addVar(lb=0.0, vtype="C", name=f"lb_up[y,{key}]")
            dn = model.addVar(lb=0.0, vtype="C", name=f"lb_dn[y,{key}]")
            model.addCons(var - target == up - dn, name=f"lb_link_y[{key}]")
            distance_terms.extend([up, dn])
        model.addCons(
            quicksum(distance_terms) <= float(local_branch_radius),
            name=f"local_branch_first_stage[{local_branch_radius}]",
        )

    if not elastic:
        eta = model.addVar(lb=-model.infinity(), vtype="C", name="eta")
        xi = {s: model.addVar(lb=0.0, vtype="C", name=f"xi[{s}]") for s in data.scenarios}
        for s in data.scenarios:
            model.addCons(xi[s] >= scenario_cost[s] - eta, name=f"cvar_excess[{s}]")
        expected_cost = quicksum(data.probabilities[s] * scenario_cost[s] for s in data.scenarios)
        cvar = eta + (
            quicksum(data.probabilities[s] * xi[s] for s in data.scenarios)
            / max(1e-9, 1.0 - data.tail_risk_alpha)
        )
        fixed_cost = (
            data.quota_admin_cost * quicksum(var for var in q.values())
            + data.extra_team_cost * quicksum(var for var in y.values())
        )
        model.setObjective(fixed_cost + expected_cost + data.tail_risk_weight * cvar, "minimize")
        aux = {"scenario_cost": scenario_cost, "eta": eta, "xi": xi}
    else:
        norm_sum = quicksum(builder.total_norm) if builder.total_norm else 0.0
        if candidate is None:
            distance = (
                quicksum(var for var in q.values())
                + quicksum(var for var in y.values())
                + quicksum(var for var in a.values())
                + quicksum(var for var in z.values())
            )
        else:
            distance_terms = []
            for key, var in q.items():
                target = candidate.get("q", {}).get(key, 0)
                up = model.addVar(lb=0.0, vtype="C", name=f"dist_up[q,{key}]")
                dn = model.addVar(lb=0.0, vtype="C", name=f"dist_dn[q,{key}]")
                model.addCons(var - target == up - dn, name=f"dist_link_q[{key}]")
                distance_terms.extend([up, dn])
            for key, var in y.items():
                target = candidate.get("y", {}).get(key, 0)
                up = model.addVar(lb=0.0, vtype="C", name=f"dist_up[y,{key}]")
                dn = model.addVar(lb=0.0, vtype="C", name=f"dist_dn[y,{key}]")
                model.addCons(var - target == up - dn, name=f"dist_link_y[{key}]")
                distance_terms.extend([up, dn])
            distance = quicksum(distance_terms) if distance_terms else 0.0
        model.setObjective(1_000_000.0 * builder.theta + 1_000.0 * norm_sum + distance, "minimize")
        aux = {"theta": builder.theta, "norm_sum": norm_sum, "scenario_cost": scenario_cost}

    return model, vars_dict, aux


def extract_solution(model: Model, vars_dict: dict) -> dict:
    if model.getNSols() <= 0 or model.getBestSol() is None:
        raise RuntimeError("No incumbent solution available for export.")
    sol = model.getBestSol()

    extracted = {
        "q": {k: clean_int(model.getSolVal(sol, v)) for k, v in vars_dict["q"].items()},
        "y": {k: clean_int(model.getSolVal(sol, v)) for k, v in vars_dict["y"].items()},
        "a": {k: clean_int(model.getSolVal(sol, v)) for k, v in vars_dict["a"].items()},
        "z": {k: clean_int(model.getSolVal(sol, v)) for k, v in vars_dict["z"].items()},
    }
    return extracted


def load_solution_dict(solution_path: Path) -> dict:
    extracted = {"q": {}, "y": {}, "a": {}, "z": {}}
    if not solution_path.exists():
        return extracted
    for row in load_csv(solution_path):
        value = clean_int(float(row["value"]))
        if value == 0:
            continue
        row_type = row["row_type"]
        if row_type == "quota":
            extracted["q"][(row["class_id"], row["slot_id"])] = value
        elif row_type == "team":
            extracted["y"][(row["block_id"], row["slot_id"])] = value
        elif row_type == "admit":
            extracted["a"][(row["scenario_id"], row["class_id"], row["slot_id"])] = value
        elif row_type == "service":
            extracted["z"][(row["scenario_id"], row["class_id"], row["block_id"], row["slot_id"])] = value
    return extracted


def load_mip_start_from_file(model: Model, vars_dict: dict, candidate: dict, path: Path) -> bool:
    lines = ["objective value:                                    0"]
    for key, var in vars_dict["q"].items():
        value = candidate.get("q", {}).get(key, 0)
        if value:
            lines.append(f"{var.name:<50} {value}")
    for key, var in vars_dict["y"].items():
        value = candidate.get("y", {}).get(key, 0)
        if value:
            lines.append(f"{var.name:<50} {value}")
    for key, var in vars_dict["a"].items():
        value = candidate.get("a", {}).get(key, 0)
        if value:
            lines.append(f"{var.name:<50} {value}")
    for key, var in vars_dict["z"].items():
        value = candidate.get("z", {}).get(key, 0)
        if value:
            lines.append(f"{var.name:<50} {value}")
    path.write_text("\n".join(lines) + "\n")
    return model.readSolFile(str(path)) is not None


def write_solution_csv(extracted: dict, path: Path = SOLUTION_PATH) -> int:
    fieldnames = ["row_type", "scenario_id", "slot_id", "class_id", "block_id", "value"]
    row_count = 0
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for (c, t), value in sorted(extracted["q"].items()):
            if value <= 0:
                continue
            writer.writerow(
                {
                    "row_type": "quota",
                    "scenario_id": "",
                    "slot_id": t,
                    "class_id": c,
                    "block_id": "",
                    "value": str(value),
                }
            )
            row_count += 1

        for (b, t), value in sorted(extracted["y"].items()):
            if value <= 0:
                continue
            writer.writerow(
                {
                    "row_type": "team",
                    "scenario_id": "",
                    "slot_id": t,
                    "class_id": "",
                    "block_id": b,
                    "value": str(value),
                }
            )
            row_count += 1

        for (s, c, t), value in sorted(extracted["a"].items()):
            if value <= 0:
                continue
            writer.writerow(
                {
                    "row_type": "admit",
                    "scenario_id": s,
                    "slot_id": t,
                    "class_id": c,
                    "block_id": "",
                    "value": str(value),
                }
            )
            row_count += 1

        for (s, c, b, t), value in sorted(extracted["z"].items()):
            if value <= 0:
                continue
            writer.writerow(
                {
                    "row_type": "service",
                    "scenario_id": s,
                    "slot_id": t,
                    "class_id": c,
                    "block_id": b,
                    "value": str(value),
                }
            )
            row_count += 1
    return row_count


def cvar_value(costs: Dict[str, float], probs: Dict[str, float], alpha: float) -> float:
    points = sorted(set(costs.values()))
    if not points:
        return 0.0
    best = float("inf")
    denom = max(1e-9, 1.0 - alpha)
    for eta in points:
        value = eta + sum(probs[s] * max(0.0, costs[s] - eta) for s in costs) / denom
        if value < best:
            best = value
    return best


def audit_solution(data: Data, solution_path: Path) -> dict:
    result = {
        "exists": solution_path.exists(),
        "valid": False,
        "records": 0,
        "theta": None,
        "violation_count": 0,
        "violations": [],
        "objective": None,
        "scenario_costs": {},
    }
    if not solution_path.exists():
        result["violations"].append("solution.csv is missing")
        return result

    rows = load_csv(solution_path)
    result["records"] = len(rows)
    expected_header = ["row_type", "scenario_id", "slot_id", "class_id", "block_id", "value"]
    with solution_path.open() as f:
        header = f.readline().strip().split(",")
    if header != expected_header:
        result["violations"].append(f"header mismatch: {header}")

    quota: Dict[Tuple[str, str], int] = {}
    team: Dict[Tuple[str, str], int] = {}
    admit: Dict[Tuple[str, str, str], int] = {}
    service: Dict[Tuple[str, str, str, str], int] = {}
    seen_exact_keys = set()

    valid_row_types = {"quota", "team", "admit", "service"}
    for idx, row in enumerate(rows, start=2):
        key = tuple(row[col] for col in expected_header[:-1])
        if key in seen_exact_keys:
            result["violations"].append(f"duplicate row key at line {idx}: {key}")
        seen_exact_keys.add(key)

        row_type = row["row_type"]
        if row_type not in valid_row_types:
            result["violations"].append(f"invalid row_type at line {idx}: {row_type}")
            continue
        try:
            raw = float(row["value"])
        except ValueError:
            result["violations"].append(f"non-numeric value at line {idx}: {row['value']}")
            continue
        if abs(raw - round(raw)) > AUDIT_TOL or raw < -AUDIT_TOL:
            result["violations"].append(f"nonnegative integer violated at line {idx}: {row['value']}")
            continue
        value = int(round(raw))

        if row_type == "quota":
            if row["scenario_id"] or row["block_id"]:
                result["violations"].append(f"quota blanks violated at line {idx}")
            if row["slot_id"] not in data.slots or row["class_id"] not in data.classes:
                result["violations"].append(f"invalid quota ids at line {idx}")
            else:
                key2 = (row["class_id"], row["slot_id"])
                quota[key2] = quota.get(key2, 0) + value
        elif row_type == "team":
            if row["scenario_id"] or row["class_id"]:
                result["violations"].append(f"team blanks violated at line {idx}")
            if row["slot_id"] not in data.slots or row["block_id"] not in data.blocks:
                result["violations"].append(f"invalid team ids at line {idx}")
            else:
                key2 = (row["block_id"], row["slot_id"])
                team[key2] = team.get(key2, 0) + value
        elif row_type == "admit":
            if row["block_id"]:
                result["violations"].append(f"admit block blank violated at line {idx}")
            if (
                row["scenario_id"] not in data.scenarios
                or row["slot_id"] not in data.slots
                or row["class_id"] not in data.classes
            ):
                result["violations"].append(f"invalid admit ids at line {idx}")
            else:
                key3 = (row["scenario_id"], row["class_id"], row["slot_id"])
                admit[key3] = admit.get(key3, 0) + value
        elif row_type == "service":
            if (
                row["scenario_id"] not in data.scenarios
                or row["slot_id"] not in data.slots
                or row["class_id"] not in data.classes
                or row["block_id"] not in data.blocks
            ):
                result["violations"].append(f"invalid service ids at line {idx}")
            else:
                key4 = (row["scenario_id"], row["class_id"], row["block_id"], row["slot_id"])
                service[key4] = service.get(key4, 0) + value

    theta = 0.0

    def register(residual: float, label: str) -> None:
        nonlocal theta
        if residual > AUDIT_TOL:
            result["violations"].append(label)
            result["violation_count"] += 1
            theta = max(theta, residual)

    slot_total_quota = {}
    total_quota = 0
    for t in data.slots:
        total = sum(quota.get((c, t), 0) for c in data.classes)
        slot_total_quota[t] = total
        total_quota += total
        register(
            max(0.0, data.min_quota[t] - total) / max(1.0, data.min_quota[t]),
            f"slot minimum quota violated at {t}",
        )
    register(
        max(0.0, data.minimum_total_quota - total_quota) / max(1.0, data.minimum_total_quota),
        "day minimum total quota violated",
    )
    register(
        max(0.0, total_quota - data.maximum_total_quota) / max(1.0, data.maximum_total_quota),
        "day maximum total quota violated",
    )
    for c, t in quota:
        if t not in data.allowed_slots[c]:
            register(1.0, f"quota in disallowed slot for class {c}, slot {t}")
    for idx in range(1, len(data.slots)):
        prev_t = data.slots[idx - 1]
        t = data.slots[idx]
        diff = abs(slot_total_quota[t] - slot_total_quota[prev_t])
        register(
            max(0.0, diff - data.quota_ramp_limit) / max(1.0, data.quota_ramp_limit),
            f"quota ramp violated between {prev_t} and {t}",
        )

    for b in data.blocks:
        for t in data.slots:
            val = team.get((b, t), 0)
            register(
                max(0.0, val - data.block_max_teams[b]) / max(1.0, data.block_max_teams[b]),
                f"block team cap violated at {b},{t}",
            )
    for t in data.slots:
        team_total = sum(team.get((b, t), 0) for b in data.blocks)
        register(
            max(0.0, team_total - data.max_extra_teams_per_slot) / max(1.0, data.max_extra_teams_per_slot),
            f"slot team cap violated at {t}",
        )
        for pool_id, pool_blocks, pool_cap in data.team_pools.get(t, []):
            pool_total = sum(team.get((b, t), 0) for b in pool_blocks)
            register(
                max(0.0, pool_total - pool_cap) / max(1.0, pool_cap),
                f"team pool cap violated at {pool_id},{t}",
            )

    scenario_costs = {}
    for s in data.scenarios:
        handling_cost = 0.0
        shortage_cost = 0.0
        waiting_cost = 0.0

        for c in data.classes:
            cum_admit = 0
            cum_service = 0
            total_admit = 0
            total_service = 0
            for t in data.slots:
                a_val = admit.get((s, c, t), 0)
                total_admit += a_val
                quota_val = quota.get((c, t), 0)
                if t not in data.allowed_slots[c]:
                    register(float(a_val > 0), f"admission in disallowed slot for {s},{c},{t}")
                register(
                    max(0.0, a_val - quota_val) / max(1.0, quota_val if quota_val > 0 else data.demand[(s, t, c)] + 1.0),
                    f"admission exceeds quota at {s},{c},{t}",
                )
                register(
                    max(0.0, a_val - data.demand[(s, t, c)]) / max(1.0, data.demand[(s, t, c)]),
                    f"admission exceeds demand at {s},{c},{t}",
                )
                shortage_cost += data.shortage_penalty[c] * (data.demand[(s, t, c)] - a_val)
                cum_admit += a_val

                service_now = 0
                for b in data.blocks:
                    served = service.get((s, c, b, t), 0)
                    if served and (c, b) not in data.work_units:
                        register(1.0, f"ineligible service pair at {s},{c},{b},{t}")
                    if data.slot_index[t] > data.latest_slot_idx[c] and served > 0:
                        register(1.0, f"service after latest slot at {s},{c},{b},{t}")
                    service_now += served
                    if (c, b) in data.work_units:
                        handling_cost += data.handling_cost[(c, b)] * served
                total_service += service_now
                cum_service += service_now
                register(
                    max(0.0, cum_service - cum_admit) / max(1.0, cum_admit),
                    f"service before admission prefix violated at {s},{c},{t}",
                )
                queue = cum_admit - cum_service
                register(
                    max(0.0, queue - data.queue_limit[t]) / max(1.0, data.queue_limit[t]),
                    f"queue limit violated at {s},{c},{t}",
                )
                waiting_cost += data.waiting_cost[c] * queue
            register(
                abs(total_service - total_admit) / max(1.0, total_admit),
                f"full service equality violated at {s},{c}",
            )

        for t in data.slots:
            gate_total = sum(admit.get((s, c, t), 0) for c in data.classes)
            register(
                max(0.0, gate_total - data.gate_capacity[(s, t)]) / max(1.0, data.gate_capacity[(s, t)]),
                f"gate capacity violated at {s},{t}",
            )
            reefer_total = sum(data.reefer_flag[c] * admit.get((s, c, t), 0) for c in data.classes)
            register(
                max(0.0, reefer_total - data.reefer_limit[t]) / max(1.0, data.reefer_limit[t]),
                f"reefer limit violated at {s},{t}",
            )
            exposure_total = sum(data.exposure_units[c] * admit.get((s, c, t), 0) for c in data.classes)
            register(
                max(0.0, exposure_total - data.exposure_cap[t]) / max(1.0, data.exposure_cap[t]),
                f"exposure cap violated at {s},{t}",
            )
            for rule_id, classes_in_rule, max_admissions in data.slot_mix_rules.get(t, []):
                mix_total = sum(admit.get((s, c, t), 0) for c in classes_in_rule)
                register(
                    max(0.0, mix_total - max_admissions) / max(1.0, max_admissions),
                    f"slot mix cap violated at {s},{t},{rule_id}",
                )

        for scenario_id, c, by_slot, min_served in data.commitments:
            if scenario_id != s:
                continue
            served = sum(
                service.get((s, c, b, t), 0)
                for b in data.eligible_blocks.get(c, [])
                for t in data.slots
                if data.slot_index[t] <= data.slot_index[by_slot]
            )
            register(
                max(0.0, min_served - served) / max(1.0, min_served),
                f"service commitment violated at {s},{c},{by_slot}",
            )

        for b in data.blocks:
            for t in data.slots:
                lhs = sum(
                    data.work_units[(c, b)] * service.get((s, c, b, t), 0)
                    for c in data.classes
                    if (c, b) in data.work_units
                )
                rhs = (
                    data.block_base_capacity[b] * data.productivity[(s, t, b)]
                    + data.team_work_unit_gain * team.get((b, t), 0)
                )
                register(
                    max(0.0, lhs - rhs) / max(1.0, rhs),
                    f"block capacity violated at {s},{b},{t}",
                )

        scenario_costs[s] = handling_cost + shortage_cost + waiting_cost

    fixed_cost = data.quota_admin_cost * total_quota + data.extra_team_cost * sum(team.values())
    expected_cost = sum(data.probabilities[s] * scenario_costs[s] for s in data.scenarios)
    cvar = cvar_value(scenario_costs, data.probabilities, data.tail_risk_alpha)
    result["objective"] = fixed_cost + expected_cost + data.tail_risk_weight * cvar
    result["scenario_costs"] = scenario_costs
    result["theta"] = theta
    result["valid"] = len(result["violations"]) == 0
    return result


def run_exact(data: Data, time_limit: float) -> Tuple[SolveResult, Optional[dict]]:
    start = time.time()
    model, vars_dict, _ = build_model(data, elastic=False)
    model.setParam("limits/time", time_limit)
    model.optimize()
    elapsed = time.time() - start

    extracted = None
    if model.getNSols() > 0 and model.getBestSol() is not None:
        extracted = extract_solution(model, vars_dict)

    result = SolveResult(
        mode="exact",
        status=str(model.getStatus()),
        elapsed_seconds=elapsed,
        objective=safe_stat(model.getObjVal) if extracted is not None else None,
        dual_bound=safe_stat(model.getDualbound),
        gap=safe_stat(model.getGap),
        solution_count=model.getNSols(),
        exported=False,
        notes=[],
    )
    return result, extracted


def run_exact_improvement(
    data: Data,
    time_limit: float,
    incumbent: dict,
    mode: str,
    local_branch_radius: Optional[int] = None,
) -> Tuple[SolveResult, Optional[dict]]:
    start = time.time()
    model, vars_dict, _ = build_model(
        data,
        elastic=False,
        candidate=incumbent,
        local_branch_radius=local_branch_radius,
    )
    mip_start_ok = load_mip_start_from_file(model, vars_dict, incumbent, START_SOL_PATH)
    model.setParam("limits/time", time_limit)
    model.optimize()
    elapsed = time.time() - start

    extracted = None
    if model.getNSols() > 0 and model.getBestSol() is not None:
        extracted = extract_solution(model, vars_dict)

    notes = [f"mip_start_loaded={mip_start_ok}"]
    if local_branch_radius is not None:
        notes.append(f"local_branch_radius={local_branch_radius}")
    result = SolveResult(
        mode=mode,
        status=str(model.getStatus()),
        elapsed_seconds=elapsed,
        objective=safe_stat(model.getObjVal) if extracted is not None else None,
        dual_bound=safe_stat(model.getDualbound),
        gap=safe_stat(model.getGap),
        solution_count=model.getNSols(),
        exported=False,
        notes=notes,
    )
    return result, extracted


def run_phase1(data: Data, time_limit: float, candidate: Optional[dict] = None) -> Tuple[SolveResult, Optional[dict]]:
    start = time.time()
    model, vars_dict, aux = build_model(data, elastic=True, candidate=candidate)
    model.setParam("limits/time", time_limit)
    model.optimize()
    elapsed = time.time() - start

    extracted = None
    notes = []
    if model.getNSols() > 0 and model.getBestSol() is not None:
        extracted = extract_solution(model, vars_dict)
        theta = safe_stat(lambda: model.getSolVal(model.getBestSol(), aux["theta"]))
        norm_sum = safe_stat(lambda: model.getSolVal(model.getBestSol(), aux["norm_sum"]))
        notes.append(f"phase1_theta={theta:.8f}" if theta is not None else "phase1_theta=NA")
        notes.append(f"phase1_norm_sum={norm_sum:.8f}" if norm_sum is not None else "phase1_norm_sum=NA")
    result = SolveResult(
        mode="phase1",
        status=str(model.getStatus()),
        elapsed_seconds=elapsed,
        objective=safe_stat(model.getObjVal) if extracted is not None else None,
        dual_bound=safe_stat(model.getDualbound),
        gap=safe_stat(model.getGap),
        solution_count=model.getNSols(),
        exported=False,
        notes=notes,
    )
    return result, extracted


def write_log(
    command: str,
    solve_results: List[SolveResult],
    audit: dict,
    time_limit: float,
    performance_summary: Optional[dict] = None,
) -> None:
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        f"- `PYTHONPATH=/opt/or-deps python -c \"import pyscipopt; print(pyscipopt.__version__)\"`",
        f"- `{command}`",
        "",
        "## Solver Settings",
        "",
        f"- Relative gap: `{REL_GAP}`",
        f"- Time limit used: `{time_limit:.2f}` seconds",
        "",
        "## Solve Attempts",
        "",
    ]
    for res in solve_results:
        lines.append(f"- Mode: `{res.mode}`")
        lines.append(f"  Status: `{res.status}`")
        lines.append(f"  Elapsed seconds: `{res.elapsed_seconds:.4f}`")
        lines.append(f"  Incumbent count: `{res.solution_count}`")
        if res.objective is not None:
            lines.append(f"  Objective: `{res.objective:.8f}`")
        if res.dual_bound is not None:
            lines.append(f"  Dual bound: `{res.dual_bound:.8f}`")
        if res.gap is not None:
            lines.append(f"  Gap: `{res.gap:.8f}`")
        for note in res.notes:
            lines.append(f"  Note: `{note}`")
    if performance_summary is not None:
        lines.extend(
            [
                "",
                "## Performance Summary",
                "",
                f"- Rollback incumbent objective: `{performance_summary['rollback_objective']:.8f}`",
                f"- Best candidate objective considered: `{performance_summary['best_candidate_objective']:.8f}`",
                f"- Published artifact objective: `{performance_summary['published_objective']:.8f}`",
                f"- Published strict improvement: `{performance_summary['published_improvement']}`",
                f"- Rollback audit valid: `{performance_summary['rollback_valid']}`",
                f"- Published audit valid: `{performance_summary['published_valid']}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Independent Audit",
            "",
            f"- Artifact: `{SOLUTION_PATH}`",
            f"- Exists: `{audit['exists']}`",
            f"- Valid: `{audit['valid']}`",
            f"- Record count: `{audit['records']}`",
            f"- Max normalized residual theta: `{0.0 if audit['theta'] is None else audit['theta']:.8f}`",
            f"- Violated constraint groups: `{audit['violation_count']}`",
        ]
    )
    if audit["objective"] is not None:
        lines.append(f"- Recomputed score proxy: `{audit['objective']:.8f}`")
    if audit["violations"]:
        lines.append("- Violations:")
        for violation in audit["violations"]:
            lines.append(f"  - `{violation}`")
    else:
        lines.append("- Violations: none")
    LOG_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Required model document missing: {MODEL_PATH}")

    data = load_data()
    command = "PYTHONPATH=/opt/or-deps python /app/submissions/solve.py"
    total_limit = limit_from_env()
    solve_results: List[SolveResult] = []

    incumbent_audit = audit_solution(data, SOLUTION_PATH) if SOLUTION_PATH.exists() else None
    incumbent_candidate = None
    if incumbent_audit is not None and incumbent_audit["valid"]:
        incumbent_candidate = load_solution_dict(SOLUTION_PATH)
        shutil.copy2(SOLUTION_PATH, ROLLBACK_PATH)

        remaining = total_limit
        best_audit = incumbent_audit
        best_candidate = incumbent_candidate
        best_extracted = incumbent_candidate
        best_objective = incumbent_audit["objective"]

        exact_slice = min(120.0, max(30.0, remaining * 0.45))
        improvement_result, improvement_extracted = run_exact_improvement(
            data,
            exact_slice,
            incumbent_candidate,
            mode="exact_improvement",
        )
        remaining = max(0.0, remaining - improvement_result.elapsed_seconds)
        if improvement_extracted is not None:
            write_solution_csv(improvement_extracted, CANDIDATE_PATH)
            candidate_audit = audit_solution(data, CANDIDATE_PATH)
            candidate_obj = candidate_audit["objective"] if candidate_audit["objective"] is not None else float("inf")
            strict_improve = candidate_audit["valid"] and candidate_obj + 1e-8 < best_objective
            improvement_result.notes.append(f"incumbent_objective={best_objective:.8f}")
            improvement_result.notes.append(
                f"candidate_audited_objective={candidate_obj:.8f}" if candidate_audit["objective"] is not None else "candidate_audited_objective=NA"
            )
            improvement_result.notes.append(f"candidate_audit_valid={candidate_audit['valid']}")
            improvement_result.notes.append(f"published={strict_improve}")
            if strict_improve:
                best_audit = candidate_audit
                best_candidate = improvement_extracted
                best_extracted = improvement_extracted
                best_objective = candidate_obj
        else:
            improvement_result.notes.append(f"incumbent_objective={best_objective:.8f}")
            improvement_result.notes.append("candidate_audited_objective=NA")
            improvement_result.notes.append("candidate_audit_valid=False")
            improvement_result.notes.append("published=False")
        solve_results.append(improvement_result)

        if remaining >= 30.0:
            local_radius = 18 if best_objective <= incumbent_audit["objective"] + 1e-8 else 12
            local_result, local_extracted = run_exact_improvement(
                data,
                remaining,
                best_candidate,
                mode="local_branching",
                local_branch_radius=local_radius,
            )
            if local_extracted is not None:
                write_solution_csv(local_extracted, CANDIDATE_PATH)
                local_audit = audit_solution(data, CANDIDATE_PATH)
                local_obj = local_audit["objective"] if local_audit["objective"] is not None else float("inf")
                strict_improve = local_audit["valid"] and local_obj + 1e-8 < best_objective
                local_result.notes.append(f"incumbent_objective={best_objective:.8f}")
                local_result.notes.append(
                    f"candidate_audited_objective={local_obj:.8f}" if local_audit["objective"] is not None else "candidate_audited_objective=NA"
                )
                local_result.notes.append(f"candidate_audit_valid={local_audit['valid']}")
                local_result.notes.append(f"published={strict_improve}")
                if strict_improve:
                    best_audit = local_audit
                    best_candidate = local_extracted
                    best_extracted = local_extracted
                    best_objective = local_obj
            else:
                local_result.notes.append(f"incumbent_objective={best_objective:.8f}")
                local_result.notes.append("candidate_audited_objective=NA")
                local_result.notes.append("candidate_audit_valid=False")
                local_result.notes.append("published=False")
            solve_results.append(local_result)

        if best_objective + 1e-8 < incumbent_audit["objective"]:
            write_solution_csv(best_extracted, SOLUTION_PATH)
            final_audit = audit_solution(data, SOLUTION_PATH)
        else:
            final_audit = incumbent_audit

        write_log(
            command,
            solve_results,
            final_audit,
            total_limit,
            performance_summary={
                "rollback_objective": incumbent_audit["objective"],
                "best_candidate_objective": best_objective,
                "published_objective": final_audit["objective"],
                "published_improvement": bool(best_objective + 1e-8 < incumbent_audit["objective"]),
                "rollback_valid": incumbent_audit["valid"],
                "published_valid": final_audit["valid"],
            },
        )
        if not final_audit["valid"]:
            raise RuntimeError(f"Exported artifact failed audit: {final_audit['violations'][:5]}")
        return

    exact_limit = min(total_limit, 240.0)
    exact_result, extracted = run_exact(data, exact_limit)
    solve_results.append(exact_result)

    if extracted is None:
        remaining = max(1.0, total_limit - exact_result.elapsed_seconds)
        phase1_result, extracted = run_phase1(data, remaining, candidate=None)
        solve_results.append(phase1_result)
        if extracted is None:
            audit = audit_solution(data, SOLUTION_PATH)
            write_log(command, solve_results, audit, total_limit)
            raise RuntimeError("No incumbent found in exact or Phase-I solve.")

    write_solution_csv(extracted)
    audit = audit_solution(data, SOLUTION_PATH)
    solve_results[-1].exported = True
    write_log(command, solve_results, audit, total_limit)
    if not audit["valid"]:
        raise RuntimeError(f"Exported artifact failed audit: {audit['violations'][:5]}")


if __name__ == "__main__":
    main()
