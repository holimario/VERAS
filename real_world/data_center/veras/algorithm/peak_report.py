#!/usr/bin/env python3
"""Report cross-transfer influence at the active peak timestamps."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

Z, H = "中卫", "怀来"

parser = argparse.ArgumentParser()
parser.add_argument("solution", type=Path)
parser.add_argument("diagnostics", type=Path)
parser.add_argument("--data-dir", type=Path, default=Path("/work/app/data"))
args = parser.parse_args()

solution = pl.read_parquet(args.solution)
huailai = set(solution.filter(pl.col("region") == H)["task_name"].to_list())
universe = set(solution["task_name"].to_list())
writers = pl.read_parquet(args.data_dir / "table_writers_fallback.parquet")
state_rows = []
for uri, names, fallback in writers.iter_rows():
    assigned = [H if name in huailai else Z for name in names if name in universe]
    regions = assigned if assigned else fallback
    state_rows.append((uri, H in regions, Z in regions))
state = pl.DataFrame(state_rows, schema=["table_uri", "has_h", "has_z"], orient="row")

diag = json.loads(args.diagnostics.read_text())
peaks = {}
for date, values in diag["per_date"].items():
    peaks[(date, 0)] = values["bandwidth_zw_to_hl_peak_minute"]
    peaks[(date, 1)] = values["bandwidth_hl_to_zw_peak_minute"]

totals = defaultdict(float)
tables = defaultdict(float)
source = pq.ParquetFile(args.data_dir / "trans_train_0808_0809.parquet")
columns = ["task_name", "table_uri", "relation_type", "dt", "trans_start_time", "trans_time", "estimated_bandwidth"]
for rg in range(source.metadata.num_row_groups):
    frame = pl.from_arrow(source.read_row_group(rg, columns=columns)).join(state, on="table_uri", how="left").with_columns(
        pl.col("task_name").is_in(huailai).alias("task_h"),
        (pl.col("trans_start_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_start_time").str.slice(3, 2).cast(pl.Int32)).alias("start"),
        (pl.col("trans_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_time").str.slice(3, 2).cast(pl.Int32)).alias("duration"),
    ).filter(
        (pl.col("task_h") & ~pl.col("has_h")) | (~pl.col("task_h") & ~pl.col("has_z"))
    ).with_columns(
        pl.when(
            (pl.col("task_h") & (pl.col("relation_type") == 0))
            | (~pl.col("task_h") & (pl.col("relation_type") != 0))
        ).then(pl.lit(0)).otherwise(pl.lit(1)).alias("direction")
    )
    for task, table, relation, date, start, duration, bandwidth, task_h, has_h, has_z, direction in frame.select(
        "task_name", "table_uri", "relation_type", "dt", "start", "duration", "estimated_bandwidth", "task_h", "has_h", "has_z", "direction"
    ).iter_rows():
        peak = peaks[(date, direction)]
        if start <= peak <= start + duration:
            key = (date, direction, task, "task_h" if task_h else "task_z")
            totals[key] += bandwidth
            tables[(date, direction, table, task)] += bandwidth

for date in ["20250808", "20250809"]:
    for direction, label in [(0, "zw_to_hl"), (1, "hl_to_zw")]:
        ranked = sorted(((key, value) for key, value in totals.items() if key[:2] == (date, direction)), key=lambda item: -item[1])
        print(date, label, "minute", peaks[(date, direction)])
        for key, value in ranked[:20]:
            print(f"  {value/1e9:12.3f}  {key[3]:6s}  {key[2]}")
