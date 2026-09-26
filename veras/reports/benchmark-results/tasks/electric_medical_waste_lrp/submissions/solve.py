from __future__ import annotations

import csv
import itertools
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pyscipopt import Model, quicksum


ROOT = Path("/app")
DATA_DIR = ROOT / "data"
SUBMISSIONS_DIR = ROOT / "submissions"

SOLUTION_PATH = SUBMISSIONS_DIR / "solution.csv"
TMP_SOLUTION_PATH = SUBMISSIONS_DIR / "_candidate_solution.csv"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"

MASTER_TIME_LIMIT_SECONDS = 120.0
REL_GAP = 0.0005
FLOAT_TOL = 1e-8


@dataclass(frozen=True)
class TripPlan:
    vehicle_id: str
    clinic_order: Tuple[str, ...]
    facility_id: str
    unload_window_id: str
    unload_time: float
    end_time: float
    end_battery: float
    kg: float
    red_kg: float
    yellow_kg: float
    objective: float


@dataclass
class Pattern:
    group_id: str
    pattern_id: str
    vehicle_sequences: Dict[str, List[Tuple[str, str]]]
    objective: float
    final_times: Dict[str, float]
    final_batteries: Dict[str, float]
    facility_total_kg: Dict[str, float]
    facility_red_kg: Dict[str, float]
    facility_yellow_kg: Dict[str, float]
    facility_window_counts: Dict[Tuple[str, str], int]
    facility_window_kg: Dict[Tuple[str, str], float]
    facilities_used: Tuple[str, ...]


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def load_data() -> Dict[str, object]:
    config = json.loads((DATA_DIR / "config.json").read_text())

    clinics = {row["clinic_id"]: row for row in read_csv(DATA_DIR / "clinics.csv")}
    vehicles = {row["vehicle_id"]: row for row in read_csv(DATA_DIR / "vehicles.csv")}
    permits = {row["vehicle_id"]: row for row in read_csv(DATA_DIR / "vehicle_waste_permits.csv")}
    facilities = {row["facility_id"]: row for row in read_csv(DATA_DIR / "treatment_facilities.csv")}
    chargers = {row["charger_id"]: row for row in read_csv(DATA_DIR / "chargers.csv")}
    nodes = {row["node_id"]: row for row in read_csv(DATA_DIR / "nodes.csv")}
    windows = read_csv(DATA_DIR / "facility_receiving_windows.csv")
    travel = {(row["from_node"], row["to_node"]): row for row in read_csv(DATA_DIR / "travel_energy_arcs.csv")}

    facility_rules: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(dict)
    for row in read_csv(DATA_DIR / "facility_waste_rules.csv"):
        facility_rules[row["facility_id"]][row["waste_type"]] = {
            "accepted": float(row["accepted"]),
            "treatment_cost_per_kg": float(row["treatment_cost_per_kg"]),
        }

    incompatible_pairs = set()
    for row in read_csv(DATA_DIR / "incompatible_pickup_pairs.csv"):
        incompatible_pairs.add(tuple(sorted((row["clinic_a"], row["clinic_b"]))))

    zone_clinics: Dict[str, List[str]] = defaultdict(list)
    for clinic_id, clinic in clinics.items():
        zone_clinics[clinic["zone"]].append(clinic_id)

    windows_by_facility: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in windows:
        windows_by_facility[row["facility_id"]].append(row)
    for rows in windows_by_facility.values():
        rows.sort(key=lambda row: float(row["start_min"]))

    return {
        "config": config,
        "clinics": clinics,
        "vehicles": vehicles,
        "permits": permits,
        "facilities": facilities,
        "chargers": chargers,
        "nodes": nodes,
        "windows": windows,
        "windows_by_facility": windows_by_facility,
        "travel": travel,
        "facility_rules": facility_rules,
        "incompatible_pairs": incompatible_pairs,
        "zone_clinics": zone_clinics,
    }


def as_float(value: str) -> float:
    return float(value)


def format_float(value: float) -> str:
    return f"{value:.8f}"


def weighted_load(red_kg: float, yellow_kg: float, data: Dict[str, object]) -> float:
    config = data["config"]
    return config["red_risk_factor"] * red_kg + config["yellow_risk_factor"] * yellow_kg


def wait_risk_cost(wait_min: float, red_kg: float, yellow_kg: float, data: Dict[str, object]) -> float:
    if wait_min <= FLOAT_TOL:
        return 0.0
    config = data["config"]
    return (
        config["risk_cost_per_unit"]
        * weighted_load(red_kg, yellow_kg, data)
        * config["loaded_wait_risk_multiplier"]
        * wait_min
    )


def advance_arc(
    current_node: str,
    next_node: str,
    departure_time: float,
    battery: float,
    red_kg: float,
    yellow_kg: float,
    data: Dict[str, object],
) -> Tuple[float, float, float]:
    config = data["config"]
    arc = data["travel"][(current_node, next_node)]

    travel_min = as_float(arc["travel_min"])
    distance_km = as_float(arc["distance_km"])
    empty_energy = as_float(arc["empty_energy_kwh"])
    pop_risk = as_float(arc["population_risk_index"])
    load_kg = red_kg + yellow_kg

    wait_min = 0.0
    if load_kg > FLOAT_TOL and int(arc["loaded_curfew_flag"]) == 1:
        curfew_start = config["curfew_start_min"]
        curfew_end = config["curfew_end_min"]
        if departure_time < curfew_end and departure_time + travel_min > curfew_start:
            wait_min = max(0.0, curfew_end - departure_time)

    route_cost = config["travel_cost_per_km"] * distance_km
    if load_kg > FLOAT_TOL:
        route_cost += (
            config["risk_cost_per_unit"]
            * weighted_load(red_kg, yellow_kg, data)
            * pop_risk
            * travel_min
        )
        route_cost += wait_risk_cost(wait_min, red_kg, yellow_kg, data)

    energy = empty_energy + config["load_energy_kwh_per_kg_km"] * load_kg * distance_km
    return departure_time + wait_min + travel_min, battery - energy, route_cost


class TripSolver:
    def __init__(self, data: Dict[str, object]):
        self.data = data
        self.cache: Dict[Tuple[str, str, float, Tuple[str, ...], str, float], Optional[TripPlan]] = {}

    def best_trip(
        self,
        vehicle_id: str,
        start_node: str,
        start_time: float,
        clinic_subset: Sequence[str],
        facility_id: str,
        start_battery: float,
    ) -> Optional[TripPlan]:
        subset_key = tuple(sorted(clinic_subset))
        cache_key = (
            vehicle_id,
            start_node,
            round(start_time, 8),
            subset_key,
            facility_id,
            round(start_battery, 8),
        )
        if cache_key in self.cache:
            return self.cache[cache_key]

        best: Optional[TripPlan] = None
        for order in itertools.permutations(subset_key):
            trip = self._simulate_trip(vehicle_id, start_node, start_time, order, facility_id, start_battery)
            if trip is None:
                continue
            if best is None or (trip.objective, trip.end_time) < (best.objective, best.end_time):
                best = trip

        self.cache[cache_key] = best
        return best

    def _simulate_trip(
        self,
        vehicle_id: str,
        start_node: str,
        start_time: float,
        clinic_order: Sequence[str],
        facility_id: str,
        start_battery: float,
    ) -> Optional[TripPlan]:
        data = self.data
        clinics = data["clinics"]
        vehicles = data["vehicles"]
        permits = data["permits"]
        facilities = data["facilities"]
        facility_rules = data["facility_rules"]
        windows_by_facility = data["windows_by_facility"]
        incompatible_pairs = data["incompatible_pairs"]

        vehicle = vehicles[vehicle_id]
        permit = permits[vehicle_id]

        total_cap = as_float(vehicle["capacity_kg"])
        red_cap = as_float(vehicle["red_compartment_kg"])
        yellow_cap = as_float(vehicle["yellow_compartment_kg"])

        time = start_time
        battery = start_battery
        node = start_node
        red_kg = 0.0
        yellow_kg = 0.0
        onboard_pick_time: Dict[str, float] = {}
        objective = 0.0
        isolation_count = 0

        for clinic_id in clinic_order:
            clinic = clinics[clinic_id]
            time, battery, arc_cost = advance_arc(node, clinic["node_id"], time, battery, red_kg, yellow_kg, data)
            if battery < -FLOAT_TOL:
                return None
            objective += arc_cost
            node = clinic["node_id"]

            earliest = as_float(clinic["earliest_min"])
            latest = as_float(clinic["latest_min"])
            if time < earliest:
                objective += wait_risk_cost(earliest - time, red_kg, yellow_kg, data)
                time = earliest
            if time > latest + FLOAT_TOL:
                return None

            service_min = as_float(clinic["service_min"])
            objective += wait_risk_cost(service_min, red_kg, yellow_kg, data)
            time += service_min

            priority = clinic["priority_class"]
            if int(permit[f"allow_{priority}"]) != 1:
                return None
            if priority == "isolation":
                isolation_count += 1
                if isolation_count > int(permit["max_isolation_pickups"]):
                    return None

            for onboard_clinic in onboard_pick_time:
                if tuple(sorted((clinic_id, onboard_clinic))) in incompatible_pairs:
                    return None

            red_kg += as_float(clinic["red_kg"])
            yellow_kg += as_float(clinic["yellow_kg"])
            if red_kg + yellow_kg > total_cap + FLOAT_TOL:
                return None
            if red_kg > red_cap + FLOAT_TOL:
                return None
            if yellow_kg > yellow_cap + FLOAT_TOL:
                return None

            onboard_pick_time[clinic_id] = time

        facility_node = facilities[facility_id]["node_id"]
        time, battery, arc_cost = advance_arc(node, facility_node, time, battery, red_kg, yellow_kg, data)
        if battery < -FLOAT_TOL:
            return None
        objective += arc_cost

        facility_windows = windows_by_facility[facility_id]
        unload_window_id = ""
        unload_time = time
        matched = False
        for window in facility_windows:
            start_min = as_float(window["start_min"])
            end_min = as_float(window["end_min"])
            if unload_time < start_min:
                objective += wait_risk_cost(start_min - unload_time, red_kg, yellow_kg, data)
                unload_time = start_min
            if unload_time <= end_min + FLOAT_TOL:
                unload_window_id = window["window_id"]
                matched = True
                break
        if not matched:
            return None
        if unload_time > as_float(facilities[facility_id]["latest_receive_min"]) + FLOAT_TOL:
            return None

        rules = facility_rules[facility_id]
        if red_kg > FLOAT_TOL and rules["red"]["accepted"] < 0.5:
            return None
        if yellow_kg > FLOAT_TOL and rules["yellow"]["accepted"] < 0.5:
            return None

        for clinic_id, pick_time in onboard_pick_time.items():
            clinic = clinics[clinic_id]
            if unload_time - pick_time > as_float(clinic["max_red_onboard_min"]) + FLOAT_TOL:
                return None
            if unload_time - pick_time > as_float(clinic["max_yellow_onboard_min"]) + FLOAT_TOL:
                return None

        objective += rules["red"]["treatment_cost_per_kg"] * red_kg
        objective += rules["yellow"]["treatment_cost_per_kg"] * yellow_kg

        return TripPlan(
            vehicle_id=vehicle_id,
            clinic_order=tuple(clinic_order),
            facility_id=facility_id,
            unload_window_id=unload_window_id,
            unload_time=unload_time,
            end_time=unload_time,
            end_battery=battery,
            kg=red_kg + yellow_kg,
            red_kg=red_kg,
            yellow_kg=yellow_kg,
            objective=objective,
        )


def vehicle_return(vehicle_id: str, from_node: str, from_time: float, battery: float, data: Dict[str, object]) -> Tuple[float, float]:
    vehicle = data["vehicles"][vehicle_id]
    arc = data["travel"][(from_node, vehicle["end_node"])]
    end_time = from_time + as_float(arc["travel_min"])
    end_battery = battery - as_float(arc["empty_energy_kwh"])
    return end_time, end_battery


def return_travel_cost(vehicle_id: str, from_node: str, data: Dict[str, object]) -> float:
    vehicle = data["vehicles"][vehicle_id]
    arc = data["travel"][(from_node, vehicle["end_node"])]
    return data["config"]["travel_cost_per_km"] * as_float(arc["distance_km"])


def pattern_signature(vehicle_sequences: Dict[str, List[Tuple[str, str]]]) -> Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]:
    return tuple(sorted((vehicle_id, tuple(events)) for vehicle_id, events in vehicle_sequences.items()))


def build_pattern(
    group_id: str,
    pattern_id: str,
    data: Dict[str, object],
    vehicle_trips: Dict[str, List[TripPlan]],
) -> Optional[Pattern]:
    facilities = data["facilities"]
    vehicles = data["vehicles"]

    vehicle_sequences: Dict[str, List[Tuple[str, str]]] = {}
    facility_total_kg: Dict[str, float] = defaultdict(float)
    facility_red_kg: Dict[str, float] = defaultdict(float)
    facility_yellow_kg: Dict[str, float] = defaultdict(float)
    facility_window_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    facility_window_kg: Dict[Tuple[str, str], float] = defaultdict(float)
    final_times: Dict[str, float] = {}
    final_batteries: Dict[str, float] = {}

    objective = 0.0
    for vehicle_id, trips in vehicle_trips.items():
        events: List[Tuple[str, str]] = []
        objective += as_float(vehicles[vehicle_id]["fixed_cost"])
        for trip in trips:
            for clinic_id in trip.clinic_order:
                events.append(("pickup", clinic_id))
            events.append(("unload", trip.facility_id))

            facility_total_kg[trip.facility_id] += trip.kg
            facility_red_kg[trip.facility_id] += trip.red_kg
            facility_yellow_kg[trip.facility_id] += trip.yellow_kg
            facility_window_counts[(trip.facility_id, trip.unload_window_id)] += 1
            facility_window_kg[(trip.facility_id, trip.unload_window_id)] += trip.kg
            objective += trip.objective

        vehicle_sequences[vehicle_id] = events
        last_trip = trips[-1]
        return_time, return_battery = vehicle_return(
            vehicle_id,
            facilities[last_trip.facility_id]["node_id"],
            last_trip.end_time,
            last_trip.end_battery,
            data,
        )
        objective += return_travel_cost(vehicle_id, facilities[last_trip.facility_id]["node_id"], data)
        if return_battery < -FLOAT_TOL:
            return None
        if return_time > as_float(vehicles[vehicle_id]["shift_end_min"]) + FLOAT_TOL:
            return None
        final_times[vehicle_id] = return_time
        final_batteries[vehicle_id] = return_battery

    return Pattern(
        group_id=group_id,
        pattern_id=pattern_id,
        vehicle_sequences=vehicle_sequences,
        objective=objective,
        final_times=final_times,
        final_batteries=final_batteries,
        facility_total_kg=dict(facility_total_kg),
        facility_red_kg=dict(facility_red_kg),
        facility_yellow_kg=dict(facility_yellow_kg),
        facility_window_counts=dict(facility_window_counts),
        facility_window_kg=dict(facility_window_kg),
        facilities_used=tuple(sorted(facility_total_kg)),
    )


def generate_partition_patterns(
    group_id: str,
    data: Dict[str, object],
    trip_solver: TripSolver,
    clinic_ids: Sequence[str],
    vehicle_a: str,
    vehicle_b: str,
    facility_id: str,
    limit: int,
) -> List[Pattern]:
    patterns: List[Pattern] = []
    all_mask = (1 << len(clinic_ids)) - 1
    pattern_idx = 0
    for mask in range(1, all_mask):
        if not (mask & 1):
            continue
        subset_a = [clinic_ids[i] for i in range(len(clinic_ids)) if (mask >> i) & 1]
        subset_b = [clinic_ids[i] for i in range(len(clinic_ids)) if not ((mask >> i) & 1)]
        if not subset_b:
            continue
        for assigned_a, assigned_b in ((subset_a, subset_b), (subset_b, subset_a)):
            trip_a = trip_solver.best_trip(
                vehicle_a,
                data["vehicles"][vehicle_a]["start_node"],
                as_float(data["vehicles"][vehicle_a]["shift_start_min"]),
                assigned_a,
                facility_id,
                as_float(data["vehicles"][vehicle_a]["start_battery_kwh"]),
            )
            if trip_a is None:
                continue
            trip_b = trip_solver.best_trip(
                vehicle_b,
                data["vehicles"][vehicle_b]["start_node"],
                as_float(data["vehicles"][vehicle_b]["shift_start_min"]),
                assigned_b,
                facility_id,
                as_float(data["vehicles"][vehicle_b]["start_battery_kwh"]),
            )
            if trip_b is None:
                continue
            pattern = build_pattern(
                group_id,
                f"{group_id}_{pattern_idx}",
                data,
                {vehicle_a: [trip_a], vehicle_b: [trip_b]},
            )
            if pattern is not None:
                patterns.append(pattern)
                pattern_idx += 1
    return keep_top_patterns(patterns, limit)


def generate_two_trip_patterns(
    group_id: str,
    data: Dict[str, object],
    trip_solver: TripSolver,
    vehicle_id: str,
    clinic_ids: Sequence[str],
    facility_choices: Sequence[str],
    limit: int,
) -> List[Pattern]:
    patterns: List[Pattern] = []
    all_mask = (1 << len(clinic_ids)) - 1
    pattern_idx = 0
    start_node = data["vehicles"][vehicle_id]["start_node"]
    start_time = as_float(data["vehicles"][vehicle_id]["shift_start_min"])
    start_battery = as_float(data["vehicles"][vehicle_id]["start_battery_kwh"])

    for mask in range(1, all_mask):
        if not (mask & 1):
            continue
        subset_a = [clinic_ids[i] for i in range(len(clinic_ids)) if (mask >> i) & 1]
        subset_b = [clinic_ids[i] for i in range(len(clinic_ids)) if not ((mask >> i) & 1)]
        if not subset_b:
            continue
        for first_subset, second_subset in ((subset_a, subset_b), (subset_b, subset_a)):
            for facility_1 in facility_choices:
                trip_1 = trip_solver.best_trip(
                    vehicle_id,
                    start_node,
                    start_time,
                    first_subset,
                    facility_1,
                    start_battery,
                )
                if trip_1 is None:
                    continue
                start_node_2 = data["facilities"][facility_1]["node_id"]
                for facility_2 in facility_choices:
                    trip_2 = trip_solver.best_trip(
                        vehicle_id,
                        start_node_2,
                        trip_1.end_time,
                        second_subset,
                        facility_2,
                        trip_1.end_battery,
                    )
                    if trip_2 is None:
                        continue
                    pattern = build_pattern(
                        group_id,
                        f"{group_id}_{pattern_idx}",
                        data,
                        {vehicle_id: [trip_1, trip_2]},
                    )
                    if pattern is not None:
                        patterns.append(pattern)
                        pattern_idx += 1
    return keep_top_patterns(patterns, limit)


def generate_three_trip_patterns(
    group_id: str,
    data: Dict[str, object],
    trip_solver: TripSolver,
    vehicle_id: str,
    clinic_ids: Sequence[str],
    facility_choices: Sequence[str],
    limit: int,
) -> List[Pattern]:
    patterns: List[Pattern] = []
    pattern_idx = 0
    start_node = data["vehicles"][vehicle_id]["start_node"]
    start_time = as_float(data["vehicles"][vehicle_id]["shift_start_min"])
    start_battery = as_float(data["vehicles"][vehicle_id]["start_battery_kwh"])

    for assignment in itertools.product(range(3), repeat=len(clinic_ids)):
        if set(assignment) != {0, 1, 2}:
            continue
        subsets = [[], [], []]
        for idx, bucket in enumerate(assignment):
            subsets[bucket].append(clinic_ids[idx])

        for facility_1 in facility_choices:
            trip_1 = trip_solver.best_trip(
                vehicle_id,
                start_node,
                start_time,
                subsets[0],
                facility_1,
                start_battery,
            )
            if trip_1 is None:
                continue

            start_node_2 = data["facilities"][facility_1]["node_id"]
            for facility_2 in facility_choices:
                trip_2 = trip_solver.best_trip(
                    vehicle_id,
                    start_node_2,
                    trip_1.end_time,
                    subsets[1],
                    facility_2,
                    trip_1.end_battery,
                )
                if trip_2 is None:
                    continue

                start_node_3 = data["facilities"][facility_2]["node_id"]
                for facility_3 in facility_choices:
                    trip_3 = trip_solver.best_trip(
                        vehicle_id,
                        start_node_3,
                        trip_2.end_time,
                        subsets[2],
                        facility_3,
                        trip_2.end_battery,
                    )
                    if trip_3 is None:
                        continue

                    pattern = build_pattern(
                        group_id,
                        f"{group_id}_3_{pattern_idx}",
                        data,
                        {vehicle_id: [trip_1, trip_2, trip_3]},
                    )
                    if pattern is not None:
                        patterns.append(pattern)
                        pattern_idx += 1

    return keep_top_patterns(patterns, limit)


def generate_north_central_patterns(
    data: Dict[str, object],
    trip_solver: TripSolver,
    limit: int,
) -> List[Pattern]:
    patterns: List[Pattern] = []
    vehicle_id = "EV8"
    pattern_idx = 0

    north_ids = data["zone_clinics"]["north"]
    central_ids = data["zone_clinics"]["central"]
    start_node = data["vehicles"][vehicle_id]["start_node"]
    start_time = as_float(data["vehicles"][vehicle_id]["shift_start_min"])
    start_battery = as_float(data["vehicles"][vehicle_id]["start_battery_kwh"])

    tasks = [
        ("north", north_ids, ["F5"]),
        ("central", central_ids, ["F1", "F3", "F5"]),
    ]
    for first_task, second_task in ((tasks[0], tasks[1]), (tasks[1], tasks[0])):
        for facility_1 in first_task[2]:
            trip_1 = trip_solver.best_trip(
                vehicle_id,
                start_node,
                start_time,
                first_task[1],
                facility_1,
                start_battery,
            )
            if trip_1 is None:
                continue
            start_node_2 = data["facilities"][facility_1]["node_id"]
            for facility_2 in second_task[2]:
                trip_2 = trip_solver.best_trip(
                    vehicle_id,
                    start_node_2,
                    trip_1.end_time,
                    second_task[1],
                    facility_2,
                    trip_1.end_battery,
                )
                if trip_2 is None:
                    continue
                pattern = build_pattern(
                    "north_central",
                    f"north_central_{pattern_idx}",
                    data,
                    {vehicle_id: [trip_1, trip_2]},
                )
                if pattern is not None:
                    patterns.append(pattern)
                    pattern_idx += 1
    return keep_top_patterns(patterns, limit)


def keep_top_patterns(patterns: List[Pattern], limit: int) -> List[Pattern]:
    dedup: Dict[Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...], Pattern] = {}
    for pattern in patterns:
        signature = pattern_signature(pattern.vehicle_sequences)
        if signature not in dedup or (pattern.objective, max(pattern.final_times.values())) < (
            dedup[signature].objective,
            max(dedup[signature].final_times.values()),
        ):
            dedup[signature] = pattern
    kept = list(dedup.values())
    kept.sort(key=lambda p: (p.objective, max(p.final_times.values())))
    return kept[:limit]


def build_candidate_groups(data: Dict[str, object]) -> Dict[str, List[Pattern]]:
    trip_solver = TripSolver(data)

    southwest_patterns = generate_two_trip_patterns(
        "southwest",
        data,
        trip_solver,
        "EV1",
        data["zone_clinics"]["southwest"],
        ["F1", "F3"],
        limit=300,
    )
    southwest_patterns.extend(
        generate_three_trip_patterns(
            "southwest",
            data,
            trip_solver,
            "EV1",
            data["zone_clinics"]["southwest"],
            ["F1", "F3"],
            limit=10000,
        )
    )

    east_patterns = generate_two_trip_patterns(
        "east",
        data,
        trip_solver,
        "EV2",
        data["zone_clinics"]["east"],
        ["F1", "F2", "F5"],
        limit=300,
    )
    east_patterns.extend(
        generate_three_trip_patterns(
            "east",
            data,
            trip_solver,
            "EV2",
            data["zone_clinics"]["east"],
            ["F1", "F2", "F5"],
            limit=10000,
        )
    )

    groups = {
        "west_pair": generate_partition_patterns(
            "west_pair",
            data,
            trip_solver,
            data["zone_clinics"]["west"],
            "EV5",
            "EV7",
            "F1",
            limit=150,
        ),
        "south_pair": generate_partition_patterns(
            "south_pair",
            data,
            trip_solver,
            data["zone_clinics"]["south"],
            "EV3",
            "EV6",
            "F3",
            limit=150,
        ),
        "southwest": keep_top_patterns(southwest_patterns, 10000),
        "east": keep_top_patterns(east_patterns, 10000),
        "southeast": generate_two_trip_patterns(
            "southeast",
            data,
            trip_solver,
            "EV4",
            data["zone_clinics"]["southeast"],
            ["F2", "F5"],
            limit=250,
        ),
        "north_central": generate_north_central_patterns(data, trip_solver, limit=100),
    }
    for group_id, patterns in groups.items():
        if not patterns:
            raise RuntimeError(f"No feasible patterns generated for group {group_id}")
    return groups


def partial_capacity_feasible(
    selected_patterns: Sequence[Pattern],
    data: Dict[str, object],
) -> bool:
    facilities = data["facilities"]
    window_caps = {
        (row["facility_id"], row["window_id"]): (int(row["max_unloads"]), as_float(row["max_kg"]))
        for row in data["windows"]
    }

    facility_total = defaultdict(float)
    facility_red = defaultdict(float)
    facility_yellow = defaultdict(float)
    window_count = defaultdict(int)
    window_kg = defaultdict(float)

    for pattern in selected_patterns:
        for facility_id, kg in pattern.facility_total_kg.items():
            facility_total[facility_id] += kg
            facility_red[facility_id] += pattern.facility_red_kg.get(facility_id, 0.0)
            facility_yellow[facility_id] += pattern.facility_yellow_kg.get(facility_id, 0.0)
            if facility_total[facility_id] > as_float(facilities[facility_id]["daily_capacity_kg"]) + FLOAT_TOL:
                return False
            if facility_red[facility_id] > as_float(facilities[facility_id]["red_capacity_kg"]) + FLOAT_TOL:
                return False
            if facility_yellow[facility_id] > as_float(facilities[facility_id]["yellow_capacity_kg"]) + FLOAT_TOL:
                return False
        for key, count in pattern.facility_window_counts.items():
            window_count[key] += count
            window_kg[key] += pattern.facility_window_kg[key]
            cap_count, cap_kg = window_caps[key]
            if window_count[key] > cap_count or window_kg[key] > cap_kg + FLOAT_TOL:
                return False
    return True


def find_feasible_combination(groups: Dict[str, List[Pattern]], data: Dict[str, object]) -> Dict[str, Pattern]:
    ordered_group_ids = sorted(groups, key=lambda group_id: len(groups[group_id]))
    best_solution: Dict[str, Pattern] = {}

    def backtrack(index: int, chosen: Dict[str, Pattern]) -> bool:
        if index == len(ordered_group_ids):
            best_solution.update(chosen)
            return True
        group_id = ordered_group_ids[index]
        for pattern in groups[group_id]:
            selected = list(chosen.values()) + [pattern]
            if not partial_capacity_feasible(selected, data):
                continue
            chosen[group_id] = pattern
            if backtrack(index + 1, chosen):
                return True
            del chosen[group_id]
        return False

    if not backtrack(0, {}):
        raise RuntimeError("No feasible incumbent found from candidate patterns")
    return best_solution


def extract_incumbent_from_solution(
    groups: Dict[str, List[Pattern]],
    solution_path: Path,
) -> Optional[Dict[str, Pattern]]:
    if not solution_path.exists():
        return None

    rows = read_csv(solution_path)
    per_vehicle_rows: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        per_vehicle_rows[row["vehicle_id"]].append(row)

    vehicle_sequences: Dict[str, List[Tuple[str, str]]] = {}
    for vehicle_id, vehicle_rows in per_vehicle_rows.items():
        vehicle_rows.sort(key=lambda row: int(row["sequence"]))
        sequence: List[Tuple[str, str]] = []
        for row in vehicle_rows:
            if row["row_type"] == "pickup":
                sequence.append(("pickup", row["clinic_id"]))
            elif row["row_type"] == "unload":
                sequence.append(("unload", row["facility_id"]))
            elif row["row_type"] == "charge":
                return None
        vehicle_sequences[vehicle_id] = sequence

    incumbent: Dict[str, Pattern] = {}
    for group_id, patterns in groups.items():
        target_signature = None
        for pattern in patterns:
            vehicle_ids = set(pattern.vehicle_sequences)
            candidate_signature = pattern_signature(
                {vehicle_id: vehicle_sequences.get(vehicle_id, []) for vehicle_id in vehicle_ids}
            )
            target_signature = candidate_signature
            break

        if target_signature is None:
            return None

        matched = None
        for pattern in patterns:
            if pattern_signature(pattern.vehicle_sequences) == target_signature:
                matched = pattern
                break
        if matched is None:
            return None
        incumbent[group_id] = matched

    return incumbent


def solve_master(
    groups: Dict[str, List[Pattern]],
    data: Dict[str, object],
    incumbent: Dict[str, Pattern],
) -> Tuple[Dict[str, Pattern], Dict[str, object]]:
    facilities = data["facilities"]
    windows = data["windows"]
    time_limit = MASTER_TIME_LIMIT_SECONDS
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit is not None:
        time_limit = min(time_limit, float(env_limit))

    model = Model("medical_waste_pattern_master")
    model.setParam("limits/gap", REL_GAP)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    x_vars = {}
    for group_id, patterns in groups.items():
        for idx, pattern in enumerate(patterns):
            x_vars[(group_id, idx)] = model.addVar(vtype="B", name=f"x[{group_id},{idx}]")

    y_vars = {facility_id: model.addVar(vtype="B", name=f"y[{facility_id}]") for facility_id in facilities}

    for group_id, patterns in groups.items():
        model.addCons(
            quicksum(x_vars[(group_id, idx)] for idx in range(len(patterns))) == 1,
            name=f"choose_one[{group_id}]",
        )

    for facility_id, facility in facilities.items():
        model.addCons(
            quicksum(
                pattern.facility_total_kg.get(facility_id, 0.0) * x_vars[(group_id, idx)]
                for group_id, patterns in groups.items()
                for idx, pattern in enumerate(patterns)
            )
            <= as_float(facility["daily_capacity_kg"]),
            name=f"facility_total[{facility_id}]",
        )
        model.addCons(
            quicksum(
                pattern.facility_red_kg.get(facility_id, 0.0) * x_vars[(group_id, idx)]
                for group_id, patterns in groups.items()
                for idx, pattern in enumerate(patterns)
            )
            <= as_float(facility["red_capacity_kg"]),
            name=f"facility_red[{facility_id}]",
        )
        model.addCons(
            quicksum(
                pattern.facility_yellow_kg.get(facility_id, 0.0) * x_vars[(group_id, idx)]
                for group_id, patterns in groups.items()
                for idx, pattern in enumerate(patterns)
            )
            <= as_float(facility["yellow_capacity_kg"]),
            name=f"facility_yellow[{facility_id}]",
        )
        model.addCons(
            quicksum(
                pattern.facility_total_kg.get(facility_id, 0.0) * x_vars[(group_id, idx)]
                for group_id, patterns in groups.items()
                for idx, pattern in enumerate(patterns)
            )
            <= as_float(facility["daily_capacity_kg"]) * y_vars[facility_id],
            name=f"open_link_upper[{facility_id}]",
        )
        model.addCons(
            quicksum(
                pattern.facility_total_kg.get(facility_id, 0.0) * x_vars[(group_id, idx)]
                for group_id, patterns in groups.items()
                for idx, pattern in enumerate(patterns)
            )
            >= data["config"]["minimum_facility_throughput_kg"] * y_vars[facility_id],
            name=f"open_link_lower[{facility_id}]",
        )

    for row in windows:
        key = (row["facility_id"], row["window_id"])
        model.addCons(
            quicksum(
                pattern.facility_window_counts.get(key, 0) * x_vars[(group_id, idx)]
                for group_id, patterns in groups.items()
                for idx, pattern in enumerate(patterns)
            )
            <= int(row["max_unloads"]),
            name=f"window_count[{key[0]},{key[1]}]",
        )
        model.addCons(
            quicksum(
                pattern.facility_window_kg.get(key, 0.0) * x_vars[(group_id, idx)]
                for group_id, patterns in groups.items()
                for idx, pattern in enumerate(patterns)
            )
            <= as_float(row["max_kg"]),
            name=f"window_kg[{key[0]},{key[1]}]",
        )

    model.addCons(
        quicksum(y_vars[facility_id] for facility_id in facilities) <= int(data["config"]["max_open_facilities"]),
        name="max_open_facilities",
    )

    objective = quicksum(
        pattern.objective * x_vars[(group_id, idx)]
        for group_id, patterns in groups.items()
        for idx, pattern in enumerate(patterns)
    ) + quicksum(
        as_float(facility["open_cost"]) * y_vars[facility_id] for facility_id, facility in facilities.items()
    )
    model.setObjective(objective, "minimize")

    warm = model.createSol()
    for group_id, patterns in groups.items():
        incumbent_signature = pattern_signature(incumbent[group_id].vehicle_sequences)
        for idx, pattern in enumerate(patterns):
            value = 1.0 if pattern_signature(pattern.vehicle_sequences) == incumbent_signature else 0.0
            model.setSolVal(warm, x_vars[(group_id, idx)], value)
    used_facilities = set()
    for pattern in incumbent.values():
        used_facilities.update(pattern.facilities_used)
    for facility_id in facilities:
        model.setSolVal(warm, y_vars[facility_id], 1.0 if facility_id in used_facilities else 0.0)
    model.addSol(warm, free=True)

    model.optimize()
    stats = {
        "status": str(model.getStatus()),
        "has_solution": model.getNSols() > 0,
        "best_bound": model.getDualbound() if model.getStage() >= 10 else None,
        "gap": model.getGap() if model.getNSols() > 0 else None,
        "time_limit": time_limit,
    }

    if model.getNSols() == 0:
        return incumbent, stats

    solution = model.getBestSol()
    chosen: Dict[str, Pattern] = {}
    for group_id, patterns in groups.items():
        picked_pattern = None
        for idx, pattern in enumerate(patterns):
            if model.getSolVal(solution, x_vars[(group_id, idx)]) > 0.5:
                picked_pattern = pattern
                break
        if picked_pattern is None:
            raise RuntimeError(f"No selected pattern found for group {group_id}")
        chosen[group_id] = picked_pattern

    stats["objective"] = model.getObjVal()
    return chosen, stats


def flatten_solution_rows(selected_patterns: Dict[str, Pattern], data: Dict[str, object]) -> List[List[str]]:
    clinics = data["clinics"]
    facilities = data["facilities"]
    vehicles = data["vehicles"]

    vehicle_events: Dict[str, List[Tuple[str, str]]] = {}
    for pattern in selected_patterns.values():
        for vehicle_id, events in pattern.vehicle_sequences.items():
            if vehicle_id in vehicle_events:
                raise RuntimeError(f"Vehicle {vehicle_id} assigned in multiple patterns")
            vehicle_events[vehicle_id] = events

    rows: List[List[str]] = []
    for vehicle_id in sorted(vehicle_events):
        sequence = 1
        rows.append(["start", vehicle_id, str(sequence), vehicles[vehicle_id]["start_node"], "", "", "", ""])
        sequence += 1
        for event_type, event_id in vehicle_events[vehicle_id]:
            if event_type == "pickup":
                rows.append(
                    ["pickup", vehicle_id, str(sequence), clinics[event_id]["node_id"], event_id, "", "", ""]
                )
            elif event_type == "unload":
                rows.append(
                    ["unload", vehicle_id, str(sequence), facilities[event_id]["node_id"], "", event_id, "", ""]
                )
            else:
                raise RuntimeError(f"Unsupported event type {event_type}")
            sequence += 1
        rows.append(["end", vehicle_id, str(sequence), vehicles[vehicle_id]["end_node"], "", "", "", ""])
    return rows


def write_solution(rows: List[List[str]], path: Path = SOLUTION_PATH) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["row_type", "vehicle_id", "sequence", "node_id", "clinic_id", "facility_id", "charger_id", "charge_kwh"]
        )
        writer.writerows(rows)


def audit_solution(solution_path: Path, data: Dict[str, object]) -> Dict[str, object]:
    rows = read_csv(solution_path)
    clinics = data["clinics"]
    vehicles = data["vehicles"]
    permits = data["permits"]
    facilities = data["facilities"]
    chargers = data["chargers"]
    nodes = data["nodes"]
    windows = data["windows"]
    windows_by_facility = data["windows_by_facility"]
    facility_rules = data["facility_rules"]
    incompatible_pairs = data["incompatible_pairs"]

    required_columns = [
        "row_type",
        "vehicle_id",
        "sequence",
        "node_id",
        "clinic_id",
        "facility_id",
        "charger_id",
        "charge_kwh",
    ]
    violations: List[str] = []
    if not rows:
        return {
            "violations": ["Solution CSV is empty"],
            "objective_estimate": math.inf,
            "open_facilities": [],
            "facility_totals": {},
            "window_usage": {},
            "slacks": {},
            "row_count": 0,
        }
    if list(rows[0].keys()) != required_columns:
        violations.append("CSV header does not match required schema")

    per_vehicle_rows: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["vehicle_id"] not in vehicles:
            violations.append(f"Unknown vehicle_id {row['vehicle_id']}")
            continue
        if row["node_id"] not in nodes:
            violations.append(f"Unknown node_id {row['node_id']}")
            continue
        per_vehicle_rows[row["vehicle_id"]].append(row)

    picked = set()
    facility_total = defaultdict(float)
    facility_red = defaultdict(float)
    facility_yellow = defaultdict(float)
    window_count = defaultdict(int)
    window_kg = defaultdict(float)
    station_sessions = defaultdict(int)
    station_kwh = defaultdict(float)
    objective = 0.0
    slacks = {
        "battery_kwh": math.inf,
        "shift_min": math.inf,
        "latest_service_start_min": math.inf,
        "vehicle_total_capacity_kg": math.inf,
        "vehicle_red_capacity_kg": math.inf,
        "vehicle_yellow_capacity_kg": math.inf,
        "red_onboard_min": math.inf,
        "yellow_onboard_min": math.inf,
        "facility_daily_capacity_kg": math.inf,
        "facility_red_capacity_kg": math.inf,
        "facility_yellow_capacity_kg": math.inf,
        "facility_min_throughput_kg": math.inf,
        "facility_window_count": math.inf,
        "facility_window_kg": math.inf,
        "charger_sessions": math.inf,
        "charger_kwh": math.inf,
        "max_open_facilities": math.inf,
        "isolation_pickup_margin": math.inf,
    }

    for vehicle_id, vehicle_rows in per_vehicle_rows.items():
        try:
            vehicle_rows.sort(key=lambda row: int(row["sequence"]))
        except ValueError:
            violations.append(f"Non-integer sequence on vehicle {vehicle_id}")
            continue
        expected_sequence = 1
        for row in vehicle_rows:
            if int(row["sequence"]) != expected_sequence:
                violations.append(f"Non-consecutive sequence on vehicle {vehicle_id}")
                break
            expected_sequence += 1
        if vehicle_rows[0]["row_type"] != "start":
            violations.append(f"Vehicle {vehicle_id} does not start with a start row")
        if vehicle_rows[-1]["row_type"] != "end":
            violations.append(f"Vehicle {vehicle_id} does not end with an end row")

        time = as_float(vehicles[vehicle_id]["shift_start_min"])
        battery = as_float(vehicles[vehicle_id]["start_battery_kwh"])
        node = vehicles[vehicle_id]["start_node"]
        red_kg = 0.0
        yellow_kg = 0.0
        onboard_pick_time: Dict[str, float] = {}
        isolation_count = 0
        used = False

        for row in vehicle_rows:
            row_type = row["row_type"]
            if row_type == "start":
                if row["node_id"] != vehicles[vehicle_id]["start_node"]:
                    violations.append(f"Vehicle {vehicle_id} start node mismatch")
                if any(row[field] for field in ("clinic_id", "facility_id", "charger_id", "charge_kwh")):
                    violations.append(f"Start row has nonblank extra fields for {vehicle_id}")
                continue

            next_node = row["node_id"]
            time, battery, arc_cost = advance_arc(node, next_node, time, battery, red_kg, yellow_kg, data)
            if battery < -FLOAT_TOL:
                violations.append(f"Vehicle {vehicle_id} battery negative")
            slacks["battery_kwh"] = min(slacks["battery_kwh"], battery)
            objective += arc_cost
            node = next_node

            if row_type == "pickup":
                used = True
                clinic_id = row["clinic_id"]
                if not clinic_id or row["facility_id"] or row["charger_id"] or row["charge_kwh"]:
                    violations.append(f"Pickup row field mismatch on vehicle {vehicle_id}")
                if clinic_id not in clinics:
                    violations.append(f"Unknown clinic_id {clinic_id}")
                    continue
                if row["node_id"] != clinics[clinic_id]["node_id"]:
                    violations.append(f"Pickup node mismatch for {clinic_id}")

                clinic = clinics[clinic_id]
                earliest = as_float(clinic["earliest_min"])
                latest = as_float(clinic["latest_min"])
                if time < earliest:
                    objective += wait_risk_cost(earliest - time, red_kg, yellow_kg, data)
                    time = earliest
                slacks["latest_service_start_min"] = min(slacks["latest_service_start_min"], latest - time)
                if time > latest + FLOAT_TOL:
                    violations.append(f"Late service at {clinic_id}")
                service_min = as_float(clinic["service_min"])
                objective += wait_risk_cost(service_min, red_kg, yellow_kg, data)
                time += service_min

                priority = clinic["priority_class"]
                if int(permits[vehicle_id][f"allow_{priority}"]) != 1:
                    violations.append(f"Vehicle {vehicle_id} lacks permit for {clinic_id}")
                if priority == "isolation":
                    isolation_count += 1
                    slacks["isolation_pickup_margin"] = min(
                        slacks["isolation_pickup_margin"],
                        int(permits[vehicle_id]["max_isolation_pickups"]) - isolation_count,
                    )
                    if isolation_count > int(permits[vehicle_id]["max_isolation_pickups"]):
                        violations.append(f"Vehicle {vehicle_id} exceeds isolation cap")

                for other_clinic in onboard_pick_time:
                    if tuple(sorted((clinic_id, other_clinic))) in incompatible_pairs:
                        violations.append(f"Incompatible pair onboard: {clinic_id}, {other_clinic}")

                red_kg += as_float(clinic["red_kg"])
                yellow_kg += as_float(clinic["yellow_kg"])
                slacks["vehicle_total_capacity_kg"] = min(
                    slacks["vehicle_total_capacity_kg"],
                    as_float(vehicles[vehicle_id]["capacity_kg"]) - (red_kg + yellow_kg),
                )
                slacks["vehicle_red_capacity_kg"] = min(
                    slacks["vehicle_red_capacity_kg"],
                    as_float(vehicles[vehicle_id]["red_compartment_kg"]) - red_kg,
                )
                slacks["vehicle_yellow_capacity_kg"] = min(
                    slacks["vehicle_yellow_capacity_kg"],
                    as_float(vehicles[vehicle_id]["yellow_compartment_kg"]) - yellow_kg,
                )
                if red_kg + yellow_kg > as_float(vehicles[vehicle_id]["capacity_kg"]) + FLOAT_TOL:
                    violations.append(f"Vehicle {vehicle_id} exceeds total capacity")
                if red_kg > as_float(vehicles[vehicle_id]["red_compartment_kg"]) + FLOAT_TOL:
                    violations.append(f"Vehicle {vehicle_id} exceeds red compartment")
                if yellow_kg > as_float(vehicles[vehicle_id]["yellow_compartment_kg"]) + FLOAT_TOL:
                    violations.append(f"Vehicle {vehicle_id} exceeds yellow compartment")

                if clinic_id in picked:
                    violations.append(f"Clinic {clinic_id} picked up more than once")
                picked.add(clinic_id)
                onboard_pick_time[clinic_id] = time

            elif row_type == "unload":
                used = True
                facility_id = row["facility_id"]
                if not facility_id or row["clinic_id"] or row["charger_id"] or row["charge_kwh"]:
                    violations.append(f"Unload row field mismatch on vehicle {vehicle_id}")
                if facility_id not in facilities:
                    violations.append(f"Unknown facility_id {facility_id}")
                    continue
                if row["node_id"] != facilities[facility_id]["node_id"]:
                    violations.append(f"Unload node mismatch for {facility_id}")
                if red_kg + yellow_kg <= FLOAT_TOL:
                    violations.append(f"Empty unload at {facility_id}")

                unload_time = time
                matched_window = None
                for window in windows_by_facility[facility_id]:
                    start_min = as_float(window["start_min"])
                    end_min = as_float(window["end_min"])
                    if unload_time < start_min:
                        objective += wait_risk_cost(start_min - unload_time, red_kg, yellow_kg, data)
                        unload_time = start_min
                    if unload_time <= end_min + FLOAT_TOL:
                        matched_window = window
                        break
                if matched_window is None:
                    violations.append(f"No feasible receiving window for {facility_id}")
                if unload_time > as_float(facilities[facility_id]["latest_receive_min"]) + FLOAT_TOL:
                    violations.append(f"Unload after latest receiving time at {facility_id}")

                rules = facility_rules[facility_id]
                if red_kg > FLOAT_TOL and rules["red"]["accepted"] < 0.5:
                    violations.append(f"Facility {facility_id} rejects red waste")
                if yellow_kg > FLOAT_TOL and rules["yellow"]["accepted"] < 0.5:
                    violations.append(f"Facility {facility_id} rejects yellow waste")

                for clinic_id, pick_time in onboard_pick_time.items():
                    clinic = clinics[clinic_id]
                    slacks["red_onboard_min"] = min(
                        slacks["red_onboard_min"],
                        as_float(clinic["max_red_onboard_min"]) - (unload_time - pick_time),
                    )
                    slacks["yellow_onboard_min"] = min(
                        slacks["yellow_onboard_min"],
                        as_float(clinic["max_yellow_onboard_min"]) - (unload_time - pick_time),
                    )
                    if unload_time - pick_time > as_float(clinic["max_red_onboard_min"]) + FLOAT_TOL:
                        violations.append(f"Red onboard limit violated for {clinic_id}")
                    if unload_time - pick_time > as_float(clinic["max_yellow_onboard_min"]) + FLOAT_TOL:
                        violations.append(f"Yellow onboard limit violated for {clinic_id}")

                facility_total[facility_id] += red_kg + yellow_kg
                facility_red[facility_id] += red_kg
                facility_yellow[facility_id] += yellow_kg
                if matched_window is not None:
                    key = (facility_id, matched_window["window_id"])
                    window_count[key] += 1
                    window_kg[key] += red_kg + yellow_kg
                objective += rules["red"]["treatment_cost_per_kg"] * red_kg
                objective += rules["yellow"]["treatment_cost_per_kg"] * yellow_kg

                time = unload_time
                red_kg = 0.0
                yellow_kg = 0.0
                onboard_pick_time.clear()

            elif row_type == "charge":
                used = True
                charger_id = row["charger_id"]
                if not charger_id or row["clinic_id"] or row["facility_id"]:
                    violations.append(f"Charge row field mismatch on vehicle {vehicle_id}")
                if charger_id not in chargers:
                    violations.append(f"Unknown charger_id {charger_id}")
                    continue
                if row["node_id"] != chargers[charger_id]["node_id"]:
                    violations.append(f"Charge node mismatch for {charger_id}")
                try:
                    charge_kwh = float(row["charge_kwh"])
                except ValueError:
                    violations.append(f"Invalid charge amount on vehicle {vehicle_id}")
                    continue
                if charge_kwh <= FLOAT_TOL:
                    violations.append(f"Nonpositive charge on vehicle {vehicle_id}")
                charger = chargers[charger_id]
                charge_duration = as_float(charger["queue_delay_min"]) + charge_kwh / as_float(charger["rate_kwh_per_min"])
                objective += wait_risk_cost(charge_duration, red_kg, yellow_kg, data)
                time += charge_duration
                battery += charge_kwh
                slacks["battery_kwh"] = min(slacks["battery_kwh"], as_float(vehicles[vehicle_id]["battery_kwh"]) - battery)
                if battery > as_float(vehicles[vehicle_id]["battery_kwh"]) + FLOAT_TOL:
                    violations.append(f"Vehicle {vehicle_id} exceeds battery cap")
                objective += charge_kwh * as_float(charger["energy_price"])
                station_sessions[charger_id] += 1
                station_kwh[charger_id] += charge_kwh

            elif row_type == "end":
                if row["node_id"] != vehicles[vehicle_id]["end_node"]:
                    violations.append(f"Vehicle {vehicle_id} end node mismatch")
                if any(row[field] for field in ("clinic_id", "facility_id", "charger_id", "charge_kwh")):
                    violations.append(f"End row has nonblank extra fields for {vehicle_id}")
                if red_kg + yellow_kg > FLOAT_TOL:
                    violations.append(f"Vehicle {vehicle_id} ends loaded")
            else:
                violations.append(f"Unknown row_type {row_type}")

        if used:
            objective += as_float(vehicles[vehicle_id]["fixed_cost"])
        slacks["shift_min"] = min(slacks["shift_min"], as_float(vehicles[vehicle_id]["shift_end_min"]) - time)
        if time > as_float(vehicles[vehicle_id]["shift_end_min"]) + FLOAT_TOL:
            violations.append(f"Vehicle {vehicle_id} exceeds shift end")

    if picked != set(clinics):
        missing = sorted(set(clinics) - picked)
        extra = sorted(picked - set(clinics))
        if missing:
            violations.append(f"Missing pickups: {','.join(missing)}")
        if extra:
            violations.append(f"Unexpected pickups: {','.join(extra)}")

    open_facilities = [facility_id for facility_id, kg in facility_total.items() if kg > FLOAT_TOL]
    slacks["max_open_facilities"] = min(
        slacks["max_open_facilities"],
        int(data["config"]["max_open_facilities"]) - len(open_facilities),
    )
    if len(open_facilities) > int(data["config"]["max_open_facilities"]):
        violations.append("Exceeded maximum number of open facilities")

    for facility_id in open_facilities:
        slacks["facility_min_throughput_kg"] = min(
            slacks["facility_min_throughput_kg"],
            facility_total[facility_id] - data["config"]["minimum_facility_throughput_kg"],
        )
        if facility_total[facility_id] + FLOAT_TOL < data["config"]["minimum_facility_throughput_kg"]:
            violations.append(f"Facility {facility_id} below minimum throughput")
        facility = facilities[facility_id]
        slacks["facility_daily_capacity_kg"] = min(
            slacks["facility_daily_capacity_kg"],
            as_float(facility["daily_capacity_kg"]) - facility_total[facility_id],
        )
        slacks["facility_red_capacity_kg"] = min(
            slacks["facility_red_capacity_kg"],
            as_float(facility["red_capacity_kg"]) - facility_red[facility_id],
        )
        slacks["facility_yellow_capacity_kg"] = min(
            slacks["facility_yellow_capacity_kg"],
            as_float(facility["yellow_capacity_kg"]) - facility_yellow[facility_id],
        )
        if facility_total[facility_id] > as_float(facility["daily_capacity_kg"]) + FLOAT_TOL:
            violations.append(f"Facility {facility_id} exceeds daily capacity")
        if facility_red[facility_id] > as_float(facility["red_capacity_kg"]) + FLOAT_TOL:
            violations.append(f"Facility {facility_id} exceeds red capacity")
        if facility_yellow[facility_id] > as_float(facility["yellow_capacity_kg"]) + FLOAT_TOL:
            violations.append(f"Facility {facility_id} exceeds yellow capacity")
        objective += as_float(facility["open_cost"])

    for window in windows:
        key = (window["facility_id"], window["window_id"])
        slacks["facility_window_count"] = min(
            slacks["facility_window_count"],
            int(window["max_unloads"]) - window_count[key],
        )
        slacks["facility_window_kg"] = min(
            slacks["facility_window_kg"],
            as_float(window["max_kg"]) - window_kg[key],
        )
        if window_count[key] > int(window["max_unloads"]):
            violations.append(f"Window {key} exceeds unload-count cap")
        if window_kg[key] > as_float(window["max_kg"]) + FLOAT_TOL:
            violations.append(f"Window {key} exceeds kg cap")

    for charger_id, charger in chargers.items():
        slacks["charger_sessions"] = min(
            slacks["charger_sessions"],
            int(charger["session_limit"]) - station_sessions[charger_id],
        )
        slacks["charger_kwh"] = min(
            slacks["charger_kwh"],
            as_float(charger["daily_kwh_cap"]) - station_kwh[charger_id],
        )
        if station_sessions[charger_id] > int(charger["session_limit"]):
            violations.append(f"Charger {charger_id} exceeds session limit")
        if station_kwh[charger_id] > as_float(charger["daily_kwh_cap"]) + FLOAT_TOL:
            violations.append(f"Charger {charger_id} exceeds daily kWh cap")

    cleaned_slacks = {key: value for key, value in slacks.items() if math.isfinite(value)}

    return {
        "violations": violations,
        "objective_estimate": objective,
        "open_facilities": sorted(open_facilities),
        "facility_totals": {facility_id: facility_total[facility_id] for facility_id in sorted(facility_total)},
        "window_usage": {
            key: {"count": window_count[key], "kg": window_kg[key]}
            for key in sorted(window_count)
            if window_count[key] > 0
        },
        "slacks": cleaned_slacks,
        "row_count": len(rows),
    }


def write_solve_log(
    version: str,
    groups: Dict[str, List[Pattern]],
    selected_patterns: Dict[str, Pattern],
    master_stats: Dict[str, object],
    incumbent_audit: Dict[str, object],
    candidate_audit: Dict[str, object],
    final_audit: Dict[str, object],
    published_new_solution: bool,
) -> None:
    lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        "- `PYTHONPATH=/opt/or-deps python -c \"import pyscipopt; print(pyscipopt.__version__)\"`",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Environment",
        "",
        f"- `pyscipopt` version: `{version}`",
        f"- SCIP relative gap: `{REL_GAP}`",
        f"- SCIP time limit used: `{master_stats['time_limit']}` seconds",
        "",
        "## Candidate Sets",
        "",
    ]
    for group_id in sorted(groups):
        lines.append(f"- `{group_id}` patterns generated: `{len(groups[group_id])}`")

    lines.extend(
        [
            "",
            "## Incumbent Audit",
            "",
            f"- Incumbent rows on disk: `{incumbent_audit.get('row_count')}`",
            f"- Incumbent objective from independent audit: `{format_float(incumbent_audit['objective_estimate'])}`",
            f"- Incumbent audit violations: `{len(incumbent_audit['violations'])}`",
        ]
    )

    lines.extend(
        [
            "",
            "## Candidate Solve",
            "",
            f"- Status: `{master_stats.get('status')}`",
            f"- Incumbent available: `{master_stats.get('has_solution')}`",
        ]
    )
    if master_stats.get("objective") is not None:
        lines.append(f"- Master objective: `{format_float(master_stats['objective'])}`")
    if master_stats.get("best_bound") is not None:
        lines.append(f"- Best bound: `{format_float(master_stats['best_bound'])}`")
    if master_stats.get("gap") is not None:
        lines.append(f"- Reported gap: `{format_float(master_stats['gap'])}`")
    lines.append(f"- Candidate objective from independent audit: `{format_float(candidate_audit['objective_estimate'])}`")
    lines.append(f"- Candidate audit violations: `{len(candidate_audit['violations'])}`")
    lines.append(f"- Published candidate: `{published_new_solution}`")

    lines.extend(["", "## Selected Patterns", ""])
    for group_id in sorted(selected_patterns):
        pattern = selected_patterns[group_id]
        lines.append(f"- `{group_id}` -> `{pattern.pattern_id}` with objective `{format_float(pattern.objective)}`")
        for vehicle_id in sorted(pattern.vehicle_sequences):
            event_text = " -> ".join(
                f"{event_type}:{event_id}" for event_type, event_id in pattern.vehicle_sequences[vehicle_id]
            )
            lines.append(f"- Vehicle `{vehicle_id}` events: `{event_text}`")

    lines.extend(
        [
            "",
            "## Final Artifact Audit",
            "",
            f"- Violations found by independent audit: `{len(final_audit['violations'])}`",
            f"- Objective estimate from exported CSV: `{format_float(final_audit['objective_estimate'])}`",
            f"- Open facilities: `{', '.join(final_audit['open_facilities'])}`",
        ]
    )
    for facility_id, kg in final_audit["facility_totals"].items():
        lines.append(f"- Facility `{facility_id}` total kg: `{format_float(kg)}`")
    for key, stats in final_audit["window_usage"].items():
        lines.append(
            f"- Window `{key[0]}:{key[1]}` -> unloads `{stats['count']}`, kg `{format_float(stats['kg'])}`"
        )
    for slack_name, slack_value in sorted(final_audit.get("slacks", {}).items()):
        lines.append(f"- Tightest slack `{slack_name}`: `{format_float(slack_value)}`")

    if final_audit["violations"]:
        lines.extend(["", "## Violations", ""])
        for violation in final_audit["violations"]:
            lines.append(f"- {violation}")
    else:
        lines.extend(
            [
                "",
                "## Notes",
                "",
                "- The audit objective is recomputed directly from the exported CSV, including travel, treatment, fixed/open costs, curfew waiting, early waiting, service-time risk, and charging-time risk.",
                "- The performance phase expands the neighborhood with three-unload patterns for `EV1` and `EV2`, then solves an exact pattern master with the on-disk incumbent as the warm start.",
            ]
        )

    LOG_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    import pyscipopt

    data = load_data()
    incumbent_audit = audit_solution(SOLUTION_PATH, data) if SOLUTION_PATH.exists() else {
        "violations": ["Missing incumbent solution"],
        "objective_estimate": math.inf,
        "open_facilities": [],
        "facility_totals": {},
        "window_usage": {},
        "slacks": {},
        "row_count": 0,
    }
    groups = build_candidate_groups(data)
    incumbent = extract_incumbent_from_solution(groups, SOLUTION_PATH)
    if incumbent is None:
        incumbent = find_feasible_combination(groups, data)
    selected_patterns, master_stats = solve_master(groups, data, incumbent)

    rows = flatten_solution_rows(selected_patterns, data)
    write_solution(rows, TMP_SOLUTION_PATH)
    candidate_audit = audit_solution(TMP_SOLUTION_PATH, data)
    publish_candidate = (
        not candidate_audit["violations"]
        and (
            incumbent_audit["violations"]
            or candidate_audit["objective_estimate"] < incumbent_audit["objective_estimate"] - FLOAT_TOL
        )
    )
    if publish_candidate:
        os.replace(TMP_SOLUTION_PATH, SOLUTION_PATH)
    elif TMP_SOLUTION_PATH.exists():
        TMP_SOLUTION_PATH.unlink()

    final_audit = audit_solution(SOLUTION_PATH, data)
    if final_audit["violations"]:
        raise RuntimeError("Final exported solution failed independent audit")
    write_solve_log(
        pyscipopt.__version__,
        groups,
        selected_patterns,
        master_stats,
        incumbent_audit,
        candidate_audit,
        final_audit,
        publish_candidate,
    )


if __name__ == "__main__":
    main()
