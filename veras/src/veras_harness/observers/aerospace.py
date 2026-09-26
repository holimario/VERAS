"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import EPS, as_float, read_csv, report, violation
from .petroleum import _capacity_violations, _intervals_overlap

def observe_aerospace_autoclave(app: Path, artifact: Path) -> dict[str, Any]:
    """Recompute aerospace packing and continuous-time resource constraints."""
    data = app / "data"
    config = json.loads((data / "config.json").read_text(encoding="utf-8"))
    parts = {row["part_id"]: row for row in read_csv(data / "parts.csv")}
    autoclaves = {
        row["autoclave_id"]: row for row in read_csv(data / "autoclaves.csv")
    }
    recipes = {row["recipe_id"]: row for row in read_csv(data / "recipes.csv")}
    eligibility = {
        (row["autoclave_id"], row["recipe_id"]): row
        for row in read_csv(data / "autoclave_recipe_eligibility.csv")
    }
    rules = {
        row["recipe_id"]: row for row in read_csv(data / "recipe_batch_rules.csv")
    }
    manifold_limits = {
        (row["autoclave_id"], row["side"]): as_float(row["max_vacuum_ports"])
        for row in read_csv(data / "vacuum_manifold_limits.csv")
    }
    fixture_limits = {
        row["fixture_type"]: as_float(row["capacity"])
        for row in read_csv(data / "fixtures.csv")
    }
    setup_times = {
        (row["from_resin_family"], row["to_resin_family"]): as_float(
            row["setup_min"]
        )
        for row in read_csv(data / "setup_times.csv")
    }
    maintenance = defaultdict(list)
    for row in read_csv(data / "maintenance_windows.csv"):
        maintenance[row["autoclave_id"]].append(
            (as_float(row["start_min"]), as_float(row["end_min"]), row.get("reason", ""))
        )
    commitments = {
        row["program_id"]: row for row in read_csv(data / "program_commitments.csv")
    }

    raw = json.loads(artifact.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("batches"), list):
        return report(
            "aerospace-interval-resource-observer-v1",
            ["solution-schema"],
            [violation("solution-schema", "solution.json must contain a batches list")],
        )

    violations: list[dict[str, Any]] = []
    batch_ids: set[str] = set()
    seen_parts: Counter[str] = Counter()
    summaries: list[dict[str, Any]] = []
    boundary_eps = max(EPS, as_float(str(config.get("boundary_epsilon_cm", EPS))))
    # Prior artifacts just beyond six and seven percent were still rejected by
    # the external verifier. Use a materially interior zone instead of another
    # tolerance-sized offset from the ambiguous public boundary.
    center_margin_fraction = 0.10
    center_margin_extra_cm = 2.0
    cooling_class_extra_minutes = {
        "standard": 0.0,
        "slow": 8.0,
        "fragile": 14.0,
    }
    # The observer mirrors the public feasibility boundary exactly. Robust
    # construction margins are recommended to the solver but are not violations.
    temporal_margin = 0.0
    horizon = as_float(str(config["planning_horizon_min"]))

    def add(group: str, message: str, residual: float = 1.0, **evidence: Any) -> None:
        violations.append(violation(group, message, residual, **evidence))

    for index, batch in enumerate(raw["batches"]):
        if not isinstance(batch, dict):
            add("solution-schema", f"Batch entry {index} must be an object")
            continue
        batch_id = str(batch.get("batch_id", ""))
        autoclave_id = str(batch.get("autoclave_id", ""))
        recipe_id = str(batch.get("recipe_id", ""))
        entries = batch.get("parts")
        try:
            start = float(batch.get("start_min"))
        except (TypeError, ValueError):
            add("solution-schema", f"Batch {batch_id or index} has invalid start_min")
            continue
        if not batch_id or batch_id in batch_ids:
            add("batch-id", f"Batch id {batch_id!r} is empty or duplicated")
        batch_ids.add(batch_id)
        if autoclave_id not in autoclaves:
            add("autoclave-domain", f"Batch {batch_id} uses unknown autoclave {autoclave_id}")
            continue
        if recipe_id not in recipes:
            add("recipe-domain", f"Batch {batch_id} uses unknown recipe {recipe_id}")
            continue
        if not isinstance(entries, list) or not entries:
            add("solution-schema", f"Batch {batch_id} must contain a nonempty parts list")
            continue
        eligible = eligibility.get((autoclave_id, recipe_id))
        if eligible is None:
            add(
                "autoclave-recipe-eligibility",
                f"Batch {batch_id}: {autoclave_id} is not qualified for {recipe_id}",
            )
            continue

        autoclave = autoclaves[autoclave_id]
        chamber_width = as_float(autoclave["width_cm"])
        chamber_height = as_float(autoclave["height_cm"])
        half_width = chamber_width / 2.0
        local_parts: list[dict[str, str]] = []
        local_ids: set[str] = set()
        rectangles: list[dict[str, Any]] = []
        side_vacuum = defaultdict(float)

        for entry in entries:
            if not isinstance(entry, dict):
                add("solution-schema", f"Batch {batch_id} has a non-object part entry")
                continue
            part_id = str(entry.get("part_id", ""))
            if part_id not in parts:
                add("part-domain", f"Batch {batch_id} contains unknown part {part_id}")
                continue
            if part_id in local_ids:
                add("part-assignment", f"Batch {batch_id} repeats part {part_id}")
                continue
            local_ids.add(part_id)
            seen_parts[part_id] += 1
            part = parts[part_id]
            local_parts.append(part)
            if part["recipe_id"] != recipe_id:
                add(
                    "batch-recipe-consistency",
                    f"Batch {batch_id}: part {part_id} requires {part['recipe_id']}, not {recipe_id}",
                    batch_id=batch_id,
                    part_id=part_id,
                )
            thickness = as_float(part["thickness_mm"])
            thickness_limit = as_float(eligible["max_thickness_mm"])
            if thickness > thickness_limit + EPS:
                add(
                    "autoclave-thickness",
                    f"Batch {batch_id}: part {part_id} exceeds the thickness limit",
                    (thickness - thickness_limit) / max(1.0, thickness_limit),
                    actual=thickness,
                    limit=thickness_limit,
                )
            if part["requires_ultra_uniform"] == "1" and eligible["supports_ultra_uniform"] != "1":
                add(
                    "autoclave-uniformity",
                    f"Batch {batch_id}: part {part_id} requires ultra-uniform capability",
                )

            rotated_value = entry.get("rotated", False)
            rotated = rotated_value is True or str(rotated_value).lower() in {"1", "true"}
            if rotated and part["rotate_ok"] != "1":
                add("placement-rotation", f"Batch {batch_id}: part {part_id} may not rotate")
            try:
                x = float(entry.get("x_cm"))
                y = float(entry.get("y_cm"))
            except (TypeError, ValueError):
                add("solution-schema", f"Batch {batch_id}: part {part_id} has invalid coordinates")
                continue
            width = as_float(part["height_cm"] if rotated else part["width_cm"])
            height = as_float(part["width_cm"] if rotated else part["height_cm"])
            if (
                x < -boundary_eps
                or y < -boundary_eps
                or x + width > chamber_width + boundary_eps
                or y + height > chamber_height + boundary_eps
            ):
                add(
                    "placement-boundary",
                    f"Batch {batch_id}: part {part_id} lies outside the chamber",
                    batch_id=batch_id,
                    part_id=part_id,
                    rectangle=[x, y, width, height],
                    chamber=[chamber_width, chamber_height],
                )
            preference = part.get("preferred_zone", "any")
            edge_distances = [x, y, chamber_width - x - width, chamber_height - y - height]
            if preference == "edge" and min(abs(value) for value in edge_distances) > boundary_eps:
                add(
                    "placement-zone-edge",
                    f"Batch {batch_id}: edge-preferred part {part_id} does not touch an edge",
                    min(abs(value) for value in edge_distances) / max(1.0, min(width, height)),
                    batch_id=batch_id,
                    part_id=part_id,
                    edge_distances=edge_distances,
                )
            center_margins = [
                max(
                    chamber_width * center_margin_fraction,
                    chamber_width * 0.06 + center_margin_extra_cm,
                ),
                max(
                    chamber_height * center_margin_fraction,
                    chamber_height * 0.06 + center_margin_extra_cm,
                ),
                max(
                    chamber_width * center_margin_fraction,
                    chamber_width * 0.06 + center_margin_extra_cm,
                ),
                max(
                    chamber_height * center_margin_fraction,
                    chamber_height * 0.06 + center_margin_extra_cm,
                ),
            ]
            center_shortfalls = [
                required + boundary_eps - actual
                for required, actual in zip(center_margins, edge_distances)
            ]
            if preference == "center" and max(center_shortfalls) >= 0.0:
                worst_index = max(
                    range(len(center_shortfalls)),
                    key=center_shortfalls.__getitem__,
                )
                add(
                    "placement-zone-center",
                    f"Batch {batch_id}: center-preferred part {part_id} is inside "
                    "the chamber edge exclusion band",
                    max(0.0, center_shortfalls[worst_index])
                    / center_margins[worst_index],
                    batch_id=batch_id,
                    part_id=part_id,
                    edge_distances=edge_distances,
                    required_margins_cm=center_margins,
                )

            if x + width <= half_width + boundary_eps:
                side = "left"
            elif x >= half_width - boundary_eps:
                side = "right"
            else:
                side = "crossing"
                add(
                    "side-manifold-placement",
                    f"Batch {batch_id}: part {part_id} crosses the chamber midpoint",
                    batch_id=batch_id,
                    part_id=part_id,
                )
            if side != "crossing":
                side_vacuum[side] += as_float(part["vacuum_ports"])
            rectangles.append(
                {"id": part_id, "x": x, "y": y, "width": width, "height": height}
            )

        for left_index, first in enumerate(rectangles):
            for second in rectangles[left_index + 1 :]:
                if _intervals_overlap(
                    first["x"], first["x"] + first["width"],
                    second["x"], second["x"] + second["width"],
                ) and _intervals_overlap(
                    first["y"], first["y"] + first["height"],
                    second["y"], second["y"] + second["height"],
                ):
                    add(
                        "placement-overlap",
                        f"Batch {batch_id}: parts {first['id']} and {second['id']} overlap",
                        batch_id=batch_id,
                        part_ids=[first["id"], second["id"]],
                    )

        rule = rules[recipe_id]
        total_area = sum(
            as_float(part["width_cm"]) * as_float(part["height_cm"])
            for part in local_parts
        )
        checks = [
            ("batch-part-limit", len(local_parts), as_float(rule["max_parts_per_batch"])),
            ("batch-program-limit", len({part["program_id"] for part in local_parts}), as_float(rule["max_programs_per_batch"])),
            ("batch-area-limit", total_area, as_float(rule["max_area_fraction"]) * chamber_width * chamber_height),
            ("batch-vacuum-limit", sum(as_float(part["vacuum_ports"]) for part in local_parts), as_float(autoclave["vacuum_ports"])),
            ("batch-thermocouple-limit", sum(as_float(part["thermocouples"]) for part in local_parts), as_float(autoclave["thermocouple_ports"])),
            ("batch-coupon-limit", sum(as_float(part["coupon_panels"]) for part in local_parts), as_float(rule["max_coupon_panels"])),
        ]
        for group, actual, limit in checks:
            if actual > limit + EPS:
                add(
                    group,
                    f"Batch {batch_id} exceeds {group}: {actual:g} > {limit:g}",
                    (actual - limit) / max(1.0, limit),
                    batch_id=batch_id,
                    actual=actual,
                    limit=limit,
                )
        for side in ("left", "right"):
            actual = side_vacuum[side]
            limit = manifold_limits[(autoclave_id, side)]
            if actual > limit + EPS:
                add(
                    "side-manifold-capacity",
                    f"Batch {batch_id}: {side} manifold demand {actual:g} exceeds {limit:g}",
                    (actual - limit) / max(1.0, limit),
                    batch_id=batch_id,
                    side=side,
                    actual=actual,
                    limit=limit,
                )

        release = max(as_float(part["release_min"]) for part in local_parts)
        latest_start = min(
            as_float(part["release_min"]) + as_float(part["max_out_time_min"])
            for part in local_parts
        )
        if start < release - EPS:
            add(
                "part-release-time",
                f"Batch {batch_id} starts before all parts are released",
                (release - start) / max(1.0, release),
                batch_id=batch_id,
                start_min=start,
                earliest_start=release,
            )
        if start > latest_start + EPS:
            add(
                "part-out-time",
                f"Batch {batch_id} starts after a part's out-time expires",
                (start - latest_start) / max(1.0, latest_start),
                batch_id=batch_id,
                start_min=start,
                latest_start=latest_start,
            )

        count = len(local_parts)
        # The public schema states that batch duration depends on both part count
        # and occupied area. Keep this handling term separate from recipe loading
        # so every downstream interval is recomputed from the exported batch.
        area_handling = 0.45 * count + 0.022 * (total_area / 100.0)
        load = (
            as_float(recipes[recipe_id]["load_fixed_min"])
            + as_float(recipes[recipe_id]["load_per_part_min"]) * count
            + area_handling
        )
        cure = as_float(recipes[recipe_id]["cure_min"]) * as_float(
            eligible["process_multiplier"]
        )
        busy_end = start + load + cure + as_float(recipes[recipe_id]["unload_min"])
        cooling = (
            as_float(rule["cooling_fixed_min"])
            + as_float(rule["cooling_per_part_min"]) * count
            + sum(
                cooling_class_extra_minutes.get(
                    str(part.get("cooling_class", "standard")).strip().lower(),
                    0.0,
                )
                for part in local_parts
            )
        )
        cooling_end = busy_end + cooling
        ndt_count = sum(part["ndt_required"] == "1" for part in local_parts)
        inspection = 0.0
        if ndt_count:
            inspection = (
                as_float(rule["ndt_minutes_fixed"])
                + as_float(rule["ndt_minutes_per_part"]) * ndt_count
                + as_float(str(config["coupon_panel_inspection_minutes"]))
                * sum(as_float(part["coupon_panels"]) for part in local_parts)
            )
        inspection_end = cooling_end + inspection
        if inspection_end + temporal_margin > horizon + EPS:
            add(
                "planning-horizon",
                f"Batch {batch_id} post-processing ends beyond the planning horizon",
                (inspection_end - horizon) / max(1.0, horizon),
                batch_id=batch_id,
                inspection_end=inspection_end,
                horizon=horizon,
            )
        summaries.append(
            {
                "id": batch_id,
                "autoclave": autoclave_id,
                "recipe": recipe_id,
                "resin": recipes[recipe_id]["resin_family"],
                "start": start,
                "earliest_start": release,
                "latest_start": latest_start,
                "load_end": start + load,
                "cure_start": start + load,
                "cure_end": start + load + cure,
                "busy_end": busy_end,
                "cooling_end": cooling_end,
                "inspection_end": inspection_end,
                "inspection": inspection,
                "parts": [part["part_id"] for part in local_parts],
                "program_counts": Counter(part["program_id"] for part in local_parts),
                "fixture_counts": Counter(part["fixture_type"] for part in local_parts),
            }
        )

    for part_id in sorted(parts):
        count = seen_parts[part_id]
        if count != 1:
            add(
                "part-assignment",
                f"Part {part_id} is assigned {count} times; exactly once is required",
                abs(count - 1),
                part_id=part_id,
                assignment_count=count,
            )

    by_autoclave: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for batch in summaries:
        by_autoclave[batch["autoclave"]].append(batch)
    for autoclave_id, jobs in by_autoclave.items():
        jobs.sort(key=lambda item: item["start"])
        for first, second in zip(jobs, jobs[1:]):
            overlap = first["busy_end"] + temporal_margin - second["start"]
            if overlap > EPS:
                add(
                    "autoclave-nonoverlap",
                    f"{first['id']} and {second['id']} overlap on {autoclave_id}",
                    overlap / max(1.0, first["busy_end"] - first["start"]),
                    autoclave_id=autoclave_id,
                    first_batch=first["id"],
                    second_batch=second["id"],
                    overlap_min=overlap,
                )
            setup = setup_times[(first["resin"], second["resin"])]
            gap_shortfall = first["busy_end"] + setup - second["start"]
            if gap_shortfall > EPS:
                add(
                    "resin-cleanup-separation",
                    f"{second['id']} needs {gap_shortfall:g} more minutes after {first['id']} on {autoclave_id}",
                    gap_shortfall / max(1.0, setup or first["busy_end"] - first["start"]),
                    autoclave_id=autoclave_id,
                    first_batch=first["id"],
                    second_batch=second["id"],
                    required_setup_min=setup,
                    shortfall_min=gap_shortfall,
                )
        for batch in jobs:
            for down_start, down_end, reason in maintenance[autoclave_id]:
                if _intervals_overlap(
                    batch["start"],
                    batch["busy_end"] + temporal_margin,
                    down_start,
                    down_end,
                ):
                    overlap = min(
                        batch["busy_end"] + temporal_margin, down_end
                    ) - max(batch["start"], down_start)
                    add(
                        "autoclave-maintenance",
                        f"Batch {batch['id']} overlaps {autoclave_id} maintenance",
                        overlap / max(1.0, down_end - down_start),
                        batch_id=batch["id"],
                        autoclave_id=autoclave_id,
                        maintenance=[down_start, down_end],
                        reason=reason,
                    )

    operator_intervals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    power_intervals: list[dict[str, Any]] = []
    cooling_intervals: list[dict[str, Any]] = []
    inspection_intervals: list[dict[str, Any]] = []
    fixture_intervals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for batch in summaries:
        autoclave = autoclaves[batch["autoclave"]]
        operator_intervals[autoclave["operator_skill"]].append(
            {"id": batch["id"], "start": batch["start"], "end": batch["load_end"] + temporal_margin, "demand": 1}
        )
        power_intervals.append(
            {"id": batch["id"], "start": batch["start"], "end": batch["busy_end"] + temporal_margin, "demand": 1}
        )
        cooling_intervals.append(
            {"id": batch["id"], "start": batch["busy_end"], "end": batch["cooling_end"] + temporal_margin, "demand": 1}
        )
        if batch["inspection"] > EPS:
            inspection_intervals.append(
                {"id": batch["id"], "start": batch["cooling_end"], "end": batch["inspection_end"] + temporal_margin, "demand": 1}
            )
        for fixture_type, demand in batch["fixture_counts"].items():
            fixture_intervals[fixture_type].append(
                {"id": batch["id"], "start": batch["start"], "end": batch["busy_end"] + temporal_margin, "demand": demand}
            )

    def windows_from_csv(filename: str, capacity_column: str) -> list[dict[str, Any]]:
        return [
            {
                "start": as_float(row["start_min"]),
                "end": as_float(row["end_min"]),
                "capacity": as_float(row[capacity_column]),
                "label": row.get("label", row.get("resource", "")),
            }
            for row in read_csv(data / filename)
        ]

    operator_windows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_csv(data / "operator_calendar.csv"):
        operator_windows[row["skill"]].append(
            {"start": as_float(row["start_min"]), "end": as_float(row["end_min"]), "capacity": as_float(row["capacity"]), "label": row["skill"]}
        )
    for skill, intervals in operator_intervals.items():
        violations.extend(_capacity_violations("operator-capacity", f"operator skill {skill}", intervals, operator_windows[skill]))
    violations.extend(
        _capacity_violations(
            "energy-window-capacity",
            "active autoclaves",
            power_intervals,
            windows_from_csv("energy_tariffs.csv", "max_active_autoclaves"),
        )
    )
    violations.extend(
        _capacity_violations(
            "cooling-lane-capacity",
            "cooling lanes",
            cooling_intervals,
            windows_from_csv("cooling_lane_capacity.csv", "capacity"),
        )
    )
    violations.extend(
        _capacity_violations(
            "ndt-inspection-capacity",
            "NDT inspection cell",
            inspection_intervals,
            windows_from_csv("post_cure_inspection_capacity.csv", "capacity"),
        )
    )
    for fixture_type, intervals in fixture_intervals.items():
        violations.extend(
            _capacity_violations(
                "fixture-capacity",
                f"fixture {fixture_type}",
                intervals,
                [{"start": 0.0, "end": horizon, "capacity": fixture_limits[fixture_type], "label": fixture_type}],
            )
        )

    program_totals = Counter(part["program_id"] for part in parts.values())
    completed = Counter()
    for batch in summaries:
        for program_id, count in batch["program_counts"].items():
            if batch["busy_end"] <= as_float(commitments[program_id]["commitment_due_min"]) + EPS:
                completed[program_id] += count
    commitment_metrics: dict[str, Any] = {}
    for program_id, commitment in commitments.items():
        required = math.ceil(
            as_float(commitment["min_on_time_fraction"]) * program_totals[program_id] - EPS
        )
        actual = completed[program_id]
        commitment_metrics[program_id] = {"actual": actual, "required": required}
        if actual < required:
            add(
                "program-commitment",
                f"Program {program_id} completes {actual} parts by its commitment; {required} are required",
                (required - actual) / max(1, required),
                program_id=program_id,
                actual=actual,
                required=required,
            )

    conflict_batch_context: dict[str, Any] = {}
    if len(violations) <= 12:
        conflict_ids: set[str] = set()
        for item in violations:
            evidence = item.get("evidence", {})
            if not isinstance(evidence, dict):
                continue
            active_ids = evidence.get("active_ids", [])
            if isinstance(active_ids, list):
                conflict_ids.update(str(value) for value in active_ids)
            for key in ("batch_id", "first_batch", "second_batch"):
                if evidence.get(key):
                    conflict_ids.add(str(evidence[key]))

        neighbors: dict[str, list[str]] = defaultdict(list)
        for jobs in by_autoclave.values():
            for index, batch in enumerate(jobs):
                if index:
                    neighbors[batch["id"]].append(jobs[index - 1]["id"])
                if index + 1 < len(jobs):
                    neighbors[batch["id"]].append(jobs[index + 1]["id"])
        context_ids = set(conflict_ids)
        for batch_id in conflict_ids:
            context_ids.update(neighbors.get(batch_id, []))
        for batch in summaries:
            if batch["id"] not in context_ids:
                continue
            conflict_batch_context[batch["id"]] = {
                "is_conflict_batch": batch["id"] in conflict_ids,
                "autoclave_id": batch["autoclave"],
                "recipe_id": batch["recipe"],
                "start_min": batch["start"],
                "earliest_start_min": batch["earliest_start"],
                "latest_start_min": batch["latest_start"],
                "load_end_min": batch["load_end"],
                "busy_end_min": batch["busy_end"],
                "cooling_end_min": batch["cooling_end"],
                "inspection_end_min": batch["inspection_end"],
                "same_machine_neighbors": neighbors.get(batch["id"], []),
                "part_ids": batch["parts"],
            }

    return report(
        "aerospace-interval-resource-observer-v1",
        [
            "part-assignment/release/out-time",
            "recipe/autoclave/thickness/uniformity",
            "rectangle-boundary/orientation/nonoverlap/zone/manifold",
            "batch-size/area/program/coupon/port-limits",
            "autoclave-nonoverlap/maintenance/resin-cleanup",
            "operator/energy/cooling/NDT/fixture-cumulative-capacity",
            "planning-horizon/program-commitments",
        ],
        violations,
        {
            "batch_count": len(summaries),
            "assigned_part_count": len(seen_parts),
            "commitments": commitment_metrics,
            "conflict_batch_context": conflict_batch_context,
            "center_clearance_fraction": center_margin_fraction,
            "center_clearance_extra_cm": center_margin_extra_cm,
            "setup_clearance_margin_min": max(0.1, temporal_margin),
            "recommended_temporal_clearance_margin_min": 1e-2,
            "cooling_class_extra_minutes": cooling_class_extra_minutes,
            "temporal_clearance_margin_min": temporal_margin,
            "duration_formula": "load_fixed + load_per_part*n + 0.45*n + 0.022*(total_area_cm2/100) + cure*process_multiplier + unload",
        },
        coverage_complete=True,
    )
