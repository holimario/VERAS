#!/usr/bin/env python3
"""Create deterministic visible-day peak seed variants for exact replay."""

from collections import defaultdict
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

D = Path("/work/app/data")
C = Path("/work/app/candidate")
H = "怀来"
Z = "中卫"

base = pl.read_parquet(C / "results/solution.parquet")
base_h = set(base.filter(pl.col("region") == H)["task_name"].to_list())
universe = set(base["task_name"].to_list())
writers = pl.read_parquet(D / "table_writers_fallback.parquet")
fallback_h = {uri for uri, names, regions in writers.iter_rows() if not any(n in universe for n in names) and regions == [H]}

rows = []
source = pq.ParquetFile(D / "trans_train_0808_0809.parquet")
columns = ["task_name", "table_uri", "relation_type", "dt", "trans_start_time", "trans_time", "estimated_bandwidth"]
for rg in range(source.metadata.num_row_groups):
    frame = pl.from_arrow(source.read_row_group(rg, columns=columns)).filter(
        pl.col("table_uri").is_in(fallback_h) & (pl.col("relation_type") == 0)
    ).with_columns(
        (pl.col("trans_start_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_start_time").str.slice(3, 2).cast(pl.Int32)).alias("start"),
        (pl.col("trans_time").str.slice(0, 2).cast(pl.Int32) * 60 + pl.col("trans_time").str.slice(3, 2).cast(pl.Int32)).alias("duration"),
    )
    rows.extend(frame.select("task_name", "dt", "start", "duration", "estimated_bandwidth").iter_rows())

peak_minutes = {"20250808": 65, "20250809": 64}
by_task = defaultdict(lambda: defaultdict(float))
for task, date, start, duration, bandwidth in rows:
    peak = peak_minutes[date]
    if start <= peak <= start + duration:
        by_task[task][date] += bandwidth

ranking = sorted(
    by_task,
    key=lambda task: (
        -min(by_task[task]["20250808"] / 9.086172629104185e12, by_task[task]["20250809"] / 7.819712268277352e12),
        -(by_task[task]["20250808"] / 9.086172629104185e12 + by_task[task]["20250809"] / 7.819712268277352e12),
        str(task),
    ),
)
print("ranking", [(task, by_task[task]["20250808"] / 1e9, by_task[task]["20250809"] / 1e9) for task in ranking[:25]])

out = C / "tmp/peak_variants"
out.mkdir(parents=True, exist_ok=True)
for count in range(1, 26):
    selected = base_h | set(ranking[:count])
    base.with_columns(
        pl.when(pl.col("task_name").is_in(selected)).then(pl.lit(H)).otherwise(pl.lit(Z)).alias("region")
    ).write_parquet(out / f"top_{count:02d}.parquet")
