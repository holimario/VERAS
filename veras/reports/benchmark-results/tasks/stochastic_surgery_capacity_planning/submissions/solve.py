import csv
import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUB_DIR = ROOT / "submissions"
SOLUTION_PATH = SUB_DIR / "solution.csv"
LOG_PATH = SUB_DIR / "solve_log.md"


@dataclass(frozen=True)
class Case:
    case_id: str
    specialty: str
    ready_day: int
    due_day: int
    expected_duration_min: int
    priority_weight: int
    defer_penalty: float
    anesthesia_type: str
    ward: str
    los_nights: int
    icu_need: int
    pediatric: int
    high_acuity: int
    infection_flag: int
    needs_cardiac_monitoring: int
    primary_surgeon: str
    pacu_minutes: int
    equipment_pool: str


@dataclass(frozen=True)
class Block:
    block_id: str
    day: int
    room: str
    regular_minutes: int
    open_cost: float
    allowed_specialties: tuple
    pediatric_capable: int
    cardiac_capable: int
    icu_direct: int
    max_cases: int
    max_high_acuity: int


@dataclass(frozen=True)
class Surgeon:
    surgeon_id: str
    specialties: tuple
    available_days: tuple
    max_minutes_per_day: int
    max_cases_per_day: int


@dataclass(frozen=True)
class Pool:
    equipment_pool: str
    description: str
    allowed_specialties: tuple
    units: int
    turnaround_minutes: int
    max_uses_per_day: int


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    probability: float
    regional_factor: float
    general_factor: float
    complex_factor: float
    high_acuity_extra_min: float
    emergency_day: int
    emergency_room: str
    emergency_start_minute: float
    emergency_minutes: float
    emergency_pacu_minutes: float
    emergency_ward: str
    emergency_ward_night: int
    emergency_ward_beds: float
    emergency_icu_night: int
    emergency_icu_beds: float


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def parse_pipe_tuple(value):
    return tuple(part for part in value.split("|") if part)


def load_data():
    config = json.loads((DATA_DIR / "config.json").read_text())

    cases = []
    for row in read_csv(DATA_DIR / "surgery_cases.csv"):
        cases.append(
            Case(
                case_id=row["case_id"],
                specialty=row["specialty"],
                ready_day=int(row["ready_day"]),
                due_day=int(row["due_day"]),
                expected_duration_min=int(row["expected_duration_min"]),
                priority_weight=int(row["priority_weight"]),
                defer_penalty=float(row["defer_penalty"]),
                anesthesia_type=row["anesthesia_type"],
                ward=row["ward"],
                los_nights=int(row["los_nights"]),
                icu_need=int(row["icu_need"]),
                pediatric=int(row["pediatric"]),
                high_acuity=int(row["high_acuity"]),
                infection_flag=int(row["infection_flag"]),
                needs_cardiac_monitoring=int(row["needs_cardiac_monitoring"]),
                primary_surgeon=row["primary_surgeon"],
                pacu_minutes=int(row["pacu_minutes"]),
                equipment_pool=row["equipment_pool"],
            )
        )

    blocks = []
    for row in read_csv(DATA_DIR / "or_blocks.csv"):
        blocks.append(
            Block(
                block_id=row["block_id"],
                day=int(row["day"]),
                room=row["room"],
                regular_minutes=int(row["regular_minutes"]),
                open_cost=float(row["open_cost"]),
                allowed_specialties=parse_pipe_tuple(row["allowed_specialties"]),
                pediatric_capable=int(row["pediatric_capable"]),
                cardiac_capable=int(row["cardiac_capable"]),
                icu_direct=int(row["icu_direct"]),
                max_cases=int(row["max_cases"]),
                max_high_acuity=int(row["max_high_acuity"]),
            )
        )

    surgeons = {}
    for row in read_csv(DATA_DIR / "surgeons.csv"):
        surgeons[row["surgeon_id"]] = Surgeon(
            surgeon_id=row["surgeon_id"],
            specialties=parse_pipe_tuple(row["specialties"]),
            available_days=tuple(int(x) for x in row["available_days"].split("|") if x),
            max_minutes_per_day=int(row["max_minutes_per_day"]),
            max_cases_per_day=int(row["max_cases_per_day"]),
        )

    pools = {}
    for row in read_csv(DATA_DIR / "equipment_pools.csv"):
        pools[row["equipment_pool"]] = Pool(
            equipment_pool=row["equipment_pool"],
            description=row["description"],
            allowed_specialties=parse_pipe_tuple(row["allowed_specialties"]),
            units=int(row["units"]),
            turnaround_minutes=int(row["turnaround_minutes"]),
            max_uses_per_day=int(row["max_uses_per_day"]),
        )

    scenarios = []
    for row in read_csv(DATA_DIR / "scenarios.csv"):
        scenarios.append(
            Scenario(
                scenario_id=row["scenario_id"],
                probability=float(row["probability"]),
                regional_factor=float(row["regional_factor"]),
                general_factor=float(row["general_factor"]),
                complex_factor=float(row["complex_factor"]),
                high_acuity_extra_min=float(row["high_acuity_extra_min"]),
                emergency_day=int(row["emergency_day"]),
                emergency_room=row["emergency_room"],
                emergency_start_minute=float(row["emergency_start_minute"]),
                emergency_minutes=float(row["emergency_minutes"]),
                emergency_pacu_minutes=float(row["emergency_pacu_minutes"]),
                emergency_ward=row["emergency_ward"],
                emergency_ward_night=int(row["emergency_ward_night"]),
                emergency_ward_beds=float(row["emergency_ward_beds"]),
                emergency_icu_night=int(row["emergency_icu_night"]),
                emergency_icu_beds=float(row["emergency_icu_beds"]),
            )
        )

    anesthesia_limits = {}
    for row in read_csv(DATA_DIR / "anesthesia_limits.csv"):
        anesthesia_limits[(int(row["day"]), row["anesthesia_type"])] = int(row["max_cases"])

    pacu_buckets = defaultdict(list)
    for row in read_csv(DATA_DIR / "pacu_capacity.csv"):
        pacu_buckets[int(row["day"])].append(
            (
                float(row["bucket_start_minute"]),
                float(row["bucket_end_minute"]),
                float(row["capacity"]),
            )
        )
    for day in pacu_buckets:
        pacu_buckets[day].sort()

    ward_capacity = {}
    wards = set()
    nights = set()
    for row in read_csv(DATA_DIR / "recovery_capacity.csv"):
        night = int(row["night"])
        ward = row["ward"]
        ward_capacity[(night, ward)] = int(row["capacity"])
        wards.add(ward)
        nights.add(night)

    icu_capacity = {}
    for row in read_csv(DATA_DIR / "icu_capacity.csv"):
        night = int(row["night"])
        icu_capacity[night] = int(row["capacity"])
        nights.add(night)

    cases_by_id = {c.case_id: c for c in cases}
    blocks_by_id = {b.block_id: b for b in blocks}

    compatible_blocks = defaultdict(list)
    compatible_days = defaultdict(set)
    impossible_cases = []
    for case in cases:
        surgeon = surgeons[case.primary_surgeon]
        pool = pools[case.equipment_pool]
        pool_ok = case.specialty in pool.allowed_specialties
        surgeon_ok = case.specialty in surgeon.specialties
        if not pool_ok or not surgeon_ok:
            impossible_cases.append(case.case_id)
            continue
        for block in blocks:
            if case.specialty not in block.allowed_specialties:
                continue
            if block.day < case.ready_day:
                continue
            if block.day not in surgeon.available_days:
                continue
            if case.pediatric and not block.pediatric_capable:
                continue
            if case.needs_cardiac_monitoring and not block.cardiac_capable:
                continue
            if case.icu_need and not block.icu_direct:
                continue
            compatible_blocks[case.case_id].append(block.block_id)
            compatible_days[case.case_id].add(block.day)
        if not compatible_blocks[case.case_id]:
            impossible_cases.append(case.case_id)

    blocks_by_day = defaultdict(list)
    for block in blocks:
        blocks_by_day[block.day].append(block.block_id)

    cases_by_surgeon = defaultdict(list)
    cases_by_pool = defaultdict(list)
    cases_by_anes = defaultdict(list)
    for case in cases:
        cases_by_surgeon[case.primary_surgeon].append(case.case_id)
        cases_by_pool[case.equipment_pool].append(case.case_id)
        cases_by_anes[case.anesthesia_type].append(case.case_id)

    return {
        "config": config,
        "cases": cases,
        "cases_by_id": cases_by_id,
        "blocks": blocks,
        "blocks_by_id": blocks_by_id,
        "surgeons": surgeons,
        "pools": pools,
        "scenarios": scenarios,
        "anesthesia_limits": anesthesia_limits,
        "pacu_buckets": dict(pacu_buckets),
        "ward_capacity": ward_capacity,
        "icu_capacity": icu_capacity,
        "wards": sorted(wards),
        "nights": sorted(nights),
        "compatible_blocks": {k: tuple(v) for k, v in compatible_blocks.items()},
        "compatible_days": {k: tuple(sorted(v)) for k, v in compatible_days.items()},
        "blocks_by_day": {k: tuple(v) for k, v in blocks_by_day.items()},
        "cases_by_surgeon": {k: tuple(v) for k, v in cases_by_surgeon.items()},
        "cases_by_pool": {k: tuple(v) for k, v in cases_by_pool.items()},
        "cases_by_anes": {k: tuple(v) for k, v in cases_by_anes.items()},
        "impossible_cases": tuple(sorted(set(impossible_cases))),
    }


def interval_overlap(start1, end1, start2, end2):
    return max(0.0, min(end1, end2) - max(start1, start2))


def factor_for_scenario(case, scenario):
    if case.anesthesia_type == "regional":
        base = scenario.regional_factor
    elif case.anesthesia_type == "general":
        base = scenario.general_factor
    else:
        base = scenario.complex_factor
    return base * case.expected_duration_min + scenario.high_acuity_extra_min * case.high_acuity


def case_sort_key(case, day, start_hint=0):
    urgency = day - case.due_day
    return (
        -case.defer_penalty,
        -case.priority_weight,
        urgency,
        -case.expected_duration_min,
        start_hint,
        case.case_id,
    )


def build_baseline_solution(data):
    rows = []
    for case in data["cases"]:
        rows.append(
            {
                "row_type": "case",
                "case_id": case.case_id,
                "block_id": "",
                "sequence": "",
                "start_minute": "",
                "decision": "defer",
            }
        )
    return rows


def read_solution(path):
    if not path.exists():
        return None
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def incumbent_case_map(rows):
    return {row["case_id"]: row for row in rows}


def solve_profile(data, profile, time_limit_seconds, incumbent_rows=None, local_branching_k=None):
    config = data["config"]
    cases = data["cases"]
    cases_by_id = data["cases_by_id"]
    blocks = data["blocks"]
    blocks_by_id = data["blocks_by_id"]
    surgeons = data["surgeons"]
    pools = data["pools"]
    compatible_blocks = data["compatible_blocks"]
    compatible_days = data["compatible_days"]
    scenarios = data["scenarios"]

    horizon = max(block.regular_minutes for block in blocks) + int(config["max_planned_overtime_minutes"])
    max_dur = max(case.expected_duration_min for case in cases)
    max_turn = max(pool.turnaround_minutes for pool in pools.values())
    big_m = horizon + max_dur + max(config["turnover_minutes"], max_turn) + 10

    model = Model(f"orclaw_{profile['name']}")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", float(max(1.0, time_limit_seconds)))

    x = {}
    for case in cases:
        for block_id in compatible_blocks.get(case.case_id, ()):
            x[(case.case_id, block_id)] = model.addVar(vtype="B", name=f"x[{case.case_id},{block_id}]")

    defer = {case.case_id: model.addVar(vtype="B", name=f"defer[{case.case_id}]") for case in cases}
    open_block = {block.block_id: model.addVar(vtype="B", name=f"open[{block.block_id}]") for block in blocks}
    start = {
        case.case_id: model.addVar(vtype="I", lb=0, ub=horizon, name=f"start[{case.case_id}]")
        for case in cases
    }

    w = {}
    for case in cases:
        for day in compatible_days.get(case.case_id, ()):
            w[(case.case_id, day)] = model.addVar(vtype="B", name=f"day[{case.case_id},{day}]")

    for case in cases:
        model.addCons(
            defer[case.case_id] + quicksum(x[(case.case_id, b)] for b in compatible_blocks.get(case.case_id, ())) == 1,
            name=f"assign[{case.case_id}]",
        )
        for day in compatible_days.get(case.case_id, ()):
            model.addCons(
                w[(case.case_id, day)]
                == quicksum(
                    x[(case.case_id, b)]
                    for b in compatible_blocks.get(case.case_id, ())
                    if blocks_by_id[b].day == day
                ),
                name=f"day_link[{case.case_id},{day}]",
            )

    for block in blocks:
        block_cases = [case_id for case_id in compatible_blocks if block.block_id in compatible_blocks[case_id]]
        if not block_cases:
            model.addCons(open_block[block.block_id] == 0, name=f"open_zero[{block.block_id}]")
            continue
        for case_id in block_cases:
            model.addCons(
                x[(case_id, block.block_id)] <= open_block[block.block_id],
                name=f"open_link[{case_id},{block.block_id}]",
            )
        model.addCons(
            quicksum(x[(case_id, block.block_id)] for case_id in block_cases) <= block.max_cases,
            name=f"max_cases[{block.block_id}]",
        )
        model.addCons(
            quicksum(cases_by_id[case_id].high_acuity * x[(case_id, block.block_id)] for case_id in block_cases)
            <= block.max_high_acuity,
            name=f"max_high[{block.block_id}]",
        )
        infected = [case_id for case_id in block_cases if cases_by_id[case_id].infection_flag]
        if infected:
            model.addCons(
                quicksum(x[(case_id, block.block_id)] for case_id in infected) <= 1,
                name=f"max_infect[{block.block_id}]",
            )
        for case_id in block_cases:
            model.addCons(
                start[case_id] >= config["first_case_setup_minutes"] - big_m * (1 - x[(case_id, block.block_id)]),
                name=f"start_lb[{case_id},{block.block_id}]",
            )
            model.addCons(
                start[case_id] + cases_by_id[case_id].expected_duration_min
                <= block.regular_minutes + config["max_planned_overtime_minutes"] + big_m * (1 - x[(case_id, block.block_id)]),
                name=f"finish_ub[{case_id},{block.block_id}]",
            )

        sorted_block_cases = sorted(block_cases)
        for idx_i in range(len(sorted_block_cases)):
            i = sorted_block_cases[idx_i]
            case_i = cases_by_id[i]
            for idx_j in range(idx_i + 1, len(sorted_block_cases)):
                j = sorted_block_cases[idx_j]
                case_j = cases_by_id[j]
                order = model.addVar(vtype="B", name=f"roomord[{i},{j},{block.block_id}]")
                model.addCons(
                    start[j]
                    >= start[i]
                    + case_i.expected_duration_min
                    + config["turnover_minutes"]
                    - big_m * (1 - order)
                    - big_m * (2 - x[(i, block.block_id)] - x[(j, block.block_id)]),
                    name=f"room_fwd[{i},{j},{block.block_id}]",
                )
                model.addCons(
                    start[i]
                    >= start[j]
                    + case_j.expected_duration_min
                    + config["turnover_minutes"]
                    - big_m * order
                    - big_m * (2 - x[(i, block.block_id)] - x[(j, block.block_id)]),
                    name=f"room_rev[{i},{j},{block.block_id}]",
                )
                if case_i.infection_flag:
                    model.addCons(
                        start[i]
                        >= start[j]
                        + case_j.expected_duration_min
                        + config["turnover_minutes"]
                        - big_m * (2 - x[(i, block.block_id)] - x[(j, block.block_id)]),
                        name=f"infect_last[{i},{j},{block.block_id}]",
                    )
                if case_j.infection_flag:
                    model.addCons(
                        start[j]
                        >= start[i]
                        + case_i.expected_duration_min
                        + config["turnover_minutes"]
                        - big_m * (2 - x[(i, block.block_id)] - x[(j, block.block_id)]),
                        name=f"infect_last[{j},{i},{block.block_id}]",
                    )

    for surgeon_id, case_ids in data["cases_by_surgeon"].items():
        surgeon = surgeons[surgeon_id]
        for day in surgeon.available_days:
            active_cases = [case_id for case_id in case_ids if (case_id, day) in w]
            if not active_cases:
                continue
            model.addCons(
                quicksum(cases_by_id[case_id].expected_duration_min * w[(case_id, day)] for case_id in active_cases)
                <= surgeon.max_minutes_per_day,
                name=f"surg_minutes[{surgeon_id},{day}]",
            )
            model.addCons(
                quicksum(w[(case_id, day)] for case_id in active_cases) <= surgeon.max_cases_per_day,
                name=f"surg_cases[{surgeon_id},{day}]",
            )
            active_cases = sorted(active_cases)
            for idx_i in range(len(active_cases)):
                i = active_cases[idx_i]
                case_i = cases_by_id[i]
                for idx_j in range(idx_i + 1, len(active_cases)):
                    j = active_cases[idx_j]
                    case_j = cases_by_id[j]
                    order = model.addVar(vtype="B", name=f"surgord[{i},{j},{day}]")
                    model.addCons(
                        start[j]
                        >= start[i]
                        + case_i.expected_duration_min
                        - big_m * (1 - order)
                        - big_m * (2 - w[(i, day)] - w[(j, day)]),
                        name=f"surg_fwd[{i},{j},{day}]",
                    )
                    model.addCons(
                        start[i]
                        >= start[j]
                        + case_j.expected_duration_min
                        - big_m * order
                        - big_m * (2 - w[(i, day)] - w[(j, day)]),
                        name=f"surg_rev[{i},{j},{day}]",
                    )

    for pool_id, case_ids in data["cases_by_pool"].items():
        pool = pools[pool_id]
        for day in range(1, config["planning_days"] + 1):
            active_cases = [case_id for case_id in case_ids if (case_id, day) in w]
            if not active_cases:
                continue
            model.addCons(
                quicksum(w[(case_id, day)] for case_id in active_cases) <= pool.max_uses_per_day,
                name=f"pool_uses[{pool_id},{day}]",
            )
            if pool.units == 1:
                active_cases = sorted(active_cases)
                for idx_i in range(len(active_cases)):
                    i = active_cases[idx_i]
                    case_i = cases_by_id[i]
                    for idx_j in range(idx_i + 1, len(active_cases)):
                        j = active_cases[idx_j]
                        case_j = cases_by_id[j]
                        order = model.addVar(vtype="B", name=f"poolord[{i},{j},{day}]")
                        model.addCons(
                            start[j]
                            >= start[i]
                            + case_i.expected_duration_min
                            + pool.turnaround_minutes
                            - big_m * (1 - order)
                            - big_m * (2 - w[(i, day)] - w[(j, day)]),
                            name=f"pool_fwd[{pool_id},{i},{j},{day}]",
                        )
                        model.addCons(
                            start[i]
                            >= start[j]
                            + case_j.expected_duration_min
                            + pool.turnaround_minutes
                            - big_m * order
                            - big_m * (2 - w[(i, day)] - w[(j, day)]),
                            name=f"pool_rev[{pool_id},{i},{j},{day}]",
                        )
            else:
                assign_unit = {}
                for case_id in active_cases:
                    for unit in range(1, pool.units + 1):
                        assign_unit[(case_id, unit)] = model.addVar(
                            vtype="B", name=f"poolunit[{pool_id},{case_id},{day},{unit}]"
                        )
                    model.addCons(
                        quicksum(assign_unit[(case_id, unit)] for unit in range(1, pool.units + 1)) == w[(case_id, day)],
                        name=f"pool_assign[{pool_id},{case_id},{day}]",
                    )
                active_cases = sorted(active_cases)
                for unit in range(1, pool.units + 1):
                    for idx_i in range(len(active_cases)):
                        i = active_cases[idx_i]
                        case_i = cases_by_id[i]
                        for idx_j in range(idx_i + 1, len(active_cases)):
                            j = active_cases[idx_j]
                            case_j = cases_by_id[j]
                            order = model.addVar(
                                vtype="B", name=f"poolord[{pool_id},{i},{j},{day},{unit}]"
                            )
                            model.addCons(
                                start[j]
                                >= start[i]
                                + case_i.expected_duration_min
                                + pool.turnaround_minutes
                                - big_m * (1 - order)
                                - big_m * (2 - assign_unit[(i, unit)] - assign_unit[(j, unit)]),
                                name=f"pool_fwd[{pool_id},{i},{j},{day},{unit}]",
                            )
                            model.addCons(
                                start[i]
                                >= start[j]
                                + case_j.expected_duration_min
                                + pool.turnaround_minutes
                                - big_m * order
                                - big_m * (2 - assign_unit[(i, unit)] - assign_unit[(j, unit)]),
                                name=f"pool_rev[{pool_id},{i},{j},{day},{unit}]",
                            )

    for day in range(1, config["planning_days"] + 1):
        for anesthesia_type, case_ids in data["cases_by_anes"].items():
            active_cases = [case_id for case_id in case_ids if (case_id, day) in w]
            if not active_cases:
                continue
            model.addCons(
                quicksum(w[(case_id, day)] for case_id in active_cases) <= data["anesthesia_limits"][(day, anesthesia_type)],
                name=f"anes[{day},{anesthesia_type}]",
            )

    if incumbent_rows is not None:
        incumbent_by_case = incumbent_case_map(incumbent_rows)
        lb_terms = []
        if local_branching_k is not None:
            for case in cases:
                row = incumbent_by_case[case.case_id]
                if row["decision"] == "defer":
                    lb_terms.append(1 - defer[case.case_id])
                else:
                    incumbent_block = row["block_id"]
                    if (case.case_id, incumbent_block) in x:
                        lb_terms.append(1 - x[(case.case_id, incumbent_block)])
            model.addCons(quicksum(lb_terms) <= local_branching_k, name=f"local_branch[{profile['name']}]")

    overtime = {}
    idle = {}
    ward_over = {}
    icu_over = {}
    scenario_cost = {}
    tail_excess = {}
    eta = model.addVar(lb=0.0, name="eta")

    overtime_weight = profile["overtime_mult"]
    bed_weight = profile["bed_mult"]

    tail_terms = []
    expected_terms = []

    for scenario in scenarios:
        sid = scenario.scenario_id
        for block in blocks:
            overtime[(block.block_id, sid)] = model.addVar(lb=0.0, name=f"ot[{block.block_id},{sid}]")
            idle[(block.block_id, sid)] = model.addVar(lb=0.0, name=f"idle[{block.block_id},{sid}]")
            duration_expr = quicksum(
                factor_for_scenario(cases_by_id[case_id], scenario) * x[(case_id, block.block_id)]
                for case_id in compatible_blocks
                if (case_id, block.block_id) in x
            )
            count_expr = quicksum(
                x[(case_id, block.block_id)]
                for case_id in compatible_blocks
                if (case_id, block.block_id) in x
            )
            emergency_minutes = (
                scenario.emergency_minutes
                if scenario.emergency_day == block.day and scenario.emergency_room == block.room
                else 0.0
            )
            load_expr = (
                config["first_case_setup_minutes"] * open_block[block.block_id]
                + duration_expr
                + config["turnover_minutes"] * (count_expr - open_block[block.block_id])
                + emergency_minutes * open_block[block.block_id]
            )
            model.addCons(
                overtime[(block.block_id, sid)] >= load_expr - block.regular_minutes,
                name=f"ot_lb[{block.block_id},{sid}]",
            )
            model.addCons(
                idle[(block.block_id, sid)] >= block.regular_minutes - load_expr,
                name=f"idle_lb[{block.block_id},{sid}]",
            )

        for night in data["nights"]:
            icu_over[(night, sid)] = model.addVar(lb=0.0, name=f"icuov[{night},{sid}]")
            icu_load_expr = quicksum(
                x[(case.case_id, block_id)]
                for case in cases
                if case.icu_need
                for block_id in compatible_blocks.get(case.case_id, ())
                if blocks_by_id[block_id].day == night
            )
            emergency_icu = scenario.emergency_icu_beds if scenario.emergency_icu_night == night else 0.0
            model.addCons(
                icu_over[(night, sid)] >= icu_load_expr + emergency_icu - data["icu_capacity"][night],
                name=f"icu_lb[{night},{sid}]",
            )
            for ward in data["wards"]:
                ward_over[(night, ward, sid)] = model.addVar(lb=0.0, name=f"wardov[{night},{ward},{sid}]")
                ward_load_expr = quicksum(
                    x[(case.case_id, block_id)]
                    for case in cases
                    if case.ward == ward
                    for block_id in compatible_blocks.get(case.case_id, ())
                    if blocks_by_id[block_id].day <= night < blocks_by_id[block_id].day + case.los_nights
                )
                emergency_ward = (
                    scenario.emergency_ward_beds
                    if scenario.emergency_ward_night == night and scenario.emergency_ward == ward
                    else 0.0
                )
                model.addCons(
                    ward_over[(night, ward, sid)]
                    >= ward_load_expr + emergency_ward - data["ward_capacity"][(night, ward)],
                    name=f"ward_lb[{night},{ward},{sid}]",
                )

        scenario_cost[sid] = model.addVar(lb=0.0, name=f"scost[{sid}]")
        tail_excess[sid] = model.addVar(lb=0.0, name=f"tailx[{sid}]")
        op_expr = (
            quicksum(
                overtime_weight * config["overtime_cost_per_minute"] * overtime[(block.block_id, sid)]
                + overtime_weight * config["idle_cost_per_minute"] * idle[(block.block_id, sid)]
                for block in blocks
            )
            + quicksum(
                bed_weight
                * config["ward_overflow_cost_per_bed_night"]
                * ward_over[(night, ward, sid)]
                for night in data["nights"]
                for ward in data["wards"]
            )
            + quicksum(
                bed_weight
                * config["icu_overflow_cost_per_bed_night"]
                * icu_over[(night, sid)]
                for night in data["nights"]
            )
        )
        model.addCons(scenario_cost[sid] == op_expr, name=f"scost_def[{sid}]")
        model.addCons(tail_excess[sid] >= scenario_cost[sid] - eta, name=f"tail_lb[{sid}]")
        tail_terms.append(
            config["tail_risk_weight"]
            * scenario.probability
            / (1.0 - config["tail_risk_alpha"])
            * tail_excess[sid]
        )
        expected_terms.append(scenario.probability * scenario_cost[sid])

    plan_cost = (
        quicksum(case.defer_penalty * defer[case.case_id] for case in cases)
        + quicksum(
            config["tardiness_cost_per_priority_day"]
            * case.priority_weight
            * max(0, blocks_by_id[block_id].day - case.due_day)
            * x[(case.case_id, block_id)]
            for case in cases
            for block_id in compatible_blocks.get(case.case_id, ())
        )
        + quicksum(block.open_cost * open_block[block.block_id] for block in blocks)
    )
    tie_break = profile["start_weight"] * quicksum(start[case.case_id] for case in cases)
    model.setObjective(
        plan_cost + quicksum(expected_terms) + config["tail_risk_weight"] * eta + quicksum(tail_terms) + tie_break,
        "minimize",
    )

    if incumbent_rows is not None:
        incumbent_by_case = incumbent_case_map(incumbent_rows)
        incumbent_sol = model.createSol()
        open_blocks = {
            row["block_id"]
            for row in incumbent_rows
            if row["decision"] == "schedule" and row["block_id"]
        }
        for block in blocks:
            model.setSolVal(incumbent_sol, open_block[block.block_id], 1.0 if block.block_id in open_blocks else 0.0)
        for case in cases:
            row = incumbent_by_case[case.case_id]
            scheduled = row["decision"] == "schedule"
            model.setSolVal(incumbent_sol, defer[case.case_id], 0.0 if scheduled else 1.0)
            for block_id in compatible_blocks.get(case.case_id, ()):
                model.setSolVal(
                    incumbent_sol,
                    x[(case.case_id, block_id)],
                    1.0 if scheduled and row["block_id"] == block_id else 0.0,
                )
            model.setSolVal(
                incumbent_sol,
                start[case.case_id],
                float(row["start_minute"]) if scheduled and row["start_minute"] else 0.0,
            )
            for day in compatible_days.get(case.case_id, ()):
                model.setSolVal(
                    incumbent_sol,
                    w[(case.case_id, day)],
                    1.0 if scheduled and data["blocks_by_id"][row["block_id"]].day == day else 0.0,
                )
        model.addSol(incumbent_sol, free=True)

    model.optimize()

    result = {
        "profile": profile["name"],
        "status": str(model.getStatus()),
        "time_limit": time_limit_seconds,
        "has_solution": model.getNSols() > 0,
        "dual_bound": None,
    }

    if model.getNSols() == 0:
        return result

    sol = model.getBestSol()
    result["objective"] = float(model.getObjVal())
    try:
        result["dual_bound"] = float(model.getDualbound())
    except Exception:
        result["dual_bound"] = None
    try:
        result["gap"] = float(model.getGap())
    except Exception:
        result["gap"] = None

    by_block = defaultdict(list)
    rows = []
    for case in cases:
        assigned_blocks = [
            block_id
            for block_id in compatible_blocks.get(case.case_id, ())
            if model.getSolVal(sol, x[(case.case_id, block_id)]) > 0.5
        ]
        if assigned_blocks:
            block_id = assigned_blocks[0]
            start_val = int(round(model.getSolVal(sol, start[case.case_id])))
            by_block[block_id].append((case.case_id, start_val))
        else:
            rows.append(
                {
                    "row_type": "case",
                    "case_id": case.case_id,
                    "block_id": "",
                    "sequence": "",
                    "start_minute": "",
                    "decision": "defer",
                }
            )

    schedule_map = {}
    for block_id, items in by_block.items():
        items.sort(key=lambda item: (item[1], item[0]))
        for seq, (case_id, start_val) in enumerate(items, start=1):
            schedule_map[case_id] = (block_id, seq, start_val)

    final_rows = []
    for case in cases:
        if case.case_id in schedule_map:
            block_id, seq, start_val = schedule_map[case.case_id]
            final_rows.append(
                {
                    "row_type": "case",
                    "case_id": case.case_id,
                    "block_id": block_id,
                    "sequence": str(seq),
                    "start_minute": str(start_val),
                    "decision": "schedule",
                }
            )
        else:
            final_rows.append(
                {
                    "row_type": "case",
                    "case_id": case.case_id,
                    "block_id": "",
                    "sequence": "",
                    "start_minute": "",
                    "decision": "defer",
                }
            )
    result["rows"] = final_rows
    return result


def validate_solution(data, rows):
    cases_by_id = data["cases_by_id"]
    blocks_by_id = data["blocks_by_id"]
    surgeons = data["surgeons"]
    pools = data["pools"]
    config = data["config"]

    errors = []
    if len(rows) != len(data["cases"]):
        errors.append(f"expected {len(data['cases'])} rows, found {len(rows)}")

    seen_cases = set()
    parsed = []
    for row in rows:
        case_id = row.get("case_id", "")
        if row.get("row_type") != "case":
            errors.append(f"{case_id}: row_type must be 'case'")
        if case_id not in cases_by_id:
            errors.append(f"{case_id}: unknown case_id")
            continue
        if case_id in seen_cases:
            errors.append(f"{case_id}: duplicate row")
            continue
        seen_cases.add(case_id)
        decision = row.get("decision", "")
        if decision not in {"schedule", "defer"}:
            errors.append(f"{case_id}: invalid decision {decision}")
            continue
        case = cases_by_id[case_id]
        if decision == "defer":
            if row.get("block_id") or row.get("sequence") or row.get("start_minute"):
                errors.append(f"{case_id}: deferred rows must leave block_id, sequence, and start_minute blank")
            parsed.append({"case_id": case_id, "decision": "defer", "case": case})
            continue
        block_id = row.get("block_id", "")
        if block_id not in blocks_by_id:
            errors.append(f"{case_id}: unknown block_id {block_id}")
            continue
        if not row.get("sequence") or not row.get("start_minute"):
            errors.append(f"{case_id}: scheduled rows must provide block_id, sequence, and start_minute")
            continue
        try:
            sequence = int(str(row["sequence"]))
            start_minute = int(str(row["start_minute"]))
        except ValueError:
            errors.append(f"{case_id}: sequence and start_minute must be integers")
            continue
        if sequence <= 0:
            errors.append(f"{case_id}: sequence must be positive")
        if start_minute < 0:
            errors.append(f"{case_id}: start_minute must be nonnegative")
        block = blocks_by_id[block_id]
        surgeon = surgeons[case.primary_surgeon]
        pool = pools[case.equipment_pool]
        if block_id not in data["compatible_blocks"].get(case_id, ()):
            errors.append(f"{case_id}: incompatible block {block_id}")
        if case.specialty not in surgeon.specialties:
            errors.append(f"{case_id}: primary surgeon lacks specialty credential")
        if block.day not in surgeon.available_days:
            errors.append(f"{case_id}: surgeon unavailable on day {block.day}")
        if case.specialty not in pool.allowed_specialties:
            errors.append(f"{case_id}: equipment pool not allowed for specialty")
        parsed.append(
            {
                "case_id": case_id,
                "decision": "schedule",
                "case": case,
                "block": block,
                "block_id": block_id,
                "day": block.day,
                "room": block.room,
                "sequence": sequence,
                "start": start_minute,
            }
        )

    missing_cases = sorted(set(cases_by_id) - seen_cases)
    for case_id in missing_cases:
        errors.append(f"{case_id}: missing row")

    scheduled = [item for item in parsed if item["decision"] == "schedule"]

    by_block = defaultdict(list)
    for item in scheduled:
        by_block[item["block_id"]].append(item)

    for block_id, items in by_block.items():
        block = blocks_by_id[block_id]
        items.sort(key=lambda x: x["sequence"])
        sequences = [item["sequence"] for item in items]
        if len(set(sequences)) != len(sequences):
            errors.append(f"{block_id}: duplicate sequences")
        if len(items) > block.max_cases:
            errors.append(f"{block_id}: exceeds max_cases")
        if sum(item["case"].high_acuity for item in items) > block.max_high_acuity:
            errors.append(f"{block_id}: exceeds max_high_acuity")
        infected = [item for item in items if item["case"].infection_flag]
        if len(infected) > 1:
            errors.append(f"{block_id}: more than one infection-control case")
        if infected and infected[0]["sequence"] != max(sequences):
            errors.append(f"{block_id}: infection-control case must be final")
        if items[0]["start"] < config["first_case_setup_minutes"]:
            errors.append(f"{block_id}: first case starts before setup time")
        for prev, curr in zip(items, items[1:]):
            required = prev["start"] + prev["case"].expected_duration_min + config["turnover_minutes"]
            if curr["start"] < required:
                errors.append(
                    f"{block_id}: sequence turnover violation between {prev['case_id']} and {curr['case_id']}"
                )
        last = items[-1]
        if last["start"] + last["case"].expected_duration_min > block.regular_minutes + config["max_planned_overtime_minutes"]:
            errors.append(f"{block_id}: planned finish exceeds overtime buffer")
        start_order = [item["case_id"] for item in sorted(items, key=lambda x: (x["start"], x["case_id"]))]
        seq_order = [item["case_id"] for item in items]
        if start_order != seq_order:
            errors.append(f"{block_id}: sequence and start order disagree")

    surgeon_day = defaultdict(list)
    for item in scheduled:
        surgeon_day[(item["case"].primary_surgeon, item["day"])].append(item)
    for (surgeon_id, day), items in surgeon_day.items():
        surgeon = surgeons[surgeon_id]
        if len(items) > surgeon.max_cases_per_day:
            errors.append(f"{surgeon_id} day {day}: exceeds max_cases_per_day")
        if sum(item["case"].expected_duration_min for item in items) > surgeon.max_minutes_per_day:
            errors.append(f"{surgeon_id} day {day}: exceeds max_minutes_per_day")
        items.sort(key=lambda x: (x["start"], x["case_id"]))
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                left = items[i]
                right = items[j]
                if left["start"] + left["case"].expected_duration_min > right["start"]:
                    errors.append(
                        f"{surgeon_id} day {day}: overlap between {left['case_id']} and {right['case_id']}"
                    )

    for day in range(1, data["config"]["planning_days"] + 1):
        for anesthesia_type in data["cases_by_anes"]:
            count = sum(
                1
                for item in scheduled
                if item["day"] == day and item["case"].anesthesia_type == anesthesia_type
            )
            if count > data["anesthesia_limits"][(day, anesthesia_type)]:
                errors.append(f"day {day} anesthesia {anesthesia_type}: exceeds limit")

    pool_day = defaultdict(list)
    for item in scheduled:
        pool_day[(item["case"].equipment_pool, item["day"])].append(item)
    for (pool_id, day), items in pool_day.items():
        pool = pools[pool_id]
        if len(items) > pool.max_uses_per_day:
            errors.append(f"{pool_id} day {day}: exceeds max_uses_per_day")
        events = []
        for item in items:
            start_t = item["start"]
            end_t = item["start"] + item["case"].expected_duration_min + pool.turnaround_minutes
            events.append((start_t, 1))
            events.append((end_t, -1))
        events.sort(key=lambda x: (x[0], x[1]))
        concurrent = 0
        max_concurrent = 0
        for _, delta in events:
            concurrent += delta
            max_concurrent = max(max_concurrent, concurrent)
        if max_concurrent > pool.units:
            errors.append(f"{pool_id} day {day}: concurrent usage {max_concurrent} exceeds units {pool.units}")

    groups_violated = len({error.split(":")[0] for error in errors})
    return {
        "valid": not errors,
        "errors": errors,
        "groups_violated": groups_violated,
        "theta": 0.0 if not errors else 1.0,
        "scheduled_count": len(scheduled),
        "deferred_count": len(data["cases"]) - len(scheduled),
        "parsed": parsed,
    }


def compute_planning_cost(data, parsed_rows):
    config = data["config"]
    total = 0.0
    for item in parsed_rows:
        case = item["case"]
        if item["decision"] == "defer":
            total += case.defer_penalty
        else:
            block = item["block"]
            tardiness_days = max(0, block.day - case.due_day)
            total += config["tardiness_cost_per_priority_day"] * case.priority_weight * tardiness_days
    opened_blocks = {item["block_id"] for item in parsed_rows if item["decision"] == "schedule"}
    for block_id in opened_blocks:
        total += data["blocks_by_id"][block_id].open_cost
    return total


def bucket_overflow_bed_hours(intervals, bucket_start, bucket_end, capacity):
    events = []
    for start_t, end_t in intervals:
        left = max(start_t, bucket_start)
        right = min(end_t, bucket_end)
        if right > left + 1e-9:
            events.append((left, 1.0))
            events.append((right, -1.0))
    if not events:
        return 0.0
    events.sort(key=lambda x: (x[0], x[1]))
    occupied = 0.0
    prev_t = bucket_start
    overflow_hours = 0.0
    for t, delta in events:
        if t > prev_t:
            overflow_hours += max(0.0, occupied - capacity) * (t - prev_t) / 60.0
            prev_t = t
        occupied += delta
    if bucket_end > prev_t:
        overflow_hours += max(0.0, occupied - capacity) * (bucket_end - prev_t) / 60.0
    return overflow_hours


def simulate_scenario(data, parsed_rows, scenario):
    config = data["config"]
    blocks_by_id = data["blocks_by_id"]

    by_block = defaultdict(list)
    elective_ward = defaultdict(float)
    elective_icu = defaultdict(float)
    case_finish = {}

    for item in parsed_rows:
        if item["decision"] != "schedule":
            continue
        by_block[item["block_id"]].append(item)
        day = item["day"]
        case = item["case"]
        for night in range(day, day + case.los_nights):
            elective_ward[(night, case.ward)] += 1.0
        if case.icu_need:
            elective_icu[day] += 1.0

    overtime_cost = 0.0
    idle_cost = 0.0
    pacu_cost = 0.0
    ward_cost = 0.0
    icu_cost = 0.0
    pacu_intervals = defaultdict(list)

    for block_id, items in by_block.items():
        block = blocks_by_id[block_id]
        items.sort(key=lambda x: x["sequence"])
        emergency_here = scenario.emergency_day == block.day and scenario.emergency_room == block.room and scenario.emergency_minutes > 0
        e_start = scenario.emergency_start_minute if emergency_here else None
        e_end = scenario.emergency_start_minute + scenario.emergency_minutes if emergency_here else None
        room_ready = 0.0
        last_finish = 0.0
        for item in items:
            case = item["case"]
            planned_start = float(item["start"])
            duration = factor_for_scenario(case, scenario)
            actual_start = max(planned_start, room_ready)
            if emergency_here and interval_overlap(actual_start, actual_start + duration, e_start, e_end) > 1e-9:
                actual_start = max(actual_start, e_end)
            actual_finish = actual_start + duration
            case_finish[item["case_id"]] = actual_finish
            pacu_intervals[block.day].append((actual_finish, actual_finish + case.pacu_minutes))
            room_ready = actual_finish + config["turnover_minutes"]
            last_finish = max(last_finish, actual_finish)
        room_finish = max(last_finish, e_end) if emergency_here else last_finish
        overtime_cost += config["overtime_cost_per_minute"] * max(0.0, room_finish - block.regular_minutes)
        idle_cost += config["idle_cost_per_minute"] * max(0.0, block.regular_minutes - room_finish)

    if scenario.emergency_day > 0 and scenario.emergency_pacu_minutes > 0:
        start_pacu = scenario.emergency_start_minute + scenario.emergency_minutes
        pacu_intervals[scenario.emergency_day].append((start_pacu, start_pacu + scenario.emergency_pacu_minutes))

    for day, buckets in data["pacu_buckets"].items():
        for bucket_start, bucket_end, capacity in buckets:
            overflow_hours = bucket_overflow_bed_hours(
                pacu_intervals.get(day, []), bucket_start, bucket_end, capacity
            )
            pacu_cost += config["pacu_overflow_cost_per_bed_hour"] * overflow_hours

    for night in data["nights"]:
        for ward in data["wards"]:
            load = elective_ward[(night, ward)]
            if scenario.emergency_ward_night == night and scenario.emergency_ward == ward:
                load += scenario.emergency_ward_beds
            overflow = max(0.0, load - data["ward_capacity"][(night, ward)])
            ward_cost += config["ward_overflow_cost_per_bed_night"] * overflow
        icu_load = elective_icu[night]
        if scenario.emergency_icu_night == night:
            icu_load += scenario.emergency_icu_beds
        icu_overflow = max(0.0, icu_load - data["icu_capacity"][night])
        icu_cost += config["icu_overflow_cost_per_bed_night"] * icu_overflow

    operating_cost = overtime_cost + idle_cost + pacu_cost + ward_cost + icu_cost
    return {
        "scenario_id": scenario.scenario_id,
        "probability": scenario.probability,
        "operating_cost": operating_cost,
        "overtime_cost": overtime_cost,
        "idle_cost": idle_cost,
        "pacu_cost": pacu_cost,
        "ward_cost": ward_cost,
        "icu_cost": icu_cost,
        "case_finish": case_finish,
    }


def weighted_cvar(alpha, scenario_results):
    costs = sorted({res["operating_cost"] for res in scenario_results})
    best = None
    for eta in costs:
        value = eta + sum(
            res["probability"] * max(0.0, res["operating_cost"] - eta) / (1.0 - alpha)
            for res in scenario_results
        )
        if best is None or value < best:
            best = value
    return 0.0 if best is None else best


def audit_solution_from_disk(data, path):
    rows = read_solution(path)
    if rows is None:
        return {
            "exists": False,
            "feasible": False,
            "audit_status": "missing",
            "validation": {"valid": False, "errors": ["solution file missing"], "groups_violated": 1, "theta": 1.0},
        }
    scored = score_solution(data, rows)
    if scored["feasible"]:
        scored["exists"] = True
        scored["audit_status"] = "pass"
    else:
        scored["exists"] = True
        scored["audit_status"] = "fail"
    return scored


def score_solution(data, rows):
    validation = validate_solution(data, rows)
    if not validation["valid"]:
        return {"feasible": False, "validation": validation}

    parsed_rows = validation["parsed"]
    planning_cost = compute_planning_cost(data, parsed_rows)
    scenario_results = [simulate_scenario(data, parsed_rows, scenario) for scenario in data["scenarios"]]
    expected_operating = sum(res["probability"] * res["operating_cost"] for res in scenario_results)
    cvar = weighted_cvar(data["config"]["tail_risk_alpha"], scenario_results)
    total_score = planning_cost + expected_operating + data["config"]["tail_risk_weight"] * cvar
    return {
        "feasible": True,
        "validation": validation,
        "planning_cost": planning_cost,
        "expected_operating_cost": expected_operating,
        "tail_cvar": cvar,
        "total_score": total_score,
        "scenario_results": scenario_results,
    }


def write_solution(rows, path):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["row_type", "case_id", "block_id", "sequence", "start_minute", "decision"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def format_attempt_log(attempts, incumbent_audit, chosen_summary, final_score, command):
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        f"- `{command}`",
        "",
        "## Guarded Repair",
        "",
        "- Repair note: disk-based audit reparses `solution.csv` with typed day/night keys before any optimization step.",
        "- Repair note: PACU overflow is recomputed exactly as bucket bed-hours above capacity from interval concurrency, not from total overlap-hours.",
        f"- incumbent_disk_audit: `{incumbent_audit['audit_status']}`",
        f"- incumbent_theta: {incumbent_audit['validation']['theta']}",
        f"- incumbent_violated_groups: {incumbent_audit['validation']['groups_violated']}",
    ]
    if incumbent_audit["feasible"]:
        lines.append(f"- incumbent_exact_objective: {incumbent_audit['total_score']:.6f}")
    else:
        lines.append(f"- incumbent_first_error: {incumbent_audit['validation']['errors'][0]}")
    lines += ["", "## Solve Attempts", ""]
    for attempt in attempts:
        lines.append(
            f"- `{attempt['profile']}`: status `{attempt['status']}`, has_solution={attempt['has_solution']}, "
            f"time_limit={attempt['time_limit']:.1f}s"
            + (f", mip_objective={attempt['objective']:.3f}" if attempt.get("objective") is not None else "")
            + (f", dual_bound={attempt['dual_bound']:.3f}" if attempt.get("dual_bound") is not None else "")
            + (f", gap={attempt['gap']:.6f}" if attempt.get("gap") is not None else "")
            + (f", candidate_exact_objective={attempt['exact_score']:.6f}" if attempt.get("exact_score") is not None else "")
            + (f", audit={attempt['audit_status']}" if attempt.get("audit_status") is not None else "")
        )
        if attempt.get("validation_errors"):
            lines.append(f"  validation: {attempt['validation_errors'][0]}")
    lines += [
        "",
        "## Final Artifact",
        "",
        f"- selected_profile: `{chosen_summary['profile']}`",
        f"- solution_file: `{SOLUTION_PATH}`",
        f"- scheduled_cases: {final_score['validation']['scheduled_count']}",
        f"- deferred_cases: {final_score['validation']['deferred_count']}",
        f"- hard_constraint_groups_violated: {final_score['validation']['groups_violated']}",
        f"- max_normalized_hard_residual_theta: {final_score['validation']['theta']}",
        f"- planning_cost: {final_score['planning_cost']:.3f}",
        f"- expected_operating_cost: {final_score['expected_operating_cost']:.3f}",
        f"- tail_cvar: {final_score['tail_cvar']:.3f}",
        f"- total_score: {final_score['total_score']:.3f}",
        f"- incumbent_exact_objective: {incumbent_audit['total_score']:.6f}" if incumbent_audit["feasible"] else "- incumbent_exact_objective: unavailable",
        f"- published_exact_objective: {final_score['total_score']:.6f}",
        "",
        "## Validation Checks",
        "",
        "- Schema: exact row count and identifiers checked against `surgery_cases.csv`.",
        "- Hard rules checked from the exported artifact: block compatibility, sequence/start consistency, setup/turnover, infection-final, surgeon availability and overlap, equipment concurrency with turnaround, daily anesthesia caps, and block cardinality/acuity.",
        "- Scenario audit recomputed independently from the exported artifact: overtime, idle time, PACU overflow, ward overflow, ICU overflow, and tail-risk CVaR.",
    ]
    return "\n".join(lines) + "\n"


def main():
    start_time = time.time()
    data = load_data()

    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit:
        try:
            overall_limit = min(float(env_limit), 295.0)
        except ValueError:
            overall_limit = 240.0
    else:
        overall_limit = 240.0

    attempts = []
    incumbent_rows = read_solution(SOLUTION_PATH)
    if incumbent_rows is None:
        incumbent_rows = build_baseline_solution(data)
    incumbent_audit = audit_solution_from_disk(data, SOLUTION_PATH) if SOLUTION_PATH.exists() else score_solution(data, incumbent_rows)
    if not incumbent_audit["feasible"]:
        raise RuntimeError(f"rollback incumbent failed disk audit: {incumbent_audit['validation']['errors'][0]}")

    best_rows = incumbent_rows
    best_score = incumbent_audit
    best_summary = {"profile": "rollback_incumbent", "status": "audited", "has_solution": True, "time_limit": 0.0}
    attempts.append(
        {
            "profile": "rollback_incumbent",
            "status": "audited",
            "has_solution": True,
            "time_limit": 0.0,
            "exact_score": best_score["total_score"],
            "audit_status": incumbent_audit["audit_status"],
            "validation_errors": [],
        }
    )

    profiles = [
        {"name": "incumbent_retime", "start_weight": 0.05, "overtime_mult": 1.0, "bed_mult": 1.2, "k": 0},
        {"name": "lb_k2_bedsafe", "start_weight": 0.03, "overtime_mult": 1.15, "bed_mult": 1.5, "k": 2},
        {"name": "lb_k4_balanced", "start_weight": 0.02, "overtime_mult": 1.0, "bed_mult": 1.2, "k": 4},
        {"name": "lb_k6_throughput", "start_weight": 0.01, "overtime_mult": 0.95, "bed_mult": 1.0, "k": 6},
    ]

    reserve_seconds = 12.0
    for idx, profile in enumerate(profiles):
        elapsed = time.time() - start_time
        remaining = overall_limit - elapsed
        profiles_left = len(profiles) - idx
        if remaining <= reserve_seconds + 5.0:
            break
        slice_time = min(50.0, max(12.0, (remaining - reserve_seconds) / profiles_left))
        result = solve_profile(
            data,
            profile,
            slice_time,
            incumbent_rows=best_rows,
            local_branching_k=profile["k"],
        )
        attempt = dict(result)
        if result.get("rows"):
            scored = score_solution(data, result["rows"])
            if scored["feasible"]:
                attempt["exact_score"] = scored["total_score"]
                attempt["audit_status"] = "pass"
                attempt["validation_errors"] = []
                if scored["total_score"] < best_score["total_score"] - 1e-6:
                    best_rows = result["rows"]
                    best_score = scored
                    best_summary = result
            else:
                attempt["audit_status"] = "fail"
                attempt["validation_errors"] = scored["validation"]["errors"]
        else:
            attempt["audit_status"] = "no-incumbent"
        attempts.append(attempt)

    write_solution(best_rows, SOLUTION_PATH)
    final_score = audit_solution_from_disk(data, SOLUTION_PATH)
    if not final_score["feasible"]:
        raise RuntimeError("final exported solution failed validation")

    command = "PYTHONPATH=/opt/or-deps python /app/submissions/solve.py"
    LOG_PATH.write_text(format_attempt_log(attempts, incumbent_audit, best_summary, final_score, command))


if __name__ == "__main__":
    main()
