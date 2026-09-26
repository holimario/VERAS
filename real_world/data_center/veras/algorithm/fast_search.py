#!/usr/bin/env python3
"""Prototype in-memory robust peak local search over the visible replay."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow.parquet as pq

Z, H = "中卫", "怀来"
LZV, LZM = 2_479_131.7126728795, 10_616_453.64463461 * 1024
LHV, LHM = 120_936.14278870597, 675_432.7278241565 * 1024


def event_peak(starts, ends):
    before = np.empty_like(starts)
    before[0] = 0.0
    if len(starts) > 1:
        before[1:] = np.cumsum(starts[:-1] - ends[:-1])
    active = before + starts
    point = int(np.argmax(active))
    return float(active[point]), point


class Model:
    def __init__(self, data: Path):
        self.data = data
        tasks = pl.read_parquet(data / "task_universe.parquet").with_row_index("task_id")
        self.tasks = tasks
        self.n = tasks.height
        task_lookup = tasks.select("task_name", pl.col("task_id").cast(pl.Int32))
        task_index = dict(zip(tasks["task_name"].to_list(), tasks["task_id"].to_list()))
        tables = pl.read_parquet(data / "table_writers_fallback.parquet").with_row_index("table_id")
        self.nt = tables.height
        table_lookup = tables.select("table_uri", pl.col("table_id").cast(pl.Int32))
        wp_task, wp_table = [], []
        fallback_h = np.zeros(self.nt, dtype=np.bool_)
        fallback_z = np.zeros(self.nt, dtype=np.bool_)
        writer_count = np.zeros(self.nt, dtype=np.int32)
        for table_id, names, regions in tables.select("table_id", "task_name_list", "region_list").iter_rows():
            ids = [task_index[name] for name in names if name in task_index]
            writer_count[table_id] = len(ids)
            wp_task.extend(ids)
            wp_table.extend([table_id] * len(ids))
            if not ids:
                fallback_h[table_id] = H in regions
                fallback_z[table_id] = Z in regions
        self.writer_task = np.asarray(wp_task, dtype=np.int32)
        self.writer_table = np.asarray(wp_table, dtype=np.int32)
        self.writer_count = writer_count
        self.fallback_h = fallback_h
        self.fallback_z = fallback_z
        self.sole_tables_by_writer = {}
        sole = np.flatnonzero(writer_count == 1)
        if len(sole):
            first_writer = np.full(self.nt, -1, dtype=np.int32)
            first_writer[self.writer_table] = self.writer_task
            for table_id in sole:
                self.sole_tables_by_writer.setdefault(int(first_writer[table_id]), []).append(int(table_id))

        trans_parts = [[] for _ in range(7)]
        pf = pq.ParquetFile(data / "trans_train_0808_0809.parquet")
        columns = ["task_name", "table_uri", "relation_type", "dt", "trans_start_time", "trans_time", "estimated_bandwidth"]
        for rg in range(pf.metadata.num_row_groups):
            frame = pl.from_arrow(pf.read_row_group(rg, columns=columns)).join(task_lookup, on="task_name", how="left").join(table_lookup, on="table_uri", how="left").with_columns(
                (pl.col("dt") == "20250809").cast(pl.UInt8).alias("day"),
                (pl.col("trans_start_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_start_time").str.slice(3, 2).cast(pl.Int32)).alias("start"),
                (pl.col("trans_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_time").str.slice(3, 2).cast(pl.Int32)).alias("duration"),
            )
            vals = frame.select("task_id", "table_id", "relation_type", "day", "start", "duration", "estimated_bandwidth")
            for i, name in enumerate(vals.columns):
                trans_parts[i].append(vals[name].to_numpy())
        arrays = [np.concatenate(part) for part in trans_parts]
        self.ttask = arrays[0].astype(np.int32, copy=False)
        self.ttable = arrays[1].astype(np.int32, copy=False)
        self.trel = arrays[2].astype(np.bool_, copy=False)
        self.tday = arrays[3].astype(np.uint8, copy=False)
        self.tstart = arrays[4].astype(np.int32, copy=False)
        self.tend = (arrays[4] + arrays[5]).astype(np.int32, copy=False)
        self.tbw = arrays[6].astype(np.float64, copy=False)
        self.max_tminute = int(self.tend.max()) + 1

        exec_parts = [[] for _ in range(7)]
        pf = pq.ParquetFile(data / "exec_train_0808_0809.parquet")
        columns = ["task_name", "running_start_time", "running_end_time", "avg_vcores", "avg_mb", "dt"]
        for rg in range(pf.metadata.num_row_groups):
            frame = pl.from_arrow(pf.read_row_group(rg, columns=columns)).join(task_lookup, on="task_name", how="left").filter(pl.col("running_end_time") > pl.col("running_start_time")).with_columns(
                (pl.col("dt") == "20250809").cast(pl.UInt8).alias("day"),
                pl.col("running_start_time").str.to_datetime().alias("start_dt"),
                pl.col("running_end_time").str.to_datetime().alias("end_dt"),
            ).with_columns(
                (pl.col("start_dt").dt.hour().cast(pl.Int32) * 60 + pl.col("start_dt").dt.minute().cast(pl.Int32)).alias("start"),
                ((pl.col("end_dt") - pl.col("start_dt")).dt.total_minutes().cast(pl.Int32) + pl.col("start_dt").dt.hour().cast(pl.Int32) * 60 + pl.col("start_dt").dt.minute().cast(pl.Int32)).alias("end"),
            )
            vals = frame.select("task_id", "day", "start", "end", "avg_vcores", "avg_mb")
            for i, name in enumerate(vals.columns):
                exec_parts[i].append(vals[name].to_numpy())
        earrays = [np.concatenate(part) for part in exec_parts[:6]]
        self.etask = earrays[0].astype(np.int32, copy=False)
        self.eday = earrays[1].astype(np.uint8, copy=False)
        self.estart = earrays[2].astype(np.int32, copy=False)
        self.eend = earrays[3].astype(np.int32, copy=False)
        self.ev = earrays[4].astype(np.float64, copy=False)
        self.em = earrays[5].astype(np.float64, copy=False)
        self.max_eminute = int(self.eend.max()) + 1

    def table_presence(self, colors):
        count_h = np.bincount(self.writer_table, weights=colors[self.writer_task], minlength=self.nt)
        covered = self.writer_count > 0
        has_h = np.where(covered, count_h > 0, self.fallback_h)
        has_z = np.where(covered, count_h < self.writer_count, self.fallback_z)
        return has_h, has_z

    def bandwidth(self, colors, details=False, include_daily=False):
        has_h, has_z = self.table_presence(colors)
        task_h = colors[self.ttask]
        cross = np.where(task_h, ~has_h[self.ttable], ~has_z[self.ttable])
        zh = task_h != self.trel
        out = []
        peak_info = []
        for day in (0, 1):
            values = []
            info = []
            for direction_zh in (True, False):
                mask = cross & (self.tday == day) & (zh == direction_zh)
                starts = np.bincount(self.tstart[mask], weights=self.tbw[mask], minlength=self.max_tminute)
                ends = np.bincount(self.tend[mask], weights=self.tbw[mask], minlength=self.max_tminute)
                peak, minute = event_peak(starts, ends)
                values.append(peak / 1e9)
                info.append((peak, minute, mask))
            out.append(values)
            peak_info.append(info)
        daily = [max(row) for row in out]
        metrics = (max(daily), sum(daily) / 2, abs(daily[0] - daily[1]))
        if details:
            return metrics, out, peak_info, cross, zh, has_h, has_z
        if include_daily:
            return metrics, tuple(daily)
        return metrics

    def resources(self, colors):
        task_h = colors[self.etask]
        result = []
        feasible = True
        for day in (0, 1):
            vals = []
            for region_h, weights, limit in ((False, self.ev, LZV), (False, self.em, LZM), (True, self.ev, LHV), (True, self.em, LHM)):
                mask = (self.eday == day) & (task_h == region_h)
                starts = np.bincount(self.estart[mask], weights=weights[mask], minlength=self.max_eminute)
                ends = np.bincount(self.eend[mask], weights=weights[mask], minlength=self.max_eminute)
                peak, minute = event_peak(starts, ends)
                vals.append((peak, minute, limit - peak))
                feasible &= peak <= limit
            result.append(vals)
        return feasible, result

    def initial_colors(self):
        colors = np.zeros(self.n, dtype=np.bool_)
        # Find the exact all-Z shortage timestamps on both dates.
        constraints = []
        for day in (0, 1):
            mask = self.eday == day
            for weights, limit in ((self.ev, LZV), (self.em, LZM)):
                starts = np.bincount(self.estart[mask], weights=weights[mask], minlength=self.max_eminute)
                ends = np.bincount(self.eend[mask], weights=weights[mask], minlength=self.max_eminute)
                before = np.empty_like(starts); before[0] = 0
                before[1:] = np.cumsum(starts[:-1] - ends[:-1])
                active = before + starts
                for minute in np.flatnonzero(active > limit):
                    constraints.append((day, int(minute), weights, float((active[minute] - limit) * 1.01 + (1.0 if weights is self.ev else 1024.0))))
        contrib = []
        for day, minute, weights, target in constraints:
            active_rows = (self.eday == day) & (self.estart <= minute) & (self.eend >= minute)
            contrib.append(np.bincount(self.etask[active_rows], weights=weights[active_rows], minlength=self.n))
        remaining = np.array([item[3] for item in constraints])
        targets = remaining.copy()
        while np.any(remaining > 0):
            gain = np.zeros(self.n)
            for i, values in enumerate(contrib):
                if remaining[i] > 0:
                    gain += np.minimum(remaining[i], values) / max(targets[i], 1.0)
            gain[colors] = -1
            task = int(np.argmax(gain))
            if gain[task] <= 0:
                raise RuntimeError("resource cover failed")
            colors[task] = True
            remaining = np.maximum(0.0, remaining - np.array([values[task] for values in contrib]))
        return colors

    def candidates_at_peaks(self, colors, limit=8):
        _, _, infos, cross, zh, _, _ = self.bandwidth(colors, details=True)
        candidates = set()
        for day in (0, 1):
            for direction_index, direction_zh in enumerate((True, False)):
                peak, minute, _ = infos[day][direction_index]
                mask = cross & (self.tday == day) & (zh == direction_zh) & (self.tstart <= minute) & (self.tend >= minute)
                scores = np.bincount(self.ttask[mask], weights=self.tbw[mask], minlength=self.n)
                if np.any(scores):
                    ids = np.argpartition(scores, -min(limit, np.count_nonzero(scores)))[-min(limit, np.count_nonzero(scores)):]
                    candidates.update(int(i) for i in ids if scores[i] > 0)
        return candidates

    def star_bundles(self, colors, task, max_consumers=8):
        tables = self.sole_tables_by_writer.get(task, [])
        if not tables:
            return []
        mask = np.isin(self.ttable, np.asarray(tables, dtype=np.int32)) & (self.ttask != task)
        scores = np.bincount(self.ttask[mask], weights=self.tbw[mask], minlength=self.n)
        ids = [int(i) for i in np.argsort(scores)[::-1] if scores[i] > 0 and not colors[i]][:max_consumers]
        return [{task, *ids[:k]} for k in (1, 2, 3, 5, 8) if len(ids) >= k]


def better(a, b, eps=1e-7):
    for x, y in zip(a, b):
        if x < y - eps:
            return True
        if x > y + eps:
            return False
    return False


def run_search(model, *, verbose=True):
    colors = model.initial_colors()
    score, daily = model.bandwidth(colors, include_daily=True)
    if verbose:
        print("initial", int(colors.sum()), score)
    for iteration in range(80):
        feasible, _ = model.resources(colors)
        if not feasible:
            raise RuntimeError("incumbent lost resource feasibility")
        candidate_ids = model.candidates_at_peaks(colors)
        best = None
        best_score = score
        for task in sorted(candidate_ids):
            # The construction starts from the smallest resource cover.  Keeping
            # those tasks fixed makes Zhongwei feasibility monotone during search.
            if colors[task]:
                continue
            trial = colors.copy()
            trial[task] = True
            trial_score, trial_daily = model.bandwidth(trial, include_daily=True)
            if not better(trial_score, best_score):
                continue
            if trial_daily[1] > daily[1] * 1.005 + 1e-7:
                continue
            feasible, _ = model.resources(trial)
            if feasible:
                best, best_score, best_daily = trial, trial_score, trial_daily
        if best is None:
            # Deterministic one-hop LNS around rejected peak contributors.
            for task in sorted(candidate_ids):
                if colors[task]:
                    continue
                for bundle in model.star_bundles(colors, task):
                    trial = colors.copy()
                    trial[list(bundle)] = True
                    trial_score, trial_daily = model.bandwidth(trial, include_daily=True)
                    if not better(trial_score, best_score):
                        continue
                    if trial_daily[1] > daily[1] * 1.005 + 1e-7:
                        continue
                    feasible, _ = model.resources(trial)
                    if feasible:
                        best, best_score, best_daily = trial, trial_score, trial_daily
        if best is None:
            if verbose:
                print("local optimum", iteration, score)
            break
        colors, score, daily = best, best_score, best_daily
        if verbose:
            print("accept", iteration + 1, int(colors.sum()), score)
    return colors, score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("/work/app/data"))
    ap.add_argument("--output", type=Path, default=Path("/work/app/candidate/tmp/fast.parquet"))
    args = ap.parse_args()
    started = time.monotonic()
    model = Model(args.data_dir)
    colors, score = run_search(model)
    feasible, resources = model.resources(colors)
    print("final", int(colors.sum()), score, "feasible", feasible, "seconds", time.monotonic() - started)
    result = model.tasks.select("task_name").with_columns(
        pl.Series("region", np.where(colors, H, Z))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.write_parquet(args.output)


if __name__ == "__main__":
    main()
