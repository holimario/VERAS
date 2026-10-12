#!/usr/bin/env python3
"""Construct and independently audit a two-region task assignment.

Only files under /app/data are used.  The algorithm starts from the all-Zhongwei
assignment, restores time-indexed resource feasibility, then performs exact
two-day peak-influence local search.  No prior task mapping is embedded.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
os.environ.setdefault("POLARS_MAX_THREADS", "8")
import polars as pl


ROOT = Path("/app")
DATA = ROOT / "data"
OUT = ROOT / "submissions"
ZW = "中卫"
HL = "怀来"
DAYS = ("20250808", "20250809")
MINUTES = 1440
SAFETY = 1.0 - 2.0e-8
GBPS = 8.0e-9  # estimated_bandwidth is bytes/second
NEAR_TIE_RAW = 0.050 / GBPS


def parse_hhmm(expr: pl.Expr) -> pl.Expr:
    return expr.str.slice(0, 2).cast(pl.Int32) * 60 + expr.str.slice(3, 2).cast(pl.Int32)


def parse_datetime_minute(expr: pl.Expr) -> pl.Expr:
    return expr.str.slice(11, 2).cast(pl.Int32) * 60 + expr.str.slice(14, 2).cast(pl.Int32)


def row_ranges(keys: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    order = np.argsort(keys, kind="quicksort")
    counts = np.bincount(keys, minlength=size).astype(np.int64, copy=False)
    starts = np.empty(size + 1, dtype=np.int64)
    starts[0] = 0
    np.cumsum(counts, out=starts[1:])
    return order, starts, counts


@dataclass
class Metrics:
    objective_raw: float
    daily_max_raw: list[float]
    daily_directional_raw: list[list[float]]
    peak_locations: list[list[int]]
    huailai_tasks: int
    theta: float
    violation_groups: int
    min_slack: dict[str, float]


class Instance:
    def __init__(self) -> None:
        limits = json.loads((DATA / "resource_limits.json").read_text())['limits']
        self.cap_z = np.array(
            [limits["vcores_zw_peak"], limits["memory_zw_peak_gb"]], dtype=np.float64
        )
        self.cap_h = np.array(
            [limits["vcores_hl_peak"], limits["memory_hl_peak_gb"]], dtype=np.float64
        )

        self.universe = pl.read_parquet(DATA / "task_universe.parquet").with_row_index("task_id")
        self.n_tasks = self.universe.height
        task_lookup = self.universe.filter(pl.col("task_name").is_not_null())

        self.writers = pl.read_parquet(DATA / "table_writers_fallback.parquet").with_row_index(
            "table_id"
        )
        self.n_tables = self.writers.height
        self.nwriters = self.writers["task_name_list"].list.len().to_numpy().astype(np.int32)
        self.fallback_has_z = self.writers["region_list"].list.contains(ZW).to_numpy()
        self.fallback_has_h = self.writers["region_list"].list.contains(HL).to_numpy()

        wx = (
            self.writers.filter(pl.col("task_name_list").list.len() > 0)
            .select("table_id", "task_name_list")
            .explode("task_name_list")
            .rename({"task_name_list": "task_name"})
            .join(task_lookup, on="task_name", how="inner")
        )
        self.writer_table = wx["table_id"].to_numpy().astype(np.int32, copy=False)
        self.writer_task = wx["task_id"].to_numpy().astype(np.int32, copy=False)
        self.writer_order, self.writer_starts, _ = row_ranges(self.writer_task, self.n_tasks)
        self.writer_table_order, self.writer_table_starts, _ = row_ranges(
            self.writer_table, self.n_tables
        )
        self.first_writer = np.full(self.n_tables, -1, dtype=np.int32)
        first = self.writers.filter(pl.col("task_name_list").list.len() > 0).select(
            "table_id", pl.col("task_name_list").list.first().alias("task_name")
        ).join(task_lookup, on="task_name", how="inner")
        self.first_writer[first["table_id"].to_numpy()] = first["task_id"].to_numpy()

        exec_df = (
            pl.read_parquet(DATA / "exec_train_0808_0809.parquet")
            .join(task_lookup, on="task_name", how="inner")
            .with_columns(
                parse_datetime_minute(pl.col("running_start_time")).alias("st"),
                parse_datetime_minute(pl.col("running_end_time")).alias("en"),
                pl.when(pl.col("dt") == DAYS[0]).then(0).otherwise(1).cast(pl.Int8).alias("day"),
                (pl.col("avg_mb") / 1024.0).alias("memgb"),
            )
            .select("task_id", "day", "st", "en", "avg_vcores", "memgb")
        )
        self.e_task = exec_df["task_id"].to_numpy().astype(np.int32, copy=False)
        self.e_day = exec_df["day"].to_numpy().astype(np.int8, copy=False)
        self.e_st = exec_df["st"].to_numpy().astype(np.int16, copy=False)
        self.e_en = exec_df["en"].to_numpy().astype(np.int16, copy=False)
        self.e_val = np.column_stack(
            [exec_df["avg_vcores"].to_numpy(), exec_df["memgb"].to_numpy()]
        ).astype(np.float64, copy=False)
        self.exec_order, self.exec_starts, _ = row_ranges(self.e_task, self.n_tasks)
        del exec_df

        table_lookup = self.writers.select("table_id", "table_uri")
        trans_df = (
            pl.read_parquet(DATA / "trans_train_0808_0809.parquet")
            .join(task_lookup, on="task_name", how="inner")
            .join(table_lookup, on="table_uri", how="inner")
            .with_columns(
                parse_hhmm(pl.col("trans_start_time")).alias("st"),
                parse_hhmm(pl.col("trans_time")).alias("dur"),
                pl.when(pl.col("dt") == DAYS[0]).then(0).otherwise(1).cast(pl.Int8).alias("day"),
            )
            .select(
                "task_id", "table_id", "relation_type", "day", "st", "dur", "estimated_bandwidth"
            )
        )
        self.t_task = trans_df["task_id"].to_numpy().astype(np.int32, copy=False)
        self.t_table = trans_df["table_id"].to_numpy().astype(np.int32, copy=False)
        self.t_rel = trans_df["relation_type"].to_numpy().astype(np.int8, copy=False)
        self.t_day = trans_df["day"].to_numpy().astype(np.int8, copy=False)
        self.t_st = trans_df["st"].to_numpy().astype(np.int16, copy=False)
        self.t_en = (self.t_st.astype(np.int32) + trans_df["dur"].to_numpy()).astype(np.int16)
        self.t_bw = trans_df["estimated_bandwidth"].to_numpy().astype(np.float64, copy=False)
        self.n_trans = len(self.t_task)
        self.trans_task_order, self.trans_task_starts, _ = row_ranges(self.t_task, self.n_tasks)
        self.trans_table_order, self.trans_table_starts, _ = row_ranges(self.t_table, self.n_tables)
        del trans_df

        self.total_load = self._event_arrays(
            self.e_day, self.e_st, self.e_en, self.e_val, directions=2
        )
        self.profile_cache: dict[int, np.ndarray] = {}

        self.direct_risk = np.bincount(
            self.t_task, weights=self.t_bw, minlength=self.n_tasks
        ).astype(np.float64, copy=False)
        single_mask = self.nwriters[self.t_table] == 1
        single_writer_tasks = self.first_writer[self.t_table[single_mask]]
        self.single_writer_risk = np.bincount(
            single_writer_tasks, weights=self.t_bw[single_mask], minlength=self.n_tasks
        ).astype(np.float64, copy=False)
        self.risk = self.direct_risk + self.single_writer_risk

    @staticmethod
    def _event_arrays(
        day: np.ndarray,
        st: np.ndarray,
        en: np.ndarray,
        values: np.ndarray,
        directions: int,
    ) -> np.ndarray:
        """Return [day, component, minute] inclusive-interval active sums."""
        out = np.zeros((2, directions, MINUTES), dtype=np.float64)
        for d in range(2):
            dm = day == d
            for r in range(directions):
                weights = values[:, r] if values.ndim == 2 else values
                diff = np.bincount(st[dm], weights=weights[dm], minlength=MINUTES + 1).astype(
                    np.float64, copy=False
                )
                ix = en[dm].astype(np.int32) + 1
                good = ix < MINUTES + 1
                diff += np.bincount(
                    ix[good], weights=-weights[dm][good], minlength=MINUTES + 1
                )
                out[d, r] = np.cumsum(diff[:MINUTES])
        return out

    def task_rows(self, task_id: int, which: str) -> np.ndarray:
        if which == "exec":
            order, starts = self.exec_order, self.exec_starts
        elif which == "trans":
            order, starts = self.trans_task_order, self.trans_task_starts
        else:
            raise ValueError(which)
        return order[starts[task_id] : starts[task_id + 1]]

    def table_rows(self, table_id: int) -> np.ndarray:
        return self.trans_table_order[
            self.trans_table_starts[table_id] : self.trans_table_starts[table_id + 1]
        ]

    def task_writer_tables(self, task_id: int) -> np.ndarray:
        q = self.writer_order[self.writer_starts[task_id] : self.writer_starts[task_id + 1]]
        return self.writer_table[q]

    def table_writers(self, table_id: int) -> np.ndarray:
        q = self.writer_table_order[
            self.writer_table_starts[table_id] : self.writer_table_starts[table_id + 1]
        ]
        return self.writer_task[q]

    def task_profile(self, task_id: int) -> np.ndarray:
        cached = self.profile_cache.get(task_id)
        if cached is not None:
            return cached
        rows = self.task_rows(task_id, "exec")
        p = np.zeros((2, 2, MINUTES), dtype=np.float64)
        for d in range(2):
            rd = rows[self.e_day[rows] == d]
            if len(rd) == 0:
                continue
            for r in range(2):
                diff = np.bincount(
                    self.e_st[rd], weights=self.e_val[rd, r], minlength=MINUTES + 1
                ).astype(np.float64, copy=False)
                ix = self.e_en[rd].astype(np.int32) + 1
                good = ix < MINUTES + 1
                diff += np.bincount(
                    ix[good], weights=-self.e_val[rd, r][good], minlength=MINUTES + 1
                )
                p[d, r] = np.cumsum(diff[:MINUTES])
        self.profile_cache[task_id] = p
        return p

    def loads_for_assignment(self, h: np.ndarray) -> np.ndarray:
        mask = h[self.e_task]
        if not mask.any():
            return np.zeros_like(self.total_load)
        return self._event_arrays(
            self.e_day[mask], self.e_st[mask], self.e_en[mask], self.e_val[mask], directions=2
        )

    def writer_counts(self, h: np.ndarray) -> np.ndarray:
        return np.bincount(
            self.writer_table,
            weights=h[self.writer_task].astype(np.int8),
            minlength=self.n_tables,
        ).astype(np.int32)

    def classify(self, rows: np.ndarray, h: np.ndarray, writer_h: np.ndarray) -> np.ndarray:
        """-1 local, 0 Zhongwei->Huailai, 1 Huailai->Zhongwei."""
        ti = self.t_task[rows]
        ki = self.t_table[rows]
        th = h[ti]
        nw = self.nwriters[ki]
        wc = writer_h[ki]
        table_z_for_h_task = np.where(nw > 0, wc == 0, ~self.fallback_has_h[ki])
        table_h_for_z_task = np.where(nw > 0, wc == nw, ~self.fallback_has_z[ki])
        cross = (th & table_z_for_h_task) | ((~th) & table_h_for_z_task)
        zh = cross & ((th & (self.t_rel[rows] == 0)) | ((~th) & (self.t_rel[rows] == 1)))
        ans = np.full(len(rows), -1, dtype=np.int8)
        ans[cross] = 1
        ans[zh] = 0
        return ans

    def bandwidth_for_assignment(
        self, h: np.ndarray, writer_h: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        rows = np.arange(self.n_trans, dtype=np.int64)
        status = self.classify(rows, h, writer_h)
        active = np.zeros((2, 2, MINUTES), dtype=np.float64)
        for d in range(2):
            for q in range(2):
                mask = (self.t_day == d) & (status == q)
                diff = np.bincount(
                    self.t_st[mask], weights=self.t_bw[mask], minlength=MINUTES + 1
                ).astype(np.float64, copy=False)
                ix = self.t_en[mask].astype(np.int32) + 1
                good = ix < MINUTES + 1
                diff += np.bincount(
                    ix[good], weights=-self.t_bw[mask][good], minlength=MINUTES + 1
                )
                active[d, q] = np.cumsum(diff[:MINUTES])
        return active, status


class State:
    def __init__(self, inst: Instance, h: np.ndarray) -> None:
        self.inst = inst
        self.h = h.copy()
        self.writer_h = inst.writer_counts(self.h)
        self.hl_load = inst.loads_for_assignment(self.h)
        self.bandwidth, self.status = inst.bandwidth_for_assignment(self.h, self.writer_h)

    def metrics(self) -> Metrics:
        directional = self.bandwidth.max(axis=2)
        daily = directional.max(axis=1)
        peaks = np.argmax(self.bandwidth, axis=2)
        zload = self.inst.total_load - self.hl_load
        rz = zload / self.inst.cap_z[None, :, None] - 1.0
        rh = self.hl_load / self.inst.cap_h[None, :, None] - 1.0
        theta = float(max(0.0, rz.max(), rh.max()))
        violations = int((rz > 1e-10).any()) + int((rh > 1e-10).any())
        slack = {
            "zw_vcores": float(self.inst.cap_z[0] - zload[:, 0].max()),
            "zw_memory_gb": float(self.inst.cap_z[1] - zload[:, 1].max()),
            "hl_vcores": float(self.inst.cap_h[0] - self.hl_load[:, 0].max()),
            "hl_memory_gb": float(self.inst.cap_h[1] - self.hl_load[:, 1].max()),
        }
        return Metrics(
            objective_raw=float(daily.max()),
            daily_max_raw=daily.tolist(),
            daily_directional_raw=directional.tolist(),
            peak_locations=peaks.tolist(),
            huailai_tasks=int(self.h.sum()),
            theta=theta,
            violation_groups=violations,
            min_slack=slack,
        )

    def theta(self, alpha: float = 1.0, prospective: np.ndarray | None = None) -> float:
        hl = self.hl_load if prospective is None else prospective
        z = self.inst.total_load - hl
        rz = z / (alpha * self.inst.cap_z[None, :, None] * SAFETY) - 1.0
        rh = hl / (self.inst.cap_h[None, :, None] * SAFETY) - 1.0
        return float(max(0.0, rz.max(), rh.max()))

    def feasible_profile(self, task_id: int, to_h: bool) -> tuple[bool, np.ndarray]:
        sign = 1.0 if to_h else -1.0
        candidate = self.hl_load + sign * self.inst.task_profile(task_id)
        z = self.inst.total_load - candidate
        feasible = bool(
            candidate.min() >= -1e-6
            and (candidate <= self.inst.cap_h[None, :, None] * SAFETY + 1e-6).all()
            and (z <= self.inst.cap_z[None, :, None] * SAFETY + 1e-6).all()
        )
        return feasible, candidate

    def affected_rows(self, task_id: int) -> np.ndarray:
        chunks = [self.inst.task_rows(task_id, "trans")]
        for table_id in self.inst.task_writer_tables(task_id):
            q = self.inst.table_rows(int(table_id))
            if len(q):
                chunks.append(q)
        nonempty = [q for q in chunks if len(q)]
        if not nonempty:
            return np.empty(0, dtype=np.int64)
        if len(nonempty) == 1:
            return nonempty[0]
        return np.unique(np.concatenate(nonempty))

    def toggle_delta(self, task_id: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rows = self.affected_rows(task_id)
        if len(rows) == 0:
            return np.zeros_like(self.bandwidth), rows, np.empty(0, dtype=np.int8)
        old = self.status[rows]
        to_h = not bool(self.h[task_id])
        self.h[task_id] = to_h
        tables = self.inst.task_writer_tables(task_id)
        self.writer_h[tables] += 1 if to_h else -1
        new = self.inst.classify(rows, self.h, self.writer_h)
        self.writer_h[tables] -= 1 if to_h else -1
        self.h[task_id] = not to_h

        delta = np.zeros_like(self.bandwidth)
        changed = old != new
        if not changed.any():
            return delta, rows, new
        rr = rows[changed]
        oo = old[changed]
        nn = new[changed]
        for d in range(2):
            for q in range(2):
                diff = np.zeros(MINUTES + 1, dtype=np.float64)
                remove = (self.inst.t_day[rr] == d) & (oo == q)
                add = (self.inst.t_day[rr] == d) & (nn == q)
                for mask, sign in ((remove, -1.0), (add, 1.0)):
                    if not mask.any():
                        continue
                    r2 = rr[mask]
                    diff += np.bincount(
                        self.inst.t_st[r2],
                        weights=sign * self.inst.t_bw[r2],
                        minlength=MINUTES + 1,
                    )
                    ix = self.inst.t_en[r2].astype(np.int32) + 1
                    good = ix < MINUTES + 1
                    diff += np.bincount(
                        ix[good],
                        weights=-sign * self.inst.t_bw[r2][good],
                        minlength=MINUTES + 1,
                    )
                delta[d, q] = np.cumsum(diff[:MINUTES])
        return delta, rows, new

    def commit_toggle(
        self,
        task_id: int,
        delta: np.ndarray,
        rows: np.ndarray,
        new_status: np.ndarray,
    ) -> None:
        to_h = not bool(self.h[task_id])
        sign = 1.0 if to_h else -1.0
        self.h[task_id] = to_h
        tables = self.inst.task_writer_tables(task_id)
        self.writer_h[tables] += int(sign)
        self.hl_load += sign * self.inst.task_profile(task_id)
        self.bandwidth += delta
        if len(rows):
            self.status[rows] = new_status


def worst_target_violation(inst: Instance, hl_load: np.ndarray, alpha: float) -> tuple[float, int, int, int]:
    z = inst.total_load - hl_load
    rz = z / (alpha * inst.cap_z[None, :, None] * SAFETY) - 1.0
    rh = hl_load / (inst.cap_h[None, :, None] * SAFETY) - 1.0
    iz = np.unravel_index(int(np.argmax(rz)), rz.shape)
    ih = np.unravel_index(int(np.argmax(rh)), rh.shape)
    if rz[iz] >= rh[ih]:
        return float(max(0.0, rz[iz])), int(iz[0]), int(iz[1]), int(iz[2])
    return float(max(0.0, rh[ih])), int(ih[0]), int(ih[1]), int(ih[2])


def construct_resource_candidate(inst: Instance, alpha: float) -> tuple[np.ndarray, list[int], int]:
    """Sparse all-Zhongwei repair with exact event replay after every move."""
    h = np.zeros(inst.n_tasks, dtype=bool)
    hl_load = np.zeros_like(inst.total_load)
    considered: set[int] = set()
    stalls = 0
    for _ in range(80):
        theta, d, r, minute = worst_target_violation(inst, hl_load, alpha)
        if theta <= 2e-10:
            return h, sorted(considered), stalls
        mask = (
            (inst.e_day == d)
            & (inst.e_st <= minute)
            & (inst.e_en >= minute)
            & (~h[inst.e_task])
        )
        contrib = np.bincount(
            inst.e_task[mask], weights=inst.e_val[mask, r], minlength=inst.n_tasks
        )
        ids = np.flatnonzero(contrib > 0)
        if len(ids) == 0:
            break
        # Covering strength first within zero-risk tasks; retain some general
        # candidates as a fallback if all bandwidth-neutral moves are exhausted.
        zero = ids[inst.risk[ids] == 0]
        general = ids[inst.risk[ids] > 0]
        zero = zero[np.argsort(contrib[zero])[-180:][::-1]] if len(zero) else zero
        if len(general):
            score = inst.risk[general] / np.maximum(contrib[general], 1e-12)
            general = general[np.argsort(score)[:40]]
        trial_ids = np.concatenate([zero, general])
        best = None
        old_theta = theta
        for task_id in trial_ids:
            task_id = int(task_id)
            considered.add(task_id)
            cand = hl_load + inst.task_profile(task_id)
            if (cand > inst.cap_h[None, :, None] * SAFETY + 1e-6).any():
                continue
            new_theta, _, _, _ = worst_target_violation(inst, cand, alpha)
            if new_theta >= old_theta - 1e-12:
                continue
            key = (inst.risk[task_id] > 0, new_theta, inst.risk[task_id], -contrib[task_id])
            if best is None or key < best[0]:
                best = (key, task_id, cand)
        if best is None:
            stalls += 1
            # Change operator after a stall: broaden to the 500 strongest tasks.
            broad = ids[np.argsort(contrib[ids])[-500:][::-1]]
            for task_id in broad:
                task_id = int(task_id)
                considered.add(task_id)
                cand = hl_load + inst.task_profile(task_id)
                if (cand > inst.cap_h[None, :, None] * SAFETY + 1e-6).any():
                    continue
                new_theta, _, _, _ = worst_target_violation(inst, cand, alpha)
                if new_theta < old_theta - 1e-12:
                    key = (new_theta, inst.risk[task_id], -contrib[task_id])
                    if best is None or key < best[0]:
                        best = (key, task_id, cand)
            if best is None:
                break
        _, task_id, hl_load = best
        h[task_id] = True
    return h, sorted(considered), stalls


def candidate_rank(metrics: Metrics, best_peak: float) -> tuple:
    near = metrics.objective_raw <= best_peak + NEAR_TIE_RAW
    if near:
        mean = float(np.mean(metrics.daily_max_raw))
        spread = float(np.ptp(metrics.daily_max_raw))
        slack_penalty = -min(metrics.min_slack.values())
        return (0, metrics.huailai_tasks, mean, spread, slack_penalty)
    return (1, metrics.objective_raw, metrics.huailai_tasks)


def peak_candidate_ids(state: State, max_direct: int = 350) -> np.ndarray:
    bw = state.bandwidth
    d, q, minute = np.unravel_index(int(np.argmax(bw)), bw.shape)
    active = (
        (state.inst.t_day == d)
        & (state.status == q)
        & (state.inst.t_st <= minute)
        & (state.inst.t_en >= minute)
    )
    rows = np.flatnonzero(active)
    if len(rows) == 0:
        return np.empty(0, dtype=np.int32)
    task_contrib = np.bincount(
        state.inst.t_task[rows], weights=state.inst.t_bw[rows], minlength=state.inst.n_tasks
    )
    direct = np.flatnonzero(task_contrib > 0)
    if len(direct) > max_direct:
        direct = direct[np.argsort(task_contrib[direct])[-max_direct:][::-1]]

    # Writer-neighborhood expansion.  For a homogeneous writer-backed table,
    # toggling any one writer has the same locality effect; test low-risk
    # representatives plus writers already in Huailai.
    table_contrib = np.bincount(
        state.inst.t_table[rows], weights=state.inst.t_bw[rows], minlength=state.inst.n_tables
    )
    tables = np.flatnonzero(table_contrib > 0)
    if len(tables) > 80:
        tables = tables[np.argsort(table_contrib[tables])[-80:][::-1]]
    extras: list[int] = []
    for k in tables:
        if state.inst.nwriters[k] == 0:
            continue
        writers = state.inst.table_writers(int(k))
        if len(writers) == 0:
            continue
        existing = writers[state.h[writers]]
        if len(existing):
            extras.extend(existing[:5].tolist())
        order = np.argsort(state.inst.risk[writers])
        extras.extend(writers[order[: min(8, len(order))]].tolist())
    return np.unique(np.concatenate([direct.astype(np.int32), np.asarray(extras, dtype=np.int32)]))


def performance_search(state: State, deadline: float) -> tuple[State, list[dict]]:
    history: list[dict] = []
    best_snapshots: list[tuple[float, int, np.ndarray]] = [
        (state.metrics().objective_raw, int(state.h.sum()), state.h.copy())
    ]
    for iteration in range(18):
        if time.monotonic() >= deadline:
            break
        current_obj = float(state.bandwidth.max())
        candidates = peak_candidate_ids(state)
        best = None
        for task_id in candidates:
            if time.monotonic() >= deadline:
                break
            task_id = int(task_id)
            feasible, candidate_load = state.feasible_profile(task_id, not bool(state.h[task_id]))
            if not feasible:
                continue
            delta, rows, new_status = state.toggle_delta(task_id)
            new_bw = state.bandwidth + delta
            new_obj = float(new_bw.max())
            if new_obj >= current_obj - max(1e-3, abs(current_obj) * 1e-12):
                continue
            directional = new_bw.max(axis=2)
            daily = directional.max(axis=1)
            key = (new_obj, float(np.mean(daily)), float(np.ptp(daily)), int(state.h[task_id]))
            if best is None or key < best[0]:
                best = (key, task_id, delta, rows, new_status, candidate_load)
        if best is None:
            break
        _, task_id, delta, rows, new_status, _ = best
        before = current_obj
        state.commit_toggle(task_id, delta, rows, new_status)
        after = float(state.bandwidth.max())
        history.append(
            {
                "iteration": iteration + 1,
                "task_id": task_id,
                "to_region": HL if state.h[task_id] else ZW,
                "peak_before_raw": before,
                "peak_after_raw": after,
            }
        )
        best_snapshots.append((after, int(state.h.sum()), state.h.copy()))

    # Apply the requested near-tie regularizer over all accepted incumbents.
    best_peak = min(x[0] for x in best_snapshots)
    eligible = [x for x in best_snapshots if x[0] <= best_peak + NEAR_TIE_RAW]
    chosen = min(eligible, key=lambda x: (x[1], x[0]))
    if not np.array_equal(chosen[2], state.h):
        state = State(state.inst, chosen[2])

    # Exact one-at-a-time intervention pruning within the 0.050 Gbps band.
    reference = float(state.bandwidth.max())
    for task_id in np.flatnonzero(state.h).tolist():
        if time.monotonic() >= deadline:
            break
        feasible, _ = state.feasible_profile(task_id, False)
        if not feasible:
            continue
        delta, rows, new_status = state.toggle_delta(task_id)
        new_obj = float((state.bandwidth + delta).max())
        if new_obj <= reference + NEAR_TIE_RAW:
            state.commit_toggle(task_id, delta, rows, new_status)
    return state, history


def strict_peak_descent(state: State, max_steps: int, deadline: float) -> tuple[State, list[dict]]:
    """Continue exact peak-influence moves without applying the near-tie prune."""
    history: list[dict] = []
    for iteration in range(max_steps):
        if time.monotonic() >= deadline:
            break
        current_obj = float(state.bandwidth.max())
        best = None
        for task_id in peak_candidate_ids(state):
            task_id = int(task_id)
            feasible, _ = state.feasible_profile(task_id, not bool(state.h[task_id]))
            if not feasible:
                continue
            delta, rows, new_status = state.toggle_delta(task_id)
            new_bw = state.bandwidth + delta
            new_obj = float(new_bw.max())
            if new_obj >= current_obj - max(1e-3, abs(current_obj) * 1e-12):
                continue
            daily = new_bw.max(axis=2).max(axis=1)
            key = (new_obj, float(np.mean(daily)), float(np.ptp(daily)))
            if best is None or key < best[0]:
                best = (key, task_id, delta, rows, new_status)
        if best is None:
            break
        _, task_id, delta, rows, new_status = best
        state.commit_toggle(task_id, delta, rows, new_status)
        history.append(
            {
                "iteration": iteration + 1,
                "task_id": task_id,
                "objective_gbps": float(state.bandwidth.max()) * GBPS,
                "huailai_tasks": int(state.h.sum()),
            }
        )
    return state, history


def scip_projection(inst: Instance, h: np.ndarray, pool: list[int], deadline: float) -> tuple[np.ndarray, dict]:
    """Exact restricted repair: hard capacities, then minimum Hamming distance."""
    info = {"used": False, "status": "not_run", "incumbents": 0}
    remaining = deadline - time.monotonic()
    if remaining < 3.0:
        info["status"] = "skipped_time_guard"
        return h, info
    try:
        if "/opt/or-deps" not in sys.path:
            sys.path.insert(0, "/opt/or-deps")
        from pyscipopt import Model, quicksum
    except Exception as exc:  # pragma: no cover - environment fallback
        info["status"] = f"import_failed:{type(exc).__name__}"
        return h, info

    ids = np.unique(np.concatenate([np.asarray(pool, dtype=np.int32), np.flatnonzero(h)])).astype(
        np.int32
    )
    if len(ids) == 0 or len(ids) > 650:
        info["status"] = "skipped_pool_size"
        return h, info
    profiles = np.stack([inst.task_profile(int(i)) for i in ids], axis=0)
    model = Model("capacity_projection")
    model.hideOutput()
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    invocation_limit = min(6.0, max(0.25, remaining - 1.0))
    if env_limit is not None:
        invocation_limit = min(invocation_limit, float(env_limit))
    model.setParam("limits/time", invocation_limit)
    model.setParam("limits/gap", 0.0005)
    model.setParam("randomization/randomseedshift", 0)
    x = [model.addVar(vtype="B", name=f"h[{int(task_id)}]") for task_id in ids]
    for d in range(2):
        for r in range(2):
            lower = inst.total_load[d, r] - inst.cap_z[r] * SAFETY
            upper = inst.cap_h[r] * SAFETY
            possible = profiles[:, d, r].sum(axis=0)
            times = np.flatnonzero((lower > 1e-7) | (possible > upper))
            for minute in times:
                nz = np.flatnonzero(profiles[:, d, r, minute] > 0)
                expr = quicksum(float(profiles[j, d, r, minute]) * x[j] for j in nz)
                if lower[minute] > 1e-7:
                    model.addCons(expr >= float(lower[minute]), name=f"lo[{d},{r},{minute}]")
                if possible[minute] > upper:
                    model.addCons(expr <= float(upper), name=f"hi[{d},{r},{minute}]")
    objective = quicksum((1.0 - x[j]) if h[task_id] else x[j] for j, task_id in enumerate(ids))
    model.setObjective(objective, "minimize")
    warm = model.createSol()
    for j, task_id in enumerate(ids):
        model.setSolVal(warm, x[j], float(h[task_id]))
    model.addSol(warm)
    model.optimize()
    info.update(
        {
            "used": True,
            "status": str(model.getStatus()),
            "incumbents": int(model.getNSols()),
        }
    )
    if model.getNSols() <= 0:
        return h, info
    sol = model.getBestSol()
    projected = h.copy()
    projected[ids] = np.array([model.getSolVal(sol, var) >= 0.5 for var in x], dtype=bool)
    info["objective"] = float(model.getSolObjVal(sol))
    try:
        info["best_bound"] = float(model.getDualbound())
        info["gap"] = float(model.getGap())
    except Exception:
        pass
    # Never trade the known feasible backup for a repair regression.
    check = State(inst, projected).metrics()
    if check.theta > 1e-9:
        info["status"] += ":rejected_infeasible"
        return h, info
    return projected, info


def scip_peak_count_lns(
    inst: Instance,
    rollback_h: np.ndarray,
    floor_h: np.ndarray,
    repair_h: np.ndarray,
    considered: list[int],
    count_cap: int,
    deadline: float,
) -> tuple[np.ndarray, dict]:
    """Risk-priced local branch fixing public peak-reduction decisions.

    The model minimizes public transfer exposure among assignments no larger
    than the rollback intervention count.  All capacity rows are exact.  The
    returned assignment is accepted only after a full transfer replay.
    """
    info = {"used": False, "status": "not_run", "incumbents": 0}
    remaining = deadline - time.monotonic()
    if remaining < 2.0:
        info["status"] = "skipped_time_guard"
        return rollback_h, info
    try:
        if "/opt/or-deps" not in sys.path:
            sys.path.insert(0, "/opt/or-deps")
        from pyscipopt import Model, quicksum
    except Exception as exc:  # pragma: no cover
        info["status"] = f"import_failed:{type(exc).__name__}"
        return rollback_h, info

    fixed_mask = floor_h & (inst.risk > 0)
    fixed_ids = np.flatnonzero(fixed_mask).astype(np.int32)
    pool = set(int(x) for x in considered)
    pool.update(np.flatnonzero(repair_h).tolist())
    pool.update(np.flatnonzero(floor_h).tolist())
    # Expand with every strong resource contributor at each actually overloaded
    # all-Zhongwei event, so the branch is not tied to a hand-picked repair list.
    for d in range(2):
        for r in range(2):
            for minute in np.flatnonzero(inst.total_load[d, r] > inst.cap_z[r] * SAFETY):
                mask = (
                    (inst.e_day == d)
                    & (inst.e_st <= minute)
                    & (inst.e_en >= minute)
                )
                contrib = np.bincount(
                    inst.e_task[mask], weights=inst.e_val[mask, r], minlength=inst.n_tasks
                )
                ids = np.flatnonzero(contrib > 0)
                pool.update(int(x) for x in ids[np.argsort(contrib[ids])[-120:]])
    ids = np.asarray(sorted(pool), dtype=np.int32)
    if len(ids) > 650:
        mandatory = np.unique(np.concatenate([fixed_ids, np.flatnonzero(rollback_h)])).astype(
            np.int32
        )
        optional = ids[~np.isin(ids, mandatory)]
        optional = optional[np.argsort(inst.risk[optional])[: max(0, 650 - len(mandatory))]]
        ids = np.unique(np.concatenate([mandatory, optional])).astype(np.int32)
    fixed_set = set(fixed_ids.tolist())
    profiles = np.stack([inst.task_profile(int(i)) for i in ids])

    model = Model("peak_count_local_branch")
    model.hideOutput()
    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    invocation_limit = min(7.0, max(0.25, remaining - 1.0))
    if env_limit is not None:
        invocation_limit = min(invocation_limit, float(env_limit))
    model.setParam("limits/time", invocation_limit)
    model.setParam("limits/gap", 0.0005)
    model.setParam("randomization/randomseedshift", 0)
    x = [
        model.addVar(
            vtype="B",
            lb=1.0 if int(task_id) in fixed_set else 0.0,
            ub=1.0,
            name=f"h[{int(task_id)}]",
        )
        for task_id in ids
    ]
    for d in range(2):
        for r in range(2):
            lower = inst.total_load[d, r] - inst.cap_z[r] * SAFETY
            upper = inst.cap_h[r] * SAFETY
            possible = profiles[:, d, r].sum(axis=0)
            times = np.flatnonzero((lower > 1e-7) | (possible > upper))
            for minute in times:
                nz = np.flatnonzero(profiles[:, d, r, minute] > 0)
                expr = quicksum(float(profiles[j, d, r, minute]) * x[j] for j in nz)
                if lower[minute] > 1e-7:
                    model.addCons(expr >= float(lower[minute]), name=f"lo[{d},{r},{minute}]")
                if possible[minute] > upper:
                    model.addCons(expr <= float(upper), name=f"hi[{d},{r},{minute}]")
    count_expr = quicksum(x)
    model.addCons(count_expr <= int(count_cap), name="local_branch_intervention_cap")
    risk_scale = max(1.0, float(inst.risk[ids].max()))
    model.setObjective(
        quicksum(
            (float(inst.risk[task_id]) / risk_scale + 1e-7) * x[j]
            for j, task_id in enumerate(ids)
        ),
        "minimize",
    )
    model.optimize()
    info.update(
        {
            "used": True,
            "status": str(model.getStatus()),
            "incumbents": int(model.getNSols()),
            "pool_size": len(ids),
            "fixed_peak_tasks": len(fixed_ids),
            "count_cap": int(count_cap),
        }
    )
    if model.getNSols() <= 0:
        return rollback_h, info
    sol = model.getBestSol()
    candidate = np.zeros(inst.n_tasks, dtype=bool)
    candidate[ids] = np.array([model.getSolVal(sol, var) >= 0.5 for var in x])
    info.update(
        {
            "objective": float(model.getSolObjVal(sol)),
            "best_bound": float(model.getDualbound()),
            "gap": float(model.getGap()),
        }
    )
    return candidate, info


def migration_metrics(inst: Instance, h: np.ndarray) -> dict:
    first_region_h = np.zeros(inst.n_tables, dtype=bool)
    has_writer = inst.nwriters > 0
    first_region_h[has_writer] = h[inst.first_writer[has_writer]]
    fallback_first = inst.writers["region_list"].list.first().to_numpy()
    first_region_h[~has_writer] = fallback_first[~has_writer] == HL
    candidate = pl.DataFrame(
        {
            "table_uri": inst.writers["table_uri"],
            "candidate_region": np.where(first_region_h, HL, ZW),
        }
    )
    current = pl.read_parquet(DATA / "current_table_regions.parquet")
    sizes = pl.read_parquet(DATA / "table_size_latest.parquet").select("table_uri", "table_size")
    joined = current.join(candidate, on="table_uri", how="inner").join(sizes, on="table_uri", how="left")
    moved = joined.filter(pl.col("current_region") != pl.col("candidate_region"))
    missing = moved.filter(pl.col("table_size").is_null()).height
    moved = moved.with_columns(pl.col("table_size").fill_null(0))
    zh = moved.filter((pl.col("current_region") == ZW) & (pl.col("candidate_region") == HL))[
        "table_size"
    ].sum()
    hz = moved.filter((pl.col("current_region") == HL) & (pl.col("candidate_region") == ZW))[
        "table_size"
    ].sum()
    return {
        "tables_changed": moved.height,
        "zh_bytes": int(zh or 0),
        "hz_bytes": int(hz or 0),
        "total_bytes": int((zh or 0) + (hz or 0)),
        "missing_size_tables": int(missing),
    }


def fixed_fallback_lower_bound(inst: Instance, state: State) -> tuple[float, int | None]:
    """A valid assignment-independent lower bound from one task's fixed tables.

    At the final worst peak, inspect every contributing task.  For each task,
    replay only writerless fallback-table rows under both possible placements.
    The smaller of those two task-only peaks is unavoidable in every solution.
    """
    d, q, minute = np.unravel_index(int(np.argmax(state.bandwidth)), state.bandwidth.shape)
    active = (
        (inst.t_day == d)
        & (state.status == q)
        & (inst.t_st <= minute)
        & (inst.t_en >= minute)
    )
    tasks = np.unique(inst.t_task[active])
    best_bound = 0.0
    best_task: int | None = None
    for task_id in tasks:
        rows = inst.task_rows(int(task_id), "trans")
        rows = rows[inst.nwriters[inst.t_table[rows]] == 0]
        side_peaks: list[float] = []
        for task_h in (False, True):
            k = inst.t_table[rows]
            cross = (~inst.fallback_has_z[k]) if not task_h else (~inst.fallback_has_h[k])
            rr = rows[cross]
            arrays = np.zeros((2, 2, MINUTES), dtype=np.float64)
            if len(rr):
                # Direction for relation 0 is table->task; relation 1 reverses it.
                direction = np.where(
                    (task_h & (inst.t_rel[rr] == 0))
                    | ((not task_h) & (inst.t_rel[rr] == 1)),
                    0,
                    1,
                )
                for dd in range(2):
                    for qq in range(2):
                        sel = rr[(inst.t_day[rr] == dd) & (direction == qq)]
                        if len(sel) == 0:
                            continue
                        diff = np.bincount(
                            inst.t_st[sel], weights=inst.t_bw[sel], minlength=MINUTES + 1
                        ).astype(np.float64, copy=False)
                        ix = inst.t_en[sel].astype(np.int32) + 1
                        good = ix < MINUTES + 1
                        diff += np.bincount(
                            ix[good], weights=-inst.t_bw[sel][good], minlength=MINUTES + 1
                        )
                        arrays[dd, qq] = np.cumsum(diff[:MINUTES])
            side_peaks.append(float(arrays.max()))
        bound = min(side_peaks)
        if bound > best_bound:
            best_bound = bound
            best_task = int(task_id)
    return best_bound, best_task


def schema_check(frame: pl.DataFrame, universe: pl.DataFrame) -> tuple[bool, str]:
    if frame.columns != ["task_name", "region"]:
        return False, "wrong columns"
    if frame.height != universe.height:
        return False, "wrong row count"
    if frame["task_name"].null_count() != 1:
        return False, "null identifier not preserved"
    if frame["task_name"].n_unique() != universe.height:
        return False, "duplicate task identifiers"
    if not frame["region"].is_in([ZW, HL]).all():
        return False, "invalid region domain"
    left = frame["task_name"].to_list()
    right = universe["task_name"].to_list()
    if any(a != b for a, b in zip(left, right) if a is not None or b is not None):
        return False, "identifier/order mismatch"
    return True, "ok"


def main() -> None:
    started = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)
    inst = Instance()
    load_seconds = time.monotonic() - started

    baseline = State(inst, np.zeros(inst.n_tasks, dtype=bool))
    baseline_metrics = baseline.metrics()

    target_results: list[dict] = []
    candidate_states: list[tuple[float, State, list[int]]] = []
    all_considered: set[int] = set()
    official_repair_h: np.ndarray | None = None
    official_considered: list[int] = []
    # Compare the requested conservative targets and the official boundary.
    target_started = time.monotonic()
    for alpha in (0.990, 0.995, 1.000):
        h, considered, stalls = construct_resource_candidate(inst, alpha)
        if alpha == 1.0:
            official_repair_h = h.copy()
            official_considered = list(considered)
        all_considered.update(considered)
        state = State(inst, h)
        met = state.metrics()
        target_theta, _, _, _ = worst_target_violation(inst, state.hl_load, alpha)
        target_results.append(
            {
                "target": alpha,
                "target_theta": target_theta,
                "official_theta": met.theta,
                "huailai_tasks": met.huailai_tasks,
                "objective_raw": met.objective_raw,
                "objective_gbps": met.objective_raw * GBPS,
                "stalls": stalls,
            }
        )
        if met.theta <= 1e-9:
            candidate_states.append((alpha, state, considered))

    if not candidate_states:
        raise RuntimeError("RESTORATION failed to produce an official-feasible incumbent")
    best_peak = min(s.metrics().objective_raw for _, s, _ in candidate_states)
    alpha, state, _ = min(candidate_states, key=lambda x: candidate_rank(x[1].metrics(), best_peak))
    selected_target = alpha
    target_seconds = time.monotonic() - target_started

    # Preserve enough time for exact projection, output, and independent audit.
    search_started = time.monotonic()
    search_deadline = started + 96.0
    state, search_history = performance_search(state, search_deadline)
    search_seconds = time.monotonic() - search_started

    projection_started = time.monotonic()
    current_h_ids = np.flatnonzero(state.h)
    alternatives = np.asarray(sorted(all_considered), dtype=np.int32)
    alternatives = alternatives[~state.h[alternatives]] if len(alternatives) else alternatives
    if len(alternatives) > max(0, 620 - len(current_h_ids)):
        order = np.argsort(inst.risk[alternatives])
        alternatives = alternatives[order[: max(0, 620 - len(current_h_ids))]]
    projection_pool = np.concatenate([current_h_ids.astype(np.int32), alternatives]).tolist()
    projected_h, scip_info = scip_projection(
        inst, state.h, projection_pool, deadline=started + 84.0
    )
    if not np.array_equal(projected_h, state.h):
        projected_state = State(inst, projected_h)
        # Projection is feasibility-first; retain it only if it does not worsen
        # the primary objective outside the specified near-tie band.
        if projected_state.metrics().objective_raw <= state.metrics().objective_raw + NEAR_TIE_RAW:
            state = projected_state
    projection_seconds = time.monotonic() - projection_started

    # Guarded improvement phase.  First reach the exact public peak floor by
    # local influence moves, then use a bounded SCIP local branch to replace the
    # capacity-repair subset without increasing the rollback intervention count.
    rollback_state = state
    rollback_metrics = rollback_state.metrics()
    floor_state, floor_history = strict_peak_descent(
        State(inst, rollback_state.h), max_steps=6, deadline=started + 86.0
    )
    lns_started = time.monotonic()
    if official_repair_h is None:
        official_repair_h = np.zeros(inst.n_tasks, dtype=bool)
    lns_h, peak_lns_info = scip_peak_count_lns(
        inst,
        rollback_state.h,
        floor_state.h,
        official_repair_h,
        official_considered,
        count_cap=rollback_metrics.huailai_tasks,
        deadline=started + 91.0,
    )
    candidate_state = State(inst, lns_h) if not np.array_equal(lns_h, rollback_state.h) else rollback_state
    candidate_metrics = candidate_state.metrics()
    strict_improvement = candidate_metrics.objective_raw < rollback_metrics.objective_raw - max(
        1e-3, abs(rollback_metrics.objective_raw) * 1e-12
    )
    near_tie_count_ok = (
        candidate_metrics.objective_raw < rollback_metrics.objective_raw - NEAR_TIE_RAW
        or candidate_metrics.huailai_tasks <= rollback_metrics.huailai_tasks
    )
    publish_candidate = bool(
        candidate_metrics.theta <= 1e-9 and strict_improvement and near_tie_count_ok
    )
    if publish_candidate:
        state = candidate_state
    else:
        state = rollback_state
    lns_seconds = time.monotonic() - lns_started

    final_metrics = state.metrics()
    if final_metrics.theta > 1e-9:
        raise RuntimeError(f"final assignment infeasible, theta={final_metrics.theta}")

    regions = np.where(state.h, HL, ZW)
    audit_started = time.monotonic()
    solution = inst.universe.select("task_name").with_columns(pl.Series("region", regions))
    artifact = OUT / "solution.parquet"
    solution.write_parquet(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (OUT / "solution.json").write_text(
        json.dumps({"artifact_path": str(artifact), "sha256": digest}, ensure_ascii=False, indent=2)
        + "\n"
    )

    # Independent exact-artifact audit: reload the emitted Parquet and derive its
    # assignment anew, rather than trusting the construction state.
    emitted = pl.read_parquet(artifact)
    schema_ok, schema_message = schema_check(emitted, pl.read_parquet(DATA / "task_universe.parquet"))
    if not schema_ok:
        raise RuntimeError(f"artifact schema audit failed: {schema_message}")
    emitted_h = emitted["region"].to_numpy() == HL
    audited = State(inst, emitted_h)
    audit_metrics = audited.metrics()
    if audit_metrics.theta > 1e-9:
        raise RuntimeError(f"exact artifact resource audit failed: theta={audit_metrics.theta}")
    if abs(audit_metrics.objective_raw - final_metrics.objective_raw) > max(
        1e-4, abs(final_metrics.objective_raw) * 1e-10
    ):
        raise RuntimeError("exact artifact bandwidth replay disagrees with construction replay")

    corrupted = emitted.with_row_index("_row").with_columns(
        pl.when(pl.col("_row") == 0)
        .then(pl.lit("INVALID"))
        .otherwise(pl.col("region"))
        .alias("region")
    ).drop("_row")
    negative_rejected = not schema_check(corrupted, inst.universe.select("task_name"))[0]
    if not negative_rejected:
        raise RuntimeError("negative schema audit did not reject a corrupted assignment")

    migration = migration_metrics(inst, emitted_h)
    fixed_lower_bound, lower_bound_task = fixed_fallback_lower_bound(inst, audited)
    dispatch_seconds = time.monotonic() - started
    manifest = {
        "embedded_external_task_mapping": False,
        "used_reference_mapping": False,
        "used_august10_data": False,
        "dispatch_time_seconds": dispatch_seconds,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    metrics_payload = {
        "mode": "PERFORMANCE",
        "artifact_present": artifact.exists(),
        "schema_ok": schema_ok,
        "schema_message": schema_message,
        "sha256": digest,
        "coverage": emitted.height,
        "expected_coverage": inst.n_tasks,
        "incumbent_count": 1,
        "maximum_normalized_constraint_residual": audit_metrics.theta,
        "violated_constraint_groups": audit_metrics.violation_groups,
        "integrality_residual": 0.0,
        "objective_raw_bytes_per_second": audit_metrics.objective_raw,
        "objective_gbps": audit_metrics.objective_raw * GBPS,
        "fixed_fallback_lower_bound_gbps": fixed_lower_bound * GBPS,
        "objective_minus_lower_bound_gbps":
            (audit_metrics.objective_raw - fixed_lower_bound) * GBPS,
        "lower_bound_task_id": lower_bound_task,
        "daily_max_gbps": [x * GBPS for x in audit_metrics.daily_max_raw],
        "daily_directional_gbps": [
            [x * GBPS for x in row] for row in audit_metrics.daily_directional_raw
        ],
        "peak_minutes": audit_metrics.peak_locations,
        "resource_slack": audit_metrics.min_slack,
        "huailai_tasks": audit_metrics.huailai_tasks,
        "baseline": {
            "theta": baseline_metrics.theta,
            "objective_gbps": baseline_metrics.objective_raw * GBPS,
            "daily_max_gbps": [x * GBPS for x in baseline_metrics.daily_max_raw],
        },
        "target_comparison": target_results,
        "selected_target_before_performance": selected_target,
        "performance_moves": search_history,
        "scip_projection": scip_info,
        "rollback_incumbent": {
            "objective_gbps": rollback_metrics.objective_raw * GBPS,
            "huailai_tasks": rollback_metrics.huailai_tasks,
            "theta": rollback_metrics.theta,
        },
        "floor_descent_moves": floor_history,
        "peak_count_lns": peak_lns_info,
        "guarded_candidate": {
            "objective_gbps": candidate_metrics.objective_raw * GBPS,
            "huailai_tasks": candidate_metrics.huailai_tasks,
            "theta": candidate_metrics.theta,
            "strict_improvement": strict_improvement,
            "near_tie_count_ok": near_tie_count_ok,
            "published": publish_candidate,
        },
        "migration": migration,
        "negative_corruption_rejected": negative_rejected,
        "load_seconds": load_seconds,
        "target_comparison_seconds": target_seconds,
        "performance_search_seconds": search_seconds,
        "projection_seconds": projection_seconds,
        "guarded_lns_seconds": lns_seconds,
        "artifact_and_audit_seconds": dispatch_seconds - (audit_started - started),
        "dispatch_time_seconds": dispatch_seconds,
    }
    (OUT / "metrics.json").write_text(
        json.dumps(metrics_payload, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(metrics_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
