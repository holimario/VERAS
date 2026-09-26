"""Independent public-data observers for high-risk benchmark constraints."""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from .common import EPS, as_float, optional_csv, read_csv, report, violation

def observe_online_liner(app: Path, artifact: Path) -> dict[str, Any]:
    data = app / "data"
    bookings = {row["booking_id"]: row for row in read_csv(data / "bookings.csv")}
    legs = {row["leg_id"]: row for row in read_csv(data / "vessel_legs.csv")}
    rules = {
        row["booking_id"]: {
            "max_transship_count": int(row["max_transship_count"]),
            "requires_direct": row["requires_direct"] == "1",
            "allowed_services": {
                value for value in row["allowed_services"].split("|") if value
            },
            "customs_group": row.get("customs_group", ""),
            "cold_chain_preclear": row.get("cold_chain_preclear", "") == "1",
        }
        for row in read_csv(data / "booking_path_rules.csv")
    }
    special_rules = {
        row["booking_id"]: row for row in optional_csv(data / "special_booking_rules.csv")
    }
    service_rules = optional_csv(data / "service_rules.csv")
    protected_slots = optional_csv(data / "protected_slots.csv")
    transfer_limits = optional_csv(data / "transfer_limits.csv")
    customs_windows = {
        (row["port_id"], int(row["day"]), row["customs_group"]): row
        for row in optional_csv(data / "customs_inspection_windows.csv")
    }
    ports = {row["port_id"]: row for row in optional_csv(data / "ports.csv")}
    port_day_limits = {
        (row["port_id"], int(row["day"])): row
        for row in optional_csv(data / "port_day_limits.csv")
    }
    lease_options = {
        (row["port_id"], int(row["day"]), row["container_type"]): row
        for row in optional_csv(data / "lease_options.csv")
    }
    rows = read_csv(artifact)
    decisions: dict[str, list[str]] = defaultdict(list)
    booking_legs: dict[str, list[dict[str, str]]] = defaultdict(list)
    empty_moves: list[dict[str, str]] = []
    leases: list[dict[str, str]] = []
    accepted_paths: dict[str, list[dict[str, str]]] = {}
    violations: list[dict[str, Any]] = []

    for row in rows:
        if row.get("row_type") == "booking_decision":
            decisions[row.get("item_id", "")].append(row.get("decision", ""))
        elif row.get("row_type") == "booking_leg":
            booking_legs[row.get("item_id", "")].append(row)
        elif row.get("row_type") == "empty_move":
            empty_moves.append(row)
        elif row.get("row_type") == "lease":
            leases.append(row)

    for booking_id, booking in bookings.items():
        values = decisions.get(booking_id, [])
        if len(values) != 1 or values[0] not in {"accept", "reject"}:
            violations.append(
                violation(
                    "booking-decision",
                    f"Booking {booking_id} must have exactly one accept/reject decision",
                    count=len(values),
                    values=values,
                )
            )
            continue
        path_rows = booking_legs.get(booking_id, [])
        if values[0] == "reject":
            if path_rows:
                violations.append(
                    violation(
                        "rejected-booking-path",
                        f"Rejected booking {booking_id} has booking-leg rows",
                        leg_ids=[row.get("leg_id") for row in path_rows],
                    )
                )
            continue
        if not path_rows:
            violations.append(
                violation(
                    "accepted-booking-path",
                    f"Accepted booking {booking_id} has no vessel path",
                )
            )
            continue

        path: list[dict[str, str]] = []
        for row in path_rows:
            leg_id = row.get("leg_id", "")
            if leg_id not in legs:
                violations.append(
                    violation(
                        "booking-leg-domain",
                        f"Booking {booking_id} uses unknown leg {leg_id}",
                    )
                )
                continue
            leg = legs[leg_id]
            path.append(leg)
            if abs(as_float(row.get("quantity_teu", "")) - as_float(booking["teu"])) > EPS:
                violations.append(
                    violation(
                        "booking-leg-quantity",
                        f"Booking {booking_id} leg {leg_id} quantity differs from booking TEU",
                        expected=as_float(booking["teu"]),
                        actual=as_float(row.get("quantity_teu", "")),
                    )
                )
            if leg["service"] not in rules[booking_id]["allowed_services"]:
                violations.append(
                    violation(
                        "service-loop-eligibility",
                        f"Booking {booking_id} uses disallowed service {leg['service']}",
                        leg_id=leg_id,
                    )
                )
        if len(path) != len(path_rows):
            continue
        accepted_paths[booking_id] = path

        chain_ok = (
            path[0]["from_port"] == booking["origin_port"]
            and path[-1]["to_port"] == booking["destination_port"]
        )
        for previous, current in zip(path, path[1:]):
            chain_ok = chain_ok and previous["to_port"] == current["from_port"]
            chain_ok = chain_ok and int(current["depart_day"]) >= int(
                previous["arrive_day"]
            )
        chain_ok = chain_ok and int(path[0]["depart_day"]) >= int(
            booking["earliest_load_day"]
        )
        chain_ok = chain_ok and int(path[-1]["arrive_day"]) <= int(
            booking["due_arrival_day"]
        )
        if not chain_ok:
            violations.append(
                violation(
                    "path-continuity-time",
                    f"Booking {booking_id} path is not a time-feasible origin-destination chain",
                    leg_ids=[leg["leg_id"] for leg in path],
                )
            )

        transshipments = max(0, len(path) - 1)
        limit = rules[booking_id]["max_transship_count"]
        if transshipments > limit:
            violations.append(
                violation(
                    "max-transshipments",
                    f"Booking {booking_id} has {transshipments} transshipments; limit is {limit}",
                    (transshipments - limit) / max(1, limit),
                    booking_id=booking_id,
                    leg_ids=[leg["leg_id"] for leg in path],
                    actual=transshipments,
                    limit=limit,
                )
            )
        if rules[booking_id]["requires_direct"] and len(path) != 1:
            violations.append(
                violation(
                    "direct-sailing",
                    f"Booking {booking_id} requires a direct one-leg sailing",
                    leg_ids=[leg["leg_id"] for leg in path],
                )
            )
        special = special_rules.get(booking_id)
        if special and special.get("max_legs"):
            max_legs = int(special["max_legs"])
            if len(path) > max_legs:
                violations.append(
                    violation(
                        "special-booking-max-legs",
                        f"Booking {booking_id} uses {len(path)} legs; special limit is {max_legs}",
                        (len(path) - max_legs) / max(1, max_legs),
                        booking_id=booking_id,
                        leg_ids=[leg["leg_id"] for leg in path],
                        actual=len(path),
                        limit=max_legs,
                    )
                )

    accepted = {
        booking_id
        for booking_id, values in decisions.items()
        if values == ["accept"] and booking_id in bookings
    }
    for booking_id, booking in bookings.items():
        if booking.get("medical_flag") == "1" and booking_id not in accepted:
            violations.append(
                violation(
                    "mandatory-medical-booking",
                    f"Medical booking {booking_id} must be accepted",
                    booking_id=booking_id,
                )
            )
    for booking_id, special in special_rules.items():
        if special.get("require_accept") == "1" and booking_id not in accepted:
            violations.append(
                violation(
                    "special-booking-acceptance",
                    f"Special rule {special.get('rule_id', '')} requires booking {booking_id} to be accepted",
                    booking_id=booking_id,
                    rule_id=special.get("rule_id", ""),
                )
            )

    acceptance_metrics: dict[str, dict[str, Any]] = {}
    for service_rule in service_rules:
        priority = service_rule["priority"]
        group = [
            booking_id
            for booking_id, booking in bookings.items()
            if booking.get("priority") == priority
        ]
        rate = as_float(service_rule["min_accept_rate"])
        required = math.ceil(rate * len(group) - EPS)
        actual = sum(booking_id in accepted for booking_id in group)
        acceptance_metrics[priority] = {
            "visible": len(group),
            "accepted": actual,
            "required": required,
        }
        if actual < required:
            violations.append(
                violation(
                    "priority-min-acceptance",
                    f"Priority {priority} accepts {actual} bookings; at least {required} are required",
                    (required - actual) / max(1, required),
                    priority=priority,
                    visible=len(group),
                    accepted=actual,
                    required=required,
                    min_accept_rate=rate,
                )
            )

    leg_priority_teu: dict[tuple[str, str], float] = defaultdict(float)
    loaded_teu: dict[str, float] = defaultdict(float)
    loaded_reefer: dict[str, float] = defaultdict(float)
    loaded_dg1: dict[str, float] = defaultdict(float)
    transfer_reefer: dict[tuple[str, int], float] = defaultdict(float)
    transfer_dg1: dict[tuple[str, int], float] = defaultdict(float)
    customs_load: dict[tuple[str, int, str], float] = defaultdict(float)
    medical_customs_load: dict[tuple[str, int, str], float] = defaultdict(float)
    loaded_departures: dict[tuple[str, int, str], float] = defaultdict(float)
    loaded_releases: dict[tuple[str, int, str], float] = defaultdict(float)
    for booking_id, path in accepted_paths.items():
        booking = bookings[booking_id]
        teu = as_float(booking["teu"])
        container_type = booking.get("container_type", "")
        for leg in path:
            leg_id = leg["leg_id"]
            loaded_teu[leg_id] += teu
            leg_priority_teu[(leg_id, booking.get("priority", ""))] += teu
            if booking.get("container_type") == "REEFER":
                loaded_reefer[leg_id] += teu
            if booking.get("hazmat_class") == "DG1":
                loaded_dg1[leg_id] += teu
        for previous, current in zip(path, path[1:]):
            key = (previous["to_port"], int(current["depart_day"]))
            if booking.get("container_type") == "REEFER":
                transfer_reefer[key] += teu
            if booking.get("hazmat_class") == "DG1":
                transfer_dg1[key] += teu

        first_leg = path[0]
        final_leg = path[-1]
        loaded_departures[
            (first_leg["from_port"], int(first_leg["depart_day"]), container_type)
        ] += teu
        release_day = int(final_leg["arrive_day"]) + (
            1 if container_type == "REEFER" else 0
        )
        loaded_releases[(final_leg["to_port"], release_day, container_type)] += teu

        customs_group = rules[booking_id].get("customs_group", "")
        if customs_group and customs_windows:
            export_day = int(first_leg["depart_day"])
            if rules[booking_id].get("cold_chain_preclear"):
                export_day -= 1
            customs_events = [(first_leg["from_port"], export_day)]
            customs_events.extend(
                (leg["to_port"], int(leg["arrive_day"])) for leg in path
            )
            for port_id, day in customs_events:
                key = (port_id, day, customs_group)
                customs_load[key] += teu
                if (
                    booking.get("medical_flag") == "1"
                    and container_type == "REEFER"
                ):
                    medical_customs_load[key] += teu

    empty_teu: dict[str, float] = defaultdict(float)
    empty_reefer: dict[str, float] = defaultdict(float)
    empty_departures: dict[tuple[str, int, str], float] = defaultdict(float)
    empty_arrivals: dict[tuple[str, int, str], float] = defaultdict(float)
    for move in empty_moves:
        leg_id = move.get("leg_id", "")
        if leg_id not in legs:
            violations.append(
                violation("empty-move-domain", f"Empty move uses unknown leg {leg_id}")
            )
            continue
        leg = legs[leg_id]
        quantity = as_float(move.get("quantity_teu", ""))
        container_type = move.get("container_type", "")
        try:
            declared_day = int(move.get("day", ""))
        except (TypeError, ValueError):
            violations.append(
                violation(
                    "empty-move-departure-day",
                    f"Empty move on {leg_id} has a non-integer departure day",
                    leg_id=leg_id,
                    declared_day=move.get("day", ""),
                    required_day=int(leg["depart_day"]),
                )
            )
        else:
            required_day = int(leg["depart_day"])
            if declared_day != required_day:
                violations.append(
                    violation(
                        "empty-move-departure-day",
                        f"Empty move on {leg_id} declares day {declared_day}; the leg departs on day {required_day}",
                        abs(declared_day - required_day) / max(1, required_day),
                        leg_id=leg_id,
                        declared_day=declared_day,
                        required_day=required_day,
                    )
                )
        if quantity < -EPS:
            violations.append(
                violation("empty-move-quantity", "Empty-move quantity must be nonnegative")
            )
        empty_teu[leg_id] += quantity
        if container_type == "REEFER":
            empty_reefer[leg_id] += quantity
        if container_type not in {"STD", "REEFER"}:
            violations.append(
                violation(
                    "empty-move-container-type",
                    f"Empty move on {leg_id} uses unknown container type {container_type}",
                    leg_id=leg_id,
                    container_type=container_type,
                )
            )
            continue
        empty_departures[
            (leg["from_port"], int(leg["depart_day"]), container_type)
        ] += quantity
        empty_arrivals[
            (leg["to_port"], int(leg["arrive_day"]), container_type)
        ] += quantity

    lease_quantities: dict[tuple[str, int, str], float] = defaultdict(float)
    for lease in leases:
        port_id = lease.get("port_id", "")
        container_type = lease.get("container_type", "")
        try:
            day = int(lease.get("day", ""))
        except ValueError:
            violations.append(
                violation(
                    "lease-domain",
                    "Lease row has a non-integer day",
                    port_id=port_id,
                    day=lease.get("day", ""),
                    container_type=container_type,
                )
            )
            continue
        quantity = as_float(lease.get("quantity_teu", ""))
        key = (port_id, day, container_type)
        if key not in lease_options:
            violations.append(
                violation(
                    "lease-domain",
                    f"Lease uses an unavailable option at {port_id} day {day} for {container_type}",
                    port_id=port_id,
                    day=day,
                    container_type=container_type,
                )
            )
            continue
        if quantity < -EPS:
            violations.append(
                violation("lease-quantity", "Lease quantity must be nonnegative")
            )
        lease_quantities[key] += quantity

    for key, quantity in lease_quantities.items():
        limit = as_float(lease_options[key]["max_teu"])
        if quantity > limit + EPS:
            violations.append(
                violation(
                    "lease-capacity",
                    f"Lease volume at {key[0]} day {key[1]} for {key[2]} exceeds its cap",
                    (quantity - limit) / max(1.0, limit),
                    port_id=key[0],
                    day=key[1],
                    container_type=key[2],
                    actual=quantity,
                    limit=limit,
                )
            )

    for leg_id, leg in legs.items():
        for group, actual, limit in (
            (
                "vessel-teu-capacity",
                loaded_teu[leg_id] + empty_teu[leg_id],
                as_float(leg["teu_capacity"]),
            ),
            (
                "vessel-reefer-capacity",
                loaded_reefer[leg_id] + empty_reefer[leg_id],
                as_float(leg["reefer_plug_capacity"]),
            ),
            ("vessel-dg1-capacity", loaded_dg1[leg_id], as_float(leg["dg1_limit"])),
        ):
            if actual > limit + EPS:
                violations.append(
                    violation(
                        group,
                        f"Leg {leg_id} exceeds {group}",
                        (actual - limit) / max(1.0, limit),
                        leg_id=leg_id,
                        actual=actual,
                        limit=limit,
                    )
                )

    for slot in protected_slots:
        key = (slot["leg_id"], slot["priority"])
        actual = leg_priority_teu[key]
        required = as_float(slot["min_teu"])
        if actual < required - EPS:
            violations.append(
                violation(
                    "protected-slot-minimum",
                    f"Protected slot {slot.get('rule_id', '')} has {actual:g} TEU; at least {required:g} are required",
                    (required - actual) / max(1.0, required),
                    rule_id=slot.get("rule_id", ""),
                    leg_id=slot["leg_id"],
                    priority=slot["priority"],
                    actual=actual,
                    required=required,
                )
            )

    for limit_row in transfer_limits:
        key = (limit_row["port_id"], int(limit_row["day"]))
        for cargo, actual, column in (
            ("reefer", transfer_reefer[key], "max_reefer_transfer_teu"),
            ("DG1", transfer_dg1[key], "max_dg1_transfer_teu"),
        ):
            limit = as_float(limit_row[column])
            if actual > limit + EPS:
                violations.append(
                    violation(
                        "transfer-capacity",
                        f"{cargo} transfer volume at {key[0]} on day {key[1]} exceeds its cap",
                        (actual - limit) / max(1.0, limit),
                        rule_id=limit_row.get("rule_id", ""),
                        port_id=key[0],
                        day=key[1],
                        cargo=cargo,
                        actual=actual,
                        limit=limit,
                    )
                )

    if customs_windows:
        for key, actual in customs_load.items():
            window = customs_windows.get(key)
            if window is None:
                violations.append(
                    violation(
                        "customs-window-domain",
                        f"No customs window exists at {key[0]} day {key[1]} for {key[2]}",
                        port_id=key[0],
                        day=key[1],
                        customs_group=key[2],
                        actual=actual,
                    )
                )
                continue
            limit = as_float(window["max_teu"])
            if actual > limit + EPS:
                violations.append(
                    violation(
                        "customs-capacity",
                        f"Customs volume at {key[0]} day {key[1]} for {key[2]} exceeds its cap",
                        (actual - limit) / max(1.0, limit),
                        port_id=key[0],
                        day=key[1],
                        customs_group=key[2],
                        actual=actual,
                        limit=limit,
                    )
                )
            medical_actual = medical_customs_load[key]
            medical_limit = as_float(window.get("max_medical_reefer_teu", ""))
            if medical_actual > medical_limit + EPS:
                violations.append(
                    violation(
                        "medical-customs-capacity",
                        f"Medical reefer customs volume at {key[0]} day {key[1]} exceeds its cap",
                        (medical_actual - medical_limit) / max(1.0, medical_limit),
                        port_id=key[0],
                        day=key[1],
                        customs_group=key[2],
                        actual=medical_actual,
                        limit=medical_limit,
                    )
                )

    gate_out: dict[tuple[str, int], float] = defaultdict(float)
    gate_in: dict[tuple[str, int], float] = defaultdict(float)
    for (port_id, day, _container_type), quantity in loaded_departures.items():
        gate_out[(port_id, day)] += quantity
    for (port_id, day, _container_type), quantity in empty_departures.items():
        gate_out[(port_id, day)] += quantity
    for source in (loaded_releases, empty_arrivals, lease_quantities):
        for (port_id, day, _container_type), quantity in source.items():
            gate_in[(port_id, day)] += quantity

    def port_limit(port_id: str, day: int, field: str) -> float | None:
        if (port_id, day) in port_day_limits:
            return as_float(port_day_limits[(port_id, day)].get(field, ""))
        port = ports.get(port_id)
        if port is None:
            return None
        default_field = {
            "gate_out_limit": "gate_out_default",
            "gate_in_limit": "gate_in_default",
            "yard_capacity": "yard_capacity",
            "reefer_plug_capacity": "reefer_plug_capacity",
        }[field]
        return as_float(port.get(default_field, ""))

    if ports or port_day_limits:
        for direction, loads, field in (
            ("out", gate_out, "gate_out_limit"),
            ("in", gate_in, "gate_in_limit"),
        ):
            for (port_id, day), actual in loads.items():
                limit = port_limit(port_id, day, field)
                if limit is None:
                    violations.append(
                        violation(
                            "gate-domain",
                            f"No gate limit exists at {port_id} on day {day}",
                            port_id=port_id,
                            day=day,
                            direction=direction,
                        )
                    )
                elif actual > limit + EPS:
                    violations.append(
                        violation(
                            f"gate-{direction}-capacity",
                            f"Gate-{direction} volume at {port_id} on day {day} exceeds its cap",
                            (actual - limit) / max(1.0, limit),
                            port_id=port_id,
                            day=day,
                            actual=actual,
                            limit=limit,
                        )
                    )

    inventory_metrics: dict[str, float] = {}
    if ports:
        event_days = [
            day
            for source in (
                loaded_departures,
                loaded_releases,
                empty_departures,
                empty_arrivals,
                lease_quantities,
            )
            for (_port_id, day, _container_type) in source
        ]
        limit_days = [day for (_port_id, day) in port_day_limits]
        last_day = max(event_days + limit_days + [0])
        stock = {
            (port_id, "STD"): as_float(port.get("initial_std", ""))
            for port_id, port in ports.items()
        }
        stock.update(
            {
                (port_id, "REEFER"): as_float(port.get("initial_reefer", ""))
                for port_id, port in ports.items()
            }
        )
        for day in range(last_day + 1):
            for port_id in ports:
                for container_type in ("STD", "REEFER"):
                    key = (port_id, day, container_type)
                    available = (
                        stock[(port_id, container_type)]
                        + lease_quantities[key]
                        + loaded_releases[key]
                        + empty_arrivals[key]
                        - loaded_departures[key]
                        - empty_departures[key]
                    )
                    if available < -EPS:
                        violations.append(
                            violation(
                                "inventory-availability",
                                f"Empty {container_type} inventory at {port_id} day {day} is unavailable for departures",
                                -available
                                / max(
                                    1.0,
                                    loaded_departures[key] + empty_departures[key],
                                ),
                                port_id=port_id,
                                day=day,
                                container_type=container_type,
                                available=available,
                            )
                        )
                    ending = available
                    capacity_field = (
                        "reefer_plug_capacity"
                        if container_type == "REEFER"
                        else "yard_capacity"
                    )
                    capacity = port_limit(port_id, day, capacity_field)
                    if ending < -EPS:
                        violations.append(
                            violation(
                                "inventory-nonnegative",
                                f"End-of-day {container_type} inventory at {port_id} day {day} is negative",
                                -ending / max(1.0, abs(ending)),
                                port_id=port_id,
                                day=day,
                                container_type=container_type,
                                actual=ending,
                            )
                        )
                    if capacity is not None and ending > capacity + EPS:
                        violations.append(
                            violation(
                                "inventory-capacity",
                                f"End-of-day {container_type} inventory at {port_id} day {day} exceeds its cap",
                                (ending - capacity) / max(1.0, capacity),
                                port_id=port_id,
                                day=day,
                                container_type=container_type,
                                actual=ending,
                                limit=capacity,
                            )
                        )
                    stock[(port_id, container_type)] = ending
                    inventory_metrics[f"{port_id}|{day}|{container_type}"] = ending

    unknown_decisions = sorted(set(decisions) - set(bookings))
    if unknown_decisions:
        violations.append(
            violation(
                "booking-domain",
                "Plan contains decisions for unknown bookings",
                booking_ids=unknown_decisions,
            )
        )
    return report(
        "online-liner-event-observer-v3",
        [
            "booking-decision",
            "accepted/rejected-path-linkage",
            "path-continuity-time",
            "booking-leg-quantity",
            "service-loop-eligibility",
            "max-transshipments",
            "direct-sailing",
            "mandatory/special-booking-rules",
            "priority-min-acceptance",
            "protected-slot-minimum",
            "vessel-capacity",
            "transfer-capacity",
            "customs-capacity/medical-sublimit",
            "gate-in/gate-out-capacity",
            "lease-capacity",
            "empty-move-departure-day",
            "empty-inventory-conservation/capacity",
        ],
        violations,
        {
            "visible_bookings": len(bookings),
            "accepted_bookings": sum(values == ["accept"] for values in decisions.values()),
            "acceptance_by_priority": acceptance_metrics,
            "customs_loads": {
                "|".join(map(str, key)): value
                for key, value in sorted(customs_load.items())
            },
            "gate_out": {
                f"{port_id}|{day}": value
                for (port_id, day), value in sorted(gate_out.items())
            },
            "gate_in": {
                f"{port_id}|{day}": value
                for (port_id, day), value in sorted(gate_in.items())
            },
            "ending_inventory": inventory_metrics,
        },
    )
