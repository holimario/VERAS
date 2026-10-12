#!/usr/bin/env python3
"""Construct a feasible two-region assignment from the public August 8/9 data.

The algorithm is deterministic and contains no stored assignment.  It uses exact
two-day replay for every accepted bandwidth move, conservative resource repair,
and a SCIP projection MIP over the active repair neighborhood.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import polars as pl
from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA = ROOT / "data"
OUT = ROOT / "submissions"
ZW = "中卫"
HL = "怀来"
DAYS = ("20250808", "20250809")
MINUTES = 1440
NEAR_TIE = 0.05e9  # 0.050 Gbps in input bandwidth units


def _minute_expr(name: str) -> pl.Expr:
    return (
        pl.col(name).str.slice(0, 2).cast(pl.Int16) * 60
        + pl.col(name).str.slice(3, 2).cast(pl.Int16)
    )


def _profile(day: np.ndarray, start: np.ndarray, end: np.ndarray,
             weight: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """Exact inclusive interval replay on the public minute grid."""
    ans = np.zeros((2, MINUTES), dtype=np.float64)
    if mask is None:
        mask = np.ones(len(day), dtype=bool)
    for d in (0, 1):
        take = mask & (day == d)
        if not np.any(take):
            continue
        delta = np.bincount(start[take], weights=weight[take], minlength=MINUTES + 1)
        delta -= np.bincount(end[take] + 1, weights=weight[take], minlength=MINUTES + 1)
        ans[d] = np.cumsum(delta[:MINUTES])
    return ans


@dataclass
class PublicData:
    task_arrow: pa.ChunkedArray
    tasks: list
    task_to_id: dict
    table_uris: list[str]
    writer_offsets: np.ndarray
    writer_ids: np.ndarray
    writer_table_ids: np.ndarray
    nwriters: np.ndarray
    fallback_has_z: np.ndarray
    fallback_has_h: np.ndarray
    first_writer: np.ndarray
    fallback_first_h: np.ndarray
    ex_task: np.ndarray
    ex_day: np.ndarray
    ex_start: np.ndarray
    ex_end: np.ndarray
    ex_v: np.ndarray
    ex_m: np.ndarray
    ex_order: np.ndarray
    ex_task_offsets: np.ndarray
    total_v: np.ndarray
    total_m: np.ndarray
    tr_task: np.ndarray
    tr_table: np.ndarray
    tr_day: np.ndarray
    tr_start: np.ndarray
    tr_end: np.ndarray
    tr_rel: np.ndarray
    tr_bw: np.ndarray
    table_traffic: np.ndarray
    task_network_risk: np.ndarray
    limits: dict


def load_public_data() -> PublicData:
    utable = pq.read_table(DATA / "task_universe.parquet")
    task_arrow = utable["task_name"]
    tasks = task_arrow.to_pylist()
    task_to_id = {name: i for i, name in enumerate(tasks)}
    ntasks = len(tasks)
    task_lookup = pl.DataFrame(
        {"task_name": tasks, "task_id": np.arange(ntasks, dtype=np.uint32)},
        schema={"task_name": pl.String, "task_id": pl.UInt32},
    )

    wt = pq.read_table(DATA / "table_writers_fallback.parquet")
    table_uris = wt["table_uri"].to_pylist()
    writer_lists = wt["task_name_list"].to_pylist()
    fallback_lists = wt["region_list"].to_pylist()
    nq = len(table_uris)
    nwriter = np.fromiter((len(v) for v in writer_lists), dtype=np.int32, count=nq)
    offsets = np.empty(nq + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(nwriter, out=offsets[1:])
    writer_ids = np.empty(int(offsets[-1]), dtype=np.uint32)
    writer_table_ids = np.empty(int(offsets[-1]), dtype=np.uint32)
    first_writer = np.full(nq, -1, dtype=np.int64)
    pos = 0
    for q, names in enumerate(writer_lists):
        if names:
            ids = [task_to_id[v] for v in names]
            writer_ids[pos:pos + len(ids)] = ids
            writer_table_ids[pos:pos + len(ids)] = q
            first_writer[q] = ids[0]
            pos += len(ids)
    has_z = np.fromiter((ZW in v for v in fallback_lists), dtype=bool, count=nq)
    has_h = np.fromiter((HL in v for v in fallback_lists), dtype=bool, count=nq)
    fallback_first_h = np.fromiter((v[0] == HL for v in fallback_lists), dtype=bool, count=nq)
    table_lookup = pl.DataFrame(
        {"table_uri": table_uris, "table_id": np.arange(nq, dtype=np.uint32)},
        schema={"table_uri": pl.String, "table_id": pl.UInt32},
    )

    ex = (
        pl.scan_parquet(DATA / "exec_train_0808_0809.parquet")
        .join(task_lookup.lazy(), on="task_name", how="left", nulls_equal=True)
        .with_columns(
            (pl.col("dt") == DAYS[1]).cast(pl.UInt8).alias("day"),
            (
                pl.col("running_start_time").str.slice(11, 2).cast(pl.Int16) * 60
                + pl.col("running_start_time").str.slice(14, 2).cast(pl.Int16)
            ).alias("start"),
            (
                pl.col("running_end_time").str.slice(11, 2).cast(pl.Int16) * 60
                + pl.col("running_end_time").str.slice(14, 2).cast(pl.Int16)
            ).alias("end"),
            (pl.col("avg_mb") / 1024.0).alias("memory_gb"),
        )
        .select("task_id", "day", "start", "end", "avg_vcores", "memory_gb")
        .collect()
    )
    ex_task = ex["task_id"].to_numpy().astype(np.int64, copy=False)
    ex_day = ex["day"].to_numpy().astype(np.int8, copy=False)
    ex_start = ex["start"].to_numpy().astype(np.int16, copy=False)
    ex_end = ex["end"].to_numpy().astype(np.int16, copy=False)
    ex_v = ex["avg_vcores"].to_numpy().astype(np.float64, copy=False)
    ex_m = ex["memory_gb"].to_numpy().astype(np.float64, copy=False)
    total_v = _profile(ex_day, ex_start, ex_end, ex_v)
    total_m = _profile(ex_day, ex_start, ex_end, ex_m)
    ex_order = np.argsort(ex_task, kind="stable")
    ex_counts = np.bincount(ex_task, minlength=ntasks)
    ex_task_offsets = np.empty(ntasks + 1, dtype=np.int64)
    ex_task_offsets[0] = 0
    np.cumsum(ex_counts, out=ex_task_offsets[1:])

    tr = (
        pl.scan_parquet(DATA / "trans_train_0808_0809.parquet")
        .join(task_lookup.lazy(), on="task_name", how="left", nulls_equal=True)
        .join(table_lookup.lazy(), on="table_uri", how="left")
        .with_columns(
            (pl.col("dt") == DAYS[1]).cast(pl.UInt8).alias("day"),
            _minute_expr("trans_start_time").alias("start"),
            _minute_expr("trans_time").alias("duration"),
        )
        .with_columns((pl.col("start") + pl.col("duration")).alias("end"))
        .select(
            "task_id", "table_id", "day", "start", "end",
            "relation_type", "estimated_bandwidth",
        )
        .collect()
    )
    tr_task = tr["task_id"].to_numpy().astype(np.int64, copy=False)
    tr_table = tr["table_id"].to_numpy().astype(np.int64, copy=False)
    tr_day = tr["day"].to_numpy().astype(np.int8, copy=False)
    tr_start = tr["start"].to_numpy().astype(np.int16, copy=False)
    tr_end = tr["end"].to_numpy().astype(np.int16, copy=False)
    tr_rel = tr["relation_type"].to_numpy().astype(np.int8, copy=False)
    tr_bw = tr["estimated_bandwidth"].to_numpy().astype(np.float64, copy=False)

    table_traffic = np.bincount(tr_table, weights=tr_bw, minlength=nq)
    direct_traffic = np.bincount(tr_task, weights=tr_bw, minlength=ntasks)
    writer_traffic = np.zeros(ntasks, dtype=np.float64)
    np.add.at(writer_traffic, writer_ids, table_traffic[writer_table_ids])
    risk = direct_traffic + writer_traffic

    limits = json.loads((DATA / "resource_limits.json").read_text())["limits"]
    return PublicData(
        task_arrow, tasks, task_to_id, table_uris, offsets, writer_ids,
        writer_table_ids, nwriter, has_z, has_h, first_writer,
        fallback_first_h, ex_task, ex_day, ex_start, ex_end, ex_v, ex_m,
        ex_order, ex_task_offsets, total_v, total_m, tr_task, tr_table,
        tr_day, tr_start, tr_end, tr_rel, tr_bw, table_traffic, risk, limits,
    )


def task_resource_profile(data: PublicData, task_id: int) -> tuple[np.ndarray, np.ndarray]:
    lo = data.ex_task_offsets[task_id]
    hi = data.ex_task_offsets[task_id + 1]
    rows = data.ex_order[lo:hi]
    if len(rows) == 0:
        return np.zeros((2, MINUTES)), np.zeros((2, MINUTES))
    return (
        _profile(data.ex_day[rows], data.ex_start[rows], data.ex_end[rows], data.ex_v[rows]),
        _profile(data.ex_day[rows], data.ex_start[rows], data.ex_end[rows], data.ex_m[rows]),
    )


def resource_profiles(data: PublicData, assignment_h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    chosen = assignment_h[data.ex_task]
    return (
        _profile(data.ex_day, data.ex_start, data.ex_end, data.ex_v, chosen),
        _profile(data.ex_day, data.ex_start, data.ex_end, data.ex_m, chosen),
    )


def resource_state(data: PublicData, assignment_h: np.ndarray,
                   alpha: float = 1.0) -> dict:
    hv, hm = resource_profiles(data, assignment_h)
    return resource_state_from_profiles(data, hv, hm, alpha)


def resource_state_from_profiles(data: PublicData, hv: np.ndarray, hm: np.ndarray,
                                 alpha: float = 1.0) -> dict:
    cvz = data.limits["vcores_zw_peak"] * alpha
    cmz = data.limits["memory_zw_peak_gb"] * alpha
    cvh = data.limits["vcores_hl_peak"] * alpha
    cmh = data.limits["memory_hl_peak_gb"] * alpha
    residuals = np.stack([
        (data.total_v - hv - cvz) / max(cvz, 1.0),
        (hv - cvh) / max(cvh, 1.0),
        (data.total_m - hm - cmz) / max(cmz, 1.0),
        (hm - cmh) / max(cmh, 1.0),
    ])
    positive = np.maximum(residuals, 0.0)
    return {
        "hv": hv,
        "hm": hm,
        "theta": float(positive.max(initial=0.0)),
        "violated_groups": int(np.count_nonzero(np.max(positive, axis=(1, 2)) > 1e-10)),
        "violated_points": int(np.count_nonzero(positive > 1e-10)),
        "min_slack_fraction": float(-residuals.max()),
        "residuals": residuals,
    }


def bandwidth_state(data: PublicData, assignment_h: np.ndarray,
                    return_masks: bool = False) -> dict:
    writer_h = np.bincount(
        data.writer_table_ids,
        weights=assignment_h[data.writer_ids].astype(np.int8),
        minlength=len(data.table_uris),
    )
    q = data.tr_table
    tx = assignment_h[data.tr_task]
    has_w = data.nwriters[q] > 0
    table_all_h = np.where(has_w, writer_h[q] == data.nwriters[q], ~data.fallback_has_z[q])
    table_all_z = np.where(has_w, writer_h[q] == 0, ~data.fallback_has_h[q])
    cross_hz = (~tx) & table_all_h  # table H, task Z
    cross_zh = tx & table_all_z     # task H, table Z
    mask_zh = ((data.tr_rel == 0) & cross_zh) | ((data.tr_rel != 0) & cross_hz)
    mask_hz = ((data.tr_rel == 0) & cross_hz) | ((data.tr_rel != 0) & cross_zh)
    prof_zh = _profile(data.tr_day, data.tr_start, data.tr_end, data.tr_bw, mask_zh)
    prof_hz = _profile(data.tr_day, data.tr_start, data.tr_end, data.tr_bw, mask_hz)
    profiles = np.stack([prof_zh, prof_hz], axis=1)  # day, direction, minute
    daily = profiles.max(axis=2)
    daily_max = daily.max(axis=1)
    result = {
        "profiles": profiles,
        "directional_peaks": daily,
        "daily_max": daily_max,
        "peak": float(daily_max.max()),
        "mean": float(daily_max.mean()),
        "spread": float(abs(daily_max[0] - daily_max[1])),
    }
    if return_masks:
        result["masks"] = (mask_zh, mask_hz)
    return result


def fixed_endpoint_lower_bound(data: PublicData) -> dict:
    """Assignment-independent visible lower bound from fixed fallback tables.

    For each task, staying in Zhongwei exposes every fixed-Huailai row while
    moving to Huailai exposes every fixed-Zhongwei row.  The smaller of the two
    largest individual rows is unavoidable for that task under either choice.
    """
    ntasks = len(data.tasks)
    fixed_h = (data.nwriters[data.tr_table] == 0) & (~data.fallback_has_z[data.tr_table])
    fixed_z = (data.nwriters[data.tr_table] == 0) & (~data.fallback_has_h[data.tr_table])
    peak_if_z = np.zeros(ntasks, dtype=np.float64)
    peak_if_h = np.zeros(ntasks, dtype=np.float64)
    np.maximum.at(peak_if_z, data.tr_task[fixed_h], data.tr_bw[fixed_h])
    np.maximum.at(peak_if_h, data.tr_task[fixed_z], data.tr_bw[fixed_z])
    per_task = np.minimum(peak_if_z, peak_if_h)
    tid = int(np.argmax(per_task))
    daily_floor = []
    for day in (0, 1):
        rows = fixed_h & (data.tr_task == tid) & (data.tr_day == day)
        daily_floor.append(float(data.tr_bw[rows].max(initial=0.0)))
    return {
        "task_id": tid,
        "task_name": data.tasks[tid],
        "lower_bound": float(per_task[tid]),
        "largest_row_if_zhongwei": float(peak_if_z[tid]),
        "largest_row_if_huailai": float(peak_if_h[tid]),
        "daily_floor_if_zhongwei": daily_floor,
    }


def _candidate_key(bw: dict, interventions: int) -> tuple:
    # Outside the 0.050 Gbps near-tie band the peak dominates.  This helper is
    # used only for exact comparisons that already enforce that band.
    return (interventions, bw["mean"], bw["spread"])


def optimize_bandwidth(data: PublicData, assignment_h: np.ndarray,
                       max_moves: int = 30) -> tuple[np.ndarray, list[dict]]:
    """Peak-influence search with exact two-day replay after accepted moves."""
    x = assignment_h.copy()
    history: list[dict] = []
    current = bandwidth_state(data, x, return_masks=True)
    resource = resource_state(data, x, 1.0)
    cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for it in range(max_moves):
        flat = np.unravel_index(np.argmax(current["profiles"]), current["profiles"].shape)
        day, direction, minute = map(int, flat)
        cross_mask = current["masks"][direction]
        active = (
            cross_mask & (data.tr_day == day) &
            (data.tr_start <= minute) & (data.tr_end >= minute)
        )
        if not np.any(active):
            break
        contribution = np.bincount(
            data.tr_task[active], weights=data.tr_bw[active], minlength=len(data.tasks)
        )
        # Adding Huailai tasks is the intended sparse search direction.  The
        # highest current-peak contributors are exact-tested below.
        contribution[x] = 0.0
        positive = np.flatnonzero(contribution > 0.0)
        if len(positive) == 0:
            break
        ntest = min(4, len(positive))
        top = positive[np.argpartition(contribution[positive], -ntest)[-ntest:]]
        top = top[np.argsort(contribution[top])[::-1]]
        best = None
        for tid in top:
            tid = int(tid)
            if tid not in cache:
                cache[tid] = task_resource_profile(data, tid)
            pv, pm = cache[tid]
            # Never accept a bandwidth move that overloads Huailai.
            if (resource["hv"] + pv).max() > data.limits["vcores_hl_peak"] * (1 - 2e-8):
                continue
            if (resource["hm"] + pm).max() > data.limits["memory_hl_peak_gb"] * (1 - 2e-8):
                continue
            trial = x.copy()
            trial[tid] = True
            trial_bw = bandwidth_state(data, trial, return_masks=False)
            improvement = current["peak"] - trial_bw["peak"]
            if improvement <= NEAR_TIE:
                continue
            if best is None or trial_bw["peak"] < best[0]["peak"] - 1e-6 or (
                abs(trial_bw["peak"] - best[0]["peak"]) <= 1e-6
                and _candidate_key(trial_bw, int(trial.sum()))
                < _candidate_key(best[0], int(x.sum()) + 1)
            ):
                best = (trial_bw, tid, pv, pm, float(contribution[tid]))
        if best is None:
            break
        trial_bw, tid, pv, pm, influence = best
        before = current["peak"]
        x[tid] = True
        resource["hv"] += pv
        resource["hm"] += pm
        current = bandwidth_state(data, x, return_masks=True)
        history.append({
            "iteration": it + 1,
            "task_id": tid,
            "task_name": data.tasks[tid],
            "peak_before_gbps": before / 1e9,
            "peak_after_gbps": current["peak"] / 1e9,
            "peak_influence_gbps": influence / 1e9,
        })
    return x, history


def _lexicographically_better(candidate: dict, incumbent: dict,
                              tolerance: float = 1e-3) -> bool:
    """True public objective: worst peak, then mean, then temporal spread."""
    if candidate["peak"] < incumbent["peak"] - tolerance:
        return True
    if abs(candidate["peak"] - incumbent["peak"]) <= tolerance:
        if candidate["mean"] < incumbent["mean"] - tolerance:
            return True
        if abs(candidate["mean"] - incumbent["mean"]) <= tolerance:
            return candidate["spread"] < incumbent["spread"] - tolerance
    return False


def optimize_secondary_days(data: PublicData, assignment_h: np.ndarray,
                            alpha: float, max_moves: int = 30
                            ) -> tuple[np.ndarray, list[dict], list[dict], dict]:
    """Reversible task/writer-group search after reaching the primary floor.

    Candidate generation is based only on tasks and writer groups influencing
    the exact daily maxima.  Every accepted bundle is checked by full two-day
    replay and the tightened resource envelope.
    """
    x = assignment_h.copy()
    history: list[dict] = []
    pareto: list[dict] = []
    writer_groups_tested = 0
    current = bandwidth_state(data, x, return_masks=True)
    pareto.append({
        "stage": "incumbent",
        "interventions": int(x.sum()),
        "peak_gbps": current["peak"] / 1e9,
        "daily_max_gbps": (current["daily_max"] / 1e9).tolist(),
        "mean_gbps": current["mean"] / 1e9,
        "spread_gbps": current["spread"] / 1e9,
    })

    for it in range(max_moves):
        bundles: set[tuple[int, ...]] = set()
        for day in (0, 1):
            direction = int(np.argmax(current["directional_peaks"][day]))
            minute = int(np.argmax(current["profiles"][day, direction]))
            active = (
                current["masks"][direction] & (data.tr_day == day)
                & (data.tr_start <= minute) & (data.tr_end >= minute)
            )
            if not np.any(active):
                continue
            contribution = np.bincount(
                data.tr_task[active], weights=data.tr_bw[active],
                minlength=len(data.tasks),
            )
            positive = np.flatnonzero(contribution > 0.0)
            ntest = min(4, len(positive))
            if ntest:
                top = positive[np.argpartition(contribution[positive], -ntest)[-ntest:]]
                for tid in top:
                    bundles.add((int(tid),))

            # Explicit coordinated writer neighborhoods for the largest active
            # transfer rows.  Fixed-fallback tables naturally add no group.
            rows = np.flatnonzero(active)
            nrows = min(6, len(rows))
            if nrows:
                top_rows = rows[np.argpartition(data.tr_bw[rows], -nrows)[-nrows:]]
                for row in top_rows:
                    q = int(data.tr_table[row])
                    lo, hi = data.writer_offsets[q], data.writer_offsets[q + 1]
                    writers = tuple(map(int, data.writer_ids[lo:hi]))
                    if not writers or len(writers) > 50:
                        continue
                    writer_groups_tested += 1
                    bundles.add(tuple(sorted(writers)))
                    bundles.add(tuple(sorted(set(writers + (int(data.tr_task[row]),)))))
                    for writer in writers:
                        bundles.add((writer,))

        best = None
        for bundle in bundles:
            trial = x.copy()
            ids = np.asarray(bundle, dtype=np.int64)
            trial[ids] = ~trial[ids]
            trial_resource = resource_state(data, trial, alpha)
            if trial_resource["theta"] > 1e-10:
                continue
            trial_bw = bandwidth_state(data, trial)
            if not _lexicographically_better(trial_bw, current):
                continue
            key = (
                trial_bw["peak"], trial_bw["mean"], trial_bw["spread"],
                int(trial.sum()), len(bundle), bundle,
            )
            if best is None or key < best[0]:
                best = (key, bundle, trial_bw, trial_resource)
        if best is None:
            break
        _, bundle, accepted_bw, accepted_resource = best
        before = current
        ids = np.asarray(bundle, dtype=np.int64)
        old_values = x[ids].copy()
        x[ids] = ~x[ids]
        current = bandwidth_state(data, x, return_masks=True)
        record = {
            "iteration": it + 1,
            "task_ids": list(bundle),
            "task_names": [data.tasks[i] for i in bundle],
            "old_huailai": old_values.tolist(),
            "new_huailai": x[ids].tolist(),
            "peak_before_gbps": before["peak"] / 1e9,
            "peak_after_gbps": accepted_bw["peak"] / 1e9,
            "daily_max_gbps": (accepted_bw["daily_max"] / 1e9).tolist(),
            "mean_gbps": accepted_bw["mean"] / 1e9,
            "spread_gbps": accepted_bw["spread"] / 1e9,
            "interventions": int(x.sum()),
            "theta_099": accepted_resource["theta"],
        }
        history.append(record)
        pareto.append({"stage": f"move_{it + 1}", **{
            k: record[k] for k in (
                "interventions", "peak_after_gbps", "daily_max_gbps",
                "mean_gbps", "spread_gbps",
            )
        }})
    diagnostics = {
        "writer_groups_tested": writer_groups_tested,
        "balance_ratio": float(current["daily_max"].min() / current["daily_max"].max()),
        "balance_target_0_85_met": bool(
            current["daily_max"].min() <= 0.85 * current["daily_max"].max() + 1e-3
        ),
    }
    return x, history, pareto, diagnostics


def greedy_resource_repair(data: PublicData, base_h: np.ndarray, alpha: float,
                           max_additions: int = 900) -> tuple[np.ndarray, set[int], set[int], dict]:
    """Restore tightened resource feasibility and collect a SCIP neighborhood."""
    x = base_h.copy()
    state = resource_state(data, x, alpha)
    selected: set[int] = set()
    alternatives: set[int] = set()
    cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    cvh = data.limits["vcores_hl_peak"] * alpha * (1 - 2e-8)
    cmh = data.limits["memory_hl_peak_gb"] * alpha * (1 - 2e-8)
    ntasks = len(data.tasks)

    for _ in range(max_additions):
        if state["theta"] <= 1e-10:
            break
        # Restoration should address Zhongwei lower-bound deficits.  Huailai
        # overloads are prevented before every accepted addition.
        lower = state["residuals"][[0, 2]]
        which, day, minute = map(int, np.unravel_index(np.argmax(lower), lower.shape))
        if lower[which, day, minute] <= 1e-10:
            break
        active_rows = (
            (data.ex_day == day) & (data.ex_start <= minute) &
            (data.ex_end >= minute)
        )
        weights = data.ex_v if which == 0 else data.ex_m
        contribution = np.bincount(
            data.ex_task[active_rows], weights=weights[active_rows], minlength=ntasks
        )
        contribution[x] = 0.0
        positive = np.flatnonzero(contribution > 1e-12)
        if len(positive) == 0:
            raise RuntimeError("Resource restoration has no influencing task")

        # First seek exact zero-network-impact tasks.  If none can be safely
        # added, widen to the lowest-risk influential tasks.
        zero = positive[data.task_network_risk[positive] == 0.0]
        # After 100 exact-zero-impact additions, widen the operator.  This is a
        # measurable-stall response and prevents hundreds of tiny choices from
        # crowding out one materially stronger, low-risk task.
        zero_only = len(zero) and len(selected) < 100
        pool = zero if zero_only else positive
        nshort = min(35, len(pool))
        if zero_only:
            short = pool[np.argpartition(contribution[pool], -nshort)[-nshort:]]
        else:
            score = contribution[pool] / (1.0 + np.log1p(data.task_network_risk[pool]))
            short = pool[np.argpartition(score, -nshort)[-nshort:]]
        alternatives.update(map(int, short))

        # Exact profile scoring rewards coverage of all current deficits, not
        # only the single worst minute.
        deficit_v = np.maximum(data.total_v - state["hv"] - data.limits["vcores_zw_peak"] * alpha, 0.0)
        deficit_m = np.maximum(data.total_m - state["hm"] - data.limits["memory_zw_peak_gb"] * alpha, 0.0)
        best = None
        for raw_tid in short:
            tid = int(raw_tid)
            if tid not in cache:
                cache[tid] = task_resource_profile(data, tid)
            pv, pm = cache[tid]
            if (state["hv"] + pv).max() > cvh or (state["hm"] + pm).max() > cmh:
                continue
            gain = (
                np.minimum(pv, deficit_v).sum() / max(data.limits["vcores_zw_peak"], 1.0)
                + np.minimum(pm, deficit_m).sum() / max(data.limits["memory_zw_peak_gb"], 1.0)
            )
            risk = data.task_network_risk[tid]
            key = ((risk > 0.0) if zero_only else False,
                   -gain, risk, -contribution[tid], tid)
            if best is None or key < best[0]:
                best = (key, tid, pv, pm)
        if best is None:
            # Change operator: widen to high-contribution tasks regardless of
            # risk and exact-test more alternatives.
            nwide = min(120, len(positive))
            wide = positive[np.argpartition(contribution[positive], -nwide)[-nwide:]]
            alternatives.update(map(int, wide))
            for raw_tid in wide:
                tid = int(raw_tid)
                if tid not in cache:
                    cache[tid] = task_resource_profile(data, tid)
                pv, pm = cache[tid]
                if (state["hv"] + pv).max() > cvh or (state["hm"] + pm).max() > cmh:
                    continue
                gain = (
                    np.minimum(pv, deficit_v).sum() / max(data.limits["vcores_zw_peak"], 1.0)
                    + np.minimum(pm, deficit_m).sum() / max(data.limits["memory_zw_peak_gb"], 1.0)
                )
                key = (data.task_network_risk[tid] > 0.0, -gain,
                       data.task_network_risk[tid], -contribution[tid], tid)
                if best is None or key < best[0]:
                    best = (key, tid, pv, pm)
        if best is None:
            raise RuntimeError("Resource restoration stalled: all influencing additions overload Huailai")

        _, tid, pv, pm = best
        x[tid] = True
        selected.add(tid)
        state["hv"] += pv
        state["hm"] += pm
        state = resource_state_from_profiles(data, state["hv"], state["hm"], alpha)

    if state["theta"] > 1e-10:
        raise RuntimeError(f"Resource restoration did not converge: theta={state['theta']}")
    return x, selected, alternatives, state


def scip_project_resource(data: PublicData, base_h: np.ndarray, candidate_h: np.ndarray,
                          selected: set[int], alternatives: set[int], alpha: float,
                          time_limit: float) -> tuple[np.ndarray, dict]:
    """Exact repair MIP on the active neighborhood, keeping all other tasks fixed."""
    cand = sorted((selected | alternatives) - set(np.flatnonzero(base_h)))
    if not cand:
        return candidate_h.copy(), {"status": "no_variables", "nsols": 1, "gap": 0.0}
    # Keep the neighborhood bounded for a fast from-scratch dispatch.  Selected
    # incumbent tasks are never removed from the candidate list.
    optional = [i for i in cand if i not in selected]
    optional.sort(key=lambda i: (data.task_network_risk[i], i))
    cand = sorted(selected) + optional[:max(0, 180 - len(selected))]
    cand = list(dict.fromkeys(cand))
    index = {tid: j for j, tid in enumerate(cand)}
    n = len(cand)
    pv = np.zeros((n, 2, MINUTES), dtype=np.float64)
    pm = np.zeros((n, 2, MINUTES), dtype=np.float64)
    for j, tid in enumerate(cand):
        pv[j], pm[j] = task_resource_profile(data, tid)

    fixed = base_h.copy()
    fixed[cand] = False
    fixed_v, fixed_m = resource_profiles(data, fixed)
    cvz = data.limits["vcores_zw_peak"] * alpha * (1 - 1e-8)
    cmz = data.limits["memory_zw_peak_gb"] * alpha * (1 - 1e-8)
    cvh = data.limits["vcores_hl_peak"] * alpha * (1 - 2e-8)
    cmh = data.limits["memory_hl_peak_gb"] * alpha * (1 - 2e-8)

    model = Model("resource_projection")
    model.hideOutput(True)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", float(time_limit))
    model.setParam("randomization/randomseedshift", 0)
    y = [model.addVar(vtype="B", name=f"h[{tid}]") for tid in cand]

    for d in (0, 1):
        for t in range(MINUTES):
            nzv = np.flatnonzero(pv[:, d, t] > 0.0)
            nzm = np.flatnonzero(pm[:, d, t] > 0.0)
            expr_v = quicksum(float(pv[j, d, t]) * y[j] for j in nzv)
            expr_m = quicksum(float(pm[j, d, t]) * y[j] for j in nzm)
            # Monotonicity permits safe removal of constraints that remain
            # satisfied at both all-zero and all-one neighborhood bounds.
            if fixed_v[d, t] + float(pv[:, d, t].sum()) > cvh:
                model.addCons(expr_v + float(fixed_v[d, t]) <= cvh,
                              name=f"vh[{d},{t}]")
            if fixed_v[d, t] < float(data.total_v[d, t] - cvz):
                model.addCons(expr_v + float(fixed_v[d, t]) >= float(data.total_v[d, t] - cvz),
                              name=f"vz[{d},{t}]")
            if fixed_m[d, t] + float(pm[:, d, t].sum()) > cmh:
                model.addCons(expr_m + float(fixed_m[d, t]) <= cmh,
                              name=f"mh[{d},{t}]")
            if fixed_m[d, t] < float(data.total_m[d, t] - cmz):
                model.addCons(expr_m + float(fixed_m[d, t]) >= float(data.total_m[d, t] - cmz),
                              name=f"mz[{d},{t}]")

    # Phase II projection: after theta=0, minimize exact Hamming distance to the
    # known feasible candidate.  Its zero objective is also a proof of optimality.
    model.setObjective(
        quicksum((1.0 - y[j]) if candidate_h[tid] else y[j]
                 for j, tid in enumerate(cand)),
        "minimize",
    )
    sol = model.createSol()
    for j, tid in enumerate(cand):
        model.setSolVal(sol, y[j], 1.0 if candidate_h[tid] else 0.0)
    model.addSol(sol, free=True)
    model.optimize()
    nsols = model.getNSols()
    info = {"status": str(model.getStatus()), "nsols": int(nsols)}
    if nsols <= 0:
        raise RuntimeError(f"SCIP projection returned no incumbent ({info['status']})")
    best = model.getBestSol()
    result = fixed.copy()
    for j, tid in enumerate(cand):
        result[tid] = model.getSolVal(best, y[j]) > 0.5
    info.update({
        "objective": float(model.getSolObjVal(best)),
        "best_bound": float(model.getDualbound()),
        "gap": float(model.getGap()),
        "variables": n,
        "constraints": int(model.getNConss()),
    })
    return result, info


def scip_minimize_zero_network_subset(data: PublicData, candidate_h: np.ndarray,
                                      alpha: float, time_limit: float
                                      ) -> tuple[np.ndarray, dict]:
    """Exact local-branching subset solve with the candidate as a MIP start.

    Only currently-Huailai tasks with exactly zero direct and writer-induced
    public transfer traffic vary.  Therefore bandwidth is invariant throughout
    this neighborhood and the MIP can minimize intervention count subject to
    the complete tightened resource replay.
    """
    cand = list(map(int, np.flatnonzero(candidate_h & (data.task_network_risk == 0.0))))
    if not cand:
        return candidate_h.copy(), {
            "status": "no_variables", "nsols": 1, "objective": 0.0,
            "best_bound": 0.0, "gap": 0.0, "variables": 0, "constraints": 0,
        }
    fixed = candidate_h.copy()
    fixed[cand] = False
    fixed_v, fixed_m = resource_profiles(data, fixed)
    n = len(cand)
    pv = np.zeros((n, 2, MINUTES), dtype=np.float64)
    pm = np.zeros((n, 2, MINUTES), dtype=np.float64)
    for j, tid in enumerate(cand):
        pv[j], pm[j] = task_resource_profile(data, tid)

    cvz = data.limits["vcores_zw_peak"] * alpha * (1 - 1e-8)
    cmz = data.limits["memory_zw_peak_gb"] * alpha * (1 - 1e-8)
    cvh = data.limits["vcores_hl_peak"] * alpha * (1 - 2e-8)
    cmh = data.limits["memory_hl_peak_gb"] * alpha * (1 - 2e-8)
    model = Model("zero_network_resource_subset")
    model.hideOutput(True)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", float(time_limit))
    model.setParam("randomization/randomseedshift", 0)
    y = [model.addVar(vtype="B", name=f"h[{tid}]") for tid in cand]

    for day in (0, 1):
        for t in range(MINUTES):
            av = np.flatnonzero(pv[:, day, t] > 0.0)
            am = np.flatnonzero(pm[:, day, t] > 0.0)
            expr_v = quicksum(float(pv[j, day, t]) * y[j] for j in av)
            expr_m = quicksum(float(pm[j, day, t]) * y[j] for j in am)
            if fixed_v[day, t] + float(pv[:, day, t].sum()) > cvh:
                model.addCons(expr_v + float(fixed_v[day, t]) <= cvh,
                              name=f"vh[{day},{t}]")
            if fixed_v[day, t] < float(data.total_v[day, t] - cvz):
                model.addCons(expr_v + float(fixed_v[day, t]) >= float(data.total_v[day, t] - cvz),
                              name=f"vz[{day},{t}]")
            if fixed_m[day, t] + float(pm[:, day, t].sum()) > cmh:
                model.addCons(expr_m + float(fixed_m[day, t]) <= cmh,
                              name=f"mh[{day},{t}]")
            if fixed_m[day, t] < float(data.total_m[day, t] - cmz):
                model.addCons(expr_m + float(fixed_m[day, t]) >= float(data.total_m[day, t] - cmz),
                              name=f"mz[{day},{t}]")

    model.setObjective(quicksum(y), "minimize")
    start = model.createSol()
    for var in y:
        model.setSolVal(start, var, 1.0)
    model.addSol(start, free=True)
    model.optimize()
    nsols = model.getNSols()
    info = {"status": str(model.getStatus()), "nsols": int(nsols)}
    if nsols <= 0:
        raise RuntimeError(f"SCIP zero-network subset returned no incumbent ({info['status']})")
    best = model.getBestSol()
    result = fixed.copy()
    for j, tid in enumerate(cand):
        result[tid] = model.getSolVal(best, y[j]) > 0.5
    info.update({
        "objective": float(model.getSolObjVal(best)),
        "best_bound": float(model.getDualbound()),
        "gap": float(model.getGap()),
        "variables": n,
        "constraints": int(model.getNConss()),
        "mip_start_interventions": int(candidate_h.sum()),
        "result_interventions": int(result.sum()),
    })
    return result, info


def migration_metrics(data: PublicData, assignment_h: np.ndarray) -> dict:
    current = pl.read_parquet(DATA / "current_table_regions.parquet")
    sizes = pl.read_parquet(DATA / "table_size_latest.parquet").select("table_uri", "table_size")
    loc_h = data.fallback_first_h.copy()
    with_w = data.first_writer >= 0
    loc_h[with_w] = assignment_h[data.first_writer[with_w]]
    candidate = pl.DataFrame({
        "table_uri": data.table_uris,
        "candidate_region": np.where(loc_h, HL, ZW),
    })
    m = current.join(candidate, on="table_uri", how="inner").join(sizes, on="table_uri", how="left")
    m = m.with_columns(
        pl.col("table_size").is_null().alias("size_missing"),
        pl.col("table_size").fill_null(0),
    )
    changed = m.filter(
        pl.col("current_region").is_not_null()
        & (pl.col("current_region") != pl.col("candidate_region"))
    )
    zh = changed.filter((pl.col("current_region") == ZW) & (pl.col("candidate_region") == HL))
    hz = changed.filter((pl.col("current_region") == HL) & (pl.col("candidate_region") == ZW))
    missing = changed.filter(pl.col("size_missing")).height
    return {
        "changed_tables": changed.height,
        "zw_to_hl_bytes": int(zh["table_size"].sum() or 0),
        "hl_to_zw_bytes": int(hz["table_size"].sum() or 0),
        "total_bytes": int(changed["table_size"].sum() or 0),
        "missing_size_changed_tables": int(missing),
    }


def export_solution(data: PublicData, assignment_h: np.ndarray) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    regions = pa.array(np.where(assignment_h, HL, ZW), type=pa.large_string())
    table = pa.Table.from_arrays([data.task_arrow, regions], names=["task_name", "region"])
    path = OUT / "solution.parquet"
    pq.write_table(table, path, compression="zstd")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    (OUT / "solution.json").write_text(
        json.dumps({"artifact_path": str(path), "sha256": sha}, ensure_ascii=False, indent=2) + "\n"
    )
    return sha


def artifact_schema_check(data: PublicData, assignment_h: np.ndarray, sha: str) -> dict:
    path = OUT / "solution.parquet"
    t = pq.read_table(path)
    manifest = json.loads((OUT / "solution.json").read_text())
    exact_columns = t.column_names == ["task_name", "region"]
    task_match = t.num_rows == len(data.tasks) and t["task_name"].equals(data.task_arrow)
    domain = set(t["region"].to_pylist()) <= {ZW, HL}
    actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "artifact_present": path.exists(),
        "rows": t.num_rows,
        "exact_columns": exact_columns,
        "task_order_and_null_preserved": task_match,
        "region_domain": domain,
        "manifest_exact_keys": set(manifest) == {"artifact_path", "sha256"},
        "sha256_match": actual_sha == sha == manifest.get("sha256"),
        "decision_universe_coverage": 1.0 if task_match else 0.0,
        "integrality_residual": 0.0 if domain else 1.0,
    }


def choose_candidate(candidates: list[dict]) -> dict:
    feasible = [c for c in candidates if c["resource_official"]["theta"] <= 1e-10]
    if not feasible:
        raise RuntimeError("No officially feasible candidate")
    # Preserve the public lexicographic objective.  Intervention count remains
    # a reported Pareto regularizer, not a reason to discard a better two-day
    # mean after the primary peak reaches its structural floor.
    return min(
        feasible,
        key=lambda c: (
            c["bandwidth"]["peak"], c["bandwidth"]["mean"],
            c["bandwidth"]["spread"], int(c["assignment"].sum()),
            -c["resource_official"]["min_slack_fraction"], -c["alpha"],
        ),
    )


def main() -> None:
    started = time.perf_counter()
    data = load_public_data()
    load_seconds = time.perf_counter() - started
    print(f"loaded public data in {load_seconds:.3f}s", flush=True)
    base = np.zeros(len(data.tasks), dtype=bool)

    bandwidth_h, move_history = optimize_bandwidth(data, base)
    bandwidth_after_search = bandwidth_state(data, bandwidth_h)
    print(
        f"bandwidth search: {len(move_history)} moves, "
        f"peak={bandwidth_after_search['peak']/1e9:.6f} Gbps, "
        f"elapsed={time.perf_counter()-started:.3f}s",
        flush=True,
    )

    alpha_summaries = []
    raw_candidates = []
    for alpha in (0.990, 0.985, 0.980, 0.970):
        try:
            repaired, selected, alternatives, tight_state = greedy_resource_repair(
                data, bandwidth_h, alpha
            )
        except RuntimeError as exc:
            alpha_summaries.append({
                "alpha": alpha, "status": "construction_failed", "reason": str(exc)
            })
            print(f"alpha={alpha:.3f} failed: {exc}", flush=True)
            continue
        bw = bandwidth_state(data, repaired)
        official = resource_state(data, repaired, 1.0)
        record = {
            "alpha": alpha,
            "assignment": repaired,
            "selected": selected,
            "alternatives": alternatives,
            "tight_state": tight_state,
            "bandwidth": bw,
            "resource_official": official,
        }
        raw_candidates.append(record)
        alpha_summaries.append({
            "alpha": alpha,
            "status": "feasible",
            "interventions": int(repaired.sum()),
            "resource_tasks": len(selected),
            "theta_tightened": tight_state["theta"],
            "official_theta": official["theta"],
            "visible_peak_gbps": bw["peak"] / 1e9,
            "daily_max_gbps": (bw["daily_max"] / 1e9).tolist(),
        })
        print(
            f"alpha={alpha:.3f}: tasks={int(repaired.sum())}, "
            f"peak={bw['peak']/1e9:.6f} Gbps, elapsed={time.perf_counter()-started:.3f}s",
            flush=True,
        )

    chosen_raw = choose_candidate(raw_candidates)
    incumbent_assignment = chosen_raw["assignment"].copy()
    incumbent_bw = bandwidth_state(data, incumbent_assignment)
    incumbent_resource = resource_state(data, incumbent_assignment, chosen_raw["alpha"])
    improved, secondary_history, pareto_points, balance_diagnostic = optimize_secondary_days(
        data, incumbent_assignment, chosen_raw["alpha"]
    )
    improved_bw = bandwidth_state(data, improved)
    print(
        f"secondary search: {len(secondary_history)} moves, "
        f"daily={(improved_bw['daily_max']/1e9).tolist()} Gbps, "
        f"elapsed={time.perf_counter()-started:.3f}s",
        flush=True,
    )

    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    invocation_limit = min(float(env_limit), 12.0) if env_limit else 12.0
    if invocation_limit < 0.0:
        raise ValueError("ORCLAW_SOLVE_TIME_LIMIT_SECONDS must be nonnegative")
    projected, scip_info = scip_minimize_zero_network_subset(
        data, improved, chosen_raw["alpha"], invocation_limit,
    )
    final_resource = resource_state(data, projected, 1.0)
    final_tight = resource_state(data, projected, chosen_raw["alpha"])
    final_bw = bandwidth_state(data, projected)
    if (
        final_resource["theta"] > 1e-10 or final_tight["theta"] > 1e-9
        or final_bw["peak"] > improved_bw["peak"] + 1e-3
        or final_bw["mean"] > improved_bw["mean"] + 1e-3
    ):
        # The exact-replayed performance candidate is the protected backup.
        projected = improved.copy()
        final_resource = resource_state(data, projected, 1.0)
        final_tight = resource_state(data, projected, chosen_raw["alpha"])
        final_bw = bandwidth_state(data, projected)
        scip_info["fallback_to_performance_incumbent"] = True

    strict_improvement = _lexicographically_better(final_bw, incumbent_bw)
    if not strict_improvement:
        projected = incumbent_assignment.copy()
        final_resource = resource_state(data, projected, 1.0)
        final_tight = resource_state(data, projected, chosen_raw["alpha"])
        final_bw = bandwidth_state(data, projected)
        scip_info["rollback_no_strict_objective_improvement"] = True
    pareto_points.append({
        "stage": "post_scip_subset",
        "interventions": int(projected.sum()),
        "peak_gbps": final_bw["peak"] / 1e9,
        "daily_max_gbps": (final_bw["daily_max"] / 1e9).tolist(),
        "mean_gbps": final_bw["mean"] / 1e9,
        "spread_gbps": final_bw["spread"] / 1e9,
    })
    lower_bound = fixed_endpoint_lower_bound(data)
    daily_floor = np.asarray(lower_bound["daily_floor_if_zhongwei"])
    mean_floor = float(daily_floor.mean())
    migration = migration_metrics(data, projected)

    sha = export_solution(data, projected)
    schema = artifact_schema_check(data, projected, sha)
    elapsed = time.perf_counter() - started
    if not all([
        schema["artifact_present"], schema["exact_columns"],
        schema["task_order_and_null_preserved"], schema["region_domain"],
        schema["manifest_exact_keys"], schema["sha256_match"],
        final_resource["theta"] <= 1e-10,
    ]):
        raise RuntimeError("Final artifact audit failed")

    provenance = {
        "embedded_external_task_mapping": False,
        "used_reference_mapping": False,
        "used_august10_data": False,
        "dispatch_time_seconds": round(elapsed, 8),
        "public_construction_days": list(DAYS),
        "selected_utilization_target": chosen_raw["alpha"],
        "solution_sha256": sha,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n"
    )

    log = {
        "command": "PYTHONPATH=/opt/or-deps python /app/submissions/solve.py",
        "dispatch_time_seconds": elapsed,
        "data_load_seconds": load_seconds,
        "bandwidth_moves": move_history,
        "bandwidth_search_peak_gbps": bandwidth_after_search["peak"] / 1e9,
        "secondary_moves": secondary_history,
        "pareto_points": pareto_points,
        "balance_diagnostic": balance_diagnostic,
        "utilization_target_comparison": alpha_summaries,
        "selected_alpha": chosen_raw["alpha"],
        "scip": scip_info,
        "final_interventions": int(projected.sum()),
        "incumbent_objective": {
            "interventions": int(incumbent_assignment.sum()),
            "worst_visible_peak_gbps": incumbent_bw["peak"] / 1e9,
            "daily_max_gbps": (incumbent_bw["daily_max"] / 1e9).tolist(),
            "mean_daily_max_gbps": incumbent_bw["mean"] / 1e9,
            "temporal_spread_gbps": incumbent_bw["spread"] / 1e9,
            "theta_099": incumbent_resource["theta"],
        },
        "final_bandwidth": {
            "worst_visible_peak_gbps": final_bw["peak"] / 1e9,
            "daily_directional_peaks_gbps": (final_bw["directional_peaks"] / 1e9).tolist(),
            "daily_max_gbps": (final_bw["daily_max"] / 1e9).tolist(),
            "mean_daily_max_gbps": final_bw["mean"] / 1e9,
            "temporal_spread_gbps": final_bw["spread"] / 1e9,
            "fixed_endpoint_lower_bound_gbps": lower_bound["lower_bound"] / 1e9,
            "gap_to_fixed_endpoint_lower_bound_gbps": (
                final_bw["peak"] - lower_bound["lower_bound"]
            ) / 1e9,
            "daily_fixed_endpoint_floors_gbps": (daily_floor / 1e9).tolist(),
            "mean_daily_floor_gbps": mean_floor / 1e9,
            "gap_to_mean_daily_floor_gbps": (final_bw["mean"] - mean_floor) / 1e9,
            "balance_ratio": float(final_bw["daily_max"].min() / final_bw["daily_max"].max()),
        },
        "state_vector": {
            **schema,
            "incumbent_count": scip_info.get("nsols", 1),
            "maximum_normalized_constraint_residual": final_resource["theta"],
            "violated_constraint_groups": final_resource["violated_groups"],
            "violated_constraint_points": final_resource["violated_points"],
            "selected_target_theta": final_tight["theta"],
            "objective_gbps": final_bw["peak"] / 1e9,
            "best_bound": scip_info.get("best_bound"),
            "gap": scip_info.get("gap"),
            "strictly_improves_constructed_incumbent": strict_improvement,
        },
        "migration": migration,
        "global_visible_lower_bound_certificate": {
            "task_name": lower_bound["task_name"],
            "lower_bound_gbps": lower_bound["lower_bound"] / 1e9,
            "largest_fixed_huailai_row_if_zhongwei_gbps": (
                lower_bound["largest_row_if_zhongwei"] / 1e9
            ),
            "largest_fixed_zhongwei_row_if_huailai_gbps": (
                lower_bound["largest_row_if_huailai"] / 1e9
            ),
            "daily_floor_if_zhongwei_gbps": (daily_floor / 1e9).tolist(),
            "mean_daily_floor_gbps": mean_floor / 1e9,
        },
        "near_tie_rule": (
            "Within 0.050 Gbps of the public worst-peak floor, retain low-intervention "
            "Pareto points but continue minimizing the other day's peak and the two-day "
            "mean; report intervention, spread, and slack tradeoffs transparently."
        ),
    }
    lines = [
        "# Solve log",
        "",
        f"- Command: `{log['command']}`",
        f"- Clean dispatch time: {elapsed:.8f} seconds",
        f"- SCIP status: {scip_info.get('status')} with {scip_info.get('nsols', 1)} incumbent(s)",
        f"- SCIP objective / best bound / gap: {scip_info.get('objective')} / {scip_info.get('best_bound')} / {scip_info.get('gap')}",
        f"- Constructed rollback-incumbent objective: peak={incumbent_bw['peak']/1e9:.8f} Gbps, mean={incumbent_bw['mean']/1e9:.8f} Gbps, tasks={int(incumbent_assignment.sum())}",
        f"- Candidate objective: peak={final_bw['peak']/1e9:.8f} Gbps, mean={final_bw['mean']/1e9:.8f} Gbps, tasks={int(projected.sum())}",
        f"- Final Huailai tasks: {int(projected.sum())}",
        f"- Worst visible directional peak: {final_bw['peak']/1e9:.8f} Gbps",
        f"- Global fixed-endpoint lower bound: {lower_bound['lower_bound']/1e9:.8f} Gbps",
        f"- Gap to that lower bound: {(final_bw['peak']-lower_bound['lower_bound'])/1e9:.8f} Gbps",
        f"- Daily maximum-direction peaks: {(final_bw['daily_max']/1e9).tolist()} Gbps",
        f"- Daily public floors: {(daily_floor/1e9).tolist()} Gbps",
        f"- Nonbinding/binding balance ratio: {final_bw['daily_max'].min()/final_bw['daily_max'].max():.8f}",
        f"- Official maximum normalized residual theta: {final_resource['theta']:.12g}",
        f"- Tightened ({chosen_raw['alpha']:.3f}) maximum normalized residual theta: {final_tight['theta']:.12g}",
        f"- Artifact SHA-256: `{sha}`",
        "",
        "## Utilization-target comparison",
        "",
        "```json",
        json.dumps(alpha_summaries, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Validation and state vector",
        "",
        "```json",
        json.dumps(log["state_vector"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Exact two-day Pareto portfolio",
        "",
        "```json",
        json.dumps(pareto_points, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Balance diagnostic",
        "",
        "```json",
        json.dumps(balance_diagnostic, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Migration auxiliary metric",
        "",
        "```json",
        json.dumps(migration, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Global visible lower-bound certificate",
        "",
        "```json",
        json.dumps(log["global_visible_lower_bound_certificate"], ensure_ascii=False, indent=2),
        "```",
        "",
        "Near-tie rule: " + log["near_tie_rule"],
    ]
    (OUT / "solve_log.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({
        "status": "ok", "seconds": elapsed, "sha256": sha,
        "peak_gbps": final_bw["peak"] / 1e9,
        "theta": final_resource["theta"], "huailai_tasks": int(projected.sum()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
