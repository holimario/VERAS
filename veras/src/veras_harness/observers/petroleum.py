"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import EPS, as_float, read_csv, report, violation

def _intervals_overlap(
    first_start: float, first_end: float, second_start: float, second_end: float
) -> bool:
    return max(first_start, second_start) < min(first_end, second_end) - EPS


def _capacity_violations(
    group: str,
    resource: str,
    intervals: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return exact overloaded event segments for a cumulative resource."""
    found: list[dict[str, Any]] = []
    for window in windows:
        window_start = float(window["start"])
        window_end = float(window["end"])
        capacity = float(window["capacity"])
        relevant = [
            item
            for item in intervals
            if _intervals_overlap(
                float(item["start"]),
                float(item["end"]),
                window_start,
                window_end,
            )
        ]
        points = sorted(
            {
                window_start,
                window_end,
                *(
                    max(window_start, float(item["start"]))
                    for item in relevant
                ),
                *(
                    min(window_end, float(item["end"]))
                    for item in relevant
                ),
            }
        )
        for start, end in zip(points, points[1:]):
            if end <= start + EPS:
                continue
            midpoint = (start + end) / 2.0
            active = [
                item
                for item in relevant
                if float(item["start"]) < midpoint < float(item["end"])
            ]
            demand = sum(float(item["demand"]) for item in active)
            if demand <= capacity + EPS:
                continue
            found.append(
                violation(
                    group,
                    f"{resource} capacity {capacity:g} is exceeded by demand "
                    f"{demand:g} during {start:g}-{end:g}",
                    (demand - capacity) / max(1.0, capacity),
                    resource=resource,
                    window_label=window.get("label", ""),
                    start_min=start,
                    end_min=end,
                    demand=demand,
                    capacity=capacity,
                    active_ids=[item["id"] for item in active],
                )
            )
    return found


def observe_petroleum_inventory(app: Path, artifact: Path) -> dict[str, Any]:
    """Replay the public two-stage inventory policy as a finite-state system."""
    data = app / "data"
    config = json.loads((data / "config.json").read_text(encoding="utf-8"))
    periods = [
        row["period_id"]
        for row in sorted(
            read_csv(data / "periods.csv"), key=lambda row: int(row["day_index"])
        )
    ]
    products = [row["product_id"] for row in read_csv(data / "products.csv")]
    scenarios = [row["scenario_id"] for row in read_csv(data / "scenarios.csv")]
    terminals = {row["terminal_id"] for row in read_csv(data / "terminals.csv")}
    stations = {row["station_id"] for row in read_csv(data / "stations.csv")}

    depots: set[str] = set()
    depot_state: dict[tuple[str, str], dict[str, float]] = {}
    for row in read_csv(data / "depots.csv"):
        depot_id = row["depot_id"]
        depots.add(depot_id)
        for product_id in products:
            depot_state[(depot_id, product_id)] = {
                "initial": as_float(row[f"initial_{product_id}"]),
                "capacity": as_float(row[f"cap_{product_id}"]),
                "reserve": as_float(row[f"reserve_{product_id}"]),
            }

    station_state: dict[tuple[str, str], dict[str, float]] = {}
    for row in read_csv(data / "station_tanks.csv"):
        capacity = as_float(row["tank_capacity_kl"])
        if capacity <= EPS:
            continue
        station_state[(row["station_id"], row["product_id"])] = {
            "initial": as_float(row["initial_inventory_kl"]),
            "capacity": capacity,
            "reserve": as_float(row["emergency_reserve_kl"]),
            "service_floor": as_float(row["daily_service_floor"]),
        }

    inbound_lanes = {
        (row["terminal_id"], row["depot_id"], row["product_id"]): as_float(
            row["max_daily_kl"]
        )
        for row in read_csv(data / "terminal_depot_lanes.csv")
    }
    commitments: dict[tuple[str, str, str, str], float] = defaultdict(float)
    for row in read_csv(data / "terminal_depot_commitments.csv"):
        commitments[
            (
                row["period_id"],
                row["terminal_id"],
                row["depot_id"],
                row["product_id"],
            )
        ] += as_float(row["minimum_commitment_kl"])

    nominal_terminal_capacity = {
        (row["period_id"], row["terminal_id"], row["product_id"]): as_float(
            row["max_load_kl"]
        )
        for row in read_csv(data / "terminal_product_capacity.csv")
    }
    terminal_multiplier = {
        (
            row["scenario_id"],
            row["period_id"],
            row["terminal_id"],
            row["product_id"],
        ): as_float(row["capacity_multiplier"])
        for row in read_csv(data / "scenario_terminal_capacity.csv")
    }
    robust_terminal_capacity: dict[tuple[str, str, str], float] = {}
    for key, nominal in nominal_terminal_capacity.items():
        period_id, terminal_id, product_id = key
        robust_terminal_capacity[key] = nominal * min(
            terminal_multiplier[(scenario_id, period_id, terminal_id, product_id)]
            for scenario_id in scenarios
        )

    outbound_lanes = {
        (row["depot_id"], row["station_id"], row["product_id"]): as_float(
            row["max_daily_kl"]
        )
        for row in read_csv(data / "depot_station_lanes.csv")
    }
    lane_multiplier = {
        (
            row["scenario_id"],
            row["period_id"],
            row["depot_id"],
            row["station_id"],
            row["product_id"],
        ): as_float(row["capacity_multiplier"])
        for row in read_csv(data / "scenario_lane_impacts.csv")
    }
    fleet = {
        (row["depot_id"], row["period_id"]): {
            "volume": as_float(row["max_outbound_kl"]),
            "stops": int(as_float(row["max_active_stops"])),
        }
        for row in read_csv(data / "depot_fleet.csv")
    }
    shift_capacity = {
        (row["depot_id"], row["period_id"], row["product_id"]): as_float(
            row["max_product_outbound_kl"]
        )
        for row in read_csv(data / "depot_product_shift_capacity.csv")
    }
    demand = {
        (
            row["scenario_id"],
            row["period_id"],
            row["station_id"],
            row["product_id"],
        ): as_float(row["demand_kl"])
        for row in read_csv(data / "scenario_demands.csv")
    }
    emergency_capacity = {
        (row["scenario_id"], row["product_id"]): as_float(
            row["max_emergency_kl"]
        )
        for row in read_csv(data / "scenario_emergency_caps.csv")
    }

    expected_columns = [
        "row_type",
        "scenario_id",
        "period_id",
        "origin_id",
        "destination_id",
        "product_id",
        "quantity",
    ]
    with artifact.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        actual_columns = reader.fieldnames or []

    violations: list[dict[str, Any]] = []
    violation_totals: Counter[str] = Counter()
    saved_per_group: Counter[str] = Counter()

    def add(
        group: str,
        message: str,
        residual: float = 1.0,
        scale: float = 1.0,
        **evidence: Any,
    ) -> None:
        violation_totals[group] += 1
        candidate = violation(
            group,
            message,
            max(0.0, residual) / max(1.0, abs(scale)),
            **evidence,
        )
        if saved_per_group[group] < 8:
            saved_per_group[group] += 1
            violations.append(candidate)
            return
        group_indices = [
            index for index, item in enumerate(violations) if item["group"] == group
        ]
        weakest = min(
            group_indices,
            key=lambda index: violations[index]["normalized_residual"],
        )
        if (
            candidate["normalized_residual"]
            > violations[weakest]["normalized_residual"]
        ):
            violations[weakest] = candidate

    if actual_columns != expected_columns:
        add(
            "schema",
            f"Expected columns {expected_columns}, found {actual_columns}",
            expected=expected_columns,
            actual=actual_columns,
        )

    inbound: dict[tuple[str, str, str, str], float] = defaultdict(float)
    delivery: dict[tuple[str, str, str, str, str], float] = defaultdict(float)
    emergency: dict[tuple[str, str, str, str], float] = defaultdict(float)
    period_set = set(periods)
    product_set = set(products)
    scenario_set = set(scenarios)

    for line_number, row in enumerate(rows, 2):
        row_type = row.get("row_type", "")
        scenario_id = row.get("scenario_id", "")
        period_id = row.get("period_id", "")
        origin_id = row.get("origin_id", "")
        destination_id = row.get("destination_id", "")
        product_id = row.get("product_id", "")
        try:
            quantity = float(row.get("quantity", ""))
        except (TypeError, ValueError):
            add("schema", f"Line {line_number} has a nonnumeric quantity")
            continue
        if not math.isfinite(quantity) or quantity < -EPS:
            add(
                "schema",
                f"Line {line_number} has invalid quantity {quantity}",
                abs(quantity) if math.isfinite(quantity) else 1.0,
            )
            continue
        quantity = max(0.0, quantity)
        if period_id not in period_set or product_id not in product_set:
            add("schema", f"Line {line_number} has an unknown period or product")
            continue

        if row_type == "inbound":
            key = (period_id, origin_id, destination_id, product_id)
            if scenario_id or origin_id not in terminals or destination_id not in depots:
                add("schema", f"Line {line_number} has an invalid inbound domain")
                continue
            if (origin_id, destination_id, product_id) not in inbound_lanes:
                add("inbound-lane", f"Line {line_number} uses a nonexistent inbound lane")
                continue
            inbound[key] += quantity
        elif row_type == "delivery":
            if (
                scenario_id not in scenario_set
                or origin_id not in depots
                or destination_id not in stations
            ):
                add("schema", f"Line {line_number} has an invalid delivery domain")
                continue
            lane = (origin_id, destination_id, product_id)
            if lane not in outbound_lanes or (destination_id, product_id) not in station_state:
                add("delivery-lane", f"Line {line_number} uses a nonexistent delivery lane")
                continue
            delivery[(scenario_id, period_id, *lane)] += quantity
        elif row_type == "emergency":
            if (
                scenario_id not in scenario_set
                or origin_id != "EMERGENCY"
                or (destination_id, product_id) not in station_state
            ):
                add("schema", f"Line {line_number} has an invalid emergency domain")
                continue
            emergency[(scenario_id, period_id, destination_id, product_id)] += quantity
        else:
            add("schema", f"Line {line_number} has unknown row_type {row_type!r}")

    for (period_id, terminal_id, depot_id, product_id), quantity in inbound.items():
        cap = inbound_lanes[(terminal_id, depot_id, product_id)]
        if quantity > cap + EPS:
            add(
                "inbound-lane-capacity",
                f"Inbound {period_id}/{terminal_id}/{depot_id}/{product_id} exceeds its lane cap",
                quantity - cap,
                cap,
                actual=quantity,
                capacity=cap,
            )
    for key, minimum in commitments.items():
        quantity = inbound[key]
        if quantity + EPS < minimum:
            add(
                "take-or-pay-commitment",
                f"Inbound {'/'.join(key)} misses its minimum commitment",
                minimum - quantity,
                minimum,
                actual=quantity,
                required=minimum,
            )
    for key, cap in robust_terminal_capacity.items():
        period_id, terminal_id, product_id = key
        total = sum(
            inbound[(period_id, terminal_id, depot_id, product_id)]
            for depot_id in depots
        )
        if total > cap + EPS:
            add(
                "robust-terminal-capacity",
                f"Terminal {terminal_id}/{period_id}/{product_id} is not feasible in every scenario",
                total - cap,
                cap,
                actual=total,
                robust_capacity=cap,
            )

    max_stop_volume = as_float(str(config["max_kl_per_active_stop"]))
    for scenario_id in scenarios:
        depot_inventory = {
            key: values["initial"] for key, values in depot_state.items()
        }
        station_inventory = {
            key: values["initial"] for key, values in station_state.items()
        }
        emergency_used: Counter[str] = Counter()
        for period_id in periods:
            for depot_id in depots:
                outbound_total = 0.0
                active_stops = 0
                for station_id in stations:
                    stop_total = 0.0
                    for product_id in products:
                        quantity = delivery[
                            (scenario_id, period_id, depot_id, station_id, product_id)
                        ]
                        if quantity <= EPS:
                            continue
                        stop_total += quantity
                        outbound_total += quantity
                        lane_key = (depot_id, station_id, product_id)
                        lane_cap = outbound_lanes[lane_key] * lane_multiplier[
                            (
                                scenario_id,
                                period_id,
                                depot_id,
                                station_id,
                                product_id,
                            )
                        ]
                        if quantity > lane_cap + EPS:
                            add(
                                "disrupted-delivery-lane-capacity",
                                f"Delivery {scenario_id}/{period_id}/{depot_id}/{station_id}/{product_id} exceeds disrupted capacity",
                                quantity - lane_cap,
                                lane_cap,
                                actual=quantity,
                                capacity=lane_cap,
                            )
                    if stop_total > EPS:
                        active_stops += 1
                    if stop_total > max_stop_volume + EPS:
                        add(
                            "active-stop-volume",
                            f"Stop {scenario_id}/{period_id}/{depot_id}/{station_id} exceeds its volume cap",
                            stop_total - max_stop_volume,
                            max_stop_volume,
                            actual=stop_total,
                            capacity=max_stop_volume,
                        )
                fleet_limit = fleet[(depot_id, period_id)]
                if outbound_total > fleet_limit["volume"] + EPS:
                    add(
                        "depot-fleet-volume",
                        f"Depot {depot_id}/{period_id} exceeds outbound fleet volume in {scenario_id}",
                        outbound_total - fleet_limit["volume"],
                        fleet_limit["volume"],
                        actual=outbound_total,
                        capacity=fleet_limit["volume"],
                    )
                if active_stops > fleet_limit["stops"]:
                    add(
                        "depot-fleet-stops",
                        f"Depot {depot_id}/{period_id} uses too many stops in {scenario_id}",
                        active_stops - fleet_limit["stops"],
                        fleet_limit["stops"],
                        actual=active_stops,
                        capacity=fleet_limit["stops"],
                    )

                for product_id in products:
                    product_outbound = sum(
                        delivery[
                            (scenario_id, period_id, depot_id, station_id, product_id)
                        ]
                        for station_id in stations
                    )
                    shift_cap = shift_capacity[(depot_id, period_id, product_id)]
                    if product_outbound > shift_cap + EPS:
                        add(
                            "depot-product-shift-capacity",
                            f"Depot {depot_id}/{period_id}/{product_id} exceeds its loading shift in {scenario_id}",
                            product_outbound - shift_cap,
                            shift_cap,
                            actual=product_outbound,
                            capacity=shift_cap,
                        )
                    inflow = sum(
                        inbound[(period_id, terminal_id, depot_id, product_id)]
                        for terminal_id in terminals
                    )
                    start_plus_inflow = depot_inventory[(depot_id, product_id)] + inflow
                    state = depot_state[(depot_id, product_id)]
                    if start_plus_inflow > state["capacity"] + EPS:
                        add(
                            "depot-receiving-capacity",
                            f"Depot {depot_id}/{period_id}/{product_id} overfills before dispatch in {scenario_id}",
                            start_plus_inflow - state["capacity"],
                            state["capacity"],
                            actual=start_plus_inflow,
                            capacity=state["capacity"],
                        )
                    ending = start_plus_inflow - product_outbound
                    if ending + EPS < state["reserve"]:
                        add(
                            "depot-reserve",
                            f"Depot {depot_id}/{period_id}/{product_id} falls below reserve in {scenario_id}",
                            state["reserve"] - ending,
                            state["reserve"],
                            actual=ending,
                            reserve=state["reserve"],
                        )
                    if ending > state["capacity"] + EPS:
                        add(
                            "depot-inventory-capacity",
                            f"Depot {depot_id}/{period_id}/{product_id} ends above capacity in {scenario_id}",
                            ending - state["capacity"],
                            state["capacity"],
                            actual=ending,
                            capacity=state["capacity"],
                        )
                    depot_inventory[(depot_id, product_id)] = ending

            for station_id, product_id in station_state:
                regular = sum(
                    delivery[
                        (scenario_id, period_id, depot_id, station_id, product_id)
                    ]
                    for depot_id in depots
                )
                spot = emergency[(scenario_id, period_id, station_id, product_id)]
                emergency_used[product_id] += spot
                available = station_inventory[(station_id, product_id)] + regular + spot
                state = station_state[(station_id, product_id)]
                daily_demand = demand[(scenario_id, period_id, station_id, product_id)]
                shortage = max(0.0, daily_demand - available)
                served = daily_demand - shortage
                shortage_cap = (1.0 - state["service_floor"]) * daily_demand
                if shortage > shortage_cap + EPS:
                    add(
                        "station-service-floor",
                        f"Station {station_id}/{period_id}/{product_id} misses its daily service floor in {scenario_id}",
                        shortage - shortage_cap,
                        max(daily_demand, 1.0),
                        shortage=shortage,
                        allowed_shortage=shortage_cap,
                    )
                # The public evaluator applies tank capacity after same-day demand is
                # served, so a delivery may temporarily make `available` exceed it.
                ending = available - served
                if ending > state["capacity"] + EPS:
                    add(
                        "station-inventory-capacity",
                        f"Station {station_id}/{period_id}/{product_id} ends above capacity in {scenario_id}",
                        ending - state["capacity"],
                        state["capacity"],
                        actual=ending,
                        capacity=state["capacity"],
                    )
                if ending + EPS < state["reserve"]:
                    add(
                        "station-reserve",
                        f"Station {station_id}/{period_id}/{product_id} falls below emergency reserve in {scenario_id}",
                        state["reserve"] - ending,
                        state["reserve"],
                        actual=ending,
                        reserve=state["reserve"],
                    )
                station_inventory[(station_id, product_id)] = ending

        for product_id in products:
            cap = emergency_capacity[(scenario_id, product_id)]
            if emergency_used[product_id] > cap + EPS:
                add(
                    "scenario-emergency-capacity",
                    f"Emergency {product_id} use exceeds the scenario-wide cap in {scenario_id}",
                    emergency_used[product_id] - cap,
                    cap,
                    actual=emergency_used[product_id],
                    capacity=cap,
                )

    return report(
        "petroleum-robust-inventory-observer-v1",
        [
            "schema/identifier-domains/nonnegativity",
            "inbound-lane/commitment/robust-terminal-capacity",
            "disrupted-outbound-lane/fleet/stop/shift-capacity",
            "depot-inventory/capacity/reserve",
            "station-inventory/service-floor/capacity/reserve",
            "scenario-wide-emergency-capacity",
        ],
        violations,
        {
            "row_count": len(rows),
            "scenario_count": len(scenarios),
            "actual_violation_count": sum(violation_totals.values()),
            "violation_counts_by_group": dict(sorted(violation_totals.items())),
            "stored_examples_per_group": 8,
        },
        coverage_complete=True,
    )
