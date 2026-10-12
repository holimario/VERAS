#!/usr/bin/env python3
"""Construct a public-data-only two-region placement and audit it exactly."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from itertools import chain
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


ROOT = Path("/app")
DATA = ROOT / "data"
OUT = ROOT / "submissions"
SOLUTION = OUT / "solution.parquet"

REGION_Z = "中卫"
REGION_H = "怀来"
DAYS = ("20250808", "20250809")
HORIZON = 1441  # event minutes 0,...,1440
DIFF_N = HORIZON + 1

CAP_Z = np.array([2479131.7126728795, 10616453.64463461], dtype=np.float64)
CAP_H = np.array([120936.14278870597, 675432.7278241565], dtype=np.float64)
FINAL_CAP_FACTOR = 1.0 - 1.0e-8
TARGETS = (0.970, 0.980, 0.985, 0.990)
NEAR_TIE_BPS = 0.050 * 1.0e9


def hhmm_minutes(arr: pa.ChunkedArray) -> np.ndarray:
    hours = pc.cast(pc.utf8_slice_codeunits(arr, 0, 2), pa.int16()).to_numpy(
        zero_copy_only=False
    )
    minutes = pc.cast(pc.utf8_slice_codeunits(arr, 3, 5), pa.int16()).to_numpy(
        zero_copy_only=False
    )
    return hours.astype(np.int32) * 60 + minutes.astype(np.int32)


def datetime_minutes(arr: pa.ChunkedArray) -> np.ndarray:
    hours = pc.cast(pc.utf8_slice_codeunits(arr, 11, 13), pa.int16()).to_numpy(
        zero_copy_only=False
    )
    minutes = pc.cast(pc.utf8_slice_codeunits(arr, 14, 16), pa.int16()).to_numpy(
        zero_copy_only=False
    )
    return hours.astype(np.int32) * 60 + minutes.astype(np.int32)


def day_codes(arr: pa.ChunkedArray) -> np.ndarray:
    return pc.equal(arr, DAYS[1]).to_numpy(zero_copy_only=False).astype(np.int32)


def grouped_index(keys: np.ndarray, n_groups: int) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(keys, kind="stable")
    counts = np.bincount(keys, minlength=n_groups)
    offsets = np.empty(n_groups + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(counts, out=offsets[1:])
    return order, offsets


def top_positive(values: np.ndarray, limit: int) -> np.ndarray:
    nz = np.flatnonzero(values > 0.0)
    if len(nz) <= limit:
        return nz[np.argsort(values[nz])[::-1]]
    part = np.argpartition(values[nz], -limit)[-limit:]
    chosen = nz[part]
    return chosen[np.argsort(values[chosen])[::-1]]


@dataclass
class PublicData:
    task_arrow: pa.ChunkedArray
    task_names: pd.Index
    n_tasks: int
    table_names: pd.Index
    n_tables: int
    writer_lengths: np.ndarray
    writer_offsets: np.ndarray
    writer_task: np.ndarray
    writer_table: np.ndarray
    writer_first: np.ndarray
    fallback_state: np.ndarray
    fallback_first_h: np.ndarray
    exec_task: np.ndarray
    exec_day: np.ndarray
    exec_start: np.ndarray
    exec_end: np.ndarray
    exec_value: np.ndarray
    exec_order: np.ndarray
    exec_offsets: np.ndarray
    total_resource: np.ndarray
    tr_task: np.ndarray
    tr_table: np.ndarray
    tr_day: np.ndarray
    tr_start: np.ndarray
    tr_end: np.ndarray
    tr_rel: np.ndarray
    tr_bw: np.ndarray
    tr_task_order: np.ndarray
    tr_task_offsets: np.ndarray
    tr_table_order: np.ndarray
    tr_table_offsets: np.ndarray
    writer_task_order: np.ndarray
    writer_task_offsets: np.ndarray
    task_risk_proxy: np.ndarray

    @classmethod
    def load(cls) -> "PublicData":
        task_table = pq.read_table(DATA / "task_universe.parquet")
        task_arrow = task_table["task_name"]
        task_names = pd.Index(task_arrow.to_pylist(), dtype="object")
        n_tasks = len(task_names)

        writer = pq.read_table(DATA / "table_writers_fallback.parquet")
        table_names = pd.Index(writer["table_uri"].to_pylist(), dtype="object")
        n_tables = len(table_names)
        writer_lists = writer["task_name_list"].to_pylist()
        region_lists = writer["region_list"].to_pylist()
        writer_lengths = np.fromiter(
            (len(v) for v in writer_lists), dtype=np.int32, count=n_tables
        )
        writer_offsets = np.empty(n_tables + 1, dtype=np.int64)
        writer_offsets[0] = 0
        np.cumsum(writer_lengths, out=writer_offsets[1:])
        flat_writer_names = np.asarray(
            list(chain.from_iterable(writer_lists)), dtype=object
        )
        writer_task = task_names.get_indexer(flat_writer_names).astype(np.int32)
        if np.any(writer_task < 0):
            raise ValueError("writer task outside task universe")
        writer_table = np.repeat(
            np.arange(n_tables, dtype=np.int32), writer_lengths
        )
        writer_first = np.full(n_tables, -1, dtype=np.int32)
        has_writer = writer_lengths > 0
        writer_first[has_writer] = writer_task[writer_offsets[:-1][has_writer]]

        # 0 means endpoint follows task (mixed writer state or two-region fallback),
        # 1 is a fixed/uniform Zhongwei endpoint, and 2 is Huailai.
        fallback_state = np.fromiter(
            (
                1
                if len(v) == 1 and v[0] == REGION_Z
                else 2
                if len(v) == 1 and v[0] == REGION_H
                else 0
                for v in region_lists
            ),
            dtype=np.int8,
            count=n_tables,
        )
        fallback_first_h = np.fromiter(
            (bool(v and v[0] == REGION_H) for v in region_lists),
            dtype=np.bool_,
            count=n_tables,
        )

        execution = pq.read_table(DATA / "exec_train_0808_0809.parquet")
        exec_task = task_names.get_indexer(
            execution["task_name"].to_pylist()
        ).astype(np.int32)
        if np.any(exec_task < 0):
            raise ValueError("execution task outside task universe")
        exec_day = day_codes(execution["dt"])
        exec_start = datetime_minutes(execution["running_start_time"])
        exec_end = datetime_minutes(execution["running_end_time"])
        exec_value = np.vstack(
            [
                execution["avg_vcores"].to_numpy(zero_copy_only=False),
                execution["avg_mb"].to_numpy(zero_copy_only=False) / 1024.0,
            ]
        ).astype(np.float64, copy=False)
        exec_order, exec_offsets = grouped_index(exec_task, n_tasks)

        total_resource = np.zeros((2, 2, HORIZON), dtype=np.float64)
        start_key = exec_day * DIFF_N + exec_start
        end_key = exec_day * DIFF_N + exec_end + 1
        for resource in range(2):
            diff = np.bincount(
                start_key,
                weights=exec_value[resource],
                minlength=2 * DIFF_N,
            ) - np.bincount(
                end_key,
                weights=exec_value[resource],
                minlength=2 * DIFF_N,
            )
            total_resource[:, resource, :] = np.cumsum(
                diff.reshape(2, DIFF_N), axis=1
            )[:, :HORIZON]

        transfer = pq.read_table(DATA / "trans_train_0808_0809.parquet")
        tr_task = task_names.get_indexer(transfer["task_name"].to_pylist()).astype(
            np.int32
        )
        tr_table = table_names.get_indexer(
            transfer["table_uri"].to_pylist()
        ).astype(np.int32)
        if np.any(tr_task < 0) or np.any(tr_table < 0):
            raise ValueError("transfer identifier outside public universes")
        tr_day = day_codes(transfer["dt"])
        tr_start = hhmm_minutes(transfer["trans_start_time"])
        tr_end = tr_start + hhmm_minutes(transfer["trans_time"])
        tr_rel = transfer["relation_type"].to_numpy(zero_copy_only=False).astype(
            np.int8
        )
        tr_bw = transfer["estimated_bandwidth"].to_numpy(
            zero_copy_only=False
        ).astype(np.float64, copy=False)
        tr_task_order, tr_task_offsets = grouped_index(tr_task, n_tasks)
        tr_table_order, tr_table_offsets = grouped_index(tr_table, n_tables)
        writer_task_order, writer_task_offsets = grouped_index(writer_task, n_tasks)

        # A conservative change-risk proxy: all rows incident to a task as a
        # transfer task, plus all rows of its single-writer tables, less twice the
        # traffic it can remove from a fixed Huailai endpoint in the all-Z state.
        incident = np.bincount(tr_task, weights=tr_bw, minlength=n_tasks)
        table_bw = np.bincount(tr_table, weights=tr_bw, minlength=n_tables)
        single = np.flatnonzero(writer_lengths == 1)
        single_writers = writer_task[writer_offsets[:-1][single]]
        incident += np.bincount(
            single_writers, weights=table_bw[single], minlength=n_tasks
        )
        all_z_state = fallback_state.copy()
        all_z_state[has_writer] = 1
        fixed_h_cross = all_z_state[tr_table] == 2
        removable = np.bincount(
            tr_task[fixed_h_cross], weights=tr_bw[fixed_h_cross], minlength=n_tasks
        )
        task_risk_proxy = incident - 2.0 * removable

        return cls(
            task_arrow=task_arrow,
            task_names=task_names,
            n_tasks=n_tasks,
            table_names=table_names,
            n_tables=n_tables,
            writer_lengths=writer_lengths,
            writer_offsets=writer_offsets,
            writer_task=writer_task,
            writer_table=writer_table,
            writer_first=writer_first,
            fallback_state=fallback_state,
            fallback_first_h=fallback_first_h,
            exec_task=exec_task,
            exec_day=exec_day,
            exec_start=exec_start,
            exec_end=exec_end,
            exec_value=exec_value,
            exec_order=exec_order,
            exec_offsets=exec_offsets,
            total_resource=total_resource,
            tr_task=tr_task,
            tr_table=tr_table,
            tr_day=tr_day,
            tr_start=tr_start,
            tr_end=tr_end,
            tr_rel=tr_rel,
            tr_bw=tr_bw,
            tr_task_order=tr_task_order,
            tr_task_offsets=tr_task_offsets,
            tr_table_order=tr_table_order,
            tr_table_offsets=tr_table_offsets,
            writer_task_order=writer_task_order,
            writer_task_offsets=writer_task_offsets,
            task_risk_proxy=task_risk_proxy,
        )

    def rows_for_task(self, task: int) -> np.ndarray:
        return self.tr_task_order[
            self.tr_task_offsets[task] : self.tr_task_offsets[task + 1]
        ]

    def rows_for_table(self, table: int) -> np.ndarray:
        return self.tr_table_order[
            self.tr_table_offsets[table] : self.tr_table_offsets[table + 1]
        ]

    def writer_positions_for_task(self, task: int) -> np.ndarray:
        return self.writer_task_order[
            self.writer_task_offsets[task] : self.writer_task_offsets[task + 1]
        ]


class ProfileCache:
    def __init__(self, data: PublicData):
        self.data = data
        self.cache: dict[int, np.ndarray] = {}

    def get(self, task: int) -> np.ndarray:
        if task in self.cache:
            return self.cache[task]
        d = self.data
        rows = d.exec_order[d.exec_offsets[task] : d.exec_offsets[task + 1]]
        profile = np.zeros((2, 2, HORIZON), dtype=np.float64)
        if len(rows):
            start_key = d.exec_day[rows] * DIFF_N + d.exec_start[rows]
            end_key = d.exec_day[rows] * DIFF_N + d.exec_end[rows] + 1
            for resource in range(2):
                diff = np.bincount(
                    start_key,
                    weights=d.exec_value[resource, rows],
                    minlength=2 * DIFF_N,
                ) - np.bincount(
                    end_key,
                    weights=d.exec_value[resource, rows],
                    minlength=2 * DIFF_N,
                )
                profile[:, resource, :] = np.cumsum(
                    diff.reshape(2, DIFF_N), axis=1
                )[:, :HORIZON]
        self.cache[task] = profile
        return profile


def resource_state(
    data: PublicData, hl_load: np.ndarray, factor: float
) -> dict[str, float | int]:
    z_load = data.total_resource - hl_load
    z_ratio = z_load / (CAP_Z[None, :, None] * factor)
    h_ratio = hl_load / (CAP_H[None, :, None] * factor)
    theta = max(0.0, float(z_ratio.max() - 1.0), float(h_ratio.max() - 1.0))
    violations = int(np.count_nonzero(z_ratio > 1.0 + 1e-10)) + int(
        np.count_nonzero(h_ratio > 1.0 + 1e-10)
    )
    return {
        "theta": theta,
        "violations": violations,
        "max_z_ratio": float(z_ratio.max()),
        "max_h_ratio": float(h_ratio.max()),
        "min_slack": float(1.0 - max(z_ratio.max(), h_ratio.max())),
    }


def repair_for_target(
    data: PublicData,
    profiles: ProfileCache,
    factor: float,
    deadline: float,
) -> tuple[np.ndarray, np.ndarray, list[str]] | None:
    x = np.zeros(data.n_tasks, dtype=np.int8)
    hl_load = np.zeros_like(data.total_resource)
    notes: list[str] = []
    for iteration in range(40):
        status = resource_state(data, hl_load, factor)
        if status["theta"] <= 1e-10:
            notes.append(
                f"target={factor:.3f} repaired in {iteration} moves; theta=0"
            )
            return x, hl_load, notes
        if time.perf_counter() >= deadline:
            return None

        z_ratio = (data.total_resource - hl_load) / (
            CAP_Z[None, :, None] * factor
        )
        day, resource, minute = np.unravel_index(np.argmax(z_ratio), z_ratio.shape)
        active = (
            (data.exec_day == day)
            & (data.exec_start <= minute)
            & (data.exec_end >= minute)
            & (x[data.exec_task] == 0)
        )
        contribution = np.bincount(
            data.exec_task[active],
            weights=data.exec_value[resource, active],
            minlength=data.n_tasks,
        )
        pool = top_positive(contribution, 160)
        if not len(pool):
            return None

        current_theta = float(status["theta"])
        trials: list[tuple[float, float, int, np.ndarray]] = []
        for task in pool:
            profile = profiles.get(int(task))
            candidate_h = hl_load + profile
            candidate_state = resource_state(data, candidate_h, factor)
            new_theta = float(candidate_state["theta"])
            if candidate_state["max_h_ratio"] > 1.0 + 1e-10:
                continue
            if new_theta >= current_theta - 1e-12:
                continue
            proxy = float(data.task_risk_proxy[task]) / max(
                float(contribution[task]), 1e-12
            )
            trials.append((new_theta, proxy, int(task), profile))

        if not trials:
            # Change operator after a stall: test pairs of the strongest tasks.
            pair_pool = pool[: min(18, len(pool))]
            best_pair = None
            for ai in range(len(pair_pool)):
                for bi in range(ai + 1, len(pair_pool)):
                    ta, tb = int(pair_pool[ai]), int(pair_pool[bi])
                    pa, pb = profiles.get(ta), profiles.get(tb)
                    candidate_h = hl_load + pa + pb
                    candidate_state = resource_state(data, candidate_h, factor)
                    new_theta = float(candidate_state["theta"])
                    if candidate_state["max_h_ratio"] > 1.0 + 1e-10:
                        continue
                    if new_theta >= current_theta - 1e-12:
                        continue
                    proxy = (
                        float(data.task_risk_proxy[ta])
                        + float(data.task_risk_proxy[tb])
                    ) / max(float(contribution[ta] + contribution[tb]), 1e-12)
                    key = (new_theta, proxy)
                    if best_pair is None or key < best_pair[0]:
                        best_pair = (key, ta, tb, pa, pb)
            if best_pair is None:
                return None
            _, ta, tb, pa, pb = best_pair
            x[ta] = 1
            x[tb] = 1
            hl_load += pa + pb
            notes.append(
                f"target={factor:.3f} pair repair tasks={ta},{tb}"
            )
            continue

        min_theta = min(v[0] for v in trials)
        allowance = max(1e-8, 0.08 * (current_theta - min_theta))
        eligible = [v for v in trials if v[0] <= min_theta + allowance]
        new_theta, _, task, profile = min(eligible, key=lambda v: (v[1], v[0]))
        x[task] = 1
        hl_load += profile
        notes.append(
            f"target={factor:.3f} move={iteration + 1} task_index={task} "
            f"theta={current_theta:.9g}->{new_theta:.9g}"
        )
    return None


def table_state(
    data: PublicData, x: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    count_h = np.bincount(
        data.writer_table,
        weights=x[data.writer_task],
        minlength=data.n_tables,
    )
    state = data.fallback_state.copy()
    has_writer = data.writer_lengths > 0
    state[has_writer & (count_h == 0)] = 1
    state[has_writer & (count_h == data.writer_lengths)] = 2
    state[has_writer & (count_h > 0) & (count_h < data.writer_lengths)] = 0
    return count_h, state


def direction_codes(
    task_h: np.ndarray, state: np.ndarray, relation: np.ndarray
) -> np.ndarray:
    code = np.zeros(len(task_h), dtype=np.int8)
    zh = ((state == 1) & task_h & (relation == 0)) | (
        (state == 2) & (~task_h) & (relation == 1)
    )
    hz = ((state == 2) & (~task_h) & (relation == 0)) | (
        (state == 1) & task_h & (relation == 1)
    )
    code[zh] = 1
    code[hz] = 2
    return code


def bandwidth_replay(
    data: PublicData, x: np.ndarray, state: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    code = direction_codes(x[data.tr_task].astype(bool), state[data.tr_table], data.tr_rel)
    load = np.zeros((2, 2, HORIZON), dtype=np.float64)
    start_key = data.tr_day * DIFF_N + data.tr_start
    end_key = data.tr_day * DIFF_N + data.tr_end + 1
    for q in range(2):
        weight = data.tr_bw * (code == q + 1)
        diff = np.bincount(
            start_key, weights=weight, minlength=2 * DIFF_N
        ) - np.bincount(end_key, weights=weight, minlength=2 * DIFF_N)
        load[:, q, :] = np.cumsum(diff.reshape(2, DIFF_N), axis=1)[:, :HORIZON]
    return load, code


def candidate_metrics(
    data: PublicData, x: np.ndarray, hl_load: np.ndarray, bw_load: np.ndarray
) -> dict[str, float | int | list[float]]:
    directional = bw_load.max(axis=2)
    daily = directional.max(axis=1)
    hard = resource_state(data, hl_load, FINAL_CAP_FACTOR)
    official = resource_state(data, hl_load, 1.0)
    return {
        "worst": float(daily.max()),
        "daily": [float(daily[0]), float(daily[1])],
        "directional": directional.tolist(),
        "mean": float(daily.mean()),
        "spread": float(abs(daily[0] - daily[1])),
        "n_h": int(x.sum()),
        "min_slack": float(hard["min_slack"]),
        "max_resource_utilization": float(
            max(official["max_z_ratio"], official["max_h_ratio"])
        ),
        "theta": float(hard["theta"]),
        "violations": int(hard["violations"]),
    }


def better(a: dict, b: dict | None) -> bool:
    if b is None:
        return True
    aw, bw = float(a["worst"]), float(b["worst"])
    if aw < bw - NEAR_TIE_BPS:
        return True
    if bw < aw - NEAR_TIE_BPS:
        return False
    if int(a["n_h"]) != int(b["n_h"]):
        return int(a["n_h"]) < int(b["n_h"])
    if abs(float(a["mean"]) - float(b["mean"])) > 1e-6:
        return float(a["mean"]) < float(b["mean"])
    if abs(float(a["spread"]) - float(b["spread"])) > 1e-6:
        return float(a["spread"]) < float(b["spread"])
    return float(a["min_slack"]) > float(b["min_slack"]) + 1e-12


@dataclass
class SearchState:
    x: np.ndarray
    hl_load: np.ndarray
    writer_count: np.ndarray
    table_state: np.ndarray
    bw_load: np.ndarray
    metrics: dict


def evaluate_flips(
    data: PublicData,
    profiles: ProfileCache,
    current: SearchState,
    flips: tuple[int, ...],
    resource_factor: float = FINAL_CAP_FACTOR,
) -> dict | None:
    flips = tuple(dict.fromkeys(int(v) for v in flips))
    deltas = np.array([1 if current.x[t] == 0 else -1 for t in flips], dtype=np.int8)

    new_h = current.hl_load.copy()
    for task, delta in zip(flips, deltas):
        new_h += int(delta) * profiles.get(task)
    hard = resource_state(data, new_h, resource_factor)
    if hard["theta"] > 1e-10:
        return None

    written_parts = []
    written_delta_parts = []
    for task, delta in zip(flips, deltas):
        positions = data.writer_positions_for_task(task)
        if len(positions):
            written_parts.append(data.writer_table[positions])
            written_delta_parts.append(
                np.full(len(positions), int(delta), dtype=np.int8)
            )
    if written_parts:
        all_tables = np.concatenate(written_parts)
        all_delta = np.concatenate(written_delta_parts)
        changed_tables, inverse = np.unique(all_tables, return_inverse=True)
        table_delta = np.bincount(
            inverse, weights=all_delta, minlength=len(changed_tables)
        )
        new_counts = current.writer_count[changed_tables] + table_delta
        changed_states = np.zeros(len(changed_tables), dtype=np.int8)
        lengths = data.writer_lengths[changed_tables]
        changed_states[new_counts == 0] = 1
        changed_states[new_counts == lengths] = 2
    else:
        changed_tables = np.empty(0, dtype=np.int32)
        table_delta = np.empty(0, dtype=np.float64)
        changed_states = np.empty(0, dtype=np.int8)

    affected_parts = [data.rows_for_task(task) for task in flips]
    affected_parts.extend(data.rows_for_table(int(t)) for t in changed_tables)
    affected_parts = [v for v in affected_parts if len(v)]
    if affected_parts:
        rows = np.unique(np.concatenate(affected_parts))
    else:
        rows = np.empty(0, dtype=np.int64)

    new_bw = current.bw_load.copy()
    if len(rows):
        old_task_h = current.x[data.tr_task[rows]].astype(bool)
        new_task_h = old_task_h.copy()
        for task in flips:
            loc = data.tr_task[rows] == task
            new_task_h[loc] = ~new_task_h[loc]

        old_states = current.table_state[data.tr_table[rows]]
        new_states = old_states.copy()
        if len(changed_tables):
            pos = np.searchsorted(changed_tables, data.tr_table[rows])
            valid = pos < len(changed_tables)
            valid_idx = np.flatnonzero(valid)
            if len(valid_idx):
                match = changed_tables[pos[valid_idx]] == data.tr_table[rows][valid_idx]
                matched_rows = valid_idx[match]
                new_states[matched_rows] = changed_states[pos[matched_rows]]

        old_code = direction_codes(old_task_h, old_states, data.tr_rel[rows])
        new_code = direction_codes(new_task_h, new_states, data.tr_rel[rows])
        changed = old_code != new_code
        rows = rows[changed]
        old_code = old_code[changed]
        new_code = new_code[changed]
        if len(rows):
            start_key = data.tr_day[rows] * DIFF_N + data.tr_start[rows]
            end_key = data.tr_day[rows] * DIFF_N + data.tr_end[rows] + 1
            for q in range(2):
                weight = data.tr_bw[rows] * (
                    (new_code == q + 1).astype(np.int8)
                    - (old_code == q + 1).astype(np.int8)
                )
                diff = np.bincount(
                    start_key, weights=weight, minlength=2 * DIFF_N
                ) - np.bincount(
                    end_key, weights=weight, minlength=2 * DIFF_N
                )
                new_bw[:, q, :] += np.cumsum(
                    diff.reshape(2, DIFF_N), axis=1
                )[:, :HORIZON]

    new_x = current.x.copy()
    for task in flips:
        new_x[task] = 1 - new_x[task]
    metrics = candidate_metrics(data, new_x, new_h, new_bw)
    return {
        "flips": flips,
        "x": new_x,
        "hl_load": new_h,
        "bw_load": new_bw,
        "metrics": metrics,
        "changed_tables": changed_tables,
        "table_delta": table_delta,
        "changed_states": changed_states,
    }


def accept_result(current: SearchState, result: dict) -> SearchState:
    writer_count = current.writer_count.copy()
    state = current.table_state.copy()
    if len(result["changed_tables"]):
        writer_count[result["changed_tables"]] += result["table_delta"]
        state[result["changed_tables"]] = result["changed_states"]
    return SearchState(
        x=result["x"],
        hl_load=result["hl_load"],
        writer_count=writer_count,
        table_state=state,
        bw_load=result["bw_load"],
        metrics=result["metrics"],
    )


def peak_influence_candidates(
    data: PublicData, current: SearchState, limit: int = 54
) -> list[int]:
    flat = int(np.argmax(current.bw_load))
    day, direction, minute = np.unravel_index(flat, current.bw_load.shape)
    code = direction_codes(
        current.x[data.tr_task].astype(bool),
        current.table_state[data.tr_table],
        data.tr_rel,
    )
    active = (
        (data.tr_day == day)
        & (data.tr_start <= minute)
        & (data.tr_end >= minute)
        & (code == direction + 1)
    )
    task_weight = np.bincount(
        data.tr_task[active], weights=data.tr_bw[active], minlength=data.n_tasks
    )
    table_weight = np.bincount(
        data.tr_table[active], weights=data.tr_bw[active], minlength=data.n_tables
    )

    candidates: list[int] = []
    candidates.extend(int(v) for v in top_positive(task_weight, 30))
    for table in top_positive(table_weight, 10):
        lo, hi = data.writer_offsets[table], data.writer_offsets[table + 1]
        writers = data.writer_task[lo:hi]
        if len(writers) > 8:
            order = np.argsort(data.task_risk_proxy[writers])[:8]
            writers = writers[order]
        candidates.extend(int(v) for v in writers)
    selected = np.flatnonzero(current.x)
    if len(selected) <= 64:
        candidates.extend(int(v) for v in selected)
    else:
        candidates.extend(
            int(v) for v in selected[np.argsort(data.task_risk_proxy[selected])[:40]]
        )
    # Stable de-duplication preserves peak-influence order.
    return list(dict.fromkeys(candidates))[:limit]


def local_search(
    data: PublicData,
    profiles: ProfileCache,
    current: SearchState,
    deadline: float,
) -> tuple[SearchState, list[str]]:
    notes: list[str] = []
    for iteration in range(30):
        if time.perf_counter() >= deadline:
            notes.append("performance search stopped at dispatch deadline")
            break
        candidates = peak_influence_candidates(data, current)
        best_result = None
        for task in candidates:
            result = evaluate_flips(data, profiles, current, (task,))
            if result is not None and better(result["metrics"], current.metrics):
                if best_result is None or better(
                    result["metrics"], best_result["metrics"]
                ):
                    best_result = result
            if time.perf_counter() >= deadline:
                break

        # If no improving single flip exists, use a capacity-preserving swap
        # neighborhood between peak-influential Z tasks and current H tasks.
        if best_result is None and time.perf_counter() < deadline:
            z_candidates = [v for v in candidates if current.x[v] == 0][:12]
            h_candidates = [v for v in candidates if current.x[v] == 1][:12]
            if len(h_candidates) < 8:
                extra_h = np.flatnonzero(current.x)
                h_candidates = list(
                    dict.fromkeys(h_candidates + [int(v) for v in extra_h[:12]])
                )[:12]
            tested = 0
            for ztask in z_candidates:
                for htask in h_candidates:
                    result = evaluate_flips(
                        data, profiles, current, (ztask, htask)
                    )
                    tested += 1
                    if result is not None and better(result["metrics"], current.metrics):
                        if best_result is None or better(
                            result["metrics"], best_result["metrics"]
                        ):
                            best_result = result
                    if tested >= 80 or time.perf_counter() >= deadline:
                        break
                if tested >= 80 or time.perf_counter() >= deadline:
                    break

        if best_result is None:
            notes.append(f"local search stalled after {iteration} accepted moves")
            break
        old = current.metrics
        current = accept_result(current, best_result)
        notes.append(
            f"local move {iteration + 1}: flips={best_result['flips']} "
            f"peak={old['worst']/1e9:.8f}->{current.metrics['worst']/1e9:.8f} "
            f"Gbps, H_tasks={current.metrics['n_h']}"
        )
    return current, notes


def intervention_cleanup(
    data: PublicData,
    profiles: ProfileCache,
    current: SearchState,
    deadline: float,
) -> tuple[SearchState, list[str]]:
    """Minimize interventions inside the global 0.050 Gbps near-tie band."""
    notes: list[str] = []
    peak_anchor = float(current.metrics["worst"])
    for iteration in range(30):
        selected = [int(v) for v in np.flatnonzero(current.x)]
        best_result = None
        for task in selected:
            if time.perf_counter() >= deadline:
                break
            result = evaluate_flips(data, profiles, current, (task,))
            if result is None:
                continue
            if float(result["metrics"]["worst"]) > peak_anchor + NEAR_TIE_BPS:
                continue
            if int(result["metrics"]["n_h"]) >= int(current.metrics["n_h"]):
                continue
            if best_result is None or better(
                result["metrics"], best_result["metrics"]
            ):
                best_result = result

        # Hypergraph interactions can make a pair removable when neither member
        # is removable alone.  Test this operator once singles stall.
        if best_result is None and len(selected) <= 30 and time.perf_counter() < deadline:
            tested = 0
            for ai in range(len(selected)):
                for bi in range(ai + 1, len(selected)):
                    result = evaluate_flips(
                        data, profiles, current, (selected[ai], selected[bi])
                    )
                    tested += 1
                    if result is not None:
                        within = (
                            float(result["metrics"]["worst"])
                            <= peak_anchor + NEAR_TIE_BPS
                        )
                        fewer = int(result["metrics"]["n_h"]) < int(
                            current.metrics["n_h"]
                        )
                        if within and fewer and (
                            best_result is None
                            or better(result["metrics"], best_result["metrics"])
                        ):
                            best_result = result
                    if tested >= 240 or time.perf_counter() >= deadline:
                        break
                if tested >= 240 or time.perf_counter() >= deadline:
                    break

        if best_result is None:
            notes.append(
                f"near-tie cleanup stopped with {current.metrics['n_h']} H tasks"
            )
            break
        current = accept_result(current, best_result)
        notes.append(
            f"cleanup move {iteration + 1}: flips={best_result['flips']}, "
            f"peak={current.metrics['worst']/1e9:.8f} Gbps, "
            f"H_tasks={current.metrics['n_h']}"
        )
    return current, notes


def visible_structural_lower_bound(data: PublicData, current: SearchState) -> dict:
    """Return a valid bound from a task facing conflicting fixed fallbacks."""
    flat = int(np.argmax(current.bw_load))
    day, direction, minute = np.unravel_index(flat, current.bw_load.shape)
    code = direction_codes(
        current.x[data.tr_task].astype(bool),
        current.table_state[data.tr_table],
        data.tr_rel,
    )
    active = (
        (data.tr_day == day)
        & (data.tr_start <= minute)
        & (data.tr_end >= minute)
        & (code == direction + 1)
    )
    weight = np.bincount(
        data.tr_task[active], weights=data.tr_bw[active], minlength=data.n_tasks
    )
    tasks = top_positive(weight, 8)
    best = {"lower_bound": 0.0, "task": -1, "peak_if_z": 0.0, "peak_if_h": 0.0}
    for task in tasks:
        rows = data.rows_for_task(int(task))
        if not len(rows):
            continue
        tables = data.tr_table[rows]
        fixed = (data.writer_lengths[tables] == 0) & (
            data.fallback_state[tables] > 0
        )
        rows = rows[fixed]
        if not len(rows):
            continue
        endpoint = data.fallback_state[data.tr_table[rows]]
        peaks = []
        for assignment_h in (False, True):
            task_h = np.full(len(rows), assignment_h, dtype=np.bool_)
            row_code = direction_codes(task_h, endpoint, data.tr_rel[rows])
            load = np.zeros((2, 2, HORIZON), dtype=np.float64)
            skey = data.tr_day[rows] * DIFF_N + data.tr_start[rows]
            ekey = data.tr_day[rows] * DIFF_N + data.tr_end[rows] + 1
            for q in range(2):
                row_weight = data.tr_bw[rows] * (row_code == q + 1)
                diff = np.bincount(
                    skey, weights=row_weight, minlength=2 * DIFF_N
                ) - np.bincount(
                    ekey, weights=row_weight, minlength=2 * DIFF_N
                )
                load[:, q, :] = np.cumsum(
                    diff.reshape(2, DIFF_N), axis=1
                )[:, :HORIZON]
            peaks.append(float(load.max()))
        bound = min(peaks)
        if bound > best["lower_bound"]:
            best = {
                "lower_bound": bound,
                "task": int(task),
                "peak_if_z": peaks[0],
                "peak_if_h": peaks[1],
            }
    return best


def state_from_artifact(
    data: PublicData, profiles: ProfileCache, path: Path
) -> SearchState:
    artifact = pq.read_table(path)
    if artifact.column_names != ["task_name", "region"]:
        raise ValueError(f"invalid incumbent schema: {artifact.column_names}")
    if artifact["task_name"].to_pylist() != data.task_arrow.to_pylist():
        raise ValueError("incumbent task universe/order mismatch")
    regions = artifact["region"].to_pylist()
    if any(v not in (REGION_Z, REGION_H) for v in regions):
        raise ValueError("incumbent region outside domain")
    x = np.fromiter((v == REGION_H for v in regions), np.int8, len(regions))
    hl_load = np.zeros_like(data.total_resource)
    for task in np.flatnonzero(x):
        hl_load += profiles.get(int(task))
    writer_count, states = table_state(data, x)
    bw_load, _ = bandwidth_replay(data, x, states)
    metrics = candidate_metrics(data, x, hl_load, bw_load)
    if metrics["theta"] > 1e-10:
        raise ValueError("incumbent artifact fails exact public capacity replay")
    return SearchState(x, hl_load, writer_count, states, bw_load, metrics)


def resource_add_candidates(
    data: PublicData, current: SearchState, factor: float, limit: int = 40
) -> list[int]:
    zratio = (data.total_resource - current.hl_load) / (
        CAP_Z[None, :, None] * factor
    )
    day, resource, minute = np.unravel_index(np.argmax(zratio), zratio.shape)
    active = (
        (data.exec_day == day)
        & (data.exec_start <= minute)
        & (data.exec_end >= minute)
        & (current.x[data.exec_task] == 0)
    )
    contribution = np.bincount(
        data.exec_task[active],
        weights=data.exec_value[resource, active],
        minlength=data.n_tasks,
    )
    return [int(v) for v in top_positive(contribution, limit)]


def robust_capacity_repair(
    data: PublicData,
    profiles: ProfileCache,
    current: SearchState,
    deadline: float,
    factor: float = 0.990,
) -> tuple[SearchState, list[str]]:
    """Restore a public utilization target without worsening the visible peak."""
    notes: list[str] = []
    peak_ceiling = float(current.metrics["worst"]) + 1e-3
    for iteration in range(30):
        target_state = resource_state(data, current.hl_load, factor)
        if target_state["theta"] <= 1e-10:
            notes.append(
                f"robust repair reached utilization <= {factor:.3f} with "
                f"{current.metrics['n_h']} H tasks"
            )
            return current, notes
        if time.perf_counter() >= deadline:
            notes.append("robust repair stopped at deadline")
            return current, notes

        pool = resource_add_candidates(data, current, factor, 180)
        trials: list[tuple[float, float, int]] = []
        current_theta = float(target_state["theta"])
        for task in pool:
            candidate_h = current.hl_load + profiles.get(task)
            status = resource_state(data, candidate_h, factor)
            new_theta = float(status["theta"])
            if status["max_h_ratio"] > 1.0 + 1e-10:
                continue
            if new_theta >= current_theta - 1e-12:
                continue
            trials.append((new_theta, float(data.task_risk_proxy[task]), task))
        if not trials:
            notes.append(f"robust repair stalled at theta={current_theta:.9g}")
            return current, notes

        min_theta = min(v[0] for v in trials)
        allowance = max(1e-8, 0.12 * (current_theta - min_theta))
        shortlist = [v for v in trials if v[0] <= min_theta + allowance]
        shortlist.sort(key=lambda v: (v[1], v[0]))
        # Also retain the strongest resource reducers even if their proxy is high.
        strongest = sorted(trials, key=lambda v: v[0])[:10]
        tasks = list(dict.fromkeys([v[2] for v in shortlist[:24] + strongest]))

        best_result = None
        best_target_theta = None
        for task in tasks:
            result = evaluate_flips(data, profiles, current, (task,))
            if result is None:
                continue
            status = resource_state(data, result["hl_load"], factor)
            if status["theta"] >= current_theta - 1e-12:
                continue
            if float(result["metrics"]["worst"]) > peak_ceiling:
                continue
            key = (
                float(result["metrics"]["worst"]),
                float(result["metrics"]["mean"]),
                float(status["theta"]),
                float(data.task_risk_proxy[task]),
            )
            if best_result is None or key < best_result[0]:
                best_result = (key, result, task)
                best_target_theta = float(status["theta"])
        if best_result is None:
            notes.append(
                f"robust repair found no peak-safe move at theta={current_theta:.9g}"
            )
            return current, notes
        _, result, task = best_result
        current = accept_result(current, result)
        notes.append(
            f"robust move {iteration + 1}: task={task}, "
            f"theta={current_theta:.9g}->{best_target_theta:.9g}, "
            f"util={current.metrics['max_resource_utilization']:.9f}"
        )
    return current, notes


def influence_operations(
    data: PublicData,
    current: SearchState,
    day: int,
    direction: int,
    minute: int,
    limit: int = 64,
) -> list[tuple[int, ...]]:
    code = direction_codes(
        current.x[data.tr_task].astype(bool),
        current.table_state[data.tr_table],
        data.tr_rel,
    )
    active = (
        (data.tr_day == day)
        & (data.tr_start <= minute)
        & (data.tr_end >= minute)
        & (code == direction + 1)
    )
    task_weight = np.bincount(
        data.tr_task[active], weights=data.tr_bw[active], minlength=data.n_tasks
    )
    table_weight = np.bincount(
        data.tr_table[active], weights=data.tr_bw[active], minlength=data.n_tables
    )
    operations: list[tuple[int, ...]] = [
        (int(v),) for v in top_positive(task_weight, 36)
    ]
    for table in top_positive(table_weight, 14):
        lo, hi = data.writer_offsets[table], data.writer_offsets[table + 1]
        writers = [int(v) for v in data.writer_task[lo:hi]]
        if len(writers) > 10:
            writers = sorted(writers, key=lambda v: data.task_risk_proxy[v])[:10]
        operations.extend((v,) for v in writers)
        if 1 < len(writers) <= 8:
            operations.append(tuple(sorted(writers)))
    dedup: list[tuple[int, ...]] = []
    seen = set()
    for op in operations:
        op = tuple(dict.fromkeys(op))
        if op and op not in seen:
            seen.add(op)
            dedup.append(op)
    return dedup[:limit]


def floor_polish(
    data: PublicData,
    profiles: ProfileCache,
    current: SearchState,
    deadline: float,
    factor: float = 0.990,
) -> tuple[SearchState, list[str]]:
    notes: list[str] = []
    for iteration in range(12):
        if time.perf_counter() >= deadline:
            break
        flat = int(np.argmax(current.bw_load))
        day, direction, minute = np.unravel_index(flat, current.bw_load.shape)
        operations = influence_operations(
            data, current, day, direction, minute, limit=40
        )
        best = None
        for op in operations:
            result = evaluate_flips(
                data, profiles, current, op, resource_factor=factor
            )
            if result is None:
                continue
            if float(result["metrics"]["worst"]) >= float(
                current.metrics["worst"]
            ) - 1e-3:
                continue
            key = (
                float(result["metrics"]["worst"]),
                float(result["metrics"]["mean"]),
                int(result["metrics"]["n_h"]),
            )
            if best is None or key < best[0]:
                best = (key, result)
        if best is None:
            notes.append(f"floor polish stalled after {iteration} moves")
            break
        old = float(current.metrics["worst"])
        current = accept_result(current, best[1])
        notes.append(
            f"floor move {iteration + 1}: flips={best[1]['flips']}, "
            f"peak={old/1e9:.9f}->{current.metrics['worst']/1e9:.9f} Gbps"
        )
    return current, notes


def secondary_key(metrics: dict, binding_day: int, nonbinding_day: int) -> tuple:
    directional = metrics["directional"]
    binding_q = int(np.argmax(directional[binding_day]))
    opposite = 1 - binding_q
    return (
        float(metrics["daily"][nonbinding_day]),
        float(metrics["mean"]),
        float(directional[binding_day][opposite]),
        float(metrics["spread"]),
        int(metrics["n_h"]),
        -float(metrics["min_slack"]),
    )


def secondary_lns(
    data: PublicData,
    profiles: ProfileCache,
    current: SearchState,
    deadline: float,
    factor: float = 0.990,
) -> tuple[SearchState, list[str]]:
    """Bounded LNS for the nonbinding day and binding opposite direction."""
    notes: list[str] = []
    worst_ceiling = float(current.metrics["worst"]) + 1e-3
    binding_day = int(np.argmax(current.metrics["daily"]))
    nonbinding_day = 1 - binding_day
    balance_reported = False

    for iteration in range(18):
        if time.perf_counter() >= deadline:
            break
        directional = np.asarray(current.metrics["directional"])
        balance_ratio = float(current.metrics["daily"][nonbinding_day]) / float(
            current.metrics["daily"][binding_day]
        )
        if balance_ratio <= 0.85 + 1e-12 and not balance_reported:
            notes.append(
                f"balance target reached: nonbinding/binding={balance_ratio:.6f}"
            )
            balance_reported = True
        direction = int(np.argmax(directional[nonbinding_day]))
        minute = int(np.argmax(current.bw_load[nonbinding_day, direction]))
        operations = influence_operations(
            data, current, nonbinding_day, direction, minute, limit=56
        )
        best = None
        current_key = secondary_key(current.metrics, binding_day, nonbinding_day)
        for op in operations:
            result = evaluate_flips(
                data, profiles, current, op, resource_factor=factor
            )
            if result is None:
                continue
            if float(result["metrics"]["worst"]) > worst_ceiling:
                continue
            key = secondary_key(result["metrics"], binding_day, nonbinding_day)
            if key >= current_key:
                continue
            if best is None or key < best[0]:
                best = (key, result)

        if best is None:
            singles = [op[0] for op in operations if len(op) == 1][:14]
            tested = 0
            for ai in range(len(singles)):
                for bi in range(ai + 1, len(singles)):
                    result = evaluate_flips(
                        data,
                        profiles,
                        current,
                        (singles[ai], singles[bi]),
                        resource_factor=factor,
                    )
                    tested += 1
                    if result is not None and float(
                        result["metrics"]["worst"]
                    ) <= worst_ceiling:
                        key = secondary_key(
                            result["metrics"], binding_day, nonbinding_day
                        )
                        if key < current_key and (best is None or key < best[0]):
                            best = (key, result)
                    if tested >= 80 or time.perf_counter() >= deadline:
                        break
                if tested >= 80 or time.perf_counter() >= deadline:
                    break
        if best is None:
            notes.append(
                f"nonbinding-day LNS stalled at ratio={balance_ratio:.6f}"
            )
            break
        old_day = float(current.metrics["daily"][nonbinding_day])
        current = accept_result(current, best[1])
        notes.append(
            f"secondary move {iteration + 1}: flips={best[1]['flips']}, "
            f"day{nonbinding_day + 8}={old_day/1e9:.8f}->"
            f"{current.metrics['daily'][nonbinding_day]/1e9:.8f} Gbps"
        )

    # With the daily mean protected, lower the binding day's other direction.
    for iteration in range(8):
        if time.perf_counter() >= deadline:
            break
        directional = np.asarray(current.metrics["directional"])
        binding_q = int(np.argmax(directional[binding_day]))
        target_q = 1 - binding_q
        minute = int(np.argmax(current.bw_load[binding_day, target_q]))
        operations = influence_operations(
            data, current, binding_day, target_q, minute, limit=48
        )
        current_opp = float(directional[binding_day, target_q])
        best = None

        def consider(result):
            nonlocal best
            if result is None:
                return
            metrics = result["metrics"]
            if float(metrics["worst"]) > worst_ceiling:
                return
            if float(metrics["daily"][nonbinding_day]) > float(
                current.metrics["daily"][nonbinding_day]
            ) + 1e-3:
                return
            opp = float(metrics["directional"][binding_day][target_q])
            if opp >= current_opp - 1e-3:
                return
            key = (
                opp,
                float(metrics["daily"][nonbinding_day]),
                float(metrics["mean"]),
                int(metrics["n_h"]),
            )
            if best is None or key < best[0]:
                best = (key, result)

        for op in operations:
            consider(
                evaluate_flips(
                    data, profiles, current, op, resource_factor=factor
                )
            )
        if best is None:
            resource_pool = resource_add_candidates(data, current, factor, 24)
            direct = [op[0] for op in operations if len(op) == 1][:10]
            tested = 0
            for target_task in direct:
                for repair_task in resource_pool:
                    if target_task == repair_task:
                        continue
                    consider(
                        evaluate_flips(
                            data,
                            profiles,
                            current,
                            (target_task, repair_task),
                            resource_factor=factor,
                        )
                    )
                    tested += 1
                    if tested >= 100 or time.perf_counter() >= deadline:
                        break
                if tested >= 100 or time.perf_counter() >= deadline:
                    break
        if best is None:
            notes.append(
                f"opposite-direction LNS stalled at {current_opp/1e9:.8f} Gbps"
            )
            break
        current = accept_result(current, best[1])
        notes.append(
            f"opposite move {iteration + 1}: flips={best[1]['flips']}, "
            f"peak={current_opp/1e9:.8f}->"
            f"{current.metrics['directional'][binding_day][target_q]/1e9:.8f} Gbps"
        )
    return current, notes


def strict_objective_improvement(candidate: dict, incumbent: dict) -> bool:
    eps = 1e-3
    if float(candidate["worst"]) < float(incumbent["worst"]) - eps:
        return True
    if abs(float(candidate["worst"]) - float(incumbent["worst"])) <= eps:
        if float(candidate["mean"]) < float(incumbent["mean"]) - eps:
            return True
        if abs(float(candidate["mean"]) - float(incumbent["mean"])) <= eps:
            return float(candidate["spread"]) < float(incumbent["spread"]) - eps
    return False


def migration_metrics(data: PublicData, x: np.ndarray) -> dict[str, int | float]:
    current = pq.read_table(DATA / "current_table_regions.parquet").to_pandas()
    sizes = pq.read_table(DATA / "table_size_latest.parquet").to_pandas()
    current_by_table = current.set_index("table_uri")["current_region"]
    size_by_table = sizes.set_index("table_uri")["table_size"]

    first_h = data.fallback_first_h.copy()
    has_writer = data.writer_lengths > 0
    first_h[has_writer] = x[data.writer_first[has_writer]].astype(bool)
    candidate = np.where(first_h, REGION_H, REGION_Z)
    current_region = current_by_table.reindex(data.table_names).to_numpy()
    size = size_by_table.reindex(data.table_names)
    missing = int(size.isna().sum())
    size_value = size.fillna(0).to_numpy(dtype=np.int64)
    z_to_h = (current_region == REGION_Z) & (candidate == REGION_H)
    h_to_z = (current_region == REGION_H) & (candidate == REGION_Z)
    return {
        "bytes_z_to_h": int(size_value[z_to_h].sum()),
        "bytes_h_to_z": int(size_value[h_to_z].sum()),
        "bytes_total": int(size_value[z_to_h | h_to_z].sum()),
        "tables_z_to_h": int(z_to_h.sum()),
        "tables_h_to_z": int(h_to_z.sum()),
        "missing_size_tables": missing,
    }


def artifact_audit(
    data: PublicData,
    x: np.ndarray,
    hl_load: np.ndarray,
    bw_load: np.ndarray,
    artifact_path: Path = SOLUTION,
) -> dict:
    artifact = pq.read_table(artifact_path)
    exact_columns = artifact.column_names == ["task_name", "region"]
    task_values = artifact["task_name"].to_pylist()
    source_values = data.task_arrow.to_pylist()
    coverage = len(task_values) == data.n_tasks and task_values == source_values
    regions = artifact["region"].to_pylist()
    domain = all(v in (REGION_Z, REGION_H) for v in regions)
    null_preserved = artifact["task_name"].null_count == 1
    recomputed_x = np.fromiter(
        (v == REGION_H for v in regions), dtype=np.int8, count=len(regions)
    )
    integrality_residual = 0.0 if domain else 1.0
    hard = resource_state(data, hl_load, 1.0)
    directional = bw_load.max(axis=2) / 1e9
    return {
        "artifact_present": artifact_path.exists(),
        "schema_exact": bool(exact_columns),
        "rows": int(artifact.num_rows),
        "coverage_exact": bool(coverage),
        "null_identifier_preserved": bool(null_preserved),
        "region_domain_valid": bool(domain),
        "assignment_matches_export": bool(np.array_equal(recomputed_x, x)),
        "incumbent_count": 1,
        "max_normalized_constraint_residual": float(hard["theta"]),
        "violated_constraint_groups": int(hard["violations"] > 0),
        "integrality_residual": integrality_residual,
        "directional_peaks_gbps": directional.tolist(),
        "worst_visible_peak_gbps": float(directional.max()),
    }


def write_artifact(data: PublicData, state: SearchState, path: Path) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    region = pa.array(
        np.where(state.x == 1, REGION_H, REGION_Z).tolist(), type=pa.large_string()
    )
    output = pa.Table.from_arrays(
        [data.task_arrow, region], names=["task_name", "region"]
    )
    pq.write_table(output, path, compression="zstd")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish_staged_artifact(staged: Path, digest: str) -> None:
    os.replace(staged, SOLUTION)
    with (OUT / "solution.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {"artifact_path": str(SOLUTION), "sha256": digest},
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")


def main() -> None:
    start_wall = time.perf_counter()
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    nominal_limit = float(env_limit) if env_limit else 120.0
    # Leave time for exact export, audit, hashes, and provenance files.
    search_deadline = start_wall + max(12.0, min(102.0, 0.82 * nominal_limit))
    log: list[str] = [
        "# Solve log",
        "",
        "Command: `python /app/submissions/solve.py`",
        "",
        "Mode sequence: RESTORATION -> ADVERSARIAL AUDIT -> PERFORMANCE.",
        "SCIP environment was validated separately; the production dispatch uses "
        "NumPy/PyArrow exact replay and no SCIP invocation.",
        "",
        "## Attempts",
        "",
    ]

    data = PublicData.load()
    profiles = ProfileCache(data)
    incumbent_state = None
    incumbent_digest = None
    if SOLUTION.exists():
        incumbent_state = state_from_artifact(data, profiles, SOLUTION)
        incumbent_digest = hashlib.sha256(SOLUTION.read_bytes()).hexdigest()
        log.extend(
            [
                "## Rollback incumbent",
                "",
                f"- SHA-256: `{incumbent_digest}`.",
                f"- Worst/mean/spread: "
                f"{incumbent_state.metrics['worst']/1e9:.9f}/"
                f"{incumbent_state.metrics['mean']/1e9:.9f}/"
                f"{incumbent_state.metrics['spread']/1e9:.9f} Gbps.",
                f"- H tasks / max utilization: "
                f"{incumbent_state.metrics['n_h']}/"
                f"{incumbent_state.metrics['max_resource_utilization']:.9f}.",
                "- Exact public replay status: FEASIBLE, theta=0.",
                "",
            ]
        )
    portfolio: list[dict] = []
    all_z = resource_state(
        data, np.zeros_like(data.total_resource), FINAL_CAP_FACTOR
    )
    log.append(
        f"- All-Z baseline: theta={all_z['theta']:.9g}, "
        f"violations={all_z['violations']}, max_Z_ratio={all_z['max_z_ratio']:.9f}."
    )

    initial_candidates: list[tuple[float, SearchState]] = []
    repair_notes: list[str] = []
    for target in TARGETS:
        if time.perf_counter() >= search_deadline - 8.0:
            break
        repaired = repair_for_target(data, profiles, target, search_deadline - 5.0)
        if repaired is None:
            log.append(f"- Target {target:.3f}: no candidate before deadline.")
            continue
        x, hl_load, notes = repaired
        repair_notes.extend(notes)
        writer_count, states = table_state(data, x)
        bw_load, _ = bandwidth_replay(data, x, states)
        metrics = candidate_metrics(data, x, hl_load, bw_load)
        state = SearchState(x, hl_load, writer_count, states, bw_load, metrics)
        initial_candidates.append((target, state))
        portfolio.append(
            {"label": f"all_z_repair_{target:.3f}", "metrics": metrics.copy()}
        )
        log.append(
            f"- Target {target:.3f}: FEASIBLE, H_tasks={metrics['n_h']}, "
            f"theta={metrics['theta']:.3g}, worst_visible_peak="
            f"{metrics['worst']/1e9:.8f} Gbps, daily="
            f"[{metrics['daily'][0]/1e9:.8f}, {metrics['daily'][1]/1e9:.8f}] Gbps."
        )

    if not initial_candidates:
        raise RuntimeError("no feasible public-data repair candidate")
    best_target, current = initial_candidates[0]
    for target, candidate in initial_candidates[1:]:
        if better(candidate.metrics, current.metrics):
            best_target, current = target, candidate
    log.extend(["", "Repair trace:", ""] + [f"- {v}" for v in repair_notes])
    log.append(
        f"- Selected restoration incumbent from target {best_target:.3f}."
    )

    # Recompute from the selected assignment before entering performance mode.
    exact_h = np.zeros_like(data.total_resource)
    for task in np.flatnonzero(current.x):
        exact_h += profiles.get(int(task))
    exact_writer_count, exact_states = table_state(data, current.x)
    exact_bw, _ = bandwidth_replay(data, current.x, exact_states)
    current = SearchState(
        current.x.copy(),
        exact_h,
        exact_writer_count,
        exact_states,
        exact_bw,
        candidate_metrics(data, current.x, exact_h, exact_bw),
    )
    if current.metrics["theta"] > 1e-10:
        raise RuntimeError("restoration incumbent failed exact replay")

    current, search_notes = local_search(
        data, profiles, current, search_deadline
    )
    log.extend(["", "## Performance search", ""] + [f"- {v}" for v in search_notes])

    current, cleanup_notes = intervention_cleanup(
        data, profiles, current, search_deadline
    )
    log.extend(
        ["", "## Near-tie intervention cleanup", ""]
        + [f"- {v}" for v in cleanup_notes]
    )
    portfolio.append(
        {"label": "public_floor_incumbent", "metrics": current.metrics.copy()}
    )

    current, robust_notes = robust_capacity_repair(
        data, profiles, current, search_deadline, factor=0.990
    )
    log.extend(
        ["", "## Robust utilization repair", ""]
        + [f"- {v}" for v in robust_notes]
    )
    portfolio.append(
        {"label": "robust_0.990", "metrics": current.metrics.copy()}
    )

    current, floor_notes = floor_polish(
        data, profiles, current, search_deadline, factor=0.990
    )
    log.extend(
        ["", "## Structural-floor polish", ""]
        + [f"- {v}" for v in floor_notes]
    )
    portfolio.append(
        {"label": "floor_polished", "metrics": current.metrics.copy()}
    )

    current, secondary_notes = secondary_lns(
        data, profiles, current, search_deadline, factor=0.990
    )
    log.extend(
        ["", "## Secondary-day and opposite-direction LNS", ""]
        + [f"- {v}" for v in secondary_notes]
    )
    portfolio.append(
        {"label": "secondary_lns", "metrics": current.metrics.copy()}
    )

    # Independent in-process recomputation before export.
    final_h = np.zeros_like(data.total_resource)
    for task in np.flatnonzero(current.x):
        final_h += profiles.get(int(task))
    final_writer_count, final_states = table_state(data, current.x)
    final_bw, _ = bandwidth_replay(data, current.x, final_states)
    final_metrics = candidate_metrics(data, current.x, final_h, final_bw)
    if final_metrics["theta"] > 1e-10:
        raise RuntimeError("final assignment is not resource-feasible")
    current = SearchState(
        current.x,
        final_h,
        final_writer_count,
        final_states,
        final_bw,
        final_metrics,
    )
    lower_bound = visible_structural_lower_bound(data, current)
    if lower_bound["lower_bound"] > 0:
        visible_gap = max(
            0.0,
            (float(current.metrics["worst"]) - lower_bound["lower_bound"])
            / lower_bound["lower_bound"],
        )
    else:
        visible_gap = float("nan")

    staged = OUT / "candidate_solution.parquet"
    digest = write_artifact(data, current, staged)
    audit = artifact_audit(
        data, current.x, current.hl_load, current.bw_load, artifact_path=staged
    )
    if not all(
        [
            audit["schema_exact"],
            audit["coverage_exact"],
            audit["null_identifier_preserved"],
            audit["region_domain_valid"],
            audit["assignment_matches_export"],
            audit["max_normalized_constraint_residual"] <= 1e-10,
        ]
    ):
        staged.unlink(missing_ok=True)
        raise RuntimeError(f"staged export audit failed: {audit}")

    robust_ok = float(current.metrics["max_resource_utilization"]) <= 0.990 + 1e-10
    improved = incumbent_state is None or strict_objective_improvement(
        current.metrics, incumbent_state.metrics
    )
    published = bool(improved and robust_ok)
    if published:
        publish_staged_artifact(staged, digest)
        final_state = current
        final_audit = audit
    else:
        staged.unlink(missing_ok=True)
        if incumbent_state is None:
            raise RuntimeError("candidate not publishable and no rollback incumbent")
        final_state = incumbent_state
        digest = incumbent_digest
        final_audit = artifact_audit(
            data,
            incumbent_state.x,
            incumbent_state.hl_load,
            incumbent_state.bw_load,
            artifact_path=SOLUTION,
        )

    migration = migration_metrics(data, final_state.x)
    dispatch_seconds = time.perf_counter() - start_wall
    if published:
        manifest = {
            "embedded_external_task_mapping": False,
            "used_reference_mapping": False,
            "used_august10_data": False,
            "dispatch_time_seconds": dispatch_seconds,
        }
        with (OUT / "manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    portfolio_output = []
    for entry in portfolio:
        metrics = entry["metrics"]
        portfolio_output.append(
            {
                "label": entry["label"],
                "worst_visible_peak_gbps": float(metrics["worst"]) / 1e9,
                "daily_maxima_gbps": [float(v) / 1e9 for v in metrics["daily"]],
                "mean_daily_max_gbps": float(metrics["mean"]) / 1e9,
                "spread_gbps": float(metrics["spread"]) / 1e9,
                "h_tasks": int(metrics["n_h"]),
                "max_resource_utilization": float(
                    metrics["max_resource_utilization"]
                ),
            }
        )
    with (OUT / "pareto_portfolio.json").open("w", encoding="utf-8") as handle:
        json.dump(portfolio_output, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    log.extend(
        [
            "",
            "## Final state vector and validation",
            "",
            f"- Status: {'PUBLISHED STRICT IMPROVEMENT' if published else 'ROLLBACK RETAINED'}.",
            f"- Rollback objective worst/mean/spread: "
            f"{incumbent_state.metrics['worst']/1e9:.9f}/"
            f"{incumbent_state.metrics['mean']/1e9:.9f}/"
            f"{incumbent_state.metrics['spread']/1e9:.9f} Gbps."
            if incumbent_state is not None
            else "- Rollback objective: none.",
            f"- Candidate objective worst/mean/spread: "
            f"{current.metrics['worst']/1e9:.9f}/"
            f"{current.metrics['mean']/1e9:.9f}/"
            f"{current.metrics['spread']/1e9:.9f} Gbps.",
            f"- Candidate max utilization / robust <=0.990: "
            f"{current.metrics['max_resource_utilization']:.9f}/{robust_ok}.",
            f"- Candidate strictly improves recomputed objective: {improved}.",
            f"- Intervention tradeoff versus rollback: "
            f"{int(current.metrics['n_h']) - int(incumbent_state.metrics['n_h'])} "
            f"additional H tasks; mean change="
            f"{(current.metrics['mean'] - incumbent_state.metrics['mean'])/1e9:.9f} "
            f"Gbps; spread change="
            f"{(current.metrics['spread'] - incumbent_state.metrics['spread'])/1e9:.9f} "
            f"Gbps; utilization change="
            f"{current.metrics['max_resource_utilization'] - incumbent_state.metrics['max_resource_utilization']:.9f}."
            if incumbent_state is not None
            else "- Intervention tradeoff versus rollback: no rollback.",
            f"- Balance diagnostic nonbinding/binding: "
            f"{min(current.metrics['daily'])/max(current.metrics['daily']):.6f} "
            f"(target <= 0.850000).",
            f"- Candidate staged-artifact audit: schema={audit['schema_exact']}, "
            f"coverage={audit['coverage_exact']}, theta="
            f"{audit['max_normalized_constraint_residual']:.12g}, "
            f"violated_groups={audit['violated_constraint_groups']}.",
            f"- Artifact SHA-256: `{digest}`.",
            f"- Artifact/schema/coverage: {final_audit['artifact_present']}/"
            f"{final_audit['schema_exact']}/{final_audit['coverage_exact']}; rows={final_audit['rows']}.",
            f"- Incumbent count: {final_audit['incumbent_count']}.",
            f"- Maximum normalized hard-constraint residual theta: "
            f"{final_audit['max_normalized_constraint_residual']:.12g}.",
            f"- Violated constraint groups: {final_audit['violated_constraint_groups']}.",
            f"- Integrality residual: {final_audit['integrality_residual']:.1f}.",
            f"- H task count: {final_state.metrics['n_h']}.",
            f"- Directional peaks [day][ZH,HZ] Gbps: "
            f"{json.dumps(final_audit['directional_peaks_gbps'])}.",
            f"- Worst visible peak: {final_audit['worst_visible_peak_gbps']:.8f} Gbps.",
            f"- Valid visible structural lower bound: "
            f"{lower_bound['lower_bound']/1e9:.8f} Gbps from task index "
            f"{lower_bound['task']} (fixed-fallback peaks if Z/H: "
            f"{lower_bound['peak_if_z']/1e9:.8f}/"
            f"{lower_bound['peak_if_h']/1e9:.8f} Gbps).",
            f"- Relative gap to that visible bound: {visible_gap:.9g}.",
            f"- Daily mean/spread: {final_state.metrics['mean']/1e9:.8f}/"
            f"{final_state.metrics['spread']/1e9:.8f} Gbps.",
            f"- Minimum normalized resource slack (tightened capacities): "
            f"{final_state.metrics['min_slack']:.9g}.",
            f"- Maximum official resource utilization: "
            f"{final_state.metrics['max_resource_utilization']:.9f}.",
            f"- Migration metrics: `{json.dumps(migration, ensure_ascii=False)}`.",
            f"- Dispatch time measured inside the clean run: "
            f"{dispatch_seconds:.6f} seconds.",
            "- Near-tie tradeoff: intervention count, two-day mean/spread, the "
            "0.85 balance diagnostic, and resource slack are reported in "
            "`pareto_portfolio.json`; robust near-ties are not discarded solely "
            "for using a few more Huailai tasks.",
            "- No August 10 data, reference mapping, prior score, or external task "
            "mapping was used.",
            "",
            "The exact exported Parquet, rather than only an in-memory candidate, "
            "was checked for identifiers, null preservation, region domain, and "
            "assignment equality. A separate independent audit command is recorded "
            "after the clean dispatch run.",
        ]
    )
    with (OUT / "solve_log.md").open("w", encoding="utf-8") as handle:
        handle.write("\n".join(log) + "\n")

    print(
        json.dumps(
            {
                "status": "PUBLISHED" if published else "ROLLBACK_RETAINED",
                "worst_visible_peak_gbps": final_audit["worst_visible_peak_gbps"],
                "h_tasks": final_state.metrics["n_h"],
                "dispatch_time_seconds": dispatch_seconds,
                "sha256": digest,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
