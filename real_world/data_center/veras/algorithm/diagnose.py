#!/usr/bin/env python3
"""Low-memory exact-semantics diagnostics used by the visible-day search."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq


Z = "中卫"
H = "怀来"
LIMITS = {
    "vcores_zw_peak": 2_479_131.7126728795,
    "memory_zw_peak_gb": 10_616_453.64463461,
    "vcores_hl_peak": 120_936.14278870597,
    "memory_hl_peak_gb": 675_432.7278241565,
}


def add_grouped(target, frame, time_col, value_cols):
    if not frame.height:
        return
    grouped = frame.group_by(["dt", time_col]).agg(
        *(pl.col(column).sum().alias(column) for column in value_cols)
    )
    for row in grouped.iter_rows(named=True):
        key = (row["dt"], int(row[time_col]))
        slot = target[key]
        for i, column in enumerate(value_cols):
            slot[i] += float(row[column])


def peak_from_events(starts, ends, date, index):
    current = peak = 0.0
    peak_time = 0
    times = sorted({t for d, t in starts if d == date} | {t for d, t in ends if d == date})
    for minute in times:
        current += starts[(date, minute)][index]
        if current > peak:
            peak, peak_time = current, minute
        current -= ends[(date, minute)][index]
    return peak, peak_time


def table_state(data_dir: Path, huailai: set[str | None]) -> pl.DataFrame:
    universe = set(pl.read_parquet(data_dir / "task_universe.parquet")["task_name"].to_list())
    writers = pl.read_parquet(data_dir / "table_writers_fallback.parquet")
    uris, has_z, has_h = [], [], []
    for uri, names, fallback in writers.iter_rows():
        assigned = [H if name in huailai else Z for name in names if name in universe]
        regions = assigned if assigned else fallback
        uris.append(uri)
        has_z.append(Z in regions)
        has_h.append(H in regions)
    return pl.DataFrame({"table_uri": uris, "has_z": has_z, "has_h": has_h})


def diagnose(solution: Path, data_dir: Path) -> dict:
    sol = pl.read_parquet(solution)
    huailai = set(sol.filter(pl.col("region") == H)["task_name"].to_list())
    state = table_state(data_dir, huailai)

    bw_s = defaultdict(lambda: [0.0, 0.0])
    bw_e = defaultdict(lambda: [0.0, 0.0])
    trans = pq.ParquetFile(data_dir / "trans_train_0808_0809.parquet")
    trans_cols = ["task_name", "table_uri", "relation_type", "dt", "trans_start_time", "trans_time", "estimated_bandwidth"]
    for rg in range(trans.metadata.num_row_groups):
        frame = pl.from_arrow(trans.read_row_group(rg, columns=trans_cols)).join(state, on="table_uri", how="left")
        frame = frame.with_columns(
            pl.col("task_name").is_in(huailai).alias("task_h"),
            (pl.col("trans_start_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_start_time").str.slice(3, 2).cast(pl.Int32)).alias("start_minute"),
            (pl.col("trans_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_time").str.slice(3, 2).cast(pl.Int32)).alias("duration"),
        ).filter(
            (pl.col("task_h") & ~pl.col("has_h")) | (~pl.col("task_h") & ~pl.col("has_z"))
        ).with_columns(
            pl.when(
                (pl.col("task_h") & (pl.col("relation_type") == 0))
                | (~pl.col("task_h") & (pl.col("relation_type") != 0))
            ).then(pl.lit(0)).otherwise(pl.lit(1)).alias("direction"),
            (pl.col("start_minute") + pl.col("duration")).alias("end_minute"),
        )
        # Split directions while retaining a compact two-value event array.
        grouped_s = frame.group_by(["dt", "start_minute", "direction"]).agg(pl.col("estimated_bandwidth").sum())
        grouped_e = frame.group_by(["dt", "end_minute", "direction"]).agg(pl.col("estimated_bandwidth").sum())
        for d, minute, direction, value in grouped_s.iter_rows():
            bw_s[(d, int(minute))][int(direction)] += float(value)
        for d, minute, direction, value in grouped_e.iter_rows():
            bw_e[(d, int(minute))][int(direction)] += float(value)

    res_s = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    res_e = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    execution = pq.ParquetFile(data_dir / "exec_train_0808_0809.parquet")
    exec_cols = ["task_name", "running_start_time", "running_end_time", "avg_vcores", "avg_mb", "dt"]
    for rg in range(execution.metadata.num_row_groups):
        frame = pl.from_arrow(execution.read_row_group(rg, columns=exec_cols)).filter(
            pl.col("running_end_time") > pl.col("running_start_time")
        ).with_columns(
            pl.col("task_name").is_in(huailai).alias("task_h"),
            (pl.col("running_start_time").str.slice(11, 2).cast(pl.Int32) * 60 + pl.col("running_start_time").str.slice(14, 2).cast(pl.Int32)).alias("start_minute"),
            (pl.col("running_end_time").str.slice(11, 2).cast(pl.Int32) * 60 + pl.col("running_end_time").str.slice(14, 2).cast(pl.Int32)).alias("end_minute"),
        ).with_columns(
            pl.when(pl.col("task_h")).then(pl.lit(0.0)).otherwise(pl.col("avg_vcores")).alias("vz"),
            pl.when(pl.col("task_h")).then(pl.lit(0.0)).otherwise(pl.col("avg_mb")).alias("mz"),
            pl.when(pl.col("task_h")).then(pl.col("avg_vcores")).otherwise(pl.lit(0.0)).alias("vh"),
            pl.when(pl.col("task_h")).then(pl.col("avg_mb")).otherwise(pl.lit(0.0)).alias("mh"),
        )
        add_grouped(res_s, frame, "start_minute", ["vz", "mz", "vh", "mh"])
        add_grouped(res_e, frame, "end_minute", ["vz", "mz", "vh", "mh"])

    per_date = {}
    for date in ["20250808", "20250809"]:
        zh, tzh = peak_from_events(bw_s, bw_e, date, 0)
        hz, thz = peak_from_events(bw_s, bw_e, date, 1)
        resources = [peak_from_events(res_s, res_e, date, i) for i in range(4)]
        values = {
            "bandwidth_zw_to_hl_peak_gbps": zh / 1e9,
            "bandwidth_hl_to_zw_peak_gbps": hz / 1e9,
            "bandwidth_zw_to_hl_peak_minute": tzh,
            "bandwidth_hl_to_zw_peak_minute": thz,
            "vcores_zw_peak": resources[0][0],
            "vcores_zw_peak_minute": resources[0][1],
            "memory_zw_peak_gb": resources[1][0] / 1024.0,
            "memory_zw_peak_minute": resources[1][1],
            "vcores_hl_peak": resources[2][0],
            "vcores_hl_peak_minute": resources[2][1],
            "memory_hl_peak_gb": resources[3][0] / 1024.0,
            "memory_hl_peak_minute": resources[3][1],
        }
        values["max_directional_bandwidth_peak_gbps"] = max(values["bandwidth_zw_to_hl_peak_gbps"], values["bandwidth_hl_to_zw_peak_gbps"])
        values["resource_margin"] = {key: LIMITS[key] - values[key] for key in LIMITS}
        values["strict_resource_feasible"] = all(value >= 0 for value in values["resource_margin"].values())
        per_date[date] = values
    objectives = [v["max_directional_bandwidth_peak_gbps"] for v in per_date.values()]
    return {
        "tasks_hl": len(huailai),
        "per_date": per_date,
        "max_directional_bandwidth_peak_gbps": max(objectives),
        "mean_daily_max_bandwidth_peak_gbps": sum(objectives) / len(objectives),
        "temporal_peak_spread_gbps": max(objectives) - min(objectives),
        "strict_resource_feasible": all(v["strict_resource_feasible"] for v in per_date.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("solution", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("/work/app/data"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = diagnose(args.solution, args.data_dir)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
