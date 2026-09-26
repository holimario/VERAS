from __future__ import annotations

import csv
import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUB_DIR = ROOT / "submissions"
SOLUTION_PATH = SUB_DIR / "solution.csv"
LOG_PATH = SUB_DIR / "solve_log.md"


def read_csv(path: Path) -> List[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def maybe_float(value: str) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


@dataclass
class BaseCommitment:
    cast_first_start: Dict[str, int]
    stage_start: Dict[Tuple[str, str], float]
    stage_end: Dict[Tuple[str, str], float]
    machine_assign: Dict[Tuple[str, str], str]
    machine_orders: Dict[Tuple[str, str], List[str]]
    ladle_assign: Dict[str, str]
    ladle_orders: Dict[str, List[str]]
    anchor_choice: Dict[str, int]
    base_makespan: float


@dataclass
class ScenarioSolveResult:
    status: str
    objective: float
    best_bound: float | None
    gap: float | None
    solve_time: float
    start: Dict[Tuple[str, str], int]


class SteelScheduler:
    def __init__(self) -> None:
        self.config = json.loads((DATA_DIR / "config.json").read_text())
        self.heats = read_csv(DATA_DIR / "heats.csv")
        self.casts = read_csv(DATA_DIR / "casts.csv")
        self.route_steps = read_csv(DATA_DIR / "route_steps.csv")
        self.machines = read_csv(DATA_DIR / "machines.csv")
        self.transitions = read_csv(DATA_DIR / "transition_rules.csv")
        self.ladles = read_csv(DATA_DIR / "ladles.csv")
        self.ladle_compat = read_csv(DATA_DIR / "ladle_grade_compatibility.csv")
        self.scenarios = read_csv(DATA_DIR / "scenarios.csv")
        self.stage_mults = read_csv(DATA_DIR / "scenario_stage_multipliers.csv")
        self.heat_delays = read_csv(DATA_DIR / "scenario_heat_delays.csv")
        self.oxygen_slots = read_csv(DATA_DIR / "oxygen_energy_slots.csv")
        self.crane_req_rows = read_csv(DATA_DIR / "crane_requirements.csv")
        self.sec_slots_rows = read_csv(DATA_DIR / "secondary_resource_slots.csv")
        self.sec_req_rows = read_csv(DATA_DIR / "secondary_resource_requirements.csv")

        self.heats_by_id = {row["heat_id"]: row for row in self.heats}
        self.casts_by_id = {row["cast_id"]: row for row in self.casts}
        self.ladles_by_id = {row["ladle_id"]: row for row in self.ladles}

        self.route_by_id: Dict[str, List[dict]] = defaultdict(list)
        for row in self.route_steps:
            self.route_by_id[row["route_id"]].append(row)
        for route_id in self.route_by_id:
            self.route_by_id[route_id].sort(key=lambda r: int(r["step_no"]))

        self.route_stage_order: Dict[str, List[str]] = {}
        self.base_duration: Dict[Tuple[str, str], float] = {}
        self.oxygen_rate: Dict[Tuple[str, str], float] = {}
        self.mw_load: Dict[Tuple[str, str], float] = {}
        for heat in self.heats:
            heat_id = heat["heat_id"]
            route = self.route_by_id[heat["route_id"]]
            self.route_stage_order[heat_id] = [step["stage"] for step in route]
            for step in route:
                key = (heat_id, step["stage"])
                self.base_duration[key] = float(step["base_duration_min"])
                self.oxygen_rate[key] = float(step["oxygen_tonnes_per_min"])
                self.mw_load[key] = float(step["mw_load"])

        self.transition = {
            (row["stage"], row["from_family"], row["to_family"]): int(row["setup_min"])
            for row in self.transitions
        }

        self.min_transfer = {
            key: int(value) for key, value in self.config["min_transfer_min"].items()
        }

        self.machines_by_stage: Dict[str, List[dict]] = defaultdict(list)
        self.machine_ready: Dict[str, int] = {}
        self.machine_stage: Dict[str, str] = {}
        self.machine_families: Dict[str, set[str]] = {}
        self.machine_width: Dict[str, Tuple[int, int]] = {}
        for row in self.machines:
            self.machines_by_stage[row["stage"]].append(row)
            self.machine_ready[row["machine_id"]] = int(row["ready_min"])
            self.machine_stage[row["machine_id"]] = row["stage"]
            self.machine_families[row["machine_id"]] = set(row["eligible_families"].split("|"))
            self.machine_width[row["machine_id"]] = (
                int(row["min_width_mm"]),
                int(row["max_width_mm"]),
            )

        self.cast_heats: Dict[str, List[str]] = defaultdict(list)
        for heat in self.heats:
            self.cast_heats[heat["cast_id"]].append(heat["heat_id"])
        for cast_id in self.cast_heats:
            self.cast_heats[cast_id].sort(
                key=lambda hid: int(self.heats_by_id[hid]["cast_sequence"])
            )

        self.caster_casts: Dict[str, List[str]] = defaultdict(list)
        for cast in self.casts:
            self.caster_casts[cast["caster_id"]].append(cast["cast_id"])
        for caster in self.caster_casts:
            self.caster_casts[caster].sort(
                key=lambda cid: int(self.casts_by_id[cid]["target_start_min"])
            )

        self.scenario_order = [row["scenario_id"] for row in self.scenarios]
        self.scenario_weight = {
            row["scenario_id"]: float(row["weight"]) for row in self.scenarios
        }
        self.duration_mult = {
            (row["scenario_id"], row["stage"]): float(row["duration_multiplier"])
            for row in self.stage_mults
        }
        self.oxygen_mult = {
            (row["scenario_id"], row["stage"]): float(row["oxygen_multiplier"])
            for row in self.stage_mults
        }
        self.release_delay = {
            (row["scenario_id"], row["heat_id"]): int(row["release_delay_min"])
            for row in self.heat_delays
        }

        self.compat = {
            (row["ladle_family"], row["grade_family"]): int(row["allowed"])
            for row in self.ladle_compat
        }
        self.ladle_ready = {
            row["ladle_id"]: int(row["ready_min"]) for row in self.ladles
        }
        self.ladle_cleanout = {
            row["ladle_id"]: int(row["cleanout_min"]) for row in self.ladles
        }
        self.ladle_maint = {
            row["ladle_id"]: (
                maybe_float(row["maintenance_start_min"]),
                maybe_float(row["maintenance_end_min"]),
            )
            for row in self.ladles
        }

        self.eligible_machines: Dict[Tuple[str, str], List[str]] = {}
        for heat in self.heats:
            heat_id = heat["heat_id"]
            width = int(heat["width_mm"])
            family = heat["grade_family"]
            for stage in self.route_stage_order[heat_id]:
                if stage == "CC":
                    self.eligible_machines[(heat_id, stage)] = [heat["caster_id"]]
                    continue
                choices = []
                for machine in self.machines_by_stage[stage]:
                    low, high = self.machine_width[machine["machine_id"]]
                    if family in self.machine_families[machine["machine_id"]] and low <= width <= high:
                        choices.append(machine["machine_id"])
                self.eligible_machines[(heat_id, stage)] = choices

        self.crane_req = {
            row["stage"]: int(row["crane_minutes"]) for row in self.crane_req_rows
        }
        self.sec_req = {
            (row["stage"], row["grade_family"]): (
                int(row["alloy_crew_minutes"]),
                int(row["lab_minutes"]),
            )
            for row in self.sec_req_rows
        }

        self.slot_rows = self.oxygen_slots
        self.slot_ids = [row["slot_id"] for row in self.slot_rows]
        self.slot_bounds = {
            row["slot_id"]: (int(float(row["start_min"])), int(float(row["end_min"])))
            for row in self.slot_rows
        }
        self.slot_crane_cap = {
            row["slot_id"]: float(row["max_crane_minutes"]) for row in self.slot_rows
        }
        self.slot_oxygen_supply = {
            row["slot_id"]: float(row["oxygen_supply_tonnes"]) for row in self.slot_rows
        }
        self.slot_oxygen_supply_tight = {
            row["slot_id"]: float(row["oxygen_supply_tonnes_OXYGEN_TIGHT"])
            for row in self.slot_rows
        }
        self.slot_price = {
            row["slot_id"]: float(row["electricity_price_per_mwh"])
            for row in self.slot_rows
        }
        self.sec_cap = {
            row["slot_id"]: (
                float(row["max_alloy_crew_minutes"]),
                float(row["max_lab_minutes"]),
                float(row["max_ladle_preheat_minutes"]),
            )
            for row in self.sec_slots_rows
        }

        self.relevant_bucket_ops = [
            (heat["heat_id"], stage)
            for heat in self.heats
            for stage in self.route_stage_order[heat["heat_id"]]
            if self.crane_req.get(stage, 0) > 0
            or self.sec_req.get((stage, heat["grade_family"]), (0, 0)) != (0, 0)
        ]

    def duration(self, scenario_id: str, heat_id: str, stage: str) -> float:
        return self.base_duration[(heat_id, stage)] * self.duration_mult[(scenario_id, stage)]

    def final_refining_stage(self, heat_id: str) -> str:
        stages = self.route_stage_order[heat_id]
        return stages[-2]

    def build_cast_schedule(self, anchors: Dict[str, int]) -> tuple[Dict[str, float], Dict[str, float], Dict[str, int]]:
        cc_start: Dict[str, float] = {}
        cc_end: Dict[str, float] = {}
        actual_first: Dict[str, int] = {}
        for caster, cast_ids in self.caster_casts.items():
            current = self.machine_ready[caster]
            prev_cast = None
            for cast_id in cast_ids:
                cast = self.casts_by_id[cast_id]
                if prev_cast is not None:
                    prev_family = self.casts_by_id[prev_cast]["primary_family"]
                    current = max(
                        current,
                        cc_end[self.cast_heats[prev_cast][-1]]
                        + self.transition[("CC", prev_family, cast["primary_family"])],
                    )
                current = max(current, anchors.get(cast_id, 0))
                actual_first[cast_id] = int(round(current))
                cursor = current
                for heat_id in self.cast_heats[cast_id]:
                    cc_start[heat_id] = cursor
                    cc_end[heat_id] = cursor + self.base_duration[(heat_id, "CC")]
                    cursor = cc_end[heat_id]
                current = cc_end[self.cast_heats[cast_id][-1]]
                prev_cast = cast_id
        return cc_start, cc_end, actual_first

    def backward_stage_assign(
        self,
        stage: str,
        deadlines: Dict[str, float],
        fixed_machine: Dict[str, str] | None = None,
    ) -> Dict[str, tuple[str, float, float]] | None:
        machine_head = {row["machine_id"]: float("inf") for row in self.machines_by_stage[stage]}
        machine_next_family = {row["machine_id"]: None for row in self.machines_by_stage[stage]}
        result: Dict[str, tuple[str, float, float]] = {}
        for heat_id in sorted(deadlines, key=lambda hid: deadlines[hid], reverse=True):
            family = self.heats_by_id[heat_id]["grade_family"]
            duration = self.base_duration[(heat_id, stage)]
            best = None
            machine_choices = (
                [fixed_machine[heat_id]]
                if fixed_machine is not None
                else self.eligible_machines[(heat_id, stage)]
            )
            for machine_id in machine_choices:
                next_family = machine_next_family[machine_id]
                setup = 0 if next_family is None else self.transition[(stage, family, next_family)]
                end = min(deadlines[heat_id] + duration, machine_head[machine_id] - setup)
                start = end - duration
                earliest = self.machine_ready[machine_id]
                if stage == "BOF":
                    earliest = max(
                        earliest,
                        int(self.heats_by_id[heat_id]["release_min"]),
                    )
                if start >= earliest - 1e-9:
                    candidate = (machine_id, start, end)
                    if best is None or start > best[1]:
                        best = candidate
            if best is None:
                return None
            result[heat_id] = best
            machine_head[best[0]] = best[1]
            machine_next_family[best[0]] = family
        return result

    def assign_ladles(
        self,
        bof_start: Dict[str, float],
        cc_end: Dict[str, float],
    ) -> tuple[Dict[str, str], Dict[str, List[str]]] | None:
        intervals = sorted((bof_start[hid], cc_end[hid], hid) for hid in self.heats_by_id)
        ladle_intervals: Dict[str, List[tuple[float, float, str]]] = defaultdict(list)
        ladle_assign: Dict[str, str] = {}
        for start, cc_finish, heat_id in intervals:
            heat = self.heats_by_id[heat_id]
            family = heat["ladle_family"]
            grade = heat["grade_family"]
            weight = float(heat["weight_tonnes"])
            candidates = []
            for ladle_id, ladle in self.ladles_by_id.items():
                if ladle["ladle_family"] != family:
                    continue
                if self.compat[(ladle["ladle_family"], grade)] != 1:
                    continue
                if float(ladle["capacity_tonnes"]) + 1e-9 < weight:
                    continue
                cleanout = self.ladle_cleanout[ladle_id]
                end = cc_finish + cleanout
                if start < self.ladle_ready[ladle_id] - 1e-9:
                    continue
                maint_start, maint_end = self.ladle_maint[ladle_id]
                if maint_start is not None and not (end <= maint_start + 1e-9 or start >= maint_end - 1e-9):
                    continue
                ok = True
                prev_end = float(self.ladle_ready[ladle_id])
                for used_start, used_end, _ in ladle_intervals[ladle_id]:
                    if not (end <= used_start + 1e-9 or start >= used_end - 1e-9):
                        ok = False
                        break
                    if used_end <= start:
                        prev_end = max(prev_end, used_end)
                if ok:
                    candidates.append((abs(start - prev_end), -prev_end, ladle_id, end))
            if not candidates:
                return None
            candidates.sort()
            _, _, ladle_id, end = candidates[0]
            ladle_assign[heat_id] = ladle_id
            ladle_intervals[ladle_id].append((start, end, heat_id))
            ladle_intervals[ladle_id].sort()
        ladle_orders = {
            ladle_id: [heat_id for _, _, heat_id in intervals]
            for ladle_id, intervals in ladle_intervals.items()
        }
        return ladle_assign, ladle_orders

    def try_base_commitment(self, anchors: Dict[str, int]) -> BaseCommitment | None:
        cc_start, cc_end, actual_first = self.build_cast_schedule(anchors)
        makespan = max(cc_end.values())
        if makespan > self.config["planning_horizon_min"] + 1e-9:
            return None

        rh_deadlines = {
            heat_id: cc_start[heat_id]
            - self.min_transfer["RH:CC"]
            - self.base_duration[(heat_id, "RH")]
            for heat_id in self.heats_by_id
            if "RH" in self.route_stage_order[heat_id]
        }
        rh_assign = self.backward_stage_assign("RH", rh_deadlines)
        if rh_assign is None:
            return None

        lf_deadlines = {}
        for heat_id in self.heats_by_id:
            if "LF" not in self.route_stage_order[heat_id]:
                continue
            if heat_id in rh_assign:
                lf_deadlines[heat_id] = (
                    rh_assign[heat_id][1]
                    - self.min_transfer["LF:RH"]
                    - self.base_duration[(heat_id, "LF")]
                )
            else:
                lf_deadlines[heat_id] = (
                    cc_start[heat_id]
                    - self.min_transfer["LF:CC"]
                    - self.base_duration[(heat_id, "LF")]
                )
        lf_assign = self.backward_stage_assign("LF", lf_deadlines)
        if lf_assign is None:
            return None

        cas_deadlines = {
            heat_id: lf_assign[heat_id][1]
            - self.min_transfer["CAS:LF"]
            - self.base_duration[(heat_id, "CAS")]
            for heat_id in self.heats_by_id
            if "CAS" in self.route_stage_order[heat_id]
        }
        cas_assign = self.backward_stage_assign("CAS", cas_deadlines)
        if cas_assign is None:
            return None

        bof_deadlines = {}
        for heat_id in self.heats_by_id:
            if heat_id in cas_assign:
                bof_deadlines[heat_id] = (
                    cas_assign[heat_id][1]
                    - self.min_transfer["BOF:CAS"]
                    - self.base_duration[(heat_id, "BOF")]
                )
            else:
                bof_deadlines[heat_id] = (
                    lf_assign[heat_id][1]
                    - self.min_transfer["BOF:LF"]
                    - self.base_duration[(heat_id, "BOF")]
                )
        bof_assign = self.backward_stage_assign("BOF", bof_deadlines)
        if bof_assign is None:
            return None

        ladle_result = self.assign_ladles(
            {heat_id: bof_assign[heat_id][1] for heat_id in self.heats_by_id},
            cc_end,
        )
        if ladle_result is None:
            return None
        ladle_assign, ladle_orders = ladle_result

        stage_start = {}
        stage_end = {}
        machine_assign = {}
        machine_orders: Dict[Tuple[str, str], List[str]] = defaultdict(list)

        for heat_id in self.heats_by_id:
            stage_start[(heat_id, "CC")] = cc_start[heat_id]
            stage_end[(heat_id, "CC")] = cc_end[heat_id]
            machine_assign[(heat_id, "CC")] = self.heats_by_id[heat_id]["caster_id"]
        for assign_map, stage in [
            (rh_assign, "RH"),
            (lf_assign, "LF"),
            (cas_assign, "CAS"),
            (bof_assign, "BOF"),
        ]:
            for heat_id, (machine_id, start, end) in assign_map.items():
                stage_start[(heat_id, stage)] = start
                stage_end[(heat_id, stage)] = end
                machine_assign[(heat_id, stage)] = machine_id

        for stage in ["BOF", "LF", "RH", "CAS", "CC"]:
            grouped: Dict[str, List[str]] = defaultdict(list)
            for heat_id in self.heats_by_id:
                if stage not in self.route_stage_order[heat_id]:
                    continue
                machine_id = machine_assign[(heat_id, stage)]
                grouped[machine_id].append(heat_id)
            for machine_id, heat_ids in grouped.items():
                heat_ids.sort(key=lambda hid: stage_start[(hid, stage)])
                machine_orders[(stage, machine_id)] = heat_ids

        return BaseCommitment(
            cast_first_start=actual_first,
            stage_start=stage_start,
            stage_end=stage_end,
            machine_assign=machine_assign,
            machine_orders=dict(machine_orders),
            ladle_assign=ladle_assign,
            ladle_orders=ladle_orders,
            anchor_choice=anchors,
            base_makespan=makespan,
        )

    def choose_base_commitment(self) -> BaseCommitment:
        best: BaseCommitment | None = None
        best_key = None
        for a1 in range(100, 211, 10):
            for b1 in range(120, 231, 10):
                for b2 in range(500, 701, 10):
                    anchors = {
                        "CAST_A1": a1,
                        "CAST_A2": 0,
                        "CAST_A3": 0,
                        "CAST_A4": 0,
                        "CAST_B1": b1,
                        "CAST_B2": b2,
                        "CAST_B3": 0,
                        "CAST_B4": 0,
                    }
                    candidate = self.try_base_commitment(anchors)
                    if candidate is None:
                        continue
                    key = (candidate.base_makespan, b2, a1, b1)
                    if best is None or key < best_key:
                        best = candidate
                        best_key = key
        if best is None:
            raise RuntimeError("Failed to build a feasible base commitment.")
        return best

    def scenario_time_limit(
        self, started_at: float, total_limit: float | None, remaining_models: int
    ) -> float | None:
        if total_limit is None:
            return 60.0
        elapsed = time.time() - started_at
        remaining = total_limit - elapsed
        if remaining <= 5.0:
            return max(1.0, remaining)
        return max(5.0, min(60.0, remaining / max(1, remaining_models)))

    def solve_scenario(
        self,
        base: BaseCommitment,
        scenario_id: str,
        time_limit: float | None,
        start_hint: Dict[Tuple[str, str], int] | None = None,
    ) -> ScenarioSolveResult:
        model = Model()
        model.setParam("display/verblevel", 0)
        model.setParam("limits/gap", 0.0005)
        model.setParam("randomization/randomseedshift", 0)
        if time_limit is not None:
            model.setParam("limits/time", float(time_limit))

        start_var: Dict[Tuple[str, str], object] = {}
        tardiness_var: Dict[str, object] = {}
        temp_var: Dict[str, object] = {}
        gap_var: Dict[Tuple[str, str], object] = {}
        bucket_var: Dict[Tuple[str, str, str], object] = {}

        for heat in self.heats:
            heat_id = heat["heat_id"]
            for stage in self.route_stage_order[heat_id]:
                start_var[(heat_id, stage)] = model.addVar(
                    vtype="I",
                    lb=0,
                    name=f"start[{heat_id},{stage}]",
                )
            tardiness_var[heat_id] = model.addVar(lb=0, name=f"tard[{heat_id}]")
            temp_var[heat_id] = model.addVar(lb=0, name=f"temp[{heat_id}]")

        for heat_id, stage in self.relevant_bucket_ops:
            binaries = []
            for slot_id in self.slot_ids:
                var = model.addVar(vtype="B", name=f"z[{heat_id},{stage},{slot_id}]")
                bucket_var[(heat_id, stage, slot_id)] = var
                binaries.append(var)
            model.addCons(quicksum(binaries) == 1, name=f"bucket_pick[{heat_id},{stage}]")
            big_m = self.config["planning_horizon_min"]
            for slot_id in self.slot_ids:
                slot_start, slot_end = self.slot_bounds[slot_id]
                var = bucket_var[(heat_id, stage, slot_id)]
                model.addCons(
                    start_var[(heat_id, stage)] >= slot_start - big_m * (1 - var),
                    name=f"bucket_lb[{heat_id},{stage},{slot_id}]",
                )
                model.addCons(
                    start_var[(heat_id, stage)] <= (slot_end - 1) + big_m * (1 - var),
                    name=f"bucket_ub[{heat_id},{stage},{slot_id}]",
                )

        for slot_id in self.slot_ids:
            model.addCons(
                quicksum(
                    self.crane_req.get(stage, 0) * bucket_var[(heat_id, stage, slot_id)]
                    for heat_id, stage in self.relevant_bucket_ops
                )
                <= self.slot_crane_cap[slot_id],
                name=f"crane_cap[{slot_id}]",
            )
            model.addCons(
                quicksum(
                    self.sec_req.get((stage, self.heats_by_id[heat_id]["grade_family"]), (0, 0))[0]
                    * bucket_var[(heat_id, stage, slot_id)]
                    for heat_id, stage in self.relevant_bucket_ops
                )
                <= self.sec_cap[slot_id][0],
                name=f"alloy_cap[{slot_id}]",
            )
            model.addCons(
                quicksum(
                    self.sec_req.get((stage, self.heats_by_id[heat_id]["grade_family"]), (0, 0))[1]
                    * bucket_var[(heat_id, stage, slot_id)]
                    for heat_id, stage in self.relevant_bucket_ops
                )
                <= self.sec_cap[slot_id][1],
                name=f"lab_cap[{slot_id}]",
            )

        for heat in self.heats:
            heat_id = heat["heat_id"]
            model.addCons(
                start_var[(heat_id, "BOF")]
                >= int(heat["release_min"]) + self.release_delay[(scenario_id, heat_id)],
                name=f"release[{heat_id}]",
            )
            model.addCons(
                start_var[(heat_id, "BOF")] >= self.ladle_ready[base.ladle_assign[heat_id]],
                name=f"ladle_ready[{heat_id}]",
            )
            for stage in self.route_stage_order[heat_id]:
                machine_id = base.machine_assign[(heat_id, stage)]
                model.addCons(
                    start_var[(heat_id, stage)] >= self.machine_ready[machine_id],
                    name=f"machine_ready[{heat_id},{stage}]",
                )
            route = self.route_stage_order[heat_id]
            for stage_a, stage_b in zip(route, route[1:]):
                model.addCons(
                    start_var[(heat_id, stage_b)]
                    >= start_var[(heat_id, stage_a)]
                    + self.duration(scenario_id, heat_id, stage_a)
                    + self.min_transfer[f"{stage_a}:{stage_b}"],
                    name=f"route[{heat_id},{stage_a},{stage_b}]",
                )

        for (stage, machine_id), ordered_heats in base.machine_orders.items():
            for heat_i, heat_j in zip(ordered_heats, ordered_heats[1:]):
                setup = self.transition[
                    (
                        stage,
                        self.heats_by_id[heat_i]["grade_family"],
                        self.heats_by_id[heat_j]["grade_family"],
                    )
                ]
                model.addCons(
                    start_var[(heat_j, stage)]
                    >= start_var[(heat_i, stage)]
                    + self.duration(scenario_id, heat_i, stage)
                    + setup,
                    name=f"machine_seq[{stage},{machine_id},{heat_i},{heat_j}]",
                )

        for cast_id, heat_ids in self.cast_heats.items():
            model.addCons(
                start_var[(heat_ids[0], "CC")] >= base.cast_first_start[cast_id],
                name=f"cast_anchor[{cast_id}]",
            )
            cast = self.casts_by_id[cast_id]
            for heat_i, heat_j in zip(heat_ids, heat_ids[1:]):
                gap = model.addVar(lb=0, name=f"gap[{heat_i},{heat_j}]")
                gap_var[(heat_i, heat_j)] = gap
                model.addCons(
                    start_var[(heat_j, "CC")]
                    >= start_var[(heat_i, "CC")] + self.duration(scenario_id, heat_i, "CC"),
                    name=f"cast_seq_lb[{heat_i},{heat_j}]",
                )
                model.addCons(
                    start_var[(heat_j, "CC")]
                    <= start_var[(heat_i, "CC")]
                    + self.duration(scenario_id, heat_i, "CC")
                    + int(cast["max_gap_min"]),
                    name=f"cast_seq_ub[{heat_i},{heat_j}]",
                )
                model.addCons(
                    gap
                    >= start_var[(heat_j, "CC")]
                    - (
                        start_var[(heat_i, "CC")]
                        + self.duration(scenario_id, heat_i, "CC")
                        + int(cast["target_gap_min"])
                    ),
                    name=f"cast_gap_pen[{heat_i},{heat_j}]",
                )

        for ladle_id, ordered_heats in base.ladle_orders.items():
            cleanout = self.ladle_cleanout[ladle_id]
            for heat_i, heat_j in zip(ordered_heats, ordered_heats[1:]):
                model.addCons(
                    start_var[(heat_j, "BOF")]
                    >= start_var[(heat_i, "CC")]
                    + self.duration(scenario_id, heat_i, "CC")
                    + cleanout,
                    name=f"ladle_seq[{ladle_id},{heat_i},{heat_j}]",
                )

        for heat_id, ladle_id in base.ladle_assign.items():
            maint_start, maint_end = self.ladle_maint[ladle_id]
            if maint_start is None:
                continue
            base_bof = base.stage_start[(heat_id, "BOF")]
            base_end = base.stage_end[(heat_id, "CC")] + self.ladle_cleanout[ladle_id]
            if base_bof >= maint_end:
                model.addCons(
                    start_var[(heat_id, "BOF")] >= maint_end,
                    name=f"maint_after[{heat_id}]",
                )
            elif base_end <= maint_start:
                model.addCons(
                    start_var[(heat_id, "CC")]
                    + self.duration(scenario_id, heat_id, "CC")
                    + self.ladle_cleanout[ladle_id]
                    <= maint_start,
                    name=f"maint_before[{heat_id}]",
                )

        objective_terms = []
        for heat in self.heats:
            heat_id = heat["heat_id"]
            final_stage = self.final_refining_stage(heat_id)
            final_duration = self.duration(scenario_id, heat_id, final_stage)
            cc_end_expr = start_var[(heat_id, "CC")] + self.duration(scenario_id, heat_id, "CC")
            model.addCons(
                tardiness_var[heat_id] >= cc_end_expr - int(heat["due_min"]),
                name=f"tardiness_def[{heat_id}]",
            )
            transfer = self.min_transfer[f"{final_stage}:CC"]
            wait_expr = (
                start_var[(heat_id, "CC")]
                - (start_var[(heat_id, final_stage)] + final_duration + transfer)
            )
            allowed_wait = float(heat["min_superheat_c"]) / float(heat["temp_drop_c_per_min"])
            model.addCons(
                temp_var[heat_id]
                >= float(heat["temp_drop_c_per_min"]) * (wait_expr - allowed_wait),
                name=f"temp_def[{heat_id}]",
            )
            model.addCons(
                start_var[(heat_id, "CC")]
                - (start_var[(heat_id, final_stage)] + final_duration)
                <= self.config["max_wait_to_cast_min"],
                name=f"max_wait[{heat_id}]",
            )
            objective_terms.append(
                int(heat["priority"])
                * self.config["tardiness_penalty_per_priority_min"]
                * tardiness_var[heat_id]
            )
            objective_terms.append(
                self.config["temperature_loss_penalty_per_c"] * temp_var[heat_id]
            )
            objective_terms.append(
                0.05 * (start_var[(heat_id, "CC")] - start_var[(heat_id, "BOF")])
            )

        for gap in gap_var.values():
            objective_terms.append(self.config["caster_break_penalty_per_min"] * gap)

        model.setObjective(quicksum(objective_terms), "minimize")

        if start_hint is not None:
            sol = model.createSol()
            for heat in self.heats:
                heat_id = heat["heat_id"]
                for stage in self.route_stage_order[heat_id]:
                    key = (heat_id, stage)
                    if key not in start_hint:
                        continue
                    value = int(start_hint[key])
                    model.setSolVal(sol, start_var[key], value)
                    if (heat_id, stage) in self.relevant_bucket_ops:
                        for slot_id in self.slot_ids:
                            slot_start, slot_end = self.slot_bounds[slot_id]
                            bucket_value = 1 if slot_start <= value < slot_end else 0
                            model.setSolVal(
                                sol,
                                bucket_var[(heat_id, stage, slot_id)],
                                bucket_value,
                            )
            model.addSol(sol, free=True)

        t0 = time.time()
        model.optimize()
        solve_time = time.time() - t0

        if model.getNSols() <= 0:
            raise RuntimeError(f"No feasible solution found for scenario {scenario_id}.")

        best_sol = model.getBestSol()
        start = {}
        for heat in self.heats:
            heat_id = heat["heat_id"]
            for stage in self.route_stage_order[heat_id]:
                value = model.getSolVal(best_sol, start_var[(heat_id, stage)])
                rounded = int(round(value))
                if abs(value - rounded) > 1e-6:
                    raise RuntimeError(f"Non-integral start for {(scenario_id, heat_id, stage)}: {value}")
                start[(heat_id, stage)] = rounded

        best_bound = None
        try:
            best_bound = model.getDualbound()
        except Exception:
            best_bound = None
        gap = None
        try:
            gap = model.getGap()
        except Exception:
            gap = None

        return ScenarioSolveResult(
            status=str(model.getStatus()),
            objective=float(model.getSolObjVal(best_sol)),
            best_bound=float(best_bound) if best_bound is not None and math.isfinite(best_bound) else None,
            gap=float(gap) if gap is not None and math.isfinite(gap) else None,
            solve_time=solve_time,
            start=start,
        )

    def build_solution_rows(
        self,
        base: BaseCommitment,
        scenario_results: Dict[str, ScenarioSolveResult],
    ) -> List[dict]:
        rows = []
        for scenario_id in self.scenario_order:
            starts = scenario_results[scenario_id].start
            for heat in self.heats:
                heat_id = heat["heat_id"]
                ladle_id = base.ladle_assign[heat_id]
                for stage in self.route_stage_order[heat_id]:
                    rows.append(
                        {
                            "row_type": "operation",
                            "scenario_id": scenario_id,
                            "heat_id": heat_id,
                            "stage": stage,
                            "machine_id": base.machine_assign[(heat_id, stage)],
                            "start_min": str(starts[(heat_id, stage)]),
                            "ladle_id": ladle_id,
                            "cast_id": heat["cast_id"],
                            "caster_id": heat["caster_id"],
                            "oxygen_mode": "normal",
                            "notes": "",
                        }
                    )
        return rows

    def write_solution(self, rows: List[dict]) -> None:
        fieldnames = [
            "row_type",
            "scenario_id",
            "heat_id",
            "stage",
            "machine_id",
            "start_min",
            "ladle_id",
            "cast_id",
            "caster_id",
            "oxygen_mode",
            "notes",
        ]
        with SOLUTION_PATH.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def audit_solution(self, solution_path: Path) -> dict:
        rows = read_csv(solution_path)
        errors: List[str] = []
        start_map: Dict[Tuple[str, str, str], int] = {}
        machine_commit: Dict[Tuple[str, str], str] = {}
        ladle_commit: Dict[str, str] = {}

        expected_rows = sum(len(self.route_stage_order[heat["heat_id"]]) for heat in self.heats) * len(
            self.scenario_order
        )
        if len(rows) != expected_rows:
            errors.append(f"Row count mismatch: got {len(rows)}, expected {expected_rows}.")

        seen = set()
        for row in rows:
            key = (row["scenario_id"], row["heat_id"], row["stage"])
            if key in seen:
                errors.append(f"Duplicate row for {key}.")
                continue
            seen.add(key)
            if row["row_type"] != "operation":
                errors.append(f"Invalid row_type for {key}: {row['row_type']}.")
            try:
                start = int(row["start_min"])
            except ValueError:
                errors.append(f"Non-integer start_min for {key}: {row['start_min']}.")
                continue
            start_map[key] = start
            heat = self.heats_by_id.get(row["heat_id"])
            if heat is None:
                errors.append(f"Unknown heat {row['heat_id']}.")
                continue
            if row["stage"] not in self.route_stage_order[heat["heat_id"]]:
                errors.append(f"Invalid stage for {key}.")
            if row["cast_id"] != heat["cast_id"] or row["caster_id"] != heat["caster_id"]:
                errors.append(f"Cast/caster mismatch for {key}.")
            if row["oxygen_mode"] not in {"normal", "cutback"}:
                errors.append(f"Invalid oxygen_mode for {key}.")
            if row["stage"] != "BOF" and row["oxygen_mode"] != "normal":
                errors.append(f"Non-BOF cutback is invalid for {key}.")
            commit_key = (row["heat_id"], row["stage"])
            if commit_key in machine_commit and machine_commit[commit_key] != row["machine_id"]:
                errors.append(f"Machine changes across scenarios for {commit_key}.")
            else:
                machine_commit[commit_key] = row["machine_id"]
            if row["heat_id"] in ladle_commit and ladle_commit[row["heat_id"]] != row["ladle_id"]:
                errors.append(f"Ladle changes across scenarios for {row['heat_id']}.")
            else:
                ladle_commit[row["heat_id"]] = row["ladle_id"]

        for scenario_id in self.scenario_order:
            for heat in self.heats:
                heat_id = heat["heat_id"]
                for stage in self.route_stage_order[heat_id]:
                    if (scenario_id, heat_id, stage) not in start_map:
                        errors.append(f"Missing row for {(scenario_id, heat_id, stage)}.")

        hard_group_flags = {
            "eligibility": False,
            "route": False,
            "machines": False,
            "casts": False,
            "ladles": False,
            "buckets": False,
        }
        max_residual = 0.0
        scenario_scores = {}
        scenario_metrics = {}

        for scenario_id in self.scenario_order:
            machine_ops: Dict[str, List[tuple[float, float, str, str]]] = defaultdict(list)
            ladle_ops: Dict[str, List[tuple[float, float, str]]] = defaultdict(list)

            tardiness_cost = 0.0
            temp_cost = 0.0
            cast_gap_cost = 0.0
            energy_cost = 0.0
            oxygen_shortage_cost = 0.0
            oxygen_vent_cost = 0.0
            preheat_cost = 0.0

            for heat in self.heats:
                heat_id = heat["heat_id"]
                route = self.route_stage_order[heat_id]
                starts = {stage: start_map[(scenario_id, heat_id, stage)] for stage in route}

                for stage in route:
                    machine_id = machine_commit[(heat_id, stage)]
                    if stage != self.machine_stage[machine_id]:
                        errors.append(f"Machine stage mismatch for {(scenario_id, heat_id, stage)}.")
                        hard_group_flags["eligibility"] = True
                    low, high = self.machine_width[machine_id]
                    if (
                        int(heat["width_mm"]) < low
                        or int(heat["width_mm"]) > high
                        or heat["grade_family"] not in self.machine_families[machine_id]
                    ):
                        errors.append(f"Machine eligibility violation for {(scenario_id, heat_id, stage)}.")
                        hard_group_flags["eligibility"] = True

                    duration = self.duration(scenario_id, heat_id, stage)
                    machine_ops[machine_id].append((starts[stage], starts[stage] + duration, heat_id, stage))

                if starts["BOF"] < int(heat["release_min"]) + self.release_delay[(scenario_id, heat_id)] - 1e-9:
                    residual = (
                        int(heat["release_min"])
                        + self.release_delay[(scenario_id, heat_id)]
                        - starts["BOF"]
                    ) / max(1.0, int(heat["release_min"]) + self.release_delay[(scenario_id, heat_id)])
                    max_residual = max(max_residual, residual)
                    errors.append(f"Release violation for {(scenario_id, heat_id)}.")
                    hard_group_flags["route"] = True

                for stage_a, stage_b in zip(route, route[1:]):
                    lhs = starts[stage_b]
                    rhs = starts[stage_a] + self.duration(scenario_id, heat_id, stage_a) + self.min_transfer[f"{stage_a}:{stage_b}"]
                    if lhs + 1e-9 < rhs:
                        residual = (rhs - lhs) / max(1.0, rhs)
                        max_residual = max(max_residual, residual)
                        errors.append(f"Route precedence violation for {(scenario_id, heat_id, stage_a, stage_b)}.")
                        hard_group_flags["route"] = True

                final_stage = self.final_refining_stage(heat_id)
                final_end = starts[final_stage] + self.duration(scenario_id, heat_id, final_stage)
                final_wait = starts["CC"] - final_end
                if final_wait > self.config["max_wait_to_cast_min"] + 1e-9:
                    residual = (final_wait - self.config["max_wait_to_cast_min"]) / self.config["max_wait_to_cast_min"]
                    max_residual = max(max_residual, residual)
                    errors.append(f"Max wait violation for {(scenario_id, heat_id)}.")
                    hard_group_flags["route"] = True

                transfer_wait = starts["CC"] - (
                    final_end + self.min_transfer[f"{final_stage}:CC"]
                )
                allowed_wait = float(heat["min_superheat_c"]) / float(heat["temp_drop_c_per_min"])
                temp_excess = max(0.0, float(heat["temp_drop_c_per_min"]) * (transfer_wait - allowed_wait))
                temp_cost += self.config["temperature_loss_penalty_per_c"] * temp_excess

                cc_end = starts["CC"] + self.duration(scenario_id, heat_id, "CC")
                tardiness = max(0.0, cc_end - int(heat["due_min"]))
                tardiness_cost += (
                    self.config["tardiness_penalty_per_priority_min"]
                    * int(heat["priority"])
                    * tardiness
                )

                ladle_id = ladle_commit[heat_id]
                ladle = self.ladles_by_id[ladle_id]
                if ladle["ladle_family"] != heat["ladle_family"]:
                    errors.append(f"Ladle family violation for {(scenario_id, heat_id)}.")
                    hard_group_flags["ladles"] = True
                if self.compat[(ladle["ladle_family"], heat["grade_family"])] != 1:
                    errors.append(f"Ladle compatibility violation for {(scenario_id, heat_id)}.")
                    hard_group_flags["ladles"] = True
                if float(ladle["capacity_tonnes"]) + 1e-9 < float(heat["weight_tonnes"]):
                    errors.append(f"Ladle capacity violation for {(scenario_id, heat_id)}.")
                    hard_group_flags["ladles"] = True
                cleanout = self.ladle_cleanout[ladle_id]
                ladle_ops[ladle_id].append((starts["BOF"], cc_end + cleanout, heat_id))

            for machine_id, ops in machine_ops.items():
                ops.sort()
                for (start_i, end_i, heat_i, stage_i), (start_j, end_j, heat_j, stage_j) in zip(ops, ops[1:]):
                    if stage_i != stage_j:
                        errors.append(f"Machine {machine_id} mixes stages unexpectedly.")
                        hard_group_flags["machines"] = True
                        continue
                    setup = self.transition[
                        (
                            stage_i,
                            self.heats_by_id[heat_i]["grade_family"],
                            self.heats_by_id[heat_j]["grade_family"],
                        )
                    ]
                    rhs = end_i + setup
                    if start_j + 1e-9 < rhs:
                        residual = (rhs - start_j) / max(1.0, rhs)
                        max_residual = max(max_residual, residual)
                        errors.append(f"Machine overlap/setup violation for {(scenario_id, machine_id, heat_i, heat_j)}.")
                        hard_group_flags["machines"] = True

            for cast_id, heat_ids in self.cast_heats.items():
                cast = self.casts_by_id[cast_id]
                for heat_i, heat_j in zip(heat_ids, heat_ids[1:]):
                    start_i = start_map[(scenario_id, heat_i, "CC")]
                    start_j = start_map[(scenario_id, heat_j, "CC")]
                    end_i = start_i + self.duration(scenario_id, heat_i, "CC")
                    gap = start_j - end_i
                    if gap < -1e-9 or gap > int(cast["max_gap_min"]) + 1e-9:
                        residual = 0.0
                        if gap < 0:
                            residual = (-gap) / max(1.0, end_i)
                        else:
                            residual = (gap - int(cast["max_gap_min"])) / max(1.0, int(cast["max_gap_min"]))
                        max_residual = max(max_residual, residual)
                        errors.append(f"Cast gap violation for {(scenario_id, heat_i, heat_j)}.")
                        hard_group_flags["casts"] = True
                    cast_gap_cost += self.config["caster_break_penalty_per_min"] * max(
                        0.0, gap - int(cast["target_gap_min"])
                    )

            slot_load = {
                slot_id: {"crane": 0.0, "alloy": 0.0, "lab": 0.0, "preheat": 0.0, "oxygen": 0.0, "energy": 0.0}
                for slot_id in self.slot_ids
            }

            for heat in self.heats:
                heat_id = heat["heat_id"]
                grade = heat["grade_family"]
                for stage in self.route_stage_order[heat_id]:
                    start = start_map[(scenario_id, heat_id, stage)]
                    duration = self.duration(scenario_id, heat_id, stage)
                    for slot_id in self.slot_ids:
                        slot_start, slot_end = self.slot_bounds[slot_id]
                        if slot_start <= start < slot_end:
                            slot_load[slot_id]["crane"] += self.crane_req.get(stage, 0)
                            alloy, lab = self.sec_req.get((stage, grade), (0, 0))
                            slot_load[slot_id]["alloy"] += alloy
                            slot_load[slot_id]["lab"] += lab
                        overlap = max(
                            0.0,
                            min(start + duration, slot_end) - max(start, slot_start),
                        )
                        if overlap <= 0:
                            continue
                        slot_load[slot_id]["energy"] += (
                            overlap / 60.0 * self.mw_load[(heat_id, stage)]
                        )
                        if stage in {"BOF", "RH"}:
                            slot_load[slot_id]["oxygen"] += (
                                overlap
                                * self.oxygen_rate[(heat_id, stage)]
                                * self.oxygen_mult[(scenario_id, stage)]
                            )

            for ladle_id, ops in ladle_ops.items():
                ops.sort()
                maint_start, maint_end = self.ladle_maint[ladle_id]
                if ops and ops[0][0] < self.ladle_ready[ladle_id] - 1e-9:
                    errors.append(f"Ladle ready-time violation for {(scenario_id, ladle_id)}.")
                    hard_group_flags["ladles"] = True
                for start, end, heat_id in ops:
                    if maint_start is not None and not (end <= maint_start + 1e-9 or start >= maint_end - 1e-9):
                        errors.append(f"Ladle maintenance violation for {(scenario_id, ladle_id, heat_id)}.")
                        hard_group_flags["ladles"] = True
                for (start_i, end_i, heat_i), (start_j, end_j, heat_j) in zip(ops, ops[1:]):
                    if start_j + 1e-9 < end_i:
                        residual = (end_i - start_j) / max(1.0, end_i)
                        max_residual = max(max_residual, residual)
                        errors.append(f"Ladle overlap violation for {(scenario_id, ladle_id, heat_i, heat_j)}.")
                        hard_group_flags["ladles"] = True
                    idle = start_j - end_i
                    if idle > self.config["max_hot_idle_before_preheat_min"] + 1e-9:
                        preheat = (
                            self.config["base_ladle_preheat_min"]
                            + self.config["extra_preheat_per_idle_min"]
                            * (idle - self.config["max_hot_idle_before_preheat_min"])
                        )
                        preheat_cost += self.config["ladle_preheat_penalty_per_min"] * preheat
                        pre_start = start_j - preheat
                        for slot_id in self.slot_ids:
                            slot_start, slot_end = self.slot_bounds[slot_id]
                            overlap = max(
                                0.0, min(start_j, slot_end) - max(pre_start, slot_start)
                            )
                            if overlap > 0:
                                slot_load[slot_id]["preheat"] += overlap

            for slot_id in self.slot_ids:
                crane = slot_load[slot_id]["crane"]
                alloy = slot_load[slot_id]["alloy"]
                lab = slot_load[slot_id]["lab"]
                preheat = slot_load[slot_id]["preheat"]
                crane_cap = self.slot_crane_cap[slot_id]
                alloy_cap, lab_cap, preheat_cap = self.sec_cap[slot_id]

                for load, cap, name in [
                    (crane, crane_cap, "crane"),
                    (alloy, alloy_cap, "alloy"),
                    (lab, lab_cap, "lab"),
                    (preheat, preheat_cap, "preheat"),
                ]:
                    if load > cap + 1e-6:
                        residual = (load - cap) / max(1.0, cap)
                        max_residual = max(max_residual, residual)
                        errors.append(f"{name} bucket violation in {(scenario_id, slot_id)}: {load} > {cap}.")
                        hard_group_flags["buckets"] = True

                oxygen_supply = (
                    self.slot_oxygen_supply_tight[slot_id]
                    if scenario_id == "OXYGEN_TIGHT"
                    else self.slot_oxygen_supply[slot_id]
                )
                oxygen = slot_load[slot_id]["oxygen"]
                shortage = max(0.0, oxygen - oxygen_supply)
                vent = max(0.0, oxygen_supply - oxygen)
                oxygen_shortage_cost += (
                    self.config["oxygen_shortage_penalty_per_tonne"] * shortage
                )
                oxygen_vent_cost += self.config["oxygen_vent_penalty_per_tonne"] * vent
                energy_cost += (
                    slot_load[slot_id]["energy"]
                    * self.slot_price[slot_id]
                    * self.config["electricity_cost_scale"]
                )

            scenario_score = (
                tardiness_cost
                + temp_cost
                + cast_gap_cost
                + energy_cost
                + oxygen_shortage_cost
                + oxygen_vent_cost
                + preheat_cost
            )
            scenario_scores[scenario_id] = scenario_score
            scenario_metrics[scenario_id] = {
                "tardiness_cost": tardiness_cost,
                "temp_cost": temp_cost,
                "cast_gap_cost": cast_gap_cost,
                "energy_cost": energy_cost,
                "oxygen_shortage_cost": oxygen_shortage_cost,
                "oxygen_vent_cost": oxygen_vent_cost,
                "preheat_cost": preheat_cost,
                "score": scenario_score,
            }

        weighted_score = 0.0
        for scenario_id in self.scenario_order:
            weighted_score += (
                self.scenario_weight[scenario_id]
                * scenario_scores[scenario_id]
                * self.config["scenario_risk_weight_scale"]
            )

        violated_groups = sum(1 for flagged in hard_group_flags.values() if flagged)
        return {
            "errors": errors,
            "max_normalized_residual": max_residual,
            "violated_groups": violated_groups,
            "weighted_score": weighted_score,
            "scenario_metrics": scenario_metrics,
        }

    def write_log(
        self,
        base: BaseCommitment,
        scenario_results: Dict[str, ScenarioSolveResult],
        audit: dict,
        command: str,
    ) -> None:
        lines = [
            "# Solve Log",
            "",
            "## Commands",
            f"- `{command}`",
            "",
            "## Base Commitment",
            f"- anchor search choice: {base.anchor_choice}",
            f"- actual cast first starts: {base.cast_first_start}",
            f"- base makespan: {base.base_makespan:.8f}",
            f"- committed ladles used: {len(set(base.ladle_assign.values()))}",
            "",
            "## SCIP Runs",
        ]
        for scenario_id in self.scenario_order:
            result = scenario_results[scenario_id]
            lines.append(
                "- "
                + f"{scenario_id}: status={result.status}, obj={result.objective:.8f}, "
                + f"best_bound={result.best_bound if result.best_bound is not None else 'n/a'}, "
                + f"gap={result.gap if result.gap is not None else 'n/a'}, "
                + f"solve_time_sec={result.solve_time:.4f}"
            )
        lines.extend(
            [
                "",
                "## Validation",
                f"- hard errors: {len(audit['errors'])}",
                f"- violated hard-constraint groups: {audit['violated_groups']}",
                f"- max normalized hard residual: {audit['max_normalized_residual']:.8f}",
                f"- weighted public-data score estimate: {audit['weighted_score']:.8f}",
                "",
                "## Scenario Metrics",
            ]
        )
        for scenario_id in self.scenario_order:
            metrics = audit["scenario_metrics"][scenario_id]
            lines.append(
                "- "
                + f"{scenario_id}: score={metrics['score']:.8f}, "
                + f"tardiness={metrics['tardiness_cost']:.8f}, "
                + f"temp={metrics['temp_cost']:.8f}, "
                + f"cast_gap={metrics['cast_gap_cost']:.8f}, "
                + f"energy={metrics['energy_cost']:.8f}, "
                + f"oxygen_shortage={metrics['oxygen_shortage_cost']:.8f}, "
                + f"oxygen_vent={metrics['oxygen_vent_cost']:.8f}, "
                + f"preheat={metrics['preheat_cost']:.8f}"
            )
        lines.extend(
            [
                "",
                "## Feasibility Checks",
                "- checked schema, full operation coverage, and cross-scenario fixed machine/ladle commitments",
                "- checked route order, machine eligibility, machine non-overlap with setups, cast continuity, and ladle maintenance/non-overlap",
                "- checked crane, alloy-crew, laboratory, and ladle-preheat bucket capacities from the exported artifact",
            ]
        )
        if audit["errors"]:
            lines.extend(["", "## Errors"] + [f"- {err}" for err in audit["errors"][:100]])

        LOG_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    scheduler = SteelScheduler()
    started_at = time.time()
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    total_limit = float(env_limit) if env_limit else None

    base = scheduler.choose_base_commitment()

    scenario_results: Dict[str, ScenarioSolveResult] = {}
    for index, scenario_id in enumerate(scheduler.scenario_order):
        time_limit = scheduler.scenario_time_limit(
            started_at, total_limit, len(scheduler.scenario_order) - index
        )
        scenario_results[scenario_id] = scheduler.solve_scenario(base, scenario_id, time_limit)

    rows = scheduler.build_solution_rows(base, scenario_results)
    scheduler.write_solution(rows)

    audit = scheduler.audit_solution(SOLUTION_PATH)
    command = "PYTHONPATH=/opt/or-deps python /app/submissions/solve.py"
    scheduler.write_log(base, scenario_results, audit, command)

    if audit["errors"]:
        raise RuntimeError(
            f"Exported solution failed audit with {len(audit['errors'])} errors. See {LOG_PATH}."
        )


if __name__ == "__main__":
    main()
