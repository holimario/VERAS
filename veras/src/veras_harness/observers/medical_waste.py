"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import EPS, as_float, read_csv, report, violation

def observe_medical_waste(app: Path, artifact: Path) -> dict[str, Any]:
    data = app / "data"
    config = json.loads((data / "config.json").read_text(encoding="utf-8"))
    clinics = {row["clinic_id"]: row for row in read_csv(data / "clinics.csv")}
    vehicles = {row["vehicle_id"]: row for row in read_csv(data / "vehicles.csv")}
    facilities = {
        row["facility_id"]: row for row in read_csv(data / "treatment_facilities.csv")
    }
    facility_rules = {
        (row["facility_id"], row["waste_type"]): row
        for row in read_csv(data / "facility_waste_rules.csv")
    }
    windows = read_csv(data / "facility_receiving_windows.csv")
    windows_by_facility: dict[str, list[dict[str, str]]] = defaultdict(list)
    for window in windows:
        windows_by_facility[window["facility_id"]].append(window)
    permits = {row["vehicle_id"]: row for row in read_csv(data / "vehicle_waste_permits.csv")}
    incompatible = {
        tuple(sorted((row["clinic_a"], row["clinic_b"])))
        for row in read_csv(data / "incompatible_pickup_pairs.csv")
    }
    chargers = {row["charger_id"]: row for row in read_csv(data / "chargers.csv")}
    arcs = {
        (row["from_node"], row["to_node"]): row
        for row in read_csv(data / "travel_energy_arcs.csv")
    }
    rows = read_csv(artifact)
    by_vehicle: dict[str, list[dict[str, str]]] = defaultdict(list)
    violations: list[dict[str, Any]] = []
    for row in rows:
        by_vehicle[row.get("vehicle_id", "")].append(row)

    clinic_visits: list[str] = []
    facility_total: dict[str, float] = defaultdict(float)
    facility_red: dict[str, float] = defaultdict(float)
    facility_yellow: dict[str, float] = defaultdict(float)
    window_count: dict[tuple[str, str], int] = defaultdict(int)
    window_kg: dict[tuple[str, str], float] = defaultdict(float)
    charger_sessions: dict[str, int] = defaultdict(int)
    charger_energy: dict[str, float] = defaultdict(float)
    route_metrics: list[dict[str, Any]] = []

    load_energy_rate = float(config["load_energy_kwh_per_kg_km"])
    curfew_start = float(config["curfew_start_min"])
    curfew_end = float(config["curfew_end_min"])

    def add(group: str, message: str, residual: float = 1.0, **evidence: Any) -> None:
        violations.append(violation(group, message, residual, **evidence))

    for vehicle_id, route_rows in by_vehicle.items():
        if vehicle_id not in vehicles:
            add("vehicle-domain", f"Unknown vehicle {vehicle_id}")
            continue
        vehicle = vehicles[vehicle_id]
        try:
            route_rows.sort(key=lambda row: int(row["sequence"]))
        except (KeyError, ValueError):
            add("route-sequence", f"Vehicle {vehicle_id} has invalid sequence values")
            continue
        sequences = [int(row["sequence"]) for row in route_rows]
        if any(sequence <= 0 for sequence in sequences):
            add("route-sequence", f"Vehicle {vehicle_id} has nonpositive sequence values")
        if len(sequences) != len(set(sequences)):
            add("route-sequence", f"Vehicle {vehicle_id} has duplicate sequence values")
        expected_sequences = list(range(1, len(route_rows) + 1))
        if sequences != expected_sequences:
            add(
                "route-sequence",
                f"Vehicle {vehicle_id} sequences must be consecutive starting at 1",
            )
        if not route_rows or route_rows[0].get("row_type") != "start" or route_rows[-1].get("row_type") != "end":
            add("route-boundary", f"Vehicle {vehicle_id} must start and end explicitly")
            continue

        current_node = vehicle["start_node"]
        current_time = as_float(vehicle["shift_start_min"])
        battery = as_float(vehicle["start_battery_kwh"])
        red_load = yellow_load = 0.0
        onboard: dict[str, float] = {}
        isolation_count = 0

        def travel(destination: str) -> bool:
            nonlocal current_node, current_time, battery
            arc = arcs.get((current_node, destination))
            if arc is None:
                add(
                    "travel-arc",
                    f"Vehicle {vehicle_id} has no arc {current_node}->{destination}",
                )
                return False
            travel_min = as_float(arc["travel_min"])
            total_load = red_load + yellow_load
            if (
                total_load > EPS
                and int(arc["loaded_curfew_flag"])
                and current_time < curfew_end
                and current_time + travel_min > curfew_start
            ):
                current_time = curfew_end
            energy = as_float(arc["empty_energy_kwh"]) + (
                load_energy_rate * total_load * as_float(arc["distance_km"])
            )
            battery -= energy
            if battery < -EPS:
                add(
                    "battery-lower-bound",
                    f"Vehicle {vehicle_id} battery becomes negative before {destination}",
                    -battery / max(1.0, as_float(vehicle["battery_kwh"])),
                    battery_kwh=battery,
                    destination=destination,
                )
            current_time += travel_min
            current_node = destination
            return True

        for index, row in enumerate(route_rows):
            row_type = row.get("row_type", "")
            node_id = row.get("node_id", "")
            if index == 0:
                if node_id != vehicle["start_node"]:
                    add("route-boundary", f"Vehicle {vehicle_id} starts at the wrong node")
                continue
            if row_type == "pickup":
                clinic_id = row.get("clinic_id", "")
                clinic = clinics.get(clinic_id)
                if clinic is None:
                    add("clinic-domain", f"Vehicle {vehicle_id} visits unknown clinic {clinic_id}")
                    continue
                if node_id != clinic["node_id"] or not travel(node_id):
                    continue
                current_time = max(current_time, as_float(clinic["earliest_min"]))
                if current_time > as_float(clinic["latest_min"]) + EPS:
                    add(
                        "clinic-time-window",
                        f"Vehicle {vehicle_id} reaches {clinic_id} after its latest service time",
                        (current_time - as_float(clinic["latest_min"]))
                        / max(1.0, as_float(clinic["latest_min"])),
                        service_start=current_time,
                        latest=as_float(clinic["latest_min"]),
                    )
                permit_key = f"allow_{clinic['priority_class']}"
                if permits.get(vehicle_id, {}).get(permit_key) != "1":
                    add("vehicle-permit", f"Vehicle {vehicle_id} cannot serve {clinic_id}")
                for other in onboard:
                    if tuple(sorted((clinic_id, other))) in incompatible:
                        add(
                            "incompatible-pickups",
                            f"Vehicle {vehicle_id} co-loads incompatible clinics {other} and {clinic_id}",
                        )
                current_time += as_float(clinic["service_min"])
                red_load += as_float(clinic["red_kg"])
                yellow_load += as_float(clinic["yellow_kg"])
                onboard[clinic_id] = current_time
                clinic_visits.append(clinic_id)
                if clinic["priority_class"] == "isolation":
                    isolation_count += 1
                for group, actual, limit in (
                    ("vehicle-total-capacity", red_load + yellow_load, as_float(vehicle["capacity_kg"])),
                    ("vehicle-red-capacity", red_load, as_float(vehicle["red_compartment_kg"])),
                    ("vehicle-yellow-capacity", yellow_load, as_float(vehicle["yellow_compartment_kg"])),
                ):
                    if actual > limit + EPS:
                        add(
                            group,
                            f"Vehicle {vehicle_id} exceeds {group}",
                            (actual - limit) / max(1.0, limit),
                            actual=actual,
                            limit=limit,
                        )
            elif row_type == "charge":
                charger_id = row.get("charger_id", "")
                charger = chargers.get(charger_id)
                if charger is None or node_id != (charger or {}).get("node_id"):
                    add("charger-domain", f"Vehicle {vehicle_id} uses invalid charger {charger_id}")
                    continue
                if not travel(node_id):
                    continue
                amount = as_float(row.get("charge_kwh", ""))
                if amount <= EPS:
                    add("charge-amount", f"Vehicle {vehicle_id} has a nonpositive charge")
                current_time += as_float(charger["queue_delay_min"]) + amount / as_float(
                    charger["rate_kwh_per_min"]
                )
                battery += amount
                if battery > as_float(vehicle["battery_kwh"]) + EPS:
                    add("battery-upper-bound", f"Vehicle {vehicle_id} overcharges its battery")
                charger_sessions[charger_id] += 1
                charger_energy[charger_id] += amount
            elif row_type == "unload":
                facility_id = row.get("facility_id", "")
                facility = facilities.get(facility_id)
                if facility is None or node_id != (facility or {}).get("node_id"):
                    add("facility-domain", f"Vehicle {vehicle_id} uses invalid facility {facility_id}")
                    continue
                if not travel(node_id):
                    continue
                unload_kg = red_load + yellow_load
                if current_time > as_float(facility["latest_receive_min"]) + EPS:
                    add("facility-latest-receive", f"Vehicle {vehicle_id} reaches {facility_id} too late")
                for waste_type, amount in (("red", red_load), ("yellow", yellow_load)):
                    if amount > EPS and facility_rules[(facility_id, waste_type)]["accepted"] != "1":
                        add(
                            "facility-waste-compatibility",
                            f"Facility {facility_id} cannot accept {waste_type} waste",
                        )
                for clinic_id, pickup_time in onboard.items():
                    clinic = clinics[clinic_id]
                    elapsed = current_time - pickup_time
                    for waste_type in ("red", "yellow"):
                        if as_float(clinic[f"{waste_type}_kg"]) <= EPS:
                            continue
                        limit = as_float(clinic[f"max_{waste_type}_onboard_min"])
                        if elapsed > limit + EPS:
                            add(
                                "maximum-onboard-time",
                                f"Clinic {clinic_id} {waste_type} waste is onboard too long",
                                (elapsed - limit) / max(1.0, limit),
                                vehicle_id=vehicle_id,
                                elapsed=elapsed,
                                limit=limit,
                            )
                containing = [
                    window
                    for window in windows_by_facility[facility_id]
                    if as_float(window["start_min"]) - EPS
                    <= current_time
                    <= as_float(window["end_min"]) + EPS
                ]
                if not containing:
                    add(
                        "facility-receiving-window",
                        f"Vehicle {vehicle_id} unload at {facility_id} is outside every receiving window",
                        unload_time=current_time,
                    )
                else:
                    window = containing[0]
                    key = (facility_id, window["window_id"])
                    window_count[key] += 1
                    window_kg[key] += unload_kg
                facility_total[facility_id] += unload_kg
                facility_red[facility_id] += red_load
                facility_yellow[facility_id] += yellow_load
                route_metrics.append(
                    {
                        "vehicle_id": vehicle_id,
                        "facility_id": facility_id,
                        "unload_time": round(current_time, 6),
                        "unload_kg": round(unload_kg, 6),
                    }
                )
                red_load = yellow_load = 0.0
                onboard.clear()
            elif row_type == "end":
                if node_id != vehicle["end_node"]:
                    add("route-boundary", f"Vehicle {vehicle_id} ends at the wrong node")
                travel(node_id)
            elif row_type != "start":
                add("row-type", f"Vehicle {vehicle_id} has unsupported row type {row_type}")

        if red_load + yellow_load > EPS:
            add("end-empty", f"Vehicle {vehicle_id} ends with waste onboard")
        if current_time > as_float(vehicle["shift_end_min"]) + EPS:
            add(
                "vehicle-shift",
                f"Vehicle {vehicle_id} finishes after its shift",
                (current_time - as_float(vehicle["shift_end_min"]))
                / max(1.0, as_float(vehicle["shift_end_min"])),
            )
        isolation_limit = int(permits[vehicle_id]["max_isolation_pickups"])
        if isolation_count > isolation_limit:
            add(
                "isolation-pickup-cap",
                f"Vehicle {vehicle_id} exceeds its isolation pickup cap",
                (isolation_count - isolation_limit) / max(1, isolation_limit),
            )

    counts = Counter(clinic_visits)
    missing = sorted(set(clinics) - set(counts))
    duplicates = sorted(clinic_id for clinic_id, count in counts.items() if count != 1)
    if missing or duplicates:
        add(
            "clinic-coverage",
            "Every clinic must be collected exactly once",
            missing=missing,
            duplicates=duplicates,
        )

    used_facilities = {key for key, amount in facility_total.items() if amount > EPS}
    if len(used_facilities) > int(config["max_open_facilities"]):
        add("max-open-facilities", "Too many treatment facilities are used")
    minimum = float(config["minimum_facility_throughput_kg"])
    for facility_id in used_facilities:
        facility = facilities[facility_id]
        for group, actual, limit in (
            ("facility-daily-capacity", facility_total[facility_id], as_float(facility["daily_capacity_kg"])),
            ("facility-red-capacity", facility_red[facility_id], as_float(facility["red_capacity_kg"])),
            ("facility-yellow-capacity", facility_yellow[facility_id], as_float(facility["yellow_capacity_kg"])),
        ):
            if actual > limit + EPS:
                add(group, f"Facility {facility_id} exceeds {group}", (actual - limit) / max(1.0, limit), actual=actual, limit=limit)
        if facility_total[facility_id] < minimum - EPS:
            add("facility-minimum-throughput", f"Facility {facility_id} is below minimum throughput")

    for window in windows:
        key = (window["facility_id"], window["window_id"])
        count_limit = int(window["max_unloads"])
        kg_limit = as_float(window["max_kg"])
        if window_count[key] > count_limit:
            add(
                "facility-window-unload-cap",
                f"Facility {key[0]} window {key[1]} unload-count cap exceeded",
                (window_count[key] - count_limit) / max(1, count_limit),
                actual=window_count[key],
                limit=count_limit,
            )
        if window_kg[key] > kg_limit + EPS:
            add(
                "facility-window-kg-cap",
                f"Facility {key[0]} window {key[1]} kg cap exceeded",
                (window_kg[key] - kg_limit) / max(1.0, kg_limit),
                actual=window_kg[key],
                limit=kg_limit,
            )
    for charger_id, charger in chargers.items():
        if charger_sessions[charger_id] > int(charger["session_limit"]):
            add("charger-session-cap", f"Charger {charger_id} session cap exceeded")
        if charger_energy[charger_id] > as_float(charger["daily_kwh_cap"]) + EPS:
            add("charger-energy-cap", f"Charger {charger_id} daily energy cap exceeded")

    return report(
        "medical-waste-route-observer-v1",
        [
            "clinic-coverage",
            "route-boundary/sequence",
            "vehicle-permit/isolation/incompatibility",
            "vehicle-load/battery/shift",
            "clinic-time-window/maximum-onboard-time",
            "facility-compatibility/daily-capacity/minimum-throughput",
            "facility-window-unload/kg-cap",
            "charger-session/energy-cap",
        ],
        violations,
        {
            "route_count": len(by_vehicle),
            "clinic_count": len(clinics),
            "route_metrics": route_metrics,
            "window_kg": {f"{key[0]}|{key[1]}": value for key, value in window_kg.items()},
        },
    )
