"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .common import EPS, as_float, read_csv, report, violation

def observe_online_bus_schema(app: Path, artifact: Path) -> dict[str, Any]:
    """Check the online-bus row-type and nullability contracts."""
    del app
    violations: list[dict[str, Any]] = []
    rows = read_csv(artifact)
    trip_ids: set[str] = set()
    charge_ids: set[str] = set()
    for line_no, row in enumerate(rows, 2):
        row_type = row.get("row_type", "").strip().lower()
        if row_type == "trip":
            trip_id = row.get("trip_id", "").strip()
            decision = row.get("decision", "").strip().lower()
            forbidden = [
                field
                for field in (
                    "item_id",
                    "charger_id",
                    "start_min",
                    "end_min",
                    "energy_kwh",
                )
                if row.get(field, "").strip()
            ]
            if forbidden:
                violations.append(
                    violation(
                        "trip-row-nullability",
                        f"Line {line_no} trip row must leave {forbidden} blank",
                        line_no=line_no,
                        trip_id=trip_id,
                        fields=forbidden,
                    )
                )
            if not trip_id or trip_id in trip_ids:
                violations.append(
                    violation(
                        "trip-row-key",
                        f"Line {line_no} has a missing or duplicate trip_id",
                        line_no=line_no,
                        trip_id=trip_id,
                    )
                )
            trip_ids.add(trip_id)
            if decision not in {"operate", "cancel"}:
                violations.append(
                    violation(
                        "trip-decision",
                        f"Line {line_no} trip decision must be operate or cancel",
                        line_no=line_no,
                        decision=decision,
                    )
                )
            bus_id = row.get("bus_id", "").strip()
            if (decision == "operate" and not bus_id) or (
                decision == "cancel" and bus_id
            ):
                violations.append(
                    violation(
                        "trip-bus-linkage",
                        f"Line {line_no} has an inconsistent bus assignment",
                        line_no=line_no,
                        trip_id=trip_id,
                        decision=decision,
                        bus_id=bus_id,
                    )
                )
        elif row_type == "charge":
            item_id = row.get("item_id", "").strip()
            required = [
                field
                for field in (
                    "item_id",
                    "bus_id",
                    "charger_id",
                    "start_min",
                    "end_min",
                    "energy_kwh",
                )
                if not row.get(field, "").strip()
            ]
            if required or row.get("decision", "").strip().lower() != "charge":
                violations.append(
                    violation(
                        "charge-row-required-fields",
                        f"Line {line_no} charge row is missing required values",
                        line_no=line_no,
                        missing=required,
                    )
                )
            if row.get("trip_id", "").strip():
                violations.append(
                    violation(
                        "charge-row-nullability",
                        f"Line {line_no} charge row must leave trip_id blank",
                        line_no=line_no,
                    )
                )
            if not item_id or item_id in charge_ids:
                violations.append(
                    violation(
                        "charge-row-key",
                        f"Line {line_no} has a missing or duplicate charge item_id",
                        line_no=line_no,
                        item_id=item_id,
                    )
                )
            charge_ids.add(item_id)
            try:
                start = float(row.get("start_min", ""))
                end = float(row.get("end_min", ""))
                energy = float(row.get("energy_kwh", ""))
                if start < 0 or end <= start or energy <= 0:
                    raise ValueError
            except ValueError:
                violations.append(
                    violation(
                        "charge-row-domain",
                        f"Line {line_no} charge needs nonnegative start, end > start, and positive energy",
                        line_no=line_no,
                    )
                )
        elif row_type or any(value.strip() for value in row.values() if value):
            violations.append(
                violation(
                    "row-type",
                    f"Line {line_no} has unknown row_type {row_type!r}",
                    line_no=line_no,
                )
            )
    return report(
        "online-bus-schema-observer-v1",
        [
            "trip-row-nullability/key/decision",
            "charge-row-nullability/key/domain",
        ],
        violations,
        {"row_count": len(rows)},
    )

def observe_microgrid_generator_reserve(app: Path, artifact: Path) -> dict[str, Any]:
    """Recompute generator reserve headroom and ramp-response limits."""
    generators = {
        row["generator_id"]: row
        for row in read_csv(app / "data" / "generators.csv")
    }
    rows = read_csv(artifact)
    violations: list[dict[str, Any]] = []
    checked = 0
    for line_no, row in enumerate(rows, 2):
        if row.get("row_type", "").strip().lower() != "generator":
            continue
        generator_id = row.get("asset_id", "").strip()
        if generator_id not in generators:
            continue
        checked += 1
        try:
            commit = as_float(row.get("commit", ""))
            power = as_float(row.get("power_kw", ""))
            reserve = as_float(row.get("reserve_kw", ""))
        except ValueError:
            violations.append(
                violation(
                    "generator-reserve-domain",
                    f"Line {line_no} contains nonnumeric generator values",
                    line_no=line_no,
                )
            )
            continue
        generator = generators[generator_id]
        headroom = as_float(generator["max_kw"]) * commit - power
        ramp = as_float(generator["ramp_up_kw"])
        limit = min(headroom, ramp)
        if reserve > limit + EPS:
            violations.append(
                violation(
                    "generator-reserve-ramp-headroom",
                    f"{row.get('scenario_id')} hour {row.get('hour')}: {generator_id} reserve exceeds ramp/headroom",
                    (reserve - limit) / max(1.0, abs(limit)),
                    line_no=line_no,
                    scenario_id=row.get("scenario_id"),
                    hour=row.get("hour"),
                    generator_id=generator_id,
                    reserve_kw=reserve,
                    limit_kw=limit,
                    ramp_up_kw=ramp,
                    headroom_kw=headroom,
                )
            )
    return report(
        "microgrid-generator-reserve-observer-v1",
        ["generator-reserve-headroom/ramp-up"],
        violations,
        {"generator_rows_checked": checked},
    )


def observe_evtol_timeline(app: Path, artifact: Path) -> dict[str, Any]:
    """Recompute aircraft release, turnaround, overlap, and location continuity."""
    data = app / "data"
    aircraft = {row["aircraft_id"]: row for row in read_csv(data / "aircraft.csv")}
    availability = {
        (row["scenario_id"], row["aircraft_id"]): row
        for row in read_csv(data / "scenario_aircraft_availability.csv")
    }
    rows = read_csv(artifact)
    actions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    violations: list[dict[str, Any]] = []
    for line_no, row in enumerate(rows, 2):
        row_type = row.get("row_type", "").strip().lower()
        if row_type not in {"flight", "reposition", "charge"}:
            continue
        scenario_id = row.get("scenario_id", "").strip()
        aircraft_id = row.get("aircraft_id", "").strip()
        if aircraft_id not in aircraft or (scenario_id, aircraft_id) not in availability:
            continue
        try:
            start = int(round(float(row.get("start_slot", ""))))
            end = int(round(float(row.get("end_slot", ""))))
        except ValueError:
            violations.append(
                violation(
                    "aircraft-time-domain",
                    f"Line {line_no} has invalid action slots",
                    line_no=line_no,
                )
            )
            continue
        actions[(scenario_id, aircraft_id)].append(
            {"line_no": line_no, "row_type": row_type, "start": start, "end": end,
             "origin": row.get("origin", "").strip(), "destination": row.get("destination", "").strip()}
        )

    for key, aircraft_actions in actions.items():
        scenario_id, aircraft_id = key
        spec = aircraft[aircraft_id]
        ready = int(round(as_float(availability[key]["release_slot"])))
        location = spec["home_vertiport"]
        turnaround = int(round(as_float(spec["turnaround_slots"])))
        for action in sorted(aircraft_actions, key=lambda item: (item["start"], item["end"], item["line_no"])):
            if action["start"] < ready:
                violations.append(
                    violation(
                        "aircraft-readiness-turnaround",
                        f"Line {action['line_no']}: {aircraft_id} starts at {action['start']} before ready slot {ready}",
                        (ready - action["start"]) / max(1, ready),
                        scenario_id=scenario_id,
                        aircraft_id=aircraft_id,
                        line_no=action["line_no"],
                        start_slot=action["start"],
                        ready_slot=ready,
                    )
                )
            if action["origin"] != location:
                violations.append(
                    violation(
                        "aircraft-location-continuity",
                        f"Line {action['line_no']}: {aircraft_id} is at {location}, not {action['origin']}",
                        scenario_id=scenario_id,
                        aircraft_id=aircraft_id,
                        line_no=action["line_no"],
                        expected_origin=location,
                        actual_origin=action["origin"],
                    )
                )
            if action["row_type"] == "charge":
                ready = max(ready, action["end"])
            else:
                location = action["destination"]
                ready = max(ready, action["end"] + turnaround)
    return report(
        "evtol-aircraft-timeline-observer-v1",
        ["aircraft-release/turnaround/nonoverlap/location"],
        violations,
        {"aircraft_scenario_timelines": len(actions)},
    )


def observe_sterile_nonnegative_times(app: Path, artifact: Path) -> dict[str, Any]:
    """Reject negative exported event times, including cart departures."""
    del app
    rows = read_csv(artifact)
    violations: list[dict[str, Any]] = []
    time_fields = ("start_min", "wash_start", "assembly_start", "sterilizer_start")
    for line_no, row in enumerate(rows, 2):
        for field in time_fields:
            raw = row.get(field, "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                violations.append(
                    violation(
                        "time-domain",
                        f"Line {line_no}: {field} is not numeric",
                        line_no=line_no,
                        field=field,
                        value=raw,
                    )
                )
                continue
            if value < -EPS:
                violations.append(
                    violation(
                        "nonnegative-time",
                        f"Line {line_no}: {field} is negative",
                        -value,
                        line_no=line_no,
                        field=field,
                        value=value,
                    )
                )
    return report(
        "sterile-nonnegative-time-observer-v1",
        ["exported-time-nonnegativity"],
        violations,
        {"row_count": len(rows)},
    )
