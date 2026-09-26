#!/usr/bin/env python3
"""Final deterministic visible-input task-to-region optimizer."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import polars as pl

from fast_search import H, Z, Model, run_search


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/work/app/data"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/work/app/candidate/results/solution.parquet"),
    )
    parser.add_argument("--runtime-file", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    model = Model(args.data_dir)
    colors, score = run_search(model, verbose=True)
    feasible, _ = model.resources(colors)
    if not feasible:
        raise RuntimeError("final assignment is not strictly resource-feasible")
    result = model.tasks.select("task_name").with_columns(
        pl.Series("region", np.where(colors, H, Z))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.write_parquet(args.output)
    elapsed = time.monotonic() - started
    if args.runtime_file:
        args.runtime_file.parent.mkdir(parents=True, exist_ok=True)
        args.runtime_file.write_text(f"{elapsed:.9f}\n", encoding="utf-8")
    print(f"final tasks_hl={int(colors.sum())} score={score} seconds={elapsed:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
