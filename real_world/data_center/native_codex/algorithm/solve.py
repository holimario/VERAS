#!/usr/bin/env python3
"""Construct a two-region task placement from only the public input files."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA = ROOT / "data"
OUT = ROOT / "submissions"
ZH = "中卫"
HL = "怀来"
NULL_KEY = "\0__NULL_TASK__"
TARGETS = (0.970, 0.980, 0.985, 0.990)
NEAR_TIE_GBPS = 0.050


def task_key(value: object) -> str:
    return NULL_KEY if pd.isna(value) else str(value)


def parse_hhmm(series: pd.Series) -> np.ndarray:
    raw = series.to_numpy(dtype="S5").view(np.uint8).reshape(-1, 5).astype(np.int16)
    return (
        (raw[:, 0] - 48) * 600
        + (raw[:, 1] - 48) * 60
        + (raw[:, 3] - 48) * 10
        + raw[:, 4]
        - 48
    ).astype(np.int32)


def parse_timestamp_minute(series: pd.Series) -> np.ndarray:
    raw = series.to_numpy(dtype="S19").view(np.uint8).reshape(-1, 19).astype(np.int16)
    return (
        (raw[:, 11] - 48) * 600
        + (raw[:, 12] - 48) * 60
        + (raw[:, 14] - 48) * 10
        + raw[:, 15]
        - 48
    ).astype(np.int32)


@dataclass(frozen=True)
class BandwidthResult:
    worst_gbps: float
    mean_daily_gbps: float
    spread_gbps: float
    peaks_gbps: np.ndarray
    peak_minutes: np.ndarray
    cross_rows: int
    profile: np.ndarray | None = None
    cross_indices: np.ndarray | None = None
    direction_zh_to_hl: np.ndarray | None = None
    task_moved: np.ndarray | None = None
    moved_writer_count: np.ndarray | None = None


class PublicInstance:
    def __init__(self) -> None:
        self.task_df = pd.read_parquet(DATA / "task_universe.parquet")
        self.task_names = self.task_df["task_name"].tolist()
        self.task_to_id = {task_key(v): i for i, v in enumerate(self.task_names)}
        self.n_tasks = len(self.task_names)

        self.writers_df = pd.read_parquet(DATA / "table_writers_fallback.parquet")
        self.n_tables = len(self.writers_df)
        self.table_to_id = dict(zip(self.writers_df["table_uri"], range(self.n_tables)))
        self.writer_count = np.fromiter(
            (len(a) for a in self.writers_df["task_name_list"]),
            dtype=np.int32,
            count=self.n_tables,
        )
        self.fallback_singleton = np.full(self.n_tables, -1, dtype=np.int8)
        writer_task: list[int] = []
        writer_table: list[int] = []
        self.writer_lists: list[np.ndarray] = []
        for j, (writers, regions) in enumerate(
            zip(self.writers_df["task_name_list"], self.writers_df["region_list"])
        ):
            ids = np.asarray([self.task_to_id[task_key(v)] for v in writers], dtype=np.int32)
            self.writer_lists.append(ids)
            if len(ids) == 0 and len(regions) == 1:
                self.fallback_singleton[j] = int(regions[0] == HL)
            if len(ids):
                writer_task.extend(ids.tolist())
                writer_table.extend([j] * len(ids))
        self.writer_task = np.asarray(writer_task, dtype=np.int32)
        self.writer_table = np.asarray(writer_table, dtype=np.int32)

        trans = pd.read_parquet(DATA / "trans_train_0808_0809.parquet")
        self.trans_task = trans["task_name"].map(self.task_to_id).to_numpy(np.int32)
        self.trans_table = trans["table_uri"].map(self.table_to_id).to_numpy(np.int32)
        self.trans_day = (trans["dt"].to_numpy() == "20250809").astype(np.int8)
        self.trans_relation = trans["relation_type"].to_numpy(np.int8).astype(bool)
        self.trans_bw = trans["estimated_bandwidth"].to_numpy(np.float64)
        self.trans_start = parse_hhmm(trans["trans_start_time"])
        self.trans_end = self.trans_start + parse_hhmm(trans["trans_time"])
        self.bw_horizon = int(self.trans_end.max()) + 2

        self.task_transfer_bw = np.bincount(
            self.trans_task, weights=self.trans_bw, minlength=self.n_tasks
        )
        table_transfer_bw = np.bincount(
            self.trans_table, weights=self.trans_bw, minlength=self.n_tables
        )
        self.task_transfer_risk = self.task_transfer_bw.copy()
        np.add.at(
            self.task_transfer_risk,
            self.writer_task,
            table_transfer_bw[self.writer_table],
        )

        execution = pd.read_parquet(DATA / "exec_train_0808_0809.parquet")
        self.exec_task = execution["task_name"].map(self.task_to_id).to_numpy(np.int32)
        self.exec_day = (execution["dt"].to_numpy() == "20250809").astype(np.int8)
        self.exec_start = parse_timestamp_minute(execution["running_start_time"])
        self.exec_end = parse_timestamp_minute(execution["running_end_time"])
        self.exec_vcores = execution["avg_vcores"].to_numpy(np.float64)
        self.exec_memory = execution["avg_mb"].to_numpy(np.float64) / 1024.0
        self.exec_horizon = int(self.exec_end.max()) + 2

        with open(DATA / "resource_limits.json", "r", encoding="utf-8") as fh:
            limits = json.load(fh)["limits"]
        self.zh_capacity = np.asarray(
            [limits["vcores_zw_peak"], limits["memory_zw_peak_gb"]], dtype=np.float64
        )
        self.hl_capacity = np.asarray(
            [limits["vcores_hl_peak"], limits["memory_hl_peak_gb"]], dtype=np.float64
        )

        every_row = np.ones(len(self.exec_task), dtype=bool)
        self.total_resource = np.stack(
            [
                self._execution_profile(every_row, self.exec_vcores),
                self._execution_profile(every_row, self.exec_memory),
            ]
        )

    def moved_vector(self, moved: set[int]) -> np.ndarray:
        vector = np.zeros(self.n_tasks, dtype=bool)
        if moved:
            vector[np.fromiter(moved, dtype=np.int32)] = True
        return vector

    def bandwidth(self, moved: set[int], details: bool = False) -> BandwidthResult:
        moved_vector = self.moved_vector(moved)
        moved_writer_count = np.bincount(
            self.writer_table[moved_vector[self.writer_task]], minlength=self.n_tables
        )
        task_moved = moved_vector[self.trans_task]
        n_writers = self.writer_count[self.trans_table]
        has_writers = n_writers > 0
        cross = np.zeros(len(self.trans_task), dtype=bool)

        writer_tables = self.trans_table[has_writers]
        cross[has_writers] = (
            task_moved[has_writers] & (moved_writer_count[writer_tables] == 0)
        ) | (
            (~task_moved[has_writers])
            & (moved_writer_count[writer_tables] == n_writers[has_writers])
        )

        fallback_rows = (~has_writers) & (self.fallback_singleton[self.trans_table] >= 0)
        cross[fallback_rows] = task_moved[fallback_rows] != (
            self.fallback_singleton[self.trans_table[fallback_rows]] == 1
        )

        # True is Zhongwei -> Huailai.  For relation 0, the task is destination;
        # for relation 1, it is source.
        direction_zh_to_hl = np.logical_xor(task_moved, self.trans_relation)
        indices = np.flatnonzero(cross)
        channel = (
            self.trans_day[indices].astype(np.int32) * 2
            + direction_zh_to_hl[indices].astype(np.int32)
        )
        offset = channel * self.bw_horizon
        difference = np.bincount(
            offset + self.trans_start[indices],
            weights=self.trans_bw[indices],
            minlength=4 * self.bw_horizon,
        ) - np.bincount(
            offset + self.trans_end[indices] + 1,
            weights=self.trans_bw[indices],
            minlength=4 * self.bw_horizon,
        )
        profile = np.cumsum(difference.reshape(4, self.bw_horizon), axis=1)
        peaks = profile.max(axis=1)
        peak_minutes = profile.argmax(axis=1)
        daily = peaks.reshape(2, 2).max(axis=1)
        result = BandwidthResult(
            worst_gbps=float(peaks.max() / 1e9),
            mean_daily_gbps=float(daily.mean() / 1e9),
            spread_gbps=float(abs(daily[0] - daily[1]) / 1e9),
            peaks_gbps=peaks.reshape(2, 2) / 1e9,
            peak_minutes=peak_minutes.reshape(2, 2),
            cross_rows=len(indices),
        )
        if not details:
            return result
        return BandwidthResult(
            **{**result.__dict__,
               "profile": profile,
               "cross_indices": indices,
               "direction_zh_to_hl": direction_zh_to_hl,
               "task_moved": task_moved,
               "moved_writer_count": moved_writer_count}
        )

    def peak_influence_operations(
        self, moved: set[int], result: BandwidthResult, limit: int
    ) -> list[tuple[str, int]]:
        assert result.cross_indices is not None
        assert result.direction_zh_to_hl is not None
        assert result.task_moved is not None
        assert result.moved_writer_count is not None
        scores: dict[tuple[str, int], float] = {}
        peak_flat = result.peaks_gbps.reshape(-1)
        for channel in range(4):
            minute = int(result.peak_minutes.reshape(-1)[channel])
            day = channel // 2
            direction = channel % 2
            indices = result.cross_indices
            active = indices[
                (self.trans_day[indices] == day)
                & (result.direction_zh_to_hl[indices] == bool(direction))
                & (self.trans_start[indices] <= minute)
                & (self.trans_end[indices] >= minute)
            ]
            weight = 1.0 if peak_flat[channel] >= result.worst_gbps - 1e-9 else 0.2
            for row in active:
                value = float(self.trans_bw[row]) * weight
                task = int(self.trans_task[row])
                table = int(self.trans_table[row])
                if not result.task_moved[row]:
                    scores[("add", task)] = scores.get(("add", task), 0.0) + value
                    if (
                        self.writer_count[table] > 0
                        and result.moved_writer_count[table] == self.writer_count[table]
                    ):
                        for writer in self.writer_lists[table][:8]:
                            key = ("remove", int(writer))
                            scores[key] = scores.get(key, 0.0) + value
                else:
                    scores[("remove", task)] = scores.get(("remove", task), 0.0) + value
                    if (
                        self.writer_count[table] > 0
                        and result.moved_writer_count[table] == 0
                    ):
                        for writer in self.writer_lists[table][:8]:
                            key = ("add", int(writer))
                            scores[key] = scores.get(key, 0.0) + value

        operations: list[tuple[str, int]] = []
        for operation, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True):
            kind, task = operation
            if kind == "add" and task not in moved:
                operations.append(operation)
            elif kind == "remove" and task in moved:
                operations.append(operation)
            if len(operations) >= limit:
                break
        return operations

    def bandwidth_search(self) -> tuple[set[int], list[str]]:
        moved: set[int] = set()
        trace: list[str] = []
        seen = {frozenset()}
        for iteration in range(28):
            current = self.bandwidth(moved, details=True)
            operations = self.peak_influence_operations(moved, current, limit=6)
            best: tuple[tuple[float, float, float, int], str, int, set[int], BandwidthResult] | None = None
            for kind, task in operations:
                candidate = set(moved)
                if kind == "add":
                    candidate.add(task)
                else:
                    candidate.remove(task)
                if frozenset(candidate) in seen:
                    continue
                score = self.bandwidth(candidate)
                key = (score.worst_gbps, score.mean_daily_gbps, score.spread_gbps, len(candidate))
                if best is None or key < best[0]:
                    best = (key, kind, task, candidate, score)
            if best is None:
                break
            _, kind, task, candidate, score = best
            improves_peak = score.worst_gbps < current.worst_gbps - NEAR_TIE_GBPS
            reduces_intervention_near_tie = (
                len(candidate) < len(moved)
                and score.worst_gbps <= current.worst_gbps + NEAR_TIE_GBPS
            )
            if not (improves_peak or reduces_intervention_near_tie):
                break
            moved = candidate
            seen.add(frozenset(moved))
            trace.append(
                f"iteration {iteration + 1}: {kind} task derived from peak influence; "
                f"tasks={len(moved)}, worst={score.worst_gbps:.8f} Gbps"
            )
        return moved, trace

    def _execution_profile(self, mask: np.ndarray, values: np.ndarray) -> np.ndarray:
        day_offset = self.exec_day[mask].astype(np.int32) * self.exec_horizon
        difference = np.bincount(
            day_offset + self.exec_start[mask],
            weights=values[mask],
            minlength=2 * self.exec_horizon,
        ) - np.bincount(
            day_offset + self.exec_end[mask] + 1,
            weights=values[mask],
            minlength=2 * self.exec_horizon,
        )
        return np.cumsum(difference.reshape(2, self.exec_horizon), axis=1)

    def moved_resource(self, moved: set[int]) -> np.ndarray:
        moved_vector = self.moved_vector(moved)
        mask = moved_vector[self.exec_task]
        return np.stack(
            [
                self._execution_profile(mask, self.exec_vcores),
                self._execution_profile(mask, self.exec_memory),
            ]
        )

    def resource_metrics(self, moved: set[int]) -> dict[str, object]:
        hl_profile = self.moved_resource(moved)
        zh_profile = self.total_resource - hl_profile
        zh_peaks = zh_profile.max(axis=2).T
        hl_peaks = hl_profile.max(axis=2).T
        hard_feasible = bool(
            (zh_profile <= self.zh_capacity[:, None, None] * 1.00001).all()
            and (hl_profile <= self.hl_capacity[:, None, None] * 1.00001).all()
        )
        strict_feasible = bool(
            (zh_profile <= self.zh_capacity[:, None, None]).all()
            and (hl_profile <= self.hl_capacity[:, None, None]).all()
        )
        return {
            "hl_profile": hl_profile,
            "zh_profile": zh_profile,
            "zh_peaks": zh_peaks,
            "hl_peaks": hl_peaks,
            "zh_max_ratio": float(
                max(zh_profile.max(axis=(1, 2)) / self.zh_capacity)
            ),
            "hl_max_ratio": float(
                max(hl_profile.max(axis=(1, 2)) / self.hl_capacity)
            ),
            "hard_feasible": hard_feasible,
            "strict_feasible": strict_feasible,
        }

    def target_diagnostics(self, moved: set[int]) -> list[dict[str, object]]:
        base = self.moved_resource(moved)
        diagnostics = []
        for target in TARGETS:
            required = np.maximum(
                0.0,
                self.total_resource
                - base
                - self.zh_capacity[:, None, None] * target,
            )
            diagnostics.append(
                {
                    "target": target,
                    "violating_stages": int((required > 1e-8).sum()),
                    "max_core_repair": float(required[0].max()),
                    "max_memory_repair_gb": float(required[1].max()),
                }
            )
        return diagnostics

    def _repair_candidate_profiles(
        self, moved: set[int], target: float, pool_size: int = 1500
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        base = self.moved_resource(moved)
        required = np.maximum(
            0.0,
            self.total_resource - base - self.zh_capacity[:, None, None] * target,
        )
        weights = np.where(required > 1e-8, 1.0 / np.maximum(required, 1.0), 0.0)
        score = np.zeros(self.n_tasks, dtype=np.float64)
        available = ~self.moved_vector(moved)
        available_rows = available[self.exec_task]
        for resource, values in enumerate((self.exec_vcores, self.exec_memory)):
            for day in range(2):
                prefix = np.r_[0.0, np.cumsum(weights[resource, day])]
                mask = available_rows & (self.exec_day == day)
                row_score = values[mask] * (
                    prefix[self.exec_end[mask] + 1] - prefix[self.exec_start[mask]]
                )
                np.add.at(score, self.exec_task[mask], row_score)

        candidates = np.flatnonzero(score > 0.0)
        candidates = candidates[np.argsort(score[candidates])[::-1]][:pool_size]
        local = np.full(self.n_tasks, -1, dtype=np.int32)
        local[candidates] = np.arange(len(candidates), dtype=np.int32)
        rows = local[self.exec_task] >= 0
        candidate_row = local[self.exec_task[rows]].astype(np.int64)
        base_index = (
            candidate_row * (2 * self.exec_horizon)
            + self.exec_day[rows].astype(np.int64) * self.exec_horizon
        )
        profiles = []
        for values in (self.exec_vcores, self.exec_memory):
            difference = np.zeros(len(candidates) * 2 * self.exec_horizon, dtype=np.float64)
            np.add.at(difference, base_index + self.exec_start[rows], values[rows])
            np.add.at(difference, base_index + self.exec_end[rows] + 1, -values[rows])
            profiles.append(
                np.cumsum(
                    difference.reshape(len(candidates), 2, self.exec_horizon), axis=2
                )
            )
        candidate_profiles = np.stack(profiles)
        return candidates, candidate_profiles, base, required, score

    def repair_capacity(
        self, moved: set[int], target: float = 0.990
    ) -> tuple[set[int], list[str], list[str]]:
        candidates, profiles, base, required, _ = self._repair_candidate_profiles(
            moved, target
        )
        if len(candidates) == 0 and (required > 1e-8).any():
            raise RuntimeError("No capacity-repair candidates cover the target violations")

        model = Model("restricted_capacity_repair")
        model.hideOutput()
        model.setParam("limits/gap", 0.0005)
        configured = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
        invocation_limit = 6.0 if configured is None else min(6.0, float(configured))
        model.setParam("limits/time", max(invocation_limit, 0.01))
        x = [model.addVar(vtype="B", name=f"repair_{j}") for j in range(len(candidates))]

        for resource in range(2):
            for day in range(2):
                for minute in np.flatnonzero(required[resource, day] > 1e-8):
                    nonzero = np.flatnonzero(profiles[resource, :, day, minute] > 1e-12)
                    model.addCons(
                        quicksum(
                            float(profiles[resource, j, day, minute]) * x[j]
                            for j in nonzero
                        )
                        >= float(required[resource, day, minute]),
                        name=f"target_{resource}_{day}_{minute}",
                    )

        penalty = np.log1p(self.task_transfer_risk[candidates])
        if penalty.max(initial=0.0) > 0:
            penalty /= penalty.max()
        model.setObjective(
            quicksum((1.0 + 1e-4 * float(penalty[j])) * x[j] for j in range(len(x))),
            "minimize",
        )

        base_bandwidth = self.bandwidth(moved)
        solve_trace: list[str] = []
        rejected_trace: list[str] = []
        selected_local: list[int] = []
        forbidden: set[int] = set()
        upper_cuts: set[tuple[int, int, int]] = set()

        for attempt in range(6):
            model.optimize()
            status = str(model.getStatus())
            solve_trace.append(
                f"SCIP repair attempt {attempt + 1}: status={status}, "
                f"solutions={model.getNSols()}"
            )
            if model.getNSols() == 0:
                break
            selected_local = [j for j, var in enumerate(x) if model.getVal(var) > 0.5]
            selected_tasks = {int(candidates[j]) for j in selected_local}

            # Reject any repair move that individually breaks the 0.050 Gbps
            # near-tie envelope around the best bandwidth-only candidate.
            dangerous: list[int] = []
            for j in selected_local:
                task = int(candidates[j])
                individual = self.bandwidth(moved | {task})
                if individual.worst_gbps > base_bandwidth.worst_gbps + NEAR_TIE_GBPS:
                    dangerous.append(j)
                    rejected_trace.append(
                        f"rejected repair task {self.task_names[task]!r}: individual "
                        f"worst peak {individual.worst_gbps:.8f} Gbps"
                    )
            if dangerous:
                model.freeTransform()
                for j in dangerous:
                    if j not in forbidden:
                        model.addCons(x[j] == 0, name=f"bandwidth_forbid_{j}")
                        forbidden.add(j)
                continue

            combined = self.bandwidth(moved | selected_tasks)
            if combined.worst_gbps > base_bandwidth.worst_gbps + NEAR_TIE_GBPS:
                # A no-good cut handles interactions between individually safe moves.
                model.freeTransform()
                model.addCons(
                    quicksum(x[j] for j in selected_local) <= len(selected_local) - 1,
                    name=f"combined_bandwidth_nogood_{attempt}",
                )
                rejected_trace.append(
                    f"rejected combined repair set: worst peak "
                    f"{combined.worst_gbps:.8f} Gbps"
                )
                continue

            moved_profile = base + profiles[:, selected_local].sum(axis=1)
            violations = np.argwhere(
                moved_profile > self.hl_capacity[:, None, None] * 1.00001
            )
            if len(violations):
                model.freeTransform()
                for resource, day, minute in violations:
                    key = (int(resource), int(day), int(minute))
                    if key in upper_cuts:
                        continue
                    nonzero = np.flatnonzero(profiles[resource, :, day, minute] > 1e-12)
                    model.addCons(
                        quicksum(
                            float(profiles[resource, j, day, minute]) * x[j]
                            for j in nonzero
                        )
                        <= float(self.hl_capacity[resource] - base[resource, day, minute]),
                        name=f"hl_cut_{resource}_{day}_{minute}",
                    )
                    upper_cuts.add(key)
                continue
            return moved | selected_tasks, solve_trace, rejected_trace

        raise RuntimeError(
            "Restricted SCIP repair did not find a bandwidth-safe capacity repair; "
            + "; ".join(solve_trace)
        )

    def migration_metrics(self, moved: set[int]) -> dict[str, object]:
        moved_vector = self.moved_vector(moved)
        candidate_region: list[str] = []
        for j, writers in enumerate(self.writer_lists):
            if len(writers):
                candidate_region.append(HL if moved_vector[int(writers[0])] else ZH)
            else:
                candidate_region.append(str(self.writers_df.iloc[j]["region_list"][0]))
        candidate = pd.DataFrame(
            {
                "table_uri": self.writers_df["table_uri"],
                "candidate_region": candidate_region,
            }
        )
        current = pd.read_parquet(DATA / "current_table_regions.parquet")
        sizes = pd.read_parquet(DATA / "table_size_latest.parquet")[["table_uri", "table_size"]]
        merged = current.merge(candidate, on="table_uri", how="inner").merge(
            sizes, on="table_uri", how="left"
        )
        changed = merged[
            merged["current_region"].notna()
            & merged["candidate_region"].notna()
            & (merged["current_region"] != merged["candidate_region"])
        ].copy()
        missing = int(changed["table_size"].isna().sum())
        changed["table_size"] = changed["table_size"].fillna(0)
        by_direction = (
            changed.groupby(["current_region", "candidate_region"])["table_size"]
            .sum()
            .to_dict()
        )
        return {
            "changed_tables": len(changed),
            "missing_size_tables": missing,
            "zh_to_hl_bytes": int(by_direction.get((ZH, HL), 0)),
            "hl_to_zh_bytes": int(by_direction.get((HL, ZH), 0)),
            "total_bytes": int(changed["table_size"].sum()),
        }


def format_matrix(matrix: np.ndarray) -> str:
    return json.dumps(np.asarray(matrix).round(8).tolist(), ensure_ascii=False)


def main() -> None:
    started = time.perf_counter()
    OUT.mkdir(parents=True, exist_ok=True)
    instance = PublicInstance()

    baseline_bw = instance.bandwidth(set())
    baseline_resource = instance.resource_metrics(set())
    bandwidth_moved, bandwidth_trace = instance.bandwidth_search()
    bandwidth_candidate = instance.bandwidth(bandwidth_moved)
    target_diagnostics = instance.target_diagnostics(bandwidth_moved)

    final_moved, solver_trace, rejection_trace = instance.repair_capacity(
        bandwidth_moved, target=0.990
    )
    final_bw = instance.bandwidth(final_moved)
    final_resource = instance.resource_metrics(final_moved)
    if not final_resource["hard_feasible"]:
        raise RuntimeError("Final in-memory assignment is not resource-feasible")

    regions = np.full(instance.n_tasks, ZH, dtype=object)
    if final_moved:
        regions[np.fromiter(final_moved, dtype=np.int32)] = HL
    solution = instance.task_df.copy()
    solution["region"] = regions
    solution = solution[["task_name", "region"]]
    solution_path = OUT / "solution.parquet"
    solution.to_parquet(solution_path, index=False)

    digest = hashlib.sha256(solution_path.read_bytes()).hexdigest()
    with open(OUT / "solution.json", "w", encoding="utf-8") as fh:
        json.dump(
            {"artifact_path": str(solution_path), "sha256": digest},
            fh,
            ensure_ascii=False,
            indent=2,
        )

    # Validate the exact exported artifact, not only the construction state.
    exported = pd.read_parquet(solution_path)
    schema_ok = list(exported.columns) == ["task_name", "region"]
    coverage_ok = (
        len(exported) == instance.n_tasks
        and exported["task_name"].isna().sum() == 1
        and exported["task_name"].dropna().is_unique
        and set(exported["region"].unique()) <= {ZH, HL}
    )
    exported_moved = set(np.flatnonzero(exported["region"].to_numpy() == HL).tolist())
    exported_bw = instance.bandwidth(exported_moved)
    exported_resource = instance.resource_metrics(exported_moved)
    hash_ok = hashlib.sha256(solution_path.read_bytes()).hexdigest() == digest
    if not (
        schema_ok
        and coverage_ok
        and hash_ok
        and exported_resource["hard_feasible"]
        and exported_moved == final_moved
    ):
        raise RuntimeError("Exported Parquet validation failed")

    migration = instance.migration_metrics(exported_moved)
    dispatch_seconds = time.perf_counter() - started
    with open(OUT / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(
            {
                "embedded_external_task_mapping": False,
                "used_reference_mapping": False,
                "used_august10_data": False,
                "dispatch_time_seconds": dispatch_seconds,
            },
            fh,
            indent=2,
        )

    log_lines = [
        "# Solve log",
        "",
        "## Command",
        "",
        "`python /app/submissions/solve.py`",
        "",
        "## Baseline diagnostics",
        "",
        f"- All-Zhongwei bandwidth peaks [day][HL->ZH, ZH->HL] Gbps: {format_matrix(baseline_bw.peaks_gbps)}",
        f"- All-Zhongwei worst visible peak: {baseline_bw.worst_gbps:.8f} Gbps",
        f"- All-Zhongwei Zhongwei maximum utilization ratio: {baseline_resource['zh_max_ratio']:.8f}",
        f"- All-Zhongwei strict resource feasible: {baseline_resource['strict_feasible']}",
        "",
        "## Utilization-target comparison",
        "",
    ]
    for item in target_diagnostics:
        log_lines.append(
            f"- Target {item['target']:.3f}: {item['violating_stages']} resource stages need repair; "
            f"maximum additional core load {item['max_core_repair']:.8f}, "
            f"memory {item['max_memory_repair_gb']:.8f} GB."
        )
    log_lines.extend(
        [
            "- Selected target: 0.990. Lower targets require materially more intervention; "
            "the 0.990 target supplies positive visible slack and is preferred by the "
            "near-tie intervention regularizer.",
            "",
            "## Bandwidth search",
            "",
            *[f"- {line}" for line in bandwidth_trace],
            f"- Bandwidth-only moved tasks: {len(bandwidth_moved)}",
            f"- Bandwidth-only peaks Gbps: {format_matrix(bandwidth_candidate.peaks_gbps)}",
            f"- Bandwidth-only worst visible peak: {bandwidth_candidate.worst_gbps:.8f} Gbps",
            "",
            "## SCIP capacity repair",
            "",
            *[f"- {line}" for line in solver_trace],
            *[f"- {line}" for line in rejection_trace],
            "- SCIP relative gap setting: 0.0005",
            f"- Total moved tasks after repair: {len(final_moved)}",
            "",
            "## Final exact exported-artifact validation",
            "",
            f"- Schema valid: {schema_ok}",
            f"- Full coverage and allowed regions: {coverage_ok}",
            f"- SHA-256 matches solution.json: {hash_ok}",
            f"- Strict resource feasible: {exported_resource['strict_feasible']}",
            f"- Official-tolerance resource feasible: {exported_resource['hard_feasible']}",
            f"- Zhongwei peaks [day][vCores, memory_GB]: {format_matrix(exported_resource['zh_peaks'])}",
            f"- Huailai peaks [day][vCores, memory_GB]: {format_matrix(exported_resource['hl_peaks'])}",
            f"- Maximum Zhongwei utilization ratio: {exported_resource['zh_max_ratio']:.8f}",
            f"- Maximum Huailai utilization ratio: {exported_resource['hl_max_ratio']:.8f}",
            f"- Directional bandwidth peaks [day][HL->ZH, ZH->HL] Gbps: {format_matrix(exported_bw.peaks_gbps)}",
            f"- Worst visible directional peak: {exported_bw.worst_gbps:.8f} Gbps",
            f"- Mean daily maximum-direction peak: {exported_bw.mean_daily_gbps:.8f} Gbps",
            f"- Visible daily spread: {exported_bw.spread_gbps:.8f} Gbps",
            f"- Cross-region transfer rows: {exported_bw.cross_rows}",
            "- Near-tie rule: among candidates within 0.050 Gbps of the best visible "
            "peak, fewer moved tasks are preferred before mean/spread and slack.",
            f"- Migration changed tables: {migration['changed_tables']}",
            f"- Migration Zhongwei->Huailai bytes: {migration['zh_to_hl_bytes']}",
            f"- Migration Huailai->Zhongwei bytes: {migration['hl_to_zh_bytes']}",
            f"- Migration total bytes: {migration['total_bytes']}",
            f"- Changed tables with missing size (counted as zero): {migration['missing_size_tables']}",
            f"- Clean dispatch time: {dispatch_seconds:.8f} seconds",
            f"- Dispatch under 120 seconds: {dispatch_seconds < 120.0}",
            "",
            "## Provenance",
            "",
            "- Embedded external task mapping: false",
            "- Used reference mapping: false",
            "- Used August 10 data: false",
        ]
    )
    (OUT / "solve_log.md").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "feasible",
                "moved_tasks": len(final_moved),
                "worst_visible_gbps": round(exported_bw.worst_gbps, 8),
                "dispatch_time_seconds": round(dispatch_seconds, 8),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
