#!/usr/bin/env python3
"""Construct a two-region placement from the visible benchmark inputs.

The transfer objective can be viewed as a weighted task/table cut.  A table
with writers is local to both regions when its writers are split; otherwise a
consumer in the opposite region pays the transfer cost.  The search below
uses that exact rule with aggregate visible bandwidth as a stable proxy for
the temporal peak, then repairs resource margins using tasks that cannot
affect any visible transfer endpoint.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import polars as pl


REGION_Z = "中卫"
REGION_H = "怀来"

LIMITS = {
    "vcores_zw_peak": 2_479_131.7126728795,
    "memory_zw_peak_gb": 10_616_453.64463461,
    "vcores_hl_peak": 120_936.14278870597,
    "memory_hl_peak_gb": 675_432.7278241565,
}


def build_network_placement(
    data_dir: Path, iterations: int
) -> tuple[pl.DataFrame, np.ndarray, dict[str, np.ndarray]]:
    universe = pl.read_parquet(data_dir / "task_universe.parquet").with_row_index(
        "task_id"
    )
    tables = (
        pl.read_parquet(data_dir / "table_writers_fallback.parquet")
        .with_row_index("table_id")
        .with_columns(
            pl.col("task_name_list").list.len().alias("writer_count"),
            pl.col("region_list").list.len().alias("fallback_count"),
            pl.col("region_list").list.first().alias("fallback_first"),
        )
    )

    # Rows for the same task/table pair always have the same cut status.  Sum
    # their bandwidths so the local search is small and rewards placements
    # that remain good across both visible days.
    transfers = pl.read_parquet(
        data_dir / "trans_train_0808_0809.parquet",
        columns=["task_name", "table_uri", "estimated_bandwidth"],
    )
    edges = (
        transfers.group_by(["task_name", "table_uri"])
        .agg(pl.col("estimated_bandwidth").sum().alias("weight"))
        .join(universe, on="task_name", how="inner")
        .join(tables.select("table_uri", "table_id"), on="table_uri", how="inner")
    )
    writers = (
        tables.select("table_id", "task_name_list")
        .explode("task_name_list")
        .rename({"task_name_list": "task_name"})
        .filter(pl.col("task_name").is_not_null())
        .join(universe, on="task_name", how="inner")
    )

    task_count = universe.height
    table_count = tables.height
    edge_task = edges["task_id"].to_numpy().astype(np.int32, copy=False)
    edge_table = edges["table_id"].to_numpy().astype(np.int32, copy=False)
    edge_weight = edges["weight"].to_numpy().astype(np.float64, copy=False)
    writer_task = writers["task_id"].to_numpy().astype(np.int32, copy=False)
    writer_table = writers["table_id"].to_numpy().astype(np.int32, copy=False)
    writer_count = tables["writer_count"].to_numpy().astype(np.int32, copy=False)

    fallback_count = tables["fallback_count"].to_numpy()
    fallback_first = tables["fallback_first"].to_numpy()
    fixed_state = np.full(table_count, 2, dtype=np.int8)  # 2 means both/local.
    no_writers = writer_count == 0
    fixed_state[no_writers & (fallback_count == 1) & (fallback_first == REGION_Z)] = 0
    fixed_state[no_writers & (fallback_count == 1) & (fallback_first == REGION_H)] = 1

    total_weight = np.bincount(
        edge_table, weights=edge_weight, minlength=table_count
    )

    # Fixed one-region tables provide reliable anchors.  Start each task on
    # the side with the lower fixed-table transfer weight.
    fixed_h_edge = fixed_state[edge_table] == 1
    fixed_z_edge = fixed_state[edge_table] == 0
    anchor_h = np.bincount(
        edge_task[fixed_h_edge],
        weights=edge_weight[fixed_h_edge],
        minlength=task_count,
    )
    anchor_z = np.bincount(
        edge_task[fixed_z_edge],
        weights=edge_weight[fixed_z_edge],
        minlength=task_count,
    )
    is_h = anchor_h > anchor_z

    blocked = np.zeros(task_count, dtype=bool)
    previous_objective: float | None = None
    previous_task = -1

    # Deterministic one-coordinate descent.  Recomputing vectorized gains is
    # faster here than maintaining a large Python graph and stays well inside
    # the dispatch time limit.
    for _ in range(iterations):
        h_writer_count = np.bincount(
            writer_table, weights=is_h[writer_task], minlength=table_count
        ).astype(np.int32)
        h_consumer_weight = np.bincount(
            edge_table,
            weights=edge_weight * is_h[edge_task],
            minlength=table_count,
        )
        z_consumer_weight = total_weight - h_consumer_weight

        table_state = np.full(table_count, 2, dtype=np.int8)
        table_state[(writer_count > 0) & (h_writer_count == 0)] = 0
        table_state[
            (writer_count > 0) & (h_writer_count == writer_count)
        ] = 1
        table_state[no_writers] = fixed_state[no_writers]
        contribution = np.where(
            table_state == 0,
            h_consumer_weight,
            np.where(table_state == 1, z_consumer_weight, 0.0),
        )
        objective = float(contribution.sum())

        # The vectorized gain double-counts the rare case where a task both
        # consumes and writes the same table.  Reject and blacklist any such
        # proposed move if its exact next-round objective is worse.
        if previous_objective is not None and objective > previous_objective + 1e-3:
            is_h[previous_task] = ~is_h[previous_task]
            blocked[previous_task] = True
            previous_objective = None
            continue

        previous_objective = objective
        h_edge = is_h[edge_task].astype(np.int8)
        consumer_gain = np.where(
            table_state[edge_table] == 0,
            (2 * h_edge - 1) * edge_weight,
            np.where(
                table_state[edge_table] == 1,
                (1 - 2 * h_edge) * edge_weight,
                0.0,
            ),
        )
        gain = np.bincount(
            edge_task, weights=consumer_gain, minlength=task_count
        )

        new_h_writer_count = h_writer_count[writer_table] + np.where(
            is_h[writer_task], -1, 1
        )
        new_state = np.full(writer_table.size, 2, dtype=np.int8)
        new_state[new_h_writer_count == 0] = 0
        new_state[
            new_h_writer_count == writer_count[writer_table]
        ] = 1
        new_contribution = np.where(
            new_state == 0,
            h_consumer_weight[writer_table],
            np.where(new_state == 1, z_consumer_weight[writer_table], 0.0),
        )
        writer_gain = contribution[writer_table] - new_contribution
        gain += np.bincount(
            writer_task, weights=writer_gain, minlength=task_count
        )
        gain[blocked] = -np.inf
        selected = int(np.argmax(gain))
        if not np.isfinite(gain[selected]) or gain[selected] <= 1e-6:
            break
        previous_task = selected
        is_h[selected] = ~is_h[selected]

    # Validate the final tentative move and undo it if necessary.
    if previous_objective is not None and previous_task >= 0:
        h_writer_count = np.bincount(
            writer_table, weights=is_h[writer_task], minlength=table_count
        ).astype(np.int32)
        h_consumer_weight = np.bincount(
            edge_table,
            weights=edge_weight * is_h[edge_task],
            minlength=table_count,
        )
        state = np.full(table_count, 2, dtype=np.int8)
        state[(writer_count > 0) & (h_writer_count == 0)] = 0
        state[(writer_count > 0) & (h_writer_count == writer_count)] = 1
        state[no_writers] = fixed_state[no_writers]
        final_objective = float(
            np.where(
                state == 0,
                h_consumer_weight,
                np.where(state == 1, total_weight - h_consumer_weight, 0.0),
            ).sum()
        )
        if final_objective > previous_objective + 1e-3:
            is_h[previous_task] = ~is_h[previous_task]

    network = {
        "edge_task": edge_task,
        "edge_table": edge_table,
        "writer_task": writer_task,
        "writer_table": writer_table,
        "table_has_transfer": total_weight > 0,
    }
    return universe, is_h, network


def _profiles(
    day: np.ndarray,
    start: np.ndarray,
    end: np.ndarray,
    values: np.ndarray,
    row_is_h: np.ndarray,
    day_count: int,
    lengths: list[int],
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    z_profiles: list[np.ndarray] = []
    h_profiles: list[np.ndarray] = []
    for date_id in range(day_count):
        use = day == date_id
        length = lengths[date_id]
        starts = start[use]
        stops = end[use] + 1  # starts precede ends, so endpoints are inclusive.
        vals = values[use]
        labels = row_is_h[use]
        all_diff = np.bincount(starts, weights=vals, minlength=length + 1)
        all_diff -= np.bincount(stops, weights=vals, minlength=length + 1)
        h_diff = np.bincount(
            starts[labels], weights=vals[labels], minlength=length + 1
        )
        h_diff -= np.bincount(
            stops[labels], weights=vals[labels], minlength=length + 1
        )
        total = np.cumsum(all_diff[:length])
        h_value = np.cumsum(h_diff[:length])
        z_profiles.append(total - h_value)
        h_profiles.append(h_value)
    return z_profiles, h_profiles


def repair_resources(
    data_dir: Path,
    is_h: np.ndarray,
    network: dict[str, np.ndarray],
    target_z_utilization: float,
) -> int:
    execution = (
        pl.read_parquet(data_dir / "exec_train_0808_0809.parquet")
        .with_columns(
            pl.col("running_start_time").str.to_datetime().alias("start_dt"),
            pl.col("running_end_time").str.to_datetime().alias("end_dt"),
        )
        .filter(pl.col("end_dt") > pl.col("start_dt"))
    )
    universe = pl.read_parquet(data_dir / "task_universe.parquet").with_row_index(
        "task_id"
    )
    execution = execution.join(universe, on="task_name", how="inner")

    dates = sorted(execution["dt"].unique().to_list())
    date_to_id = {value: index for index, value in enumerate(dates)}
    day = np.array([date_to_id[value] for value in execution["dt"].to_list()], dtype=np.int8)
    start_dt = execution["start_dt"].to_numpy().astype("datetime64[m]")
    end_dt = execution["end_dt"].to_numpy().astype("datetime64[m]")
    bases = np.array([np.datetime64(f"{d[:4]}-{d[4:6]}-{d[6:8]}", "m") for d in dates])
    start = (start_dt - bases[day]).astype(np.int64)
    end = (end_dt - bases[day]).astype(np.int64)
    lengths = [int(end[day == i].max()) + 2 for i in range(len(dates))]
    task = execution["task_id"].to_numpy().astype(np.int32, copy=False)
    vcores = execution["avg_vcores"].to_numpy().astype(np.float64, copy=False)
    memory = execution["avg_mb"].to_numpy().astype(np.float64, copy=False) / 1024.0

    # A task is bandwidth-neutral on the visible data only if it is neither a
    # transfer task nor a writer of a table used by a visible transfer.
    relevant = np.zeros(is_h.size, dtype=bool)
    relevant[network["edge_task"]] = True
    writer_is_relevant = network["table_has_transfer"][network["writer_table"]]
    relevant[network["writer_task"][writer_is_relevant]] = True
    safe = ~relevant

    row_is_h = is_h[task]
    z_v, h_v = _profiles(day, start, end, vcores, row_is_h, len(dates), lengths)
    z_m, h_m = _profiles(day, start, end, memory, row_is_h, len(dates), lengths)

    # CSR-style row lookup makes testing and applying a selected task cheap.
    order = np.argsort(task, kind="stable")
    sorted_task = task[order]
    counts = np.bincount(sorted_task, minlength=is_h.size)
    offsets = np.empty(is_h.size + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(counts, out=offsets[1:])

    z_limits = (LIMITS["vcores_zw_peak"], LIMITS["memory_zw_peak_gb"])
    h_limits = (LIMITS["vcores_hl_peak"], LIMITS["memory_hl_peak_gb"])
    selected_count = 0

    for _round in range(30):
        points: list[tuple[int, int, int, float]] = []
        for date_id in range(len(dates)):
            for resource, profile in enumerate((z_v[date_id], z_m[date_id])):
                when = int(np.argmax(profile))
                ratio = float(profile[when]) / (z_limits[resource] * target_z_utilization)
                if ratio > 1.0:
                    points.append((date_id, resource, when, ratio))
        if not points:
            break

        score = np.zeros(is_h.size, dtype=np.float64)
        for date_id, resource, when, _ratio in points:
            values = vcores if resource == 0 else memory
            active = (
                (day == date_id)
                & (start <= when)
                & (end >= when)
                & safe[task]
                & (~is_h[task])
            )
            score += np.bincount(
                task[active],
                weights=values[active] / z_limits[resource],
                minlength=is_h.size,
            )
        candidates = np.flatnonzero(score > 0)
        if candidates.size == 0:
            break
        candidates = candidates[np.argsort(score[candidates], kind="stable")[::-1]]

        accepted = 0
        for candidate in candidates[:300]:
            rows = order[offsets[candidate] : offsets[candidate + 1]]
            if rows.size == 0:
                continue
            fits = True
            for row in rows:
                d = int(day[row])
                sl = slice(int(start[row]), int(end[row]) + 1)
                if (
                    np.max(h_v[d][sl] + vcores[row])
                    > h_limits[0] * 0.95
                    or np.max(h_m[d][sl] + memory[row])
                    > h_limits[1] * 0.95
                ):
                    fits = False
                    break
            if not fits:
                continue
            is_h[candidate] = True
            for row in rows:
                d = int(day[row])
                sl = slice(int(start[row]), int(end[row]) + 1)
                z_v[d][sl] -= vcores[row]
                h_v[d][sl] += vcores[row]
                z_m[d][sl] -= memory[row]
                h_m[d][sl] += memory[row]
            selected_count += 1
            accepted += 1
            if accepted >= 24:
                break
        if accepted == 0:
            break

    return selected_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parents[1] / "data")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results" / "solution.parquet")
    parser.add_argument("--iterations", type=int, default=1600)
    parser.add_argument("--target-z-utilization", type=float, default=0.985)
    args = parser.parse_args()

    started = time.monotonic()
    universe, is_h, network = build_network_placement(args.data_dir, args.iterations)
    network_h = int(is_h.sum())
    repaired = repair_resources(
        args.data_dir, is_h, network, args.target_z_utilization
    )
    solution = universe.select("task_name").with_columns(
        pl.Series("region", np.where(is_h, REGION_H, REGION_Z))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    solution.write_parquet(args.output)
    elapsed = time.monotonic() - started
    print(
        f"wrote {solution.height} tasks to {args.output}; "
        f"network_h={network_h}, resource_repairs={repaired}, "
        f"total_h={int(is_h.sum())}, seconds={elapsed:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
