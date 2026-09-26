#!/usr/bin/env python3
from pathlib import Path
import polars as pl

DATA = Path('/work/app/data')
OUT = Path('/work/app/candidate/tmp')
OUT.mkdir(parents=True, exist_ok=True)
tasks = pl.read_parquet(DATA / 'task_universe.parquet')
for label, region in [('all_zw', '中卫'), ('all_hl', '怀来')]:
    tasks.with_columns(pl.lit(region).alias('region')).write_parquet(OUT / f'{label}.parquet')
