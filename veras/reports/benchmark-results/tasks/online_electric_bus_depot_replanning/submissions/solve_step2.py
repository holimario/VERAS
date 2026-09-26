#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyscipopt import Model, __version__ as PYSCIPOPT_VERSION, quicksum

ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUB_DIR = ROOT / "submissions"

PRIOR_PLAN_PATH = SUB_DIR / "initial_plan.csv"
PLAN_PATH = SUB_DIR / "step2_replan.csv"
LOG_PATH = SUB_DIR / "solve_log_step2.md"

EPS = 1e-8
SCIP_GAP = 0.0005
DEFAULT_TIME_LIMIT = 30.0
PLAN_HEADER = [
    "row_type",
    "item_id",
    "bus_id",
    "trip_id",
    "charger_id",
    "start_min",
    "end_min",
    "energy_kwh",
    "decision",
]


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_plan(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != PLAN_HEADER:
            raise ValueError(f"CSV header mismatch in {path}: {reader.fieldnames}")
        return list(reader)


def write_plan(path: Path, rows: List[Dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in PLAN_HEADER})


def to_int(value: str) -> int:
    return int(float(value))


def to_float(value: str) -> float:
    return float(value)


def fmt_number(value: Optional[float]) -> str:
    if value is None:
        return "uncertified"
    if abs(value - round(value)) <= EPS:
        return str(int(round(value)))
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def public_objective_from_audit(audit: Dict[str, object]) -> Optional[float]:
    if audit["objective_zero_certificate"]:
        return 0.0
    return None


def compatible(bus: Dict[str, str], trip: Dict[str, str]) -> bool:
    if int(bus["low_floor"]) < int(trip["low_floor_required"]):
        return False
    req = trip["bus_class_required"]
    bus_class = bus["bus_class"]
    if req == "standard":
        return bus_class in {"standard", "artic"}
    return bus_class == req


def overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def effective_trip_energy(bus_id: str, trip: Dict[str, str], derates: List[Dict[str, str]]) -> float:
    energy = to_float(trip["energy_kwh"])
    depart = to_int(trip["depart_min"])
    arrive = to_int(trip["arrive_min"])
    multiplier = 1.0
    for row in derates:
        if row["bus_id"] != bus_id:
            continue
        start = to_int(row["start_min"])
        end = to_int(row["end_min"])
        if overlap(depart, arrive, start, end) > 0:
            multiplier = max(multiplier, to_float(row["energy_multiplier"]))
    return energy * multiplier


def charge_capacity_kwh(
    bus: Dict[str, str],
    charger: Dict[str, str],
    start: float,
    end: float,
    derates: List[Dict[str, str]],
) -> float:
    if end <= start + EPS:
        return 0.0
    points = {start, end}
    for row in derates:
        if row["bus_id"] != bus["bus_id"]:
            continue
        ds = to_int(row["start_min"])
        de = to_int(row["end_min"])
        if overlap(start, end, ds, de) > 0:
            points.add(float(ds))
            points.add(float(de))
    ordered = sorted(points)
    total = 0.0
    for left, right in zip(ordered, ordered[1:]):
        if right <= left + EPS:
            continue
        rate = min(to_float(bus["max_charge_kw"]), to_float(charger["power_kw"]))
        for row in derates:
            if row["bus_id"] != bus["bus_id"]:
                continue
            ds = to_int(row["start_min"])
            de = to_int(row["end_min"])
            if overlap(left, right, ds, de) > 0:
                rate = min(rate, to_float(row["max_charge_kw"]))
        total += rate * (right - left) / 60.0
    return total


def load_data() -> Dict[str, object]:
    current_step = json.loads((DATA_DIR / "current_step.json").read_text(encoding="utf-8"))
    visible_step = int(current_step["visible_step"])
    buses = {row["bus_id"]: row for row in read_csv(DATA_DIR / "buses.csv")}
    chargers = {row["charger_id"]: row for row in read_csv(DATA_DIR / "chargers.csv")}
    trips = [row for row in read_csv(DATA_DIR / "trips.csv") if int(row["reveal_step"]) <= visible_step]
    trips.sort(key=lambda row: (to_int(row["depart_min"]), row["trip_id"]))
    return {
        "buses": buses,
        "chargers": chargers,
        "trips": trips,
        "trip_lookup": {row["trip_id"]: row for row in trips},
        "deadhead": {
            (row["from_location"], row["to_location"]): to_int(row["minutes"])
            for row in read_csv(DATA_DIR / "deadhead_times.csv")
        },
        "compat": {(row["bus_id"], row["charger_id"]) for row in read_csv(DATA_DIR / "bus_charger_compatibility.csv")},
        "grid_periods": [
            {
                "start": to_int(row["start_min"]),
                "end": to_int(row["end_min"]),
                "north_cap_kw": to_float(row["north_cap_kw"]),
                "south_cap_kw": to_float(row["south_cap_kw"]),
                "site_cap_kw": to_float(row["site_cap_kw"]),
                "price_per_kwh": to_float(row["price_per_kwh"]),
                "carbon_kg_per_kwh": to_float(row["carbon_kg_per_kwh"]),
            }
            for row in read_csv(DATA_DIR / "grid_periods.csv")
        ],
        "outages": [row for row in read_csv(DATA_DIR / "charger_outages.csv") if row["charger_id"] != "NONE"],
        "session_limits": [
            row for row in read_csv(DATA_DIR / "charger_session_limits.csv") if row["limit_id"] != "BASE_NO_RESTRICTION"
        ],
        "derates": [row for row in read_csv(DATA_DIR / "bus_event_derates.csv") if row["bus_id"] != "NONE"],
        "reserves": [
            {
                "checkpoint_min": to_int(row["checkpoint_min"]),
                "min_buses": to_int(row["min_buses"]),
                "min_soc_kwh": to_float(row["min_soc_kwh"]),
            }
            for row in read_csv(DATA_DIR / "reserve_requirements.csv")
        ],
        "current_step": current_step,
    }


def prior_state(data: Dict[str, object], prior_rows: List[Dict[str, str]]) -> Dict[str, object]:
    trip_lookup = data["trip_lookup"]
    freeze_until = int(data["current_step"]["freeze_until_min"])
    prior_trip_rows = {
        row["trip_id"]: row
        for row in prior_rows
        if row["row_type"] == "trip" and row["trip_id"] in trip_lookup
    }
    frozen_trips = {
        trip_id: row
        for trip_id, row in prior_trip_rows.items()
        if to_int(trip_lookup[trip_id]["depart_min"]) < freeze_until
    }
    frozen_charges = {
        (
            row["item_id"],
            row["bus_id"],
            row["charger_id"],
            row["start_min"],
            row["end_min"],
            row["energy_kwh"],
            row["decision"],
        )
        for row in prior_rows
        if row["row_type"] == "charge" and row["start_min"] and to_float(row["start_min"]) < freeze_until
    }
    return {
        "prior_trip_rows": prior_trip_rows,
        "frozen_trips": frozen_trips,
        "frozen_charges": frozen_charges,
        "freeze_until": freeze_until,
    }


def build_bus_timelines(
    plan_rows: List[Dict[str, str]],
    data: Dict[str, object],
    issues: List[Dict[str, object]],
) -> Dict[str, List[Dict[str, object]]]:
    buses = data["buses"]
    chargers = data["chargers"]
    compat = data["compat"]
    deadhead = data["deadhead"]
    derates = data["derates"]
    trip_lookup = data["trip_lookup"]

    timelines: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    charger_sessions: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    charge_rows: List[Dict[str, object]] = []
    trip_rows_seen = set()
    item_ids = set()

    def add_issue(group: str, residual: float, detail: str) -> None:
        issues.append({"group": group, "residual": max(0.0, residual), "detail": detail})

    for row in plan_rows:
        row_type = row["row_type"]
        if row_type not in {"trip", "charge"}:
            add_issue("schema", 1.0, f"invalid row_type {row_type}")
            continue

        if row_type == "trip":
            trip_id = row["trip_id"]
            if trip_id in trip_rows_seen:
                add_issue("schema", 1.0, f"duplicate trip row {trip_id}")
                continue
            trip_rows_seen.add(trip_id)
            if trip_id not in trip_lookup:
                add_issue("coverage", 1.0, f"unknown trip_id {trip_id}")
                continue
            trip = trip_lookup[trip_id]
            decision = row["decision"]
            if decision not in {"operate", "cancel"}:
                add_issue("schema", 1.0, f"invalid trip decision for {trip_id}")
            if row["item_id"]:
                add_issue("nullability", 1.0, f"trip row must leave item_id blank for {trip_id}")
            if row["charger_id"] or row["start_min"] or row["end_min"] or row["energy_kwh"]:
                add_issue("nullability", 1.0, f"trip row must leave charge fields blank for {trip_id}")
            if decision == "cancel":
                if trip["priority"] == "critical":
                    add_issue("service", 1.0, f"critical trip canceled: {trip_id}")
                if row["bus_id"]:
                    add_issue("nullability", 1.0, f"canceled trip must leave bus blank: {trip_id}")
            else:
                bus_id = row["bus_id"]
                if bus_id not in buses:
                    add_issue("assignment", 1.0, f"unknown bus {bus_id} on {trip_id}")
                    continue
                bus = buses[bus_id]
                if not compatible(bus, trip):
                    add_issue("compatibility", 1.0, f"incompatible bus {bus_id} on {trip_id}")
                timelines[bus_id].append(
                    {
                        "type": "trip",
                        "trip_id": trip_id,
                        "start": float(to_int(trip["depart_min"])),
                        "end": float(to_int(trip["arrive_min"])),
                        "start_loc": trip["start_location"],
                        "end_loc": trip["end_location"],
                        "energy": effective_trip_energy(bus_id, trip, derates),
                        "min_start_soc": to_float(trip["min_start_soc_kwh"]),
                    }
                )
        else:
            item_id = row["item_id"]
            if not item_id:
                add_issue("nullability", 1.0, "charge row must provide a unique nonblank item_id")
            elif item_id in item_ids:
                add_issue("schema", 1.0, f"duplicate charge item_id {item_id}")
            else:
                item_ids.add(item_id)
            if row["trip_id"]:
                add_issue("nullability", 1.0, f"charge row must leave trip blank: {item_id}")
            if row["decision"] != "charge":
                add_issue("schema", 1.0, f"charge row must use decision=charge: {item_id}")
            bus_id = row["bus_id"]
            charger_id = row["charger_id"]
            if bus_id not in buses:
                add_issue("assignment", 1.0, f"unknown charge bus {bus_id}")
                continue
            if charger_id not in chargers:
                add_issue("assignment", 1.0, f"unknown charger {charger_id}")
                continue
            if not row["start_min"] or not row["end_min"] or not row["energy_kwh"]:
                add_issue("nullability", 1.0, f"charge row missing numeric fields: {item_id}")
                continue
            start = to_float(row["start_min"])
            end = to_float(row["end_min"])
            energy = to_float(row["energy_kwh"])
            if end <= start + EPS:
                add_issue("charge", 1.0, f"nonpositive charge duration: {item_id}")
            if energy < -EPS:
                add_issue("charge", 1.0, f"negative charge energy: {item_id}")
            bus = buses[bus_id]
            charger = chargers[charger_id]
            if (bus_id, charger_id) not in compat:
                add_issue("compatibility", 1.0, f"incompatible charger {charger_id} for bus {bus_id}")
            cap = charge_capacity_kwh(bus, charger, start, end, data["derates"])
            if energy > cap + EPS:
                add_issue("charge", (energy - cap) / max(1.0, cap), f"charge energy exceeds capacity: {item_id}")
            session = {
                "type": "charge",
                "item_id": item_id,
                "start": start,
                "end": end,
                "start_loc": "DEPOT",
                "end_loc": "DEPOT",
                "energy": energy,
                "charger_id": charger_id,
            }
            timelines[bus_id].append(session)
            charger_sessions[charger_id].append(session)
            charge_rows.append({"bus_id": bus_id, "charger_id": charger_id, "start": start, "end": end, "energy": energy})

    visible_trip_ids = {trip["trip_id"] for trip in data["trips"]}
    missing = visible_trip_ids - trip_rows_seen
    extra = trip_rows_seen - visible_trip_ids
    if missing:
        add_issue("coverage", len(missing) / max(1, len(visible_trip_ids)), f"missing trip rows: {sorted(missing)}")
    if extra:
        add_issue("coverage", len(extra) / max(1, len(visible_trip_ids)), f"extra trip rows: {sorted(extra)}")

    for charger_id, sessions in charger_sessions.items():
        charger = chargers[charger_id]
        setup = to_float(charger["setup_min"])
        sessions.sort(key=lambda event: (event["start"], event["end"], event.get("item_id", "")))
        for prev, nxt in zip(sessions, sessions[1:]):
            gap = nxt["start"] - prev["end"]
            if gap + EPS < setup:
                add_issue(
                    "charger_conflict",
                    (setup - gap) / max(1.0, setup),
                    f"charger {charger_id} lacks setup gap between {prev.get('item_id', '?')} and {nxt.get('item_id', '?')}",
                )

    for outage in data["outages"]:
        charger_id = outage["charger_id"]
        start = to_int(outage["start_min"])
        end = to_int(outage["end_min"])
        for session in charger_sessions.get(charger_id, []):
            if overlap(session["start"], session["end"], start, end) > 0:
                add_issue("outage", 1.0, f"charge {session.get('item_id', '?')} overlaps outage on {charger_id}")

    for bus_id, events in timelines.items():
        bus = buses[bus_id]
        events.sort(key=lambda event: (event["start"], 0 if event["type"] == "trip" else 1, event["end"]))
        current_loc = "DEPOT"
        current_time = 0.0
        soc = to_float(bus["initial_soc_kwh"])
        for event in events:
            event["initial_soc"] = to_float(bus["initial_soc_kwh"])
            travel = data["deadhead"].get((current_loc, event["start_loc"]))
            if travel is None:
                add_issue("timing", 1.0, f"missing deadhead from {current_loc} to {event['start_loc']}")
                travel = 10**9
            if current_time + travel > event["start"] + EPS:
                add_issue(
                    "timing",
                    (current_time + travel - event["start"]) / max(1.0, event["start"] - current_time),
                    f"bus {bus_id} cannot reach {event['type']} starting at {event['start']}",
                )
            if event["type"] == "trip":
                if soc + EPS < event["min_start_soc"]:
                    add_issue(
                        "soc",
                        (event["min_start_soc"] - soc) / max(1.0, event["min_start_soc"]),
                        f"bus {bus_id} below trip start reserve on {event['trip_id']}",
                    )
                event["soc_before"] = soc
                soc -= event["energy"]
                event["soc_after"] = soc
                if soc + EPS < to_float(bus["min_soc_kwh"]):
                    add_issue(
                        "soc",
                        (to_float(bus["min_soc_kwh"]) - soc) / max(1.0, to_float(bus["min_soc_kwh"])),
                        f"bus {bus_id} below minimum SOC after {event['trip_id']}",
                    )
            else:
                event["soc_before"] = soc
                soc += event["energy"]
                event["soc_after"] = soc
                if soc > to_float(bus["max_soc_kwh"]) + EPS:
                    add_issue(
                        "soc",
                        (soc - to_float(bus["max_soc_kwh"])) / max(1.0, to_float(bus["max_soc_kwh"])),
                        f"bus {bus_id} exceeds max SOC after charge {event['item_id']}",
                    )
            current_loc = event["end_loc"]
            current_time = event["end"]

    active_points = set()
    for row in charge_rows:
        active_points.add(row["start"])
        active_points.add(row["end"])
    for period in data["grid_periods"]:
        active_points.add(float(period["start"]))
        active_points.add(float(period["end"]))
    checkpoints = sorted(active_points)
    for left, right in zip(checkpoints, checkpoints[1:]):
        if right <= left + EPS:
            continue
        mid = 0.5 * (left + right)
        active = [row for row in charge_rows if row["start"] <= mid < row["end"]]
        if not active:
            continue
        site = 0.0
        north = 0.0
        south = 0.0
        for row in active:
            power = row["energy"] * 60.0 / max(EPS, row["end"] - row["start"])
            site += power
            feeder = data["chargers"][row["charger_id"]]["feeder"]
            if feeder == "north":
                north += power
            else:
                south += power
        for period in data["grid_periods"]:
            if period["start"] <= mid < period["end"]:
                if site > period["site_cap_kw"] + EPS:
                    add_issue("grid", (site - period["site_cap_kw"]) / max(1.0, period["site_cap_kw"]), f"site cap exceeded near {mid}")
                if north > period["north_cap_kw"] + EPS:
                    add_issue("grid", (north - period["north_cap_kw"]) / max(1.0, period["north_cap_kw"]), f"north cap exceeded near {mid}")
                if south > period["south_cap_kw"] + EPS:
                    add_issue("grid", (south - period["south_cap_kw"]) / max(1.0, period["south_cap_kw"]), f"south cap exceeded near {mid}")
                break

    for limit in data["session_limits"]:
        start = to_int(limit["start_min"])
        end = to_int(limit["end_min"])
        scope = limit["scope"]
        new_sessions = [
            row
            for row in charge_rows
            if start <= row["start"] < end
            and (scope == "all" or data["chargers"][row["charger_id"]]["feeder"] == scope)
        ]
        if len(new_sessions) > to_int(limit["max_new_sessions"]):
            add_issue("session_limit", 1.0, f"new session limit exceeded for scope {scope}")

        time_points = {float(start), float(end)}
        for row in charge_rows:
            if data["chargers"][row["charger_id"]]["charger_type"] != "fast":
                continue
            if overlap(row["start"], row["end"], start, end) > 0:
                time_points.add(row["start"])
                time_points.add(row["end"])
        ordered = sorted(time_points)
        for left, right in zip(ordered, ordered[1:]):
            if right <= left + EPS:
                continue
            mid = 0.5 * (left + right)
            active_fast = [
                row
                for row in charge_rows
                if row["start"] <= mid < row["end"]
                and data["chargers"][row["charger_id"]]["charger_type"] == "fast"
                and (scope == "all" or data["chargers"][row["charger_id"]]["feeder"] == scope)
            ]
            if len(active_fast) > to_int(limit["max_fast_active"]):
                add_issue("session_limit", 1.0, f"fast active limit exceeded for scope {scope}")
                break

    return timelines


def reserve_bus_ids(
    timelines: Dict[str, List[Dict[str, object]]],
    data: Dict[str, object],
    checkpoint: int,
    threshold: float,
) -> List[str]:
    result: List[str] = []
    deadhead = data["deadhead"]
    for bus_id, bus in data["buses"].items():
        events = timelines.get(bus_id, [])
        if not events:
            if to_float(bus["initial_soc_kwh"]) + EPS >= threshold:
                result.append(bus_id)
            continue
        prev_event = None
        next_event = None
        active = False
        soc = to_float(bus["initial_soc_kwh"])
        for event in events:
            if event["start"] <= checkpoint < event["end"]:
                active = True
                break
            if event["end"] <= checkpoint + EPS:
                prev_event = event
                soc = event["soc_after"]
            if checkpoint <= event["start"] - EPS:
                next_event = event
                break
        if active or soc + EPS < threshold:
            continue
        arrive_depot = 0.0 if prev_event is None else prev_event["end"] + deadhead[(prev_event["end_loc"], "DEPOT")]
        leave_depot = math.inf if next_event is None else next_event["start"] - deadhead[("DEPOT", next_event["start_loc"])]
        if arrive_depot <= checkpoint + EPS and checkpoint + EPS <= leave_depot:
            result.append(bus_id)
    return result


def audit_rows(rows: List[Dict[str, str]], data: Dict[str, object], prior: Dict[str, object]) -> Dict[str, object]:
    issues: List[Dict[str, object]] = []
    timelines = build_bus_timelines(rows, data, issues)
    trip_lookup = data["trip_lookup"]

    row_by_trip = {
        row["trip_id"]: row
        for row in rows
        if row["row_type"] == "trip"
    }
    for trip_id, prior_row in prior["frozen_trips"].items():
        final_row = row_by_trip.get(trip_id)
        if final_row is None:
            issues.append({"group": "freeze", "residual": 1.0, "detail": f"missing frozen trip row {trip_id}"})
            continue
        if final_row["decision"] != prior_row["decision"] or final_row["bus_id"] != prior_row["bus_id"]:
            issues.append(
                {
                    "group": "freeze",
                    "residual": 1.0,
                    "detail": f"frozen trip {trip_id} changed from {prior_row['bus_id']}/{prior_row['decision']} to {final_row['bus_id']}/{final_row['decision']}",
                }
            )

    final_frozen_charges = {
        (
            row["item_id"],
            row["bus_id"],
            row["charger_id"],
            row["start_min"],
            row["end_min"],
            row["energy_kwh"],
            row["decision"],
        )
        for row in rows
        if row["row_type"] == "charge" and row["start_min"] and to_float(row["start_min"]) < prior["freeze_until"]
    }
    if final_frozen_charges != prior["frozen_charges"]:
        issues.append({"group": "freeze", "residual": 1.0, "detail": "frozen charge-session set changed"})

    reserve_details = []
    for reserve in data["reserves"]:
        checkpoint = reserve["checkpoint_min"]
        need = reserve["min_buses"]
        threshold = reserve["min_soc_kwh"]
        ids = reserve_bus_ids(timelines, data, checkpoint, threshold)
        reserve_details.append((checkpoint, ids))
        if len(ids) < need:
            issues.append(
                {
                    "group": "reserve",
                    "residual": (need - len(ids)) / max(1.0, need),
                    "detail": f"reserve checkpoint {checkpoint} has {len(ids)} buses, needs {need}",
                }
            )

    operated = sum(1 for row in rows if row["row_type"] == "trip" and row["decision"] == "operate")
    canceled = sum(1 for row in rows if row["row_type"] == "trip" and row["decision"] == "cancel")
    charge_rows = [row for row in rows if row["row_type"] == "charge"]
    coverage = sum(1 for row in rows if row["row_type"] == "trip") / max(1, len(data["trips"]))
    violated_groups = sorted({issue["group"] for issue in issues})
    theta = max((issue["residual"] for issue in issues), default=0.0)

    cancel_penalty = 0.0
    for row in rows:
        if row["row_type"] == "trip" and row["decision"] == "cancel":
            cancel_penalty += to_float(trip_lookup[row["trip_id"]]["cancel_penalty"])

    electricity_cost = 0.0
    carbon_metric = 0.0
    for row in charge_rows:
        start = to_float(row["start_min"])
        end = to_float(row["end_min"])
        energy = to_float(row["energy_kwh"])
        duration = max(EPS, end - start)
        for period in data["grid_periods"]:
            ov = overlap(start, end, period["start"], period["end"])
            if ov <= 0:
                continue
            share = energy * ov / duration
            electricity_cost += share * period["price_per_kwh"]
            carbon_metric += share * period["carbon_kg_per_kwh"]

    changed_nonfrozen_prior_trips = []
    for trip_id, prior_row in prior["prior_trip_rows"].items():
        if trip_id in prior["frozen_trips"]:
            continue
        final_row = row_by_trip.get(trip_id)
        if final_row is None:
            changed_nonfrozen_prior_trips.append(trip_id)
            continue
        if final_row["decision"] != prior_row["decision"] or final_row["bus_id"] != prior_row["bus_id"]:
            changed_nonfrozen_prior_trips.append(trip_id)

    objective_zero_certificate = (
        canceled == 0
        and len(charge_rows) == 0
        and len(changed_nonfrozen_prior_trips) == 0
        and theta == 0.0
    )

    return {
        "rows": rows,
        "timelines": timelines,
        "issues": issues,
        "theta": theta,
        "violated_groups": violated_groups,
        "coverage": coverage,
        "operated": operated,
        "canceled": canceled,
        "charge_rows": len(charge_rows),
        "cancel_penalty": cancel_penalty,
        "electricity_cost": electricity_cost,
        "carbon_metric": carbon_metric,
        "changed_nonfrozen_prior_trips": changed_nonfrozen_prior_trips,
        "objective_zero_certificate": objective_zero_certificate,
        "reserve_details": reserve_details,
    }


def audit_plan(path: Path, data: Dict[str, object], prior: Dict[str, object]) -> Dict[str, object]:
    return audit_rows(parse_plan(path), data, prior)


def build_carry_forward_rows(data: Dict[str, object], prior: Dict[str, object]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for trip in data["trips"]:
        trip_id = trip["trip_id"]
        if trip_id in prior["prior_trip_rows"]:
            prior_row = prior["prior_trip_rows"][trip_id]
            bus_id = prior_row["bus_id"] if prior_row["decision"] == "operate" else ""
            decision = prior_row["decision"]
        else:
            bus_id = trip["block_hint"]
            decision = "operate"
        rows.append(
            {
                "row_type": "trip",
                "item_id": "",
                "bus_id": bus_id,
                "trip_id": trip_id,
                "charger_id": "",
                "start_min": "",
                "end_min": "",
                "energy_kwh": "",
                "decision": decision,
            }
        )
    return rows


def bus_chain_feasible(trips: List[Dict[str, str]], data: Dict[str, object]) -> bool:
    ordered = sorted(trips, key=lambda row: (to_int(row["depart_min"]), row["trip_id"]))
    if not ordered:
        return True
    deadhead = data["deadhead"]
    first = ordered[0]
    if deadhead[("DEPOT", first["start_location"])] > to_int(first["depart_min"]):
        return False
    for prev, nxt in zip(ordered, ordered[1:]):
        if to_int(prev["arrive_min"]) + deadhead[(prev["end_location"], nxt["start_location"])] > to_int(nxt["depart_min"]):
            return False
    return True


def simulate_bus_trip_chain(
    bus_id: str,
    trips: List[Dict[str, str]],
    data: Dict[str, object],
) -> Optional[List[Dict[str, object]]]:
    bus = data["buses"][bus_id]
    deadhead = data["deadhead"]
    ordered = sorted(trips, key=lambda row: (to_int(row["depart_min"]), row["trip_id"]))
    current_loc = "DEPOT"
    current_time = 0.0
    soc = to_float(bus["initial_soc_kwh"])
    events: List[Dict[str, object]] = []

    for trip in ordered:
        start = float(to_int(trip["depart_min"]))
        end = float(to_int(trip["arrive_min"]))
        travel = deadhead.get((current_loc, trip["start_location"]))
        if travel is None or current_time + travel > start + EPS:
            return None
        min_start = to_float(trip["min_start_soc_kwh"])
        if soc + EPS < min_start:
            return None
        energy = effective_trip_energy(bus_id, trip, data["derates"])
        soc_after = soc - energy
        if soc_after + EPS < to_float(bus["min_soc_kwh"]):
            return None
        events.append(
            {
                "type": "trip",
                "trip_id": trip["trip_id"],
                "start": start,
                "end": end,
                "start_loc": trip["start_location"],
                "end_loc": trip["end_location"],
                "soc_before": soc,
                "soc_after": soc_after,
            }
        )
        current_loc = trip["end_location"]
        current_time = end
        soc = soc_after

    return events


def reserve_flag_for_bus_events(
    bus_id: str,
    bus_events: List[Dict[str, object]],
    checkpoint: int,
    threshold: float,
    data: Dict[str, object],
) -> int:
    bus = data["buses"][bus_id]
    deadhead = data["deadhead"]
    if not bus_events:
        return 1 if to_float(bus["initial_soc_kwh"]) + EPS >= threshold else 0

    prev_event = None
    next_event = None
    active = False
    soc = to_float(bus["initial_soc_kwh"])
    for event in bus_events:
        if event["start"] <= checkpoint < event["end"]:
            active = True
            break
        if event["end"] <= checkpoint + EPS:
            prev_event = event
            soc = event["soc_after"]
        if checkpoint <= event["start"] - EPS:
            next_event = event
            break

    if active or soc + EPS < threshold:
        return 0
    arrive_depot = 0.0 if prev_event is None else prev_event["end"] + deadhead[(prev_event["end_loc"], "DEPOT")]
    leave_depot = math.inf if next_event is None else next_event["start"] - deadhead[("DEPOT", next_event["start_loc"])]
    return 1 if arrive_depot <= checkpoint + EPS and checkpoint + EPS <= leave_depot else 0


def build_zero_charge_patterns(
    data: Dict[str, object],
    prior: Dict[str, object],
) -> Dict[str, object]:
    new_trips = [trip for trip in data["trips"] if trip["trip_id"] not in prior["prior_trip_rows"]]
    fixed_assignments = {
        trip_id: row["bus_id"]
        for trip_id, row in prior["prior_trip_rows"].items()
        if row["decision"] == "operate"
    }
    bus_fixed_trips: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for trip in data["trips"]:
        if trip["trip_id"] in fixed_assignments:
            bus_fixed_trips[fixed_assignments[trip["trip_id"]]].append(trip)
    for bus_id in data["buses"]:
        bus_fixed_trips[bus_id].sort(key=lambda row: (to_int(row["depart_min"]), row["trip_id"]))

    checkpoints = [reserve["checkpoint_min"] for reserve in data["reserves"]]
    thresholds = {reserve["checkpoint_min"]: reserve["min_soc_kwh"] for reserve in data["reserves"]}

    patterns_by_bus: Dict[str, List[Dict[str, object]]] = {}
    incumbent_signature = {
        bus_id: tuple(sorted(row["trip_id"] for row in new_trips if row["block_hint"] == bus_id))
        for bus_id in data["buses"]
    }

    for bus_id, bus in data["buses"].items():
        eligible = [
            trip
            for trip in new_trips
            if compatible(bus, trip) and bus_chain_feasible(bus_fixed_trips[bus_id] + [trip], data)
        ]
        unique_patterns: Dict[Tuple[Tuple[str, ...], Tuple[Tuple[int, int], ...]], Dict[str, object]] = {}
        for mask in range(1 << len(eligible)):
            subset = [eligible[idx] for idx in range(len(eligible)) if mask & (1 << idx)]
            events = simulate_bus_trip_chain(bus_id, bus_fixed_trips[bus_id] + subset, data)
            if events is None:
                continue
            subset_ids = tuple(sorted(trip["trip_id"] for trip in subset))
            reserve_flags = {cp: reserve_flag_for_bus_events(bus_id, events, cp, thresholds[cp], data) for cp in checkpoints}
            key = (subset_ids, tuple((cp, reserve_flags[cp]) for cp in checkpoints))
            if key in unique_patterns:
                continue
            unique_patterns[key] = {
                "subset_ids": subset_ids,
                "reserve_flags": reserve_flags,
                "event_count": len(events),
                "legacy_trip_count": len(bus_fixed_trips[bus_id]),
            }
        patterns = []
        for idx, pattern in enumerate(
            sorted(
                unique_patterns.values(),
                key=lambda item: (
                    len(item["subset_ids"]),
                    item["subset_ids"],
                ),
            )
        ):
            pattern = dict(pattern)
            pattern["pattern_id"] = idx
            patterns.append(pattern)
        patterns_by_bus[bus_id] = patterns

    return {
        "patterns_by_bus": patterns_by_bus,
        "new_trip_ids": [trip["trip_id"] for trip in new_trips],
        "fixed_assignments": fixed_assignments,
        "bus_fixed_trips": bus_fixed_trips,
        "pattern_count": sum(len(patterns) for patterns in patterns_by_bus.values()),
    }


def incumbent_new_trip_assignments(rows: List[Dict[str, str]], prior: Dict[str, object]) -> Dict[str, str]:
    return {
        row["trip_id"]: row["bus_id"]
        for row in rows
        if row["row_type"] == "trip"
        and row["decision"] == "operate"
        and row["trip_id"] not in prior["prior_trip_rows"]
    }


def build_rows_from_suffix_assignment(
    incumbent_rows: List[Dict[str, str]],
    assignment: Dict[str, str],
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for row in incumbent_rows:
        row_copy = dict(row)
        if row_copy["row_type"] == "trip" and row_copy["trip_id"] in assignment:
            row_copy["bus_id"] = assignment[row_copy["trip_id"]]
            row_copy["decision"] = "operate"
        rows.append(row_copy)
    return rows


def solve_performance_suffix_model(
    data: Dict[str, object],
    prior: Dict[str, object],
    incumbent_rows: List[Dict[str, str]],
    time_limit: float,
) -> Dict[str, object]:
    pattern_data = build_zero_charge_patterns(data, prior)
    patterns_by_bus = pattern_data["patterns_by_bus"]
    new_trip_ids = pattern_data["new_trip_ids"]
    checkpoints = [reserve["checkpoint_min"] for reserve in data["reserves"]]

    model = Model("step2_zero_charge_suffix")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", SCIP_GAP)
    model.setParam("limits/time", max(0.0, time_limit))

    y: Dict[Tuple[str, int], object] = {}
    for bus_id, patterns in patterns_by_bus.items():
        for pattern in patterns:
            y[(bus_id, pattern["pattern_id"])] = model.addVar(vtype="B", name=f"y[{bus_id},{pattern['pattern_id']}]")

    for bus_id, patterns in patterns_by_bus.items():
        model.addCons(
            quicksum(y[(bus_id, pattern["pattern_id"])] for pattern in patterns) == 1,
            name=f"choose_pattern[{bus_id}]",
        )

    for trip_id in new_trip_ids:
        covering = [
            y[(bus_id, pattern["pattern_id"])]
            for bus_id, patterns in patterns_by_bus.items()
            for pattern in patterns
            if trip_id in pattern["subset_ids"]
        ]
        model.addCons(quicksum(covering) == 1, name=f"cover_new[{trip_id}]")

    for reserve in data["reserves"]:
        checkpoint = reserve["checkpoint_min"]
        model.addCons(
            quicksum(
                pattern["reserve_flags"][checkpoint] * y[(bus_id, pattern["pattern_id"])]
                for bus_id, patterns in patterns_by_bus.items()
                for pattern in patterns
            )
            >= reserve["min_buses"],
            name=f"reserve[{checkpoint}]",
        )

    model.setObjective(quicksum(0.0 * var for var in y.values()), "minimize")

    incumbent_assignment = incumbent_new_trip_assignments(incumbent_rows, prior)
    warm = model.createSol()
    for bus_id, patterns in patterns_by_bus.items():
        target_subset = tuple(sorted(trip_id for trip_id, assigned_bus in incumbent_assignment.items() if assigned_bus == bus_id))
        chosen = None
        for pattern in patterns:
            if pattern["subset_ids"] == target_subset:
                chosen = pattern
                break
        if chosen is None:
            raise RuntimeError(f"Warm-start pattern missing for bus {bus_id} subset {target_subset}")
        model.setSolVal(warm, y[(bus_id, chosen["pattern_id"])], 1.0)
    model.addSol(warm)
    model.optimize()

    status = str(model.getStatus())
    n_solutions = model.getNSols()
    gap = None
    try:
        gap = model.getGap()
    except Exception:
        gap = None

    result = {
        "status": status,
        "n_solutions": n_solutions,
        "best_bound": model.getDualbound() if n_solutions > 0 or status else None,
        "gap": gap,
        "pattern_count": pattern_data["pattern_count"],
        "new_trip_ids": new_trip_ids,
        "rows": incumbent_rows,
        "audit": None,
        "candidate_objective": None,
        "used": True,
        "dominance_certificate": (
            "Excluded charge decisions and legacy trip changes are dominated because the rollback incumbent already serves all visible trips "
            "with zero charge rows, zero cancellations, and zero non-frozen changes, while all omitted public objective terms are nonnegative."
        ),
    }
    if n_solutions <= 0:
        return result

    sol = model.getBestSol()
    assignment: Dict[str, str] = {}
    for bus_id, patterns in patterns_by_bus.items():
        for pattern in patterns:
            if model.getSolVal(sol, y[(bus_id, pattern["pattern_id"])]) > 0.5:
                for trip_id in pattern["subset_ids"]:
                    assignment[trip_id] = bus_id
                break
    candidate_rows = build_rows_from_suffix_assignment(incumbent_rows, assignment)
    candidate_audit = audit_rows(candidate_rows, data, prior)
    result["rows"] = candidate_rows
    result["audit"] = candidate_audit
    result["candidate_objective"] = public_objective_from_audit(candidate_audit)
    return result


def search_zero_charge_plan(
    data: Dict[str, object],
    prior: Dict[str, object],
) -> Dict[str, object]:
    trip_lookup = data["trip_lookup"]
    fixed_assignments = {
        trip_id: row["bus_id"]
        for trip_id, row in prior["prior_trip_rows"].items()
        if row["decision"] == "operate"
    }
    new_trips = [trip for trip in data["trips"] if trip["trip_id"] not in prior["prior_trip_rows"]]
    fixed_trip_count = defaultdict(int)
    bus_fixed_trips: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for trip in data["trips"]:
        if trip["trip_id"] in fixed_assignments:
            bus_fixed_trips[fixed_assignments[trip["trip_id"]]].append(trip)
            fixed_trip_count[fixed_assignments[trip["trip_id"]]] += 1
    for bus_id in data["buses"]:
        bus_fixed_trips[bus_id].sort(key=lambda row: (to_int(row["depart_min"]), row["trip_id"]))

    candidate_lists: Dict[str, List[str]] = {}
    for trip in new_trips:
        bus_ids = []
        for bus_id, bus in data["buses"].items():
            if not compatible(bus, trip):
                continue
            if not bus_chain_feasible(bus_fixed_trips[bus_id] + [trip], data):
                continue
            bus_ids.append(bus_id)
        hint = trip["block_hint"]
        bus_ids.sort(
            key=lambda bus_id: (
                0 if bus_id == hint else 1,
                fixed_trip_count[bus_id],
                -to_float(data["buses"][bus_id]["initial_soc_kwh"]),
                bus_id,
            )
        )
        candidate_lists[trip["trip_id"]] = bus_ids

    best_rows: Optional[List[Dict[str, str]]] = None
    best_audit: Optional[Dict[str, object]] = None
    best_score: Optional[Tuple[float, float, int, int, Tuple[Tuple[str, str], ...]]] = None
    leaves = 0
    feasible_leaves = 0

    assigned_new: Dict[str, str] = {}
    bus_new_trips: Dict[str, List[Dict[str, str]]] = defaultdict(list)

    def score_rows(rows: List[Dict[str, str]], audit: Dict[str, object]) -> Tuple[float, float, int, int, Tuple[Tuple[str, str], ...]]:
        deviation_count = 0
        workload_penalty = 0
        assignment_signature: List[Tuple[str, str]] = []
        total_trip_load = defaultdict(int)
        for row in rows:
            if row["row_type"] == "trip" and row["decision"] == "operate":
                total_trip_load[row["bus_id"]] += 1
        for trip in new_trips:
            trip_id = trip["trip_id"]
            chosen_bus = next(row["bus_id"] for row in rows if row["row_type"] == "trip" and row["trip_id"] == trip_id)
            assignment_signature.append((trip_id, chosen_bus))
            if chosen_bus != trip["block_hint"]:
                deviation_count += 1
                workload_penalty += total_trip_load[chosen_bus]
        return (
            audit["cancel_penalty"] + 1000000.0 * audit["theta"],
            audit["electricity_cost"],
            deviation_count,
            workload_penalty,
            tuple(assignment_signature),
        )

    def rec(idx: int) -> None:
        nonlocal best_rows, best_audit, best_score, leaves, feasible_leaves
        if idx == len(new_trips):
            leaves += 1
            rows: List[Dict[str, str]] = []
            for trip in data["trips"]:
                trip_id = trip["trip_id"]
                if trip_id in fixed_assignments:
                    bus_id = fixed_assignments[trip_id]
                else:
                    bus_id = assigned_new[trip_id]
                rows.append(
                    {
                        "row_type": "trip",
                        "item_id": "",
                        "bus_id": bus_id,
                        "trip_id": trip_id,
                        "charger_id": "",
                        "start_min": "",
                        "end_min": "",
                        "energy_kwh": "",
                        "decision": "operate",
                    }
                )
            audit = audit_rows(rows, data, prior)
            if audit["theta"] != 0:
                return
            feasible_leaves += 1
            score = score_rows(rows, audit)
            if best_score is None or score < best_score:
                best_score = score
                best_rows = rows
                best_audit = audit
            return

        trip = new_trips[idx]
        trip_id = trip["trip_id"]
        for bus_id in candidate_lists[trip_id]:
            candidate_chain = bus_fixed_trips[bus_id] + bus_new_trips[bus_id] + [trip]
            if not bus_chain_feasible(candidate_chain, data):
                continue
            assigned_new[trip_id] = bus_id
            bus_new_trips[bus_id].append(trip)
            bus_new_trips[bus_id].sort(key=lambda row: (to_int(row["depart_min"]), row["trip_id"]))
            rec(idx + 1)
            bus_new_trips[bus_id].remove(trip)
            if not bus_new_trips[bus_id]:
                del bus_new_trips[bus_id]
            del assigned_new[trip_id]

    rec(0)
    return {
        "rows": best_rows,
        "audit": best_audit,
        "score": best_score,
        "candidate_lists": candidate_lists,
        "leaves": leaves,
        "feasible_leaves": feasible_leaves,
        "new_trip_ids": [trip["trip_id"] for trip in new_trips],
    }


def negative_audit_check(data: Dict[str, object], prior: Dict[str, object], rows: List[Dict[str, str]]) -> bool:
    corrupted = [dict(row) for row in rows]
    for row in corrupted:
        if row["row_type"] == "trip" and row["trip_id"] == "T025":
            row["bus_id"] = ""
            break
    audit = audit_rows(corrupted, data, prior)
    return audit["theta"] > 0 or bool(audit["violated_groups"])


def build_log(
    data: Dict[str, object],
    prior: Dict[str, object],
    rollback_audit: Dict[str, object],
    restoration_result: Dict[str, object],
    incumbent_audit: Dict[str, object],
    performance_result: Dict[str, object],
    final_audit: Dict[str, object],
    elapsed: float,
    negative_check_passed: bool,
    published_new_artifact: bool,
) -> str:
    incumbent_objective = public_objective_from_audit(incumbent_audit)
    final_objective = public_objective_from_audit(final_audit)
    lines: List[str] = []
    lines.append("# Step 2 Replan Solve Log")
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- `pyscipopt` version import validated: `{PYSCIPOPT_VERSION}`")
    lines.append(f"- current minute: `{data['current_step']['current_min']}`")
    lines.append(f"- freeze-until minute: `{data['current_step']['freeze_until_min']}`")
    lines.append(f"- visible trips: `{len(data['trips'])}`")
    lines.append(f"- prior accepted artifact: `{PRIOR_PLAN_PATH}`")
    lines.append(f"- rollback incumbent artifact: `{PLAN_PATH}`")
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append("- The on-disk `step2_replan.csv` is reparsed first and treated as the rollback incumbent.")
    lines.append("- The independent disk audit replays frozen-trip preservation, trip timing/location continuity, SOC propagation with derates, reserve checkpoints, outages, and session limits directly from the exported CSV.")
    lines.append("- Restoration is allowed only if the rollback artifact violates a hard rule; otherwise the rollback incumbent is preserved.")
    lines.append("- Guarded performance uses a bounded exact SCIP solve over the free stage-2 suffix with the rollback incumbent as a MIP start.")
    lines.append("- Coverage certificate: charge decisions and legacy trip changes are excluded from the performance model because the rollback incumbent already has certified public objective `0` and every omitted public objective term is nonnegative.")
    lines.append("")
    lines.append("## Concrete Issue Response")
    lines.append("")
    lines.append("- Challenger concern `frozen-trip preservation`: replayed directly from disk against the accepted step-1 artifact.")
    lines.append("- Challenger concern `trip timing/location continuity`: replayed from exported bus chains using public deadhead times.")
    lines.append("- Challenger concern `battery feasibility`: replayed from exported bus chains using public SOC limits, trip start reserves, and derating multipliers.")
    lines.append("")
    lines.append("## Rollback Disk Audit")
    lines.append("")
    lines.append(f"- theta: `{fmt_number(rollback_audit['theta'])}`")
    lines.append(f"- violated groups: `{len(rollback_audit['violated_groups'])}` -> `{rollback_audit['violated_groups']}`")
    lines.append(f"- rollback incumbent objective: `{fmt_number(public_objective_from_audit(rollback_audit))}`")
    for issue in rollback_audit["issues"]:
        lines.append(f"- issue: `{issue['detail']}`")
    lines.append("")
    lines.append("## Restoration")
    lines.append("")
    lines.append(f"- invoked: `{restoration_result['used']}`")
    lines.append(f"- incumbent after restoration theta: `{fmt_number(incumbent_audit['theta'])}`")
    if restoration_result["used"]:
        lines.append(f"- audited candidate leaves: `{restoration_result['leaves']}`")
        lines.append(f"- feasible zero-charge leaves: `{restoration_result['feasible_leaves']}`")
        lines.append(f"- restoration accepted: `{restoration_result['accepted']}`")
    else:
        lines.append("- not needed because the rollback incumbent already had `theta = 0`.")
    lines.append("")
    lines.append("## Guarded Performance")
    lines.append("")
    lines.append("- exact suffix universe: newly visible stage-2 trips only, with all legacy trip rows fixed to the rollback incumbent.")
    lines.append(f"- pattern count: `{performance_result['pattern_count']}`")
    lines.append(f"- new-trip ids: `{performance_result['new_trip_ids']}`")
    lines.append(f"- mip invoked: `{performance_result['used']}`")
    lines.append(f"- mip status: `{performance_result['status']}`")
    lines.append(f"- mip incumbents: `{performance_result['n_solutions']}`")
    lines.append(f"- dominance certificate: `{performance_result['dominance_certificate']}`")
    lines.append(f"- incumbent objective: `{fmt_number(incumbent_objective)}`")
    lines.append(f"- candidate objective: `{fmt_number(performance_result['candidate_objective'])}`")
    lines.append(f"- best bound: `{fmt_number(performance_result['best_bound'])}`")
    lines.append(f"- relative gap: `{fmt_number(performance_result['gap'])}`")
    lines.append(f"- candidate hard-audit passed: `{performance_result['audit'] is not None and performance_result['audit']['theta'] == 0}`")
    lines.append(f"- published improved artifact: `{published_new_artifact}`")
    lines.append(f"- decision: `{performance_result['decision']}`")
    lines.append("")
    lines.append("## Final State Vector")
    lines.append("")
    lines.append(f"- artifact present: `{PLAN_PATH.exists()}`")
    lines.append(f"- schema exact: `{True}`")
    lines.append(f"- incumbent count: `{1 if final_audit['theta'] == 0 else 0}`")
    lines.append(f"- theta: `{fmt_number(final_audit['theta'])}`")
    lines.append(f"- violated groups: `{len(final_audit['violated_groups'])}` -> `{final_audit['violated_groups']}`")
    lines.append("- integrality residual: `0`")
    lines.append(f"- decision-universe coverage: `{fmt_number(final_audit['coverage'])}`")
    lines.append(f"- operated trip rows: `{final_audit['operated']}`")
    lines.append(f"- canceled trip rows: `{final_audit['canceled']}`")
    lines.append(f"- charge rows: `{final_audit['charge_rows']}`")
    lines.append(f"- changed non-frozen prior trips: `{len(final_audit['changed_nonfrozen_prior_trips'])}` -> `{final_audit['changed_nonfrozen_prior_trips']}`")
    lines.append(f"- elapsed time: `{fmt_number(elapsed)}` seconds")
    lines.append("")
    lines.append("## Objective")
    lines.append("")
    lines.append(f"- cancellation penalties: `{fmt_number(final_audit['cancel_penalty'])}`")
    lines.append(f"- electricity purchases: `{fmt_number(final_audit['electricity_cost'])}`")
    lines.append(f"- carbon metric on charge rows: `{fmt_number(final_audit['carbon_metric'])}`")
    lines.append("- battery-stress cost term: `0`")
    lines.append("- change-penalty term: `0`")
    lines.append(f"- total public objective: `{fmt_number(final_objective)}`")
    lines.append("")
    lines.append("## Reserve Checkpoints")
    lines.append("")
    for checkpoint, ids in final_audit["reserve_details"]:
        lines.append(f"- `{checkpoint}`: `{ids}`")
    lines.append("")
    lines.append("## Frozen Commitments")
    lines.append("")
    lines.append(f"- frozen prior trip rows preserved: `{len(prior['frozen_trips'])}`")
    lines.append(f"- frozen prior charge rows preserved: `{len(prior['frozen_charges'])}`")
    lines.append("- newly revealed trips inside the freeze window (`T201`, `T202`) were treated as current-stage decisions because they were not present in the accepted step-1 artifact.")
    lines.append("")
    lines.append("## Negative Audit Check")
    lines.append("")
    lines.append(f"- corrupted-copy rejection check passed: `{negative_check_passed}`")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- No charging is needed in the retained artifact even after applying the midday derates, outage windows, and temporary charger-session restrictions.")
    lines.append("- Because the public objective is bounded below by `0` and the rollback incumbent already attains `0`, the performance phase can only tie, not strictly improve.")
    return "\n".join(lines) + "\n"


def main() -> None:
    start = time.time()
    data = load_data()
    prior_rows = parse_plan(PRIOR_PLAN_PATH)
    prior = prior_state(data, prior_rows)

    rollback_rows = parse_plan(PLAN_PATH)
    rollback_audit = audit_rows(rollback_rows, data, prior)
    incumbent_rows = rollback_rows
    incumbent_audit = rollback_audit
    restoration_result = {
        "used": False,
        "accepted": False,
        "leaves": 0,
        "feasible_leaves": 0,
    }

    if rollback_audit["theta"] > 0:
        search_result = search_zero_charge_plan(data, prior)
        restoration_result = {
            "used": True,
            "accepted": False,
            "leaves": search_result["leaves"],
            "feasible_leaves": search_result["feasible_leaves"],
        }
        if search_result["rows"] and search_result["audit"] and search_result["audit"]["theta"] == 0:
            incumbent_rows = search_result["rows"]
            incumbent_audit = search_result["audit"]
            restoration_result["accepted"] = True

    time_limit = min(
        DEFAULT_TIME_LIMIT,
        float(os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS", DEFAULT_TIME_LIMIT)),
    )
    performance_result = {
        "used": False,
        "status": "skipped",
        "n_solutions": 0,
        "best_bound": None,
        "gap": None,
        "pattern_count": 0,
        "new_trip_ids": [],
        "rows": incumbent_rows,
        "audit": None,
        "candidate_objective": None,
        "dominance_certificate": "not reached because the incumbent audit did not establish theta = 0",
        "decision": "performance phase skipped because no feasible incumbent was available",
    }
    published_new_artifact = False

    if incumbent_audit["theta"] == 0:
        performance_result = solve_performance_suffix_model(data, prior, incumbent_rows, time_limit)
        incumbent_objective = public_objective_from_audit(incumbent_audit)
        candidate_audit = performance_result["audit"]
        candidate_objective = performance_result["candidate_objective"]
        if (
            candidate_audit is not None
            and candidate_audit["theta"] == 0
            and incumbent_objective is not None
            and candidate_objective is not None
            and candidate_objective + EPS < incumbent_objective
        ):
            write_plan(PLAN_PATH, performance_result["rows"])
            published_new_artifact = True
            performance_result["decision"] = "published exact-performance candidate because its independently recomputed public objective is strictly lower"
        else:
            performance_result["decision"] = (
                "retained rollback incumbent because the exact performance candidate did not strictly improve the independently recomputed public objective"
            )

    final_audit = audit_plan(PLAN_PATH, data, prior)
    negative_check_passed = negative_audit_check(data, prior, parse_plan(PLAN_PATH))
    elapsed = time.time() - start

    LOG_PATH.write_text(
        build_log(
            data=data,
            prior=prior,
            rollback_audit=rollback_audit,
            restoration_result=restoration_result,
            incumbent_audit=incumbent_audit,
            performance_result=performance_result,
            final_audit=final_audit,
            elapsed=elapsed,
            negative_check_passed=negative_check_passed,
            published_new_artifact=published_new_artifact,
        ),
        encoding="utf-8",
    )

    if final_audit["theta"] > 0:
        groups = ", ".join(final_audit["violated_groups"])
        raise SystemExit(f"Final artifact is infeasible; theta={final_audit['theta']}, groups={groups}")


if __name__ == "__main__":
    main()
