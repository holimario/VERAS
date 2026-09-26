"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import EPS, as_float, optional_csv, read_csv, report, violation

def observe_flood_relief(app: Path, artifact: Path) -> dict[str, Any]:
    data = app / "data"
    config = json.loads((data / "config.json").read_text(encoding="utf-8"))
    staging = {row["node_id"]: row for row in read_csv(data / "staging_points.csv")}
    shelters = {row["shelter_id"]: row for row in read_csv(data / "shelters.csv")}
    drone_options = {
        (row["shelter_id"], row["launch_node"]): row
        for row in read_csv(data / "drone_options.csv")
    }
    road_neighbors: dict[str, set[str]] = defaultdict(set)
    for row in read_csv(data / "road_edges.csv"):
        if row.get("usable_after_flood", "1") != "1":
            continue
        source = row["from_node"]
        target = row["to_node"]
        road_neighbors[source].add(target)
        road_neighbors[target].add(source)
    water_rows = read_csv(data / "boat_travel.csv")
    water_edges = {
        frozenset((row["from_id"], row["to_id"])): as_float(row["minutes"])
        for row in water_rows
    }
    weather = optional_csv(data / "weather_windows.csv")
    max_boat_multiplier = max(
        (as_float(row["boat_multiplier"]) for row in weather), default=1.0
    )
    rows = read_csv(artifact)
    violations: list[dict[str, Any]] = []

    def add(group: str, message: str, residual: float = 1.0, **evidence: Any) -> None:
        violations.append(violation(group, message, residual, **evidence))

    truck_rows = [row for row in rows if row.get("row_type") == "truck_stop"]
    delivery_rows = [row for row in rows if row.get("row_type") == "delivery"]
    invalid_types = sorted(
        {row.get("row_type", "") for row in rows}
        - {"truck_stop", "delivery"}
    )
    if invalid_types:
        add("row-type", "Solution contains unsupported row types", values=invalid_types)

    try:
        truck_rows.sort(key=lambda row: int(row["sequence"]))
        truck_sequences = [int(row["sequence"]) for row in truck_rows]
    except (KeyError, ValueError):
        truck_sequences = []
        add("truck-sequence", "Truck stops must have integer sequence values")
    if len(truck_sequences) != len(set(truck_sequences)):
        add("truck-sequence", "Truck stop sequence values must be unique")
    route = [row.get("node_id", "") for row in truck_rows]
    depot = config["depot_id"]
    if not route or route[0] != depot or route[-1] != depot:
        add("truck-boundary", f"Truck route must start and end at {depot}")
    for node in route:
        if node not in staging:
            add("truck-node-domain", f"Truck route uses unknown staging point {node}")
    def road_reachable(source: str, target: str) -> bool:
        frontier = [source]
        seen = {source}
        while frontier:
            node = frontier.pop()
            if node == target:
                return True
            for neighbor in road_neighbors.get(node, set()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    frontier.append(neighbor)
        return False

    for source, target in zip(route, route[1:]):
        if not road_reachable(source, target):
            add(
                "truck-road-continuity",
                f"No surviving road path connects {source} to {target}",
                from_node=source,
                to_node=target,
            )
    visited = set(route)

    served: list[str] = []
    boat_trips: dict[str, list[dict[str, str]]] = defaultdict(list)
    drone_trip_ids: list[str] = []
    for row in delivery_rows:
        shelter_id = row.get("shelter_id", "")
        shelter = shelters.get(shelter_id)
        if shelter is None:
            add("shelter-domain", f"Delivery references unknown shelter {shelter_id}")
            continue
        served.append(shelter_id)
        mode = row.get("mode", "")
        if mode == "truck":
            expected = shelter.get("nearest_dry_node", "")
            node = row.get("node_id", "")
            if shelter.get("area_type") != "dry":
                add("mode-eligibility", f"Flooded shelter {shelter_id} cannot use truck mode")
            if node != expected or node not in visited:
                add(
                    "dry-shelter-staging",
                    f"Dry shelter {shelter_id} must be served at visited node {expected}",
                    actual=node,
                    expected=expected,
                )
        elif mode == "drone":
            launch = row.get("launch_node", "")
            trip_id = row.get("trip_id", "")
            if trip_id:
                drone_trip_ids.append(trip_id)
            else:
                add("trip-id", f"Drone delivery to {shelter_id} needs a trip_id")
            if shelter.get("area_type") != "flooded":
                add("mode-eligibility", f"Dry shelter {shelter_id} cannot use drone mode")
            if row.get("node_id", "") not in {"", launch}:
                add("launch-node-consistency", f"Drone row for {shelter_id} has conflicting nodes")
            if launch not in visited:
                add("launch-activation", f"Drone launch node {launch} is not visited")
            launch_data = staging.get(launch)
            if launch_data is None or launch_data.get("launch_clearance") == "poor":
                add("drone-launch-clearance", f"Drone launch node {launch} is invalid or poor")
            option = drone_options.get((shelter_id, launch))
            if option is None or option.get("payload_feasible") != "1":
                add("drone-option", f"No listed payload-feasible drone option serves {shelter_id} from {launch}")
            else:
                if as_float(shelter["demand_kg"]) > as_float(config["drone_capacity_kg"]) + EPS:
                    add("drone-capacity", f"Drone payload for {shelter_id} exceeds capacity")
                if as_float(option["flight_minutes"]) > as_float(config["drone_endurance_minutes"]) + EPS:
                    add("drone-endurance", f"Drone trip to {shelter_id} exceeds endurance")
        elif mode == "boat":
            trip_id = row.get("trip_id", "")
            if not trip_id:
                add("trip-id", f"Boat delivery to {shelter_id} needs a trip_id")
            else:
                boat_trips[trip_id].append(row)
            if shelter.get("area_type") != "flooded":
                add("mode-eligibility", f"Dry shelter {shelter_id} cannot use boat mode")
        else:
            add("delivery-mode", f"Shelter {shelter_id} uses unsupported mode {mode}")

    counts = Counter(served)
    missing = sorted(set(shelters) - set(counts))
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    if missing or duplicates:
        add(
            "shelter-coverage",
            "Every shelter must be served exactly once",
            missing=missing,
            duplicates=duplicates,
        )
    duplicate_drone_ids = sorted(
        trip_id for trip_id, count in Counter(drone_trip_ids).items() if count != 1
    )
    if duplicate_drone_ids:
        add("drone-trip-cardinality", "Each drone trip must serve one shelter", trip_ids=duplicate_drone_ids)

    boat_metrics: list[dict[str, Any]] = []
    for trip_id, trip_rows in boat_trips.items():
        try:
            trip_rows.sort(key=lambda row: int(row["sequence"]))
            sequences = [int(row["sequence"]) for row in trip_rows]
        except (KeyError, ValueError):
            add("boat-sequence", f"Boat trip {trip_id} has invalid sequence values")
            continue
        if len(sequences) != len(set(sequences)):
            add("boat-sequence", f"Boat trip {trip_id} has duplicate sequence values")
        launches = {row.get("launch_node", "") for row in trip_rows}
        if len(launches) != 1:
            add("boat-launch-consistency", f"Boat trip {trip_id} uses multiple launch nodes")
            continue
        launch = next(iter(launches))
        launch_data = staging.get(launch)
        if launch not in visited:
            add("launch-activation", f"Boat launch node {launch} is not visited")
        if launch_data is None or launch_data.get("boat_ramp") != "1":
            add("boat-ramp", f"Boat trip {trip_id} does not launch from a valid ramp")
        for row in trip_rows:
            if row.get("node_id", "") not in {"", launch}:
                add("launch-node-consistency", f"Boat row in {trip_id} has conflicting nodes")
        shelter_ids = [row.get("shelter_id", "") for row in trip_rows]
        chain = [launch, *shelter_ids, launch]
        water_minutes = 0.0
        for source, target in zip(chain, chain[1:]):
            edge = frozenset((source, target))
            if edge not in water_edges:
                add(
                    "boat-water-continuity",
                    f"Boat trip {trip_id} has no listed water edge {source}-{target}",
                    trip_id=trip_id,
                    from_id=source,
                    to_id=target,
                )
            else:
                water_minutes += water_edges[edge]
        demand = sum(
            as_float(shelters[shelter_id]["demand_kg"])
            for shelter_id in shelter_ids
            if shelter_id in shelters
        )
        capacity = as_float(config["boat_capacity_kg"])
        if demand > capacity + EPS:
            add(
                "boat-capacity",
                f"Boat trip {trip_id} carries {demand:g} kg; capacity is {capacity:g}",
                (demand - capacity) / max(1.0, capacity),
                actual=demand,
                limit=capacity,
            )
        robust_duration = (
            as_float(config["boat_setup_minutes"])
            + as_float(config["boat_unload_minutes_per_shelter"]) * len(trip_rows)
            + max_boat_multiplier * water_minutes
        )
        duration_limit = as_float(config["boat_trip_limit_minutes"])
        if robust_duration > duration_limit + EPS:
            add(
                "boat-trip-duration",
                f"Boat trip {trip_id} robust duration exceeds its limit",
                (robust_duration - duration_limit) / max(1.0, duration_limit),
                actual=robust_duration,
                limit=duration_limit,
            )
        boat_metrics.append(
            {
                "trip_id": trip_id,
                "launch_node": launch,
                "shelters": shelter_ids,
                "demand_kg": demand,
                "robust_duration_minutes": robust_duration,
            }
        )

    return report(
        "flood-relief-network-observer-v1",
        [
            "truck-boundary/road-continuity",
            "shelter-coverage/mode-eligibility",
            "dry-shelter-staging",
            "drone-option/clearance/capacity/endurance",
            "launch-activation",
            "boat-ramp/sequence/water-continuity",
            "boat-capacity/robust-duration",
        ],
        violations,
        {
            "truck_stop_count": len(truck_rows),
            "delivery_count": len(delivery_rows),
            "boat_trip_count": len(boat_trips),
            "boat_metrics": boat_metrics,
        },
    )
