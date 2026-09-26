import csv
import json
import os
import time
from collections import defaultdict
from heapq import heappop, heappush
from pathlib import Path

from pyscipopt import Model, quicksum


BASE_DIR = Path("/app")
DATA_DIR = BASE_DIR / "data"
SUBMISSIONS_DIR = BASE_DIR / "submissions"
SOLUTION_PATH = SUBMISSIONS_DIR / "solution.json"
LOG_PATH = SUBMISSIONS_DIR / "solve_log.md"
MODEL_PATH = SUBMISSIONS_DIR / "model.md"
TOL = 1e-6
ROUND_DIGITS = 8


def read_csv(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def f8(value):
    return round(float(value), ROUND_DIGITS)


def safe_float(value, default=0.0):
    if value is None or value == "":
        return default
    return float(value)


def safe_gap(model):
    try:
        return float(model.getGap())
    except Exception:
        return None


def safe_dualbound(model):
    try:
        return float(model.getDualbound())
    except Exception:
        return None


def load_solution_from_disk():
    if not SOLUTION_PATH.exists():
        return None
    return json.loads(SOLUTION_PATH.read_text())


def load_data():
    config = json.loads((DATA_DIR / "config.json").read_text())
    stations_rows = read_csv(DATA_DIR / "stations.csv")
    stations_rows.sort(key=lambda row: int(row["order_index"]))
    station_ids = [row["station_id"] for row in stations_rows]
    station_index = {sid: idx for idx, sid in enumerate(station_ids)}
    station_data = {
        row["station_id"]: {
            "name": row["station_name"],
            "order_index": int(row["order_index"]),
            "can_turnback": int(row["can_turnback"]),
            "platform_count": int(row["platform_count"]),
            "min_platform_gap": float(row["min_platform_gap_minutes"]),
        }
        for row in stations_rows
    }

    segments_rows = read_csv(DATA_DIR / "segments.csv")
    segment_data = {}
    pair_to_segment = {}
    for row in segments_rows:
        seg_id = row["segment_id"]
        segment_data[seg_id] = {
            "from": row["from_station"],
            "to": row["to_station"],
            "run": float(row["run_minutes"]),
            "conflict_group": row["conflict_group"],
            "headway": float(row["min_headway_minutes"]),
        }
        pair_to_segment[frozenset((row["from_station"], row["to_station"]))] = seg_id

    disruption_rows = read_csv(DATA_DIR / "disruption_windows.csv")
    disruptions_by_segment = defaultdict(list)
    for row in disruption_rows:
        disruptions_by_segment[row["segment_id"]].append(
            {
                "id": row["segment_id"],
                "start": float(row["start_minute"]),
                "end": float(row["end_minute"]),
                "reason": row["reason"],
            }
        )

    throat_rows = read_csv(DATA_DIR / "station_throats.csv")
    throat_gap = {
        (row["station_id"], row["side"]): float(row["min_route_gap_minutes"])
        for row in throat_rows
    }

    rolling_rows = read_csv(DATA_DIR / "rolling_stock.csv")
    rolling = {
        row["rolling_stock_id"]: {
            "initial_station": row["initial_station"],
            "ready_time": float(row["ready_time"]),
            "capacity": float(row["capacity"]),
            "min_turn": float(row["min_turn_minutes"]),
        }
        for row in rolling_rows
    }

    crew_rows = read_csv(DATA_DIR / "crews.csv")
    crews = {}
    for row in crew_rows:
        crews[row["crew_id"]] = {
            "initial_station": row["initial_station"],
            "ready_time": float(row["ready_time"]),
            "qualified": set(row["qualified_directions"].split("|")),
            "min_rest": float(row["min_rest_minutes"]),
            "max_duty": float(row["max_single_train_duty_minutes"]),
        }

    train_rows = read_csv(DATA_DIR / "trains.csv")
    trains = {}
    for row in train_rows:
        train_id = row["train_id"]
        origin = row["origin"]
        destination = row["destination"]
        direction = row["direction"]
        oi = station_index[origin]
        di = station_index[destination]
        if direction == "east":
            route = station_ids[oi : di + 1]
        else:
            route = list(reversed(station_ids[di : oi + 1]))
        route_pos = {sid: idx for idx, sid in enumerate(route)}
        dwell = safe_float(row["min_dwell_minutes"], config["default_min_dwell_minutes"])
        planned_arrival = {}
        planned_departure = {}
        tcur = float(row["planned_departure"])
        planned_arrival[route[0]] = tcur
        planned_departure[route[0]] = tcur
        segment_sequence = []
        planned_entry = {}
        for idx in range(len(route) - 1):
            u = route[idx]
            v = route[idx + 1]
            seg_id = pair_to_segment[frozenset((u, v))]
            segment_sequence.append(seg_id)
            planned_entry[seg_id] = planned_departure[u]
            tcur = planned_departure[u] + segment_data[seg_id]["run"]
            planned_arrival[v] = tcur
            planned_departure[v] = tcur if idx + 1 == len(route) - 1 else tcur + dwell

        feasible_endpoints = [destination]
        if int(row["allow_short_turn"]):
            for sid in route[1:-1]:
                if station_data[sid]["can_turnback"]:
                    feasible_endpoints.append(sid)
        ordered_endpoints = [sid for sid in route if sid in set(feasible_endpoints)]

        trains[train_id] = {
            "origin": origin,
            "destination": destination,
            "direction": direction,
            "planned_departure": float(row["planned_departure"]),
            "min_dwell": dwell,
            "priority": float(row["priority"]),
            "demand": float(row["demand"]),
            "preferred_unit": row["preferred_unit"],
            "preferred_crew": row["preferred_crew"],
            "allow_cancel": int(row["allow_cancel"]),
            "allow_short_turn": int(row["allow_short_turn"]),
            "route": route,
            "route_pos": route_pos,
            "planned_arrival": planned_arrival,
            "planned_departure_events": planned_departure,
            "segment_sequence": segment_sequence,
            "planned_entry": planned_entry,
            "feasible_endpoints": ordered_endpoints,
        }

    connections_rows = read_csv(DATA_DIR / "passenger_connections.csv")
    connections = []
    for row in connections_rows:
        connections.append(
            {
                "id": row["connection_id"],
                "from_train": row["from_train"],
                "to_train": row["to_train"],
                "station_id": row["station_id"],
                "min_transfer": float(row["min_transfer_minutes"]),
                "passengers": float(row["passengers"]),
                "importance": row["importance"],
            }
        )

    control_notes = read_csv(DATA_DIR / "control_notes.csv")

    return {
        "config": config,
        "stations": station_data,
        "station_ids": station_ids,
        "station_index": station_index,
        "segments": segment_data,
        "pair_to_segment": pair_to_segment,
        "disruptions_by_segment": disruptions_by_segment,
        "throat_gap": throat_gap,
        "rolling": rolling,
        "crews": crews,
        "trains": trains,
        "connections": connections,
        "control_notes": control_notes,
        "horizon": float(config["planning_horizon_minutes"]),
    }


def is_station_before(train, left, right):
    return train["route_pos"][left] < train["route_pos"][right]


def segment_entry_exit(train, data, seg_id):
    seg = data["segments"][seg_id]
    if train["direction"] == "east":
        return seg["from"], seg["to"]
    return seg["to"], seg["from"]


def build_throat_events(train_id, train, variables, data):
    events_by_resource = defaultdict(list)
    origin = train["origin"]
    direction = train["direction"]
    if direction == "east":
        origin_side = "east"
        arrival_side = "west"
        departure_side = "east"
    else:
        origin_side = "west"
        arrival_side = "east"
        departure_side = "west"

    if (origin, origin_side) in data["throat_gap"]:
        events_by_resource[(origin, origin_side)].append(
            {
                "id": f"{train_id}|{origin}|dep|{origin_side}",
                "time": variables["dep"][(train_id, origin)],
                "active": variables["active"][train_id],
            }
        )

    for station in train["route"][1:]:
        if (station, arrival_side) in data["throat_gap"]:
            events_by_resource[(station, arrival_side)].append(
                {
                    "id": f"{train_id}|{station}|arr|{arrival_side}",
                    "time": variables["arr"][(train_id, station)],
                    "active": variables["visit"][(train_id, station)],
                }
            )
        if (station, departure_side) in data["throat_gap"]:
            events_by_resource[(station, departure_side)].append(
                {
                    "id": f"{train_id}|{station}|dep|{departure_side}",
                    "time": variables["dep"][(train_id, station)],
                    "active": variables["cont"][(train_id, station)],
                }
            )
    return events_by_resource


def build_model(data, time_limit):
    horizon = data["horizon"]
    model = Model("railway_recovery")
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit)
    model.setParam("display/verblevel", 0)

    vars_ = {
        "final": {},
        "cancel": {},
        "active": {},
        "run": {},
        "short": {},
        "visit": {},
        "cont": {},
        "use": {},
        "arr": {},
        "dep": {},
        "finish": {},
        "delay": {},
        "before_window": {},
        "seg_order": {},
        "throat_order": {},
        "miss": {},
        "rs_assign": {},
        "rs_start": {},
        "rs_arc": {},
        "crew_assign": {},
        "crew_start": {},
        "crew_arc": {},
    }

    for train_id, train in data["trains"].items():
        vars_["cancel"][train_id] = model.addVar(vtype="B", name=f"cancel[{train_id}]")
        vars_["active"][train_id] = model.addVar(vtype="B", name=f"active[{train_id}]")
        vars_["run"][train_id] = model.addVar(vtype="B", name=f"run[{train_id}]")
        vars_["short"][train_id] = model.addVar(vtype="B", name=f"short[{train_id}]")
        vars_["finish"][train_id] = model.addVar(lb=0.0, ub=horizon, name=f"finish[{train_id}]")
        vars_["delay"][train_id] = model.addVar(lb=0.0, ub=horizon, name=f"delay[{train_id}]")

        endpoint_set = set(train["feasible_endpoints"])
        for endpoint in train["feasible_endpoints"]:
            vars_["final"][(train_id, endpoint)] = model.addVar(vtype="B", name=f"final[{train_id},{endpoint}]")

        model.addCons(
            quicksum(vars_["final"][(train_id, endpoint)] for endpoint in train["feasible_endpoints"]) + vars_["cancel"][train_id]
            == 1,
            name=f"one_disposition[{train_id}]",
        )
        model.addCons(
            vars_["active"][train_id]
            == quicksum(vars_["final"][(train_id, endpoint)] for endpoint in train["feasible_endpoints"]),
            name=f"active_def[{train_id}]",
        )
        model.addCons(
            vars_["run"][train_id] == vars_["final"][(train_id, train["destination"])],
            name=f"run_def[{train_id}]",
        )
        short_sum = quicksum(
            vars_["final"][(train_id, endpoint)]
            for endpoint in train["feasible_endpoints"]
            if endpoint != train["destination"]
        )
        model.addCons(vars_["short"][train_id] == short_sum, name=f"short_def[{train_id}]")
        if not train["allow_cancel"]:
            model.addCons(vars_["cancel"][train_id] == 0, name=f"cancel_forbidden[{train_id}]")
        if not train["allow_short_turn"]:
            model.addCons(vars_["short"][train_id] == 0, name=f"short_forbidden[{train_id}]")

        for station in train["route"]:
            vars_["visit"][(train_id, station)] = model.addVar(vtype="B", name=f"visit[{train_id},{station}]")
            vars_["cont"][(train_id, station)] = model.addVar(vtype="B", name=f"cont[{train_id},{station}]")
            vars_["arr"][(train_id, station)] = model.addVar(lb=0.0, ub=horizon, name=f"arr[{train_id},{station}]")
            vars_["dep"][(train_id, station)] = model.addVar(lb=0.0, ub=horizon, name=f"dep[{train_id},{station}]")

            visit_expr = quicksum(
                vars_["final"][(train_id, endpoint)]
                for endpoint in train["feasible_endpoints"]
                if train["route_pos"][station] <= train["route_pos"][endpoint]
            )
            cont_expr = quicksum(
                vars_["final"][(train_id, endpoint)]
                for endpoint in train["feasible_endpoints"]
                if train["route_pos"][station] < train["route_pos"][endpoint]
            )
            model.addCons(vars_["visit"][(train_id, station)] == visit_expr, name=f"visit_def[{train_id},{station}]")
            model.addCons(vars_["cont"][(train_id, station)] == cont_expr, name=f"cont_def[{train_id},{station}]")
            model.addCons(vars_["arr"][(train_id, station)] <= horizon * vars_["visit"][(train_id, station)], name=f"arr_act[{train_id},{station}]")
            model.addCons(vars_["dep"][(train_id, station)] <= horizon * vars_["visit"][(train_id, station)], name=f"dep_act[{train_id},{station}]")

        origin = train["origin"]
        model.addCons(vars_["arr"][(train_id, origin)] == vars_["dep"][(train_id, origin)], name=f"origin_equal[{train_id}]")
        model.addCons(
            vars_["dep"][(train_id, origin)] >= train["planned_departure"] * vars_["active"][train_id],
            name=f"origin_lb[{train_id}]",
        )

        for station in train["route"][1:]:
            model.addCons(vars_["dep"][(train_id, station)] >= vars_["arr"][(train_id, station)], name=f"nondecreasing[{train_id},{station}]")
            model.addCons(
                vars_["dep"][(train_id, station)] >= vars_["arr"][(train_id, station)] + train["min_dwell"] * vars_["cont"][(train_id, station)],
                name=f"dwell_lb[{train_id},{station}]",
            )
            model.addCons(
                vars_["dep"][(train_id, station)] <= vars_["arr"][(train_id, station)] + horizon * vars_["cont"][(train_id, station)],
                name=f"dwell_ub[{train_id},{station}]",
            )

        for idx, seg_id in enumerate(train["segment_sequence"]):
            entry_station = train["route"][idx]
            exit_station = train["route"][idx + 1]
            vars_["use"][(train_id, seg_id)] = model.addVar(vtype="B", name=f"use[{train_id},{seg_id}]")
            use_expr = quicksum(
                vars_["final"][(train_id, endpoint)]
                for endpoint in train["feasible_endpoints"]
                if train["route_pos"][endpoint] >= idx + 1
            )
            model.addCons(vars_["use"][(train_id, seg_id)] == use_expr, name=f"use_def[{train_id},{seg_id}]")
            run_minutes = data["segments"][seg_id]["run"]
            model.addCons(
                vars_["arr"][(train_id, exit_station)] >= vars_["dep"][(train_id, entry_station)] + run_minutes - horizon * (1 - vars_["use"][(train_id, seg_id)]),
                name=f"run_lb[{train_id},{seg_id}]",
            )
            model.addCons(
                vars_["arr"][(train_id, exit_station)] <= vars_["dep"][(train_id, entry_station)] + run_minutes + horizon * (1 - vars_["use"][(train_id, seg_id)]),
                name=f"run_ub[{train_id},{seg_id}]",
            )

            for w_idx, window in enumerate(data["disruptions_by_segment"].get(seg_id, [])):
                before = model.addVar(vtype="B", name=f"before_window[{train_id},{seg_id},{w_idx}]")
                vars_["before_window"][(train_id, seg_id, w_idx)] = before
                model.addCons(
                    vars_["arr"][(train_id, exit_station)] <= window["start"] + horizon * (1 - before) + horizon * (1 - vars_["use"][(train_id, seg_id)]),
                    name=f"window_before[{train_id},{seg_id},{w_idx}]",
                )
                model.addCons(
                    vars_["dep"][(train_id, entry_station)] >= window["end"] - horizon * before - horizon * (1 - vars_["use"][(train_id, seg_id)]),
                    name=f"window_after[{train_id},{seg_id},{w_idx}]",
                )

        for endpoint in train["feasible_endpoints"]:
            model.addCons(
                vars_["finish"][train_id] >= vars_["arr"][(train_id, endpoint)] - horizon * (1 - vars_["final"][(train_id, endpoint)]),
                name=f"finish_lb[{train_id},{endpoint}]",
            )
            model.addCons(
                vars_["finish"][train_id] <= vars_["arr"][(train_id, endpoint)] + horizon * (1 - vars_["final"][(train_id, endpoint)]),
                name=f"finish_ub[{train_id},{endpoint}]",
            )

        planned_terminal = quicksum(
            train["planned_arrival"][endpoint] * vars_["final"][(train_id, endpoint)]
            for endpoint in train["feasible_endpoints"]
        )
        model.addCons(vars_["delay"][train_id] >= vars_["finish"][train_id] - planned_terminal, name=f"delay_def[{train_id}]")
        model.addCons(vars_["delay"][train_id] <= horizon * vars_["active"][train_id], name=f"delay_act[{train_id}]")

    for connection in data["connections"]:
        cid = connection["id"]
        from_train = connection["from_train"]
        to_train = connection["to_train"]
        station = connection["station_id"]
        vars_["miss"][cid] = model.addVar(vtype="B", name=f"miss[{cid}]")
        model.addCons(vars_["miss"][cid] <= vars_["visit"][(from_train, station)], name=f"miss_from[{cid}]")
        model.addCons(vars_["miss"][cid] <= vars_["visit"][(to_train, station)], name=f"miss_to[{cid}]")
        model.addCons(
            vars_["dep"][(to_train, station)]
            >= vars_["arr"][(from_train, station)]
            + connection["min_transfer"]
            - horizon * vars_["miss"][cid]
            - horizon * (2 - vars_["visit"][(from_train, station)] - vars_["visit"][(to_train, station)]),
            name=f"connection_hold[{cid}]",
        )

    for seg_id, seg in data["segments"].items():
        east_trains = []
        west_trains = []
        for train_id, train in data["trains"].items():
            if (train_id, seg_id) not in vars_["use"]:
                continue
            entry_station, _ = segment_entry_exit(train, data, seg_id)
            if train["direction"] == "east":
                east_trains.append((train["planned_entry"][seg_id], train_id, entry_station))
            else:
                west_trains.append((train["planned_entry"][seg_id], train_id, entry_station))
        east_trains.sort()
        west_trains.sort()

        for idx in range(len(east_trains)):
            for jdx in range(idx + 1, len(east_trains)):
                _, t1, entry1 = east_trains[idx]
                _, t2, entry2 = east_trains[jdx]
                model.addCons(
                    vars_["dep"][(t2, entry2)]
                    >= vars_["dep"][(t1, entry1)]
                    + seg["headway"]
                    - horizon * (2 - vars_["use"][(t1, seg_id)] - vars_["use"][(t2, seg_id)]),
                    name=f"same_dir_hw_e[{t1},{t2},{seg_id}]",
                )
        for idx in range(len(west_trains)):
            for jdx in range(idx + 1, len(west_trains)):
                _, t1, entry1 = west_trains[idx]
                _, t2, entry2 = west_trains[jdx]
                model.addCons(
                    vars_["dep"][(t2, entry2)]
                    >= vars_["dep"][(t1, entry1)]
                    + seg["headway"]
                    - horizon * (2 - vars_["use"][(t1, seg_id)] - vars_["use"][(t2, seg_id)]),
                    name=f"same_dir_hw_w[{t1},{t2},{seg_id}]",
                )

        for _, east_id, east_entry in east_trains:
            for _, west_id, west_entry in west_trains:
                order = model.addVar(vtype="B", name=f"seg_order[{east_id},{west_id},{seg_id}]")
                vars_["seg_order"][(east_id, west_id, seg_id)] = order
                model.addCons(
                    vars_["dep"][(west_id, west_entry)]
                    >= vars_["dep"][(east_id, east_entry)]
                    + seg["headway"]
                    - horizon * (1 - order)
                    - horizon * (2 - vars_["use"][(east_id, seg_id)] - vars_["use"][(west_id, seg_id)]),
                    name=f"opp_hw_1[{east_id},{west_id},{seg_id}]",
                )
                model.addCons(
                    vars_["dep"][(east_id, east_entry)]
                    >= vars_["dep"][(west_id, west_entry)]
                    + seg["headway"]
                    - horizon * order
                    - horizon * (2 - vars_["use"][(east_id, seg_id)] - vars_["use"][(west_id, seg_id)]),
                    name=f"opp_hw_2[{east_id},{west_id},{seg_id}]",
                )

    events_by_resource = defaultdict(list)
    for train_id, train in data["trains"].items():
        local_events = build_throat_events(train_id, train, vars_, data)
        for resource, events in local_events.items():
            events_by_resource[resource].extend(events)

    for resource, events in events_by_resource.items():
        gap = data["throat_gap"][resource]
        for i in range(len(events)):
            for j in range(i + 1, len(events)):
                e1 = events[i]
                e2 = events[j]
                order = model.addVar(vtype="B", name=f"throat_order[{e1['id']}|{e2['id']}]")
                vars_["throat_order"][(e1["id"], e2["id"])] = order
                model.addCons(
                    e2["time"] >= e1["time"] + gap - horizon * (1 - order) - horizon * (2 - e1["active"] - e2["active"]),
                    name=f"throat_1[{e1['id']}|{e2['id']}]",
                )
                model.addCons(
                    e1["time"] >= e2["time"] + gap - horizon * order - horizon * (2 - e1["active"] - e2["active"]),
                    name=f"throat_2[{e1['id']}|{e2['id']}]",
                )

    for train_id, train in data["trains"].items():
        eligible_units = [
            unit_id for unit_id, unit in data["rolling"].items() if unit["capacity"] + TOL >= train["demand"]
        ]
        for unit_id in eligible_units:
            vars_["rs_assign"][(unit_id, train_id)] = model.addVar(vtype="B", name=f"rs_assign[{unit_id},{train_id}]")
            if data["rolling"][unit_id]["initial_station"] == train["origin"]:
                vars_["rs_start"][(unit_id, train_id)] = model.addVar(vtype="B", name=f"rs_start[{unit_id},{train_id}]")

        model.addCons(
            quicksum(vars_["rs_assign"][(unit_id, train_id)] for unit_id in eligible_units) == vars_["active"][train_id],
            name=f"rs_cover[{train_id}]",
        )

    for unit_id, unit in data["rolling"].items():
        starts = []
        in_arcs = defaultdict(list)
        out_arcs = defaultdict(list)
        for train_id, train in data["trains"].items():
            if (unit_id, train_id) not in vars_["rs_assign"]:
                continue
            if (unit_id, train_id) in vars_["rs_start"]:
                starts.append(vars_["rs_start"][(unit_id, train_id)])
                model.addCons(
                    vars_["dep"][(train_id, train["origin"])]
                    >= unit["ready_time"] - horizon * (1 - vars_["rs_start"][(unit_id, train_id)]),
                    name=f"rs_ready[{unit_id},{train_id}]",
                )
            for next_id, next_train in data["trains"].items():
                if train_id == next_id:
                    continue
                if (unit_id, next_id) not in vars_["rs_assign"]:
                    continue
                if next_train["origin"] not in train["feasible_endpoints"]:
                    continue
                arc_key = (unit_id, train_id, next_id)
                vars_["rs_arc"][arc_key] = model.addVar(vtype="B", name=f"rs_arc[{unit_id},{train_id},{next_id}]")
                in_arcs[next_id].append(vars_["rs_arc"][arc_key])
                out_arcs[train_id].append(vars_["rs_arc"][arc_key])
                model.addCons(
                    vars_["rs_arc"][arc_key] <= vars_["final"][(train_id, next_train["origin"])],
                    name=f"rs_end_match[{unit_id},{train_id},{next_id}]",
                )
                model.addCons(
                    vars_["dep"][(next_id, next_train["origin"])]
                    >= vars_["arr"][(train_id, next_train["origin"])]
                    + unit["min_turn"]
                    - horizon * (1 - vars_["rs_arc"][arc_key]),
                    name=f"rs_chain[{unit_id},{train_id},{next_id}]",
                )

        if starts:
            model.addCons(quicksum(starts) <= 1, name=f"rs_single_start[{unit_id}]")
        for train_id in data["trains"]:
            if (unit_id, train_id) not in vars_["rs_assign"]:
                continue
            start_var = vars_["rs_start"].get((unit_id, train_id), 0)
            model.addCons(
                start_var + quicksum(in_arcs[train_id]) == vars_["rs_assign"][(unit_id, train_id)],
                name=f"rs_flow_in[{unit_id},{train_id}]",
            )
            model.addCons(
                quicksum(out_arcs[train_id]) <= vars_["rs_assign"][(unit_id, train_id)],
                name=f"rs_flow_out[{unit_id},{train_id}]",
            )

    for train_id, train in data["trains"].items():
        eligible_crews = [
            crew_id
            for crew_id, crew in data["crews"].items()
            if train["direction"] in crew["qualified"]
        ]
        for crew_id in eligible_crews:
            vars_["crew_assign"][(crew_id, train_id)] = model.addVar(vtype="B", name=f"crew_assign[{crew_id},{train_id}]")
            if data["crews"][crew_id]["initial_station"] == train["origin"]:
                vars_["crew_start"][(crew_id, train_id)] = model.addVar(vtype="B", name=f"crew_start[{crew_id},{train_id}]")

        model.addCons(
            quicksum(vars_["crew_assign"][(crew_id, train_id)] for crew_id in eligible_crews) == vars_["active"][train_id],
            name=f"crew_cover[{train_id}]",
        )

    for crew_id, crew in data["crews"].items():
        starts = []
        in_arcs = defaultdict(list)
        out_arcs = defaultdict(list)
        for train_id, train in data["trains"].items():
            if (crew_id, train_id) not in vars_["crew_assign"]:
                continue
            model.addCons(
                vars_["finish"][train_id] - vars_["dep"][(train_id, train["origin"])]
                <= crew["max_duty"] + horizon * (1 - vars_["crew_assign"][(crew_id, train_id)]),
                name=f"crew_duty[{crew_id},{train_id}]",
            )
            if (crew_id, train_id) in vars_["crew_start"]:
                starts.append(vars_["crew_start"][(crew_id, train_id)])
                model.addCons(
                    vars_["dep"][(train_id, train["origin"])]
                    >= crew["ready_time"] - horizon * (1 - vars_["crew_start"][(crew_id, train_id)]),
                    name=f"crew_ready[{crew_id},{train_id}]",
                )
            for next_id, next_train in data["trains"].items():
                if train_id == next_id:
                    continue
                if (crew_id, next_id) not in vars_["crew_assign"]:
                    continue
                if next_train["origin"] not in train["feasible_endpoints"]:
                    continue
                arc_key = (crew_id, train_id, next_id)
                vars_["crew_arc"][arc_key] = model.addVar(vtype="B", name=f"crew_arc[{crew_id},{train_id},{next_id}]")
                in_arcs[next_id].append(vars_["crew_arc"][arc_key])
                out_arcs[train_id].append(vars_["crew_arc"][arc_key])
                model.addCons(
                    vars_["crew_arc"][arc_key] <= vars_["final"][(train_id, next_train["origin"])],
                    name=f"crew_end_match[{crew_id},{train_id},{next_id}]",
                )
                model.addCons(
                    vars_["dep"][(next_id, next_train["origin"])]
                    >= vars_["arr"][(train_id, next_train["origin"])]
                    + crew["min_rest"]
                    - horizon * (1 - vars_["crew_arc"][arc_key]),
                    name=f"crew_chain[{crew_id},{train_id},{next_id}]",
                )

        if starts:
            model.addCons(quicksum(starts) <= 1, name=f"crew_single_start[{crew_id}]")
        for train_id in data["trains"]:
            if (crew_id, train_id) not in vars_["crew_assign"]:
                continue
            start_var = vars_["crew_start"].get((crew_id, train_id), 0)
            model.addCons(
                start_var + quicksum(in_arcs[train_id]) == vars_["crew_assign"][(crew_id, train_id)],
                name=f"crew_flow_in[{crew_id},{train_id}]",
            )
            model.addCons(
                quicksum(out_arcs[train_id]) <= vars_["crew_assign"][(crew_id, train_id)],
                name=f"crew_flow_out[{crew_id},{train_id}]",
            )

    config = data["config"]
    objective = quicksum(config["delay_cost_per_priority_minute"] * data["trains"][train_id]["priority"] * vars_["delay"][train_id] for train_id in data["trains"])
    objective += quicksum(config["cancellation_cost_per_passenger"] * data["trains"][train_id]["demand"] * vars_["cancel"][train_id] for train_id in data["trains"])
    objective += quicksum(config["short_turn_cost_per_passenger"] * data["trains"][train_id]["demand"] * vars_["short"][train_id] for train_id in data["trains"])
    objective += quicksum(config["missed_connection_cost_per_passenger"] * conn["passengers"] * vars_["miss"][conn["id"]] for conn in data["connections"])

    for train_id, train in data["trains"].items():
        preferred_unit = train["preferred_unit"]
        preferred_crew = train["preferred_crew"]
        preferred_unit_var = vars_["rs_assign"].get((preferred_unit, train_id), None)
        preferred_crew_var = vars_["crew_assign"].get((preferred_crew, train_id), None)
        if preferred_unit_var is None:
            objective += config["rolling_stock_swap_cost"] * vars_["active"][train_id]
        else:
            objective += config["rolling_stock_swap_cost"] * (vars_["active"][train_id] - preferred_unit_var)
        if preferred_crew_var is None:
            objective += config["crew_swap_cost"] * vars_["active"][train_id]
        else:
            objective += config["crew_swap_cost"] * (vars_["active"][train_id] - preferred_crew_var)

    model.setObjective(objective, "minimize")
    return model, vars_


def build_fallback_solution(data):
    trains_out = []
    for train_id in sorted(data["trains"]):
        trains_out.append(
            {
                "train_id": train_id,
                "status": "cancel",
                "rolling_stock_id": "",
                "crew_id": "",
                "events": [],
            }
        )
    return {"trains": trains_out}


def extract_solution(data, model, vars_):
    best = model.getBestSol()
    trains_out = []
    for train_id, train in sorted(data["trains"].items()):
        if model.getSolVal(best, vars_["cancel"][train_id]) > 0.5:
            trains_out.append(
                {
                    "train_id": train_id,
                    "status": "cancel",
                    "rolling_stock_id": "",
                    "crew_id": "",
                    "events": [],
                }
            )
            continue

        chosen_endpoint = None
        for endpoint in train["feasible_endpoints"]:
            if model.getSolVal(best, vars_["final"][(train_id, endpoint)]) > 0.5:
                chosen_endpoint = endpoint
                break
        if chosen_endpoint is None:
            raise RuntimeError(f"No endpoint selected for {train_id}")

        if chosen_endpoint == train["destination"]:
            status = "run"
        else:
            status = "short_turn"

        chosen_unit = None
        for unit_id in data["rolling"]:
            var = vars_["rs_assign"].get((unit_id, train_id))
            if var is not None and model.getSolVal(best, var) > 0.5:
                chosen_unit = unit_id
                break
        chosen_crew = None
        for crew_id in data["crews"]:
            var = vars_["crew_assign"].get((crew_id, train_id))
            if var is not None and model.getSolVal(best, var) > 0.5:
                chosen_crew = crew_id
                break
        if chosen_unit is None or chosen_crew is None:
            raise RuntimeError(f"Missing unit or crew for operated train {train_id}")

        events = []
        for station in train["route"]:
            if train["route_pos"][station] > train["route_pos"][chosen_endpoint]:
                break
            events.append(
                {
                    "station_id": station,
                    "arrival": f8(model.getSolVal(best, vars_["arr"][(train_id, station)])),
                    "departure": f8(model.getSolVal(best, vars_["dep"][(train_id, station)])),
                }
            )

        trains_out.append(
            {
                "train_id": train_id,
                "status": status,
                "rolling_stock_id": chosen_unit,
                "crew_id": chosen_crew,
                "events": events,
            }
        )
    return {"trains": trains_out}


def build_throat_events_from_solution(train_id, train, train_obj, data):
    events_by_id = {}
    if train_obj["status"] == "cancel":
        return events_by_id

    origin = train["origin"]
    direction = train["direction"]
    event_lookup = {event["station_id"]: event for event in train_obj["events"]}
    endpoint = train_obj["events"][-1]["station_id"]

    if direction == "east":
        origin_side = "east"
        arrival_side = "west"
        departure_side = "east"
    else:
        origin_side = "west"
        arrival_side = "east"
        departure_side = "west"

    if (origin, origin_side) in data["throat_gap"]:
        events_by_id[f"{train_id}|{origin}|dep|{origin_side}"] = {
            "time": float(event_lookup[origin]["departure"]),
            "active": 1,
        }

    for station in train["route"][1:]:
        if station not in event_lookup:
            break
        event = event_lookup[station]
        if (station, arrival_side) in data["throat_gap"]:
            events_by_id[f"{train_id}|{station}|arr|{arrival_side}"] = {
                "time": float(event["arrival"]),
                "active": 1,
            }
        if station != endpoint and (station, departure_side) in data["throat_gap"]:
            events_by_id[f"{train_id}|{station}|dep|{departure_side}"] = {
                "time": float(event["departure"]),
                "active": 1,
            }
    return events_by_id


def apply_solution_as_mip_start(model, vars_, data, solution):
    sol = model.createPartialSol()
    trains_by_id = {item["train_id"]: item for item in solution["trains"]}

    for group in [
        "final",
        "cancel",
        "active",
        "run",
        "short",
        "visit",
        "cont",
        "use",
        "before_window",
        "seg_order",
        "throat_order",
        "miss",
        "rs_assign",
        "rs_start",
        "rs_arc",
        "crew_assign",
        "crew_start",
        "crew_arc",
    ]:
        for var in vars_[group].values():
            model.setSolVal(sol, var, 0.0)

    for group in ["arr", "dep", "finish", "delay"]:
        for var in vars_[group].values():
            model.setSolVal(sol, var, 0.0)

    for train_id, train in data["trains"].items():
        train_obj = trains_by_id[train_id]
        status = train_obj["status"]
        if status == "cancel":
            model.setSolVal(sol, vars_["cancel"][train_id], 1.0)
            continue

        endpoint = train_obj["events"][-1]["station_id"]
        event_lookup = {event["station_id"]: event for event in train_obj["events"]}
        model.setSolVal(sol, vars_["active"][train_id], 1.0)
        model.setSolVal(sol, vars_["final"][(train_id, endpoint)], 1.0)
        if status == "run":
            model.setSolVal(sol, vars_["run"][train_id], 1.0)
        else:
            model.setSolVal(sol, vars_["short"][train_id], 1.0)

        finish = float(train_obj["events"][-1]["arrival"])
        delay = max(0.0, finish - train["planned_arrival"][endpoint])
        model.setSolVal(sol, vars_["finish"][train_id], finish)
        model.setSolVal(sol, vars_["delay"][train_id], delay)

        for station in train["route"]:
            if station in event_lookup:
                event = event_lookup[station]
                model.setSolVal(sol, vars_["visit"][(train_id, station)], 1.0)
                model.setSolVal(sol, vars_["arr"][(train_id, station)], float(event["arrival"]))
                model.setSolVal(sol, vars_["dep"][(train_id, station)], float(event["departure"]))
                if station != endpoint:
                    model.setSolVal(sol, vars_["cont"][(train_id, station)], 1.0)
            if station == endpoint:
                break

        for idx, seg_id in enumerate(train["segment_sequence"]):
            if idx < train["route_pos"][endpoint]:
                model.setSolVal(sol, vars_["use"][(train_id, seg_id)], 1.0)

        chosen_unit = train_obj["rolling_stock_id"]
        chosen_crew = train_obj["crew_id"]
        unit_var = vars_["rs_assign"].get((chosen_unit, train_id))
        crew_var = vars_["crew_assign"].get((chosen_crew, train_id))
        if unit_var is not None:
            model.setSolVal(sol, unit_var, 1.0)
        if crew_var is not None:
            model.setSolVal(sol, crew_var, 1.0)

    for conn in data["connections"]:
        from_obj = trains_by_id[conn["from_train"]]
        to_obj = trains_by_id[conn["to_train"]]
        from_event = find_event(from_obj, conn["station_id"])
        to_event = find_event(to_obj, conn["station_id"])
        if from_event is not None and to_event is not None:
            if float(to_event["departure"]) + TOL < float(from_event["arrival"]) + conn["min_transfer"]:
                model.setSolVal(sol, vars_["miss"][conn["id"]], 1.0)

    for unit_id in data["rolling"]:
        assigned = [
            train_id
            for train_id, train_obj in trains_by_id.items()
            if train_obj["status"] != "cancel" and train_obj["rolling_stock_id"] == unit_id
        ]
        assigned.sort(key=lambda tid: float(find_event(trains_by_id[tid], data["trains"][tid]["origin"])["departure"]))
        if assigned and (unit_id, assigned[0]) in vars_["rs_start"]:
            model.setSolVal(sol, vars_["rs_start"][(unit_id, assigned[0])], 1.0)
        for first, second in zip(assigned, assigned[1:]):
            arc_var = vars_["rs_arc"].get((unit_id, first, second))
            if arc_var is not None:
                model.setSolVal(sol, arc_var, 1.0)

    for crew_id in data["crews"]:
        assigned = [
            train_id
            for train_id, train_obj in trains_by_id.items()
            if train_obj["status"] != "cancel" and train_obj["crew_id"] == crew_id
        ]
        assigned.sort(key=lambda tid: float(find_event(trains_by_id[tid], data["trains"][tid]["origin"])["departure"]))
        if assigned and (crew_id, assigned[0]) in vars_["crew_start"]:
            model.setSolVal(sol, vars_["crew_start"][(crew_id, assigned[0])], 1.0)
        for first, second in zip(assigned, assigned[1:]):
            arc_var = vars_["crew_arc"].get((crew_id, first, second))
            if arc_var is not None:
                model.setSolVal(sol, arc_var, 1.0)

    for (train_id, seg_id, w_idx), var in vars_["before_window"].items():
        train_obj = trains_by_id[train_id]
        if train_obj["status"] == "cancel":
            continue
        train = data["trains"][train_id]
        entry_station, exit_station = segment_entry_exit(train, data, seg_id)
        entry_event = find_event(train_obj, entry_station)
        exit_event = find_event(train_obj, exit_station)
        if entry_event is None or exit_event is None:
            continue
        window = data["disruptions_by_segment"][seg_id][w_idx]
        before = 1.0 if float(exit_event["arrival"]) <= window["start"] + TOL else 0.0
        model.setSolVal(sol, var, before)

    for (east_id, west_id, seg_id), var in vars_["seg_order"].items():
        east_obj = trains_by_id[east_id]
        west_obj = trains_by_id[west_id]
        if east_obj["status"] == "cancel" or west_obj["status"] == "cancel":
            continue
        east_train = data["trains"][east_id]
        west_train = data["trains"][west_id]
        east_entry, _ = segment_entry_exit(east_train, data, seg_id)
        west_entry, _ = segment_entry_exit(west_train, data, seg_id)
        east_event = find_event(east_obj, east_entry)
        west_event = find_event(west_obj, west_entry)
        if east_event is None or west_event is None:
            continue
        order = 1.0 if float(east_event["departure"]) <= float(west_event["departure"]) else 0.0
        model.setSolVal(sol, var, order)

    throat_event_lookup = {}
    for train_id, train in data["trains"].items():
        throat_event_lookup.update(build_throat_events_from_solution(train_id, train, trains_by_id[train_id], data))
    for (event1, event2), var in vars_["throat_order"].items():
        if event1 not in throat_event_lookup or event2 not in throat_event_lookup:
            continue
        order = 1.0 if throat_event_lookup[event1]["time"] <= throat_event_lookup[event2]["time"] else 0.0
        model.setSolVal(sol, var, order)

    return model.addSol(sol, free=True)


def assign_platforms(data, solution):
    station_events = defaultdict(list)
    for train in solution["trains"]:
        for event in train["events"]:
            station_events[event["station_id"]].append(
                {
                    "train_id": train["train_id"],
                    "arrival": event["arrival"],
                    "departure": event["departure"],
                }
            )

    platform_map = {}
    for station, events in station_events.items():
        count = data["stations"][station]["platform_count"]
        gap = data["stations"][station]["min_platform_gap"]
        events.sort(key=lambda item: (item["arrival"], item["departure"], item["train_id"]))
        busy = []
        free_platforms = list(range(1, count + 1))
        for event in events:
            while busy and busy[0][0] <= event["arrival"] + TOL:
                _, platform = heappop(busy)
                heappush(free_platforms, platform)
            if not free_platforms:
                return False, f"Platform assignment infeasible at station {station}"
            platform = heappop(free_platforms)
            platform_map[(event["train_id"], station)] = platform
            heappush(busy, (event["departure"] + gap, platform))

    for train in solution["trains"]:
        for event in train["events"]:
            event["platform"] = platform_map[(train["train_id"], event["station_id"])]
    return True, None


def find_event(train_obj, station_id):
    for event in train_obj["events"]:
        if event["station_id"] == station_id:
            return event
    return None


def normalize_violation(raw, scale):
    return max(0.0, raw) / max(1.0, abs(scale))


def audit_solution(data, solution):
    errors = []
    max_residual = 0.0
    groups_violated = set()
    trains_by_id = {item["train_id"]: item for item in solution.get("trains", [])}
    expected_trains = set(data["trains"])
    if set(trains_by_id) != expected_trains:
        missing = sorted(expected_trains - set(trains_by_id))
        extra = sorted(set(trains_by_id) - expected_trains)
        if missing:
            errors.append(f"Missing trains in artifact: {missing}")
        if extra:
            errors.append(f"Unexpected trains in artifact: {extra}")
        groups_violated.add("schema")

    objective = {
        "delay": 0.0,
        "cancel": 0.0,
        "short_turn": 0.0,
        "missed_connection": 0.0,
        "rolling_swap": 0.0,
        "crew_swap": 0.0,
    }

    segment_usage = defaultdict(list)
    throat_usage = defaultdict(list)
    platform_usage = defaultdict(list)
    unit_trains = defaultdict(list)
    crew_trains = defaultdict(list)

    for train_id in sorted(expected_trains):
        train_def = data["trains"][train_id]
        train_obj = trains_by_id.get(train_id)
        if train_obj is None:
            continue
        status = train_obj.get("status")
        events = train_obj.get("events", [])
        if status not in {"run", "short_turn", "cancel"}:
            errors.append(f"{train_id}: invalid status {status}")
            groups_violated.add("schema")
            continue

        if status == "cancel":
            if events:
                errors.append(f"{train_id}: cancelled train must have no events")
                groups_violated.add("events")
            objective["cancel"] += data["config"]["cancellation_cost_per_passenger"] * train_def["demand"]
            continue

        endpoint = events[-1]["station_id"] if events else None
        if not events:
            errors.append(f"{train_id}: operated train has no events")
            groups_violated.add("events")
            continue

        if status == "run" and endpoint != train_def["destination"]:
            errors.append(f"{train_id}: run status but endpoint {endpoint} != destination")
            groups_violated.add("events")
        if status == "short_turn":
            if endpoint == train_def["destination"]:
                errors.append(f"{train_id}: short_turn status but endpoint is destination")
                groups_violated.add("events")
            if endpoint not in train_def["feasible_endpoints"] or endpoint == train_def["destination"]:
                errors.append(f"{train_id}: unauthorized short-turn endpoint {endpoint}")
                groups_violated.add("events")
            objective["short_turn"] += data["config"]["short_turn_cost_per_passenger"] * train_def["demand"]

        route_prefix = []
        for station in train_def["route"]:
            route_prefix.append(station)
            if station == endpoint:
                break
        event_stations = [event["station_id"] for event in events]
        if event_stations != route_prefix:
            errors.append(f"{train_id}: event stations {event_stations} do not match route prefix {route_prefix}")
            groups_violated.add("events")

        unit_id = train_obj.get("rolling_stock_id", "")
        crew_id = train_obj.get("crew_id", "")
        if unit_id not in data["rolling"]:
            errors.append(f"{train_id}: invalid rolling stock {unit_id}")
            groups_violated.add("rolling")
        else:
            if data["rolling"][unit_id]["capacity"] + TOL < train_def["demand"]:
                errors.append(f"{train_id}: rolling stock {unit_id} capacity too small")
                groups_violated.add("rolling")
            unit_trains[unit_id].append(train_id)
            if unit_id != train_def["preferred_unit"]:
                objective["rolling_swap"] += data["config"]["rolling_stock_swap_cost"]
        if crew_id not in data["crews"]:
            errors.append(f"{train_id}: invalid crew {crew_id}")
            groups_violated.add("crew")
        else:
            crew = data["crews"][crew_id]
            if train_def["direction"] not in crew["qualified"]:
                errors.append(f"{train_id}: crew {crew_id} not qualified")
                groups_violated.add("crew")
            crew_trains[crew_id].append(train_id)
            if crew_id != train_def["preferred_crew"]:
                objective["crew_swap"] += data["config"]["crew_swap_cost"]

        for idx, event in enumerate(events):
            arr = float(event["arrival"])
            dep = float(event["departure"])
            if arr < -TOL or dep < -TOL or arr > data["horizon"] + TOL or dep > data["horizon"] + TOL:
                errors.append(f"{train_id}: event time outside planning horizon at {event['station_id']}")
                groups_violated.add("timing")
                max_residual = max(
                    max_residual,
                    normalize_violation(max(-arr, -dep, arr - data["horizon"], dep - data["horizon"], 0.0), data["horizon"]),
                )
            if arr - dep > TOL:
                errors.append(f"{train_id}: arrival after departure at {event['station_id']}")
                groups_violated.add("timing")
                max_residual = max(max_residual, normalize_violation(arr - dep, 1.0))
            if idx == 0:
                if abs(arr - dep) > TOL:
                    errors.append(f"{train_id}: origin must satisfy arrival=departure")
                    groups_violated.add("timing")
                    max_residual = max(max_residual, normalize_violation(abs(arr - dep), 1.0))
                if train_def["planned_departure"] - dep > TOL:
                    errors.append(f"{train_id}: departs before published departure")
                    groups_violated.add("timing")
                    max_residual = max(max_residual, normalize_violation(train_def["planned_departure"] - dep, 1.0))
            if idx > 0:
                prev = events[idx - 1]
                seg_id = data["pair_to_segment"][frozenset((prev["station_id"], event["station_id"]))]
                run_minutes = data["segments"][seg_id]["run"]
                residual = abs(arr - (float(prev["departure"]) + run_minutes))
                if residual > TOL:
                    errors.append(f"{train_id}: wrong run time on {prev['station_id']}-{event['station_id']}")
                    groups_violated.add("segments")
                    max_residual = max(max_residual, normalize_violation(residual, run_minutes))
                for window in data["disruptions_by_segment"].get(seg_id, []):
                    overlap = not (arr <= window["start"] + TOL or float(prev["departure"]) >= window["end"] - TOL)
                    if overlap:
                        errors.append(f"{train_id}: disruption overlap on {seg_id}")
                        groups_violated.add("disruptions")
                        max_residual = max(max_residual, normalize_violation(1.0, 1.0))
                entry_time = float(prev["departure"])
                segment_usage[seg_id].append(
                    {
                        "train_id": train_id,
                        "direction": train_def["direction"],
                        "entry": entry_time,
                    }
                )

            if 0 < idx < len(events) - 1:
                dwell_needed = train_def["min_dwell"]
                dwell_residual = dwell_needed - (dep - arr)
                if dwell_residual > TOL:
                    errors.append(f"{train_id}: dwell too short at {event['station_id']}")
                    groups_violated.add("timing")
                    max_residual = max(max_residual, normalize_violation(dwell_residual, dwell_needed))

            station = event["station_id"]
            if "platform" not in event or not (1 <= int(event["platform"]) <= data["stations"][station]["platform_count"]):
                errors.append(f"{train_id}: invalid platform at {station}")
                groups_violated.add("platforms")
            else:
                platform_usage[(station, int(event["platform"]))].append(
                    {
                        "train_id": train_id,
                        "arrival": arr,
                        "departure": dep,
                    }
                )

            if train_def["direction"] == "east":
                origin_side = "east"
                arrival_side = "west"
                departure_side = "east"
            else:
                origin_side = "west"
                arrival_side = "east"
                departure_side = "west"
            if idx == 0 and (station, origin_side) in data["throat_gap"]:
                throat_usage[(station, origin_side)].append({"train_id": train_id, "time": dep})
            if idx > 0 and (station, arrival_side) in data["throat_gap"]:
                throat_usage[(station, arrival_side)].append({"train_id": train_id, "time": arr})
            if 0 < idx < len(events) - 1 and (station, departure_side) in data["throat_gap"]:
                throat_usage[(station, departure_side)].append({"train_id": train_id, "time": dep})

        final_arr = float(events[-1]["arrival"])
        planned_arr = train_def["planned_arrival"][endpoint]
        delay = max(0.0, final_arr - planned_arr)
        objective["delay"] += data["config"]["delay_cost_per_priority_minute"] * train_def["priority"] * delay

    for seg_id, usages in segment_usage.items():
        headway = data["segments"][seg_id]["headway"]
        usages.sort(key=lambda item: item["entry"])
        for i in range(len(usages)):
            for j in range(i + 1, len(usages)):
                residual = headway - abs(usages[j]["entry"] - usages[i]["entry"])
                if residual > TOL:
                    errors.append(f"Headway violation on {seg_id} between {usages[i]['train_id']} and {usages[j]['train_id']}")
                    groups_violated.add("segments")
                    max_residual = max(max_residual, normalize_violation(residual, headway))

    for resource, usages in throat_usage.items():
        gap = data["throat_gap"][resource]
        usages.sort(key=lambda item: item["time"])
        for i in range(len(usages)):
            for j in range(i + 1, len(usages)):
                residual = gap - abs(usages[j]["time"] - usages[i]["time"])
                if residual > TOL:
                    errors.append(f"Throat violation at {resource} between {usages[i]['train_id']} and {usages[j]['train_id']}")
                    groups_violated.add("throats")
                    max_residual = max(max_residual, normalize_violation(residual, gap))

    for (station, platform), usages in platform_usage.items():
        gap = data["stations"][station]["min_platform_gap"]
        usages.sort(key=lambda item: (item["arrival"], item["departure"]))
        for i in range(len(usages)):
            for j in range(i + 1, len(usages)):
                a = usages[i]
                b = usages[j]
                if not (b["arrival"] >= a["departure"] + gap - TOL or a["arrival"] >= b["departure"] + gap - TOL):
                    errors.append(f"Platform conflict at {station} platform {platform} between {a['train_id']} and {b['train_id']}")
                    groups_violated.add("platforms")
                    max_residual = max(max_residual, normalize_violation(1.0, 1.0))

    for unit_id, assigned_trains in unit_trains.items():
        unit = data["rolling"][unit_id]
        assigned_trains.sort(key=lambda tid: float(find_event(trains_by_id[tid], data["trains"][tid]["origin"])["departure"]))
        current_station = unit["initial_station"]
        current_ready = unit["ready_time"]
        for train_id in assigned_trains:
            train_obj = trains_by_id[train_id]
            origin_event = find_event(train_obj, data["trains"][train_id]["origin"])
            if current_station != data["trains"][train_id]["origin"] or float(origin_event["departure"]) + TOL < current_ready:
                errors.append(f"Rolling stock chain infeasible for {unit_id} at {train_id}")
                groups_violated.add("rolling")
                max_residual = max(max_residual, normalize_violation(max(current_ready - float(origin_event["departure"]), 1.0), 1.0))
                break
            endpoint_station = train_obj["events"][-1]["station_id"]
            current_station = endpoint_station
            current_ready = float(train_obj["events"][-1]["arrival"]) + unit["min_turn"]

    for crew_id, assigned_trains in crew_trains.items():
        crew = data["crews"][crew_id]
        assigned_trains.sort(key=lambda tid: float(find_event(trains_by_id[tid], data["trains"][tid]["origin"])["departure"]))
        current_station = crew["initial_station"]
        current_ready = crew["ready_time"]
        for train_id in assigned_trains:
            train_obj = trains_by_id[train_id]
            origin_event = find_event(train_obj, data["trains"][train_id]["origin"])
            end_event = train_obj["events"][-1]
            dep_time = float(origin_event["departure"])
            arr_time = float(end_event["arrival"])
            if current_station != data["trains"][train_id]["origin"] or dep_time + TOL < current_ready:
                errors.append(f"Crew chain infeasible for {crew_id} at {train_id}")
                groups_violated.add("crew")
                max_residual = max(max_residual, normalize_violation(max(current_ready - dep_time, 1.0), 1.0))
                break
            if arr_time - dep_time - crew["max_duty"] > TOL:
                errors.append(f"Crew duty violation for {crew_id} on {train_id}")
                groups_violated.add("crew")
                max_residual = max(max_residual, normalize_violation(arr_time - dep_time - crew["max_duty"], crew["max_duty"]))
            current_station = end_event["station_id"]
            current_ready = arr_time + crew["min_rest"]

    for conn in data["connections"]:
        from_obj = trains_by_id.get(conn["from_train"])
        to_obj = trains_by_id.get(conn["to_train"])
        if from_obj is None or to_obj is None:
            continue
        from_event = find_event(from_obj, conn["station_id"])
        to_event = find_event(to_obj, conn["station_id"])
        if from_event is not None and to_event is not None:
            if float(to_event["departure"]) + TOL < float(from_event["arrival"]) + conn["min_transfer"]:
                objective["missed_connection"] += data["config"]["missed_connection_cost_per_passenger"] * conn["passengers"]

    total_objective = sum(objective.values())
    return {
        "ok": not errors,
        "errors": errors,
        "theta": max_residual,
        "violated_groups": sorted(groups_violated),
        "objective": objective,
        "total_objective": total_objective,
        "incumbent_trains": len(solution.get("trains", [])),
    }


def write_solution(solution):
    SOLUTION_PATH.write_text(json.dumps(solution, indent=2))


def write_log(log_lines):
    LOG_PATH.write_text("\n".join(log_lines) + "\n")


def append_log_section(section_lines):
    if LOG_PATH.exists():
        existing = LOG_PATH.read_text().rstrip()
        if existing:
            content = existing + "\n\n" + "\n".join(section_lines) + "\n"
        else:
            content = "\n".join(section_lines) + "\n"
    else:
        content = "\n".join(section_lines) + "\n"
    LOG_PATH.write_text(content)


def main():
    start = time.time()
    data = load_data()
    env_limit = os.environ.get("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    if env_limit is not None:
        time_limit = min(300.0, float(env_limit))
    else:
        time_limit = 300.0
    incumbent_solution = load_solution_from_disk()
    incumbent_audit = None
    incumbent_objective = None
    incumbent_ok = False
    if incumbent_solution is not None:
        incumbent_audit = audit_solution(data, incumbent_solution)
        incumbent_objective = incumbent_audit["total_objective"]
        incumbent_ok = incumbent_audit["ok"]

    search_time_limit = min(time_limit, 120.0)
    solve_status = "not_run"
    best_obj = None
    dualbound = None
    gap = None
    nsols = 0
    mip_start_accepted = None
    candidate_solution = None
    candidate_audit = None
    candidate_objective = None
    candidate_platform_result = "not_run"
    error_message = None

    if incumbent_ok:
        try:
            model_obj, vars_ = build_model(data, search_time_limit)
            mip_start_accepted = apply_solution_as_mip_start(model_obj, vars_, data, incumbent_solution)
            model_obj.optimize()
            solve_status = str(model_obj.getStatus())
            nsols = int(model_obj.getNSols())
            best_obj = float(model_obj.getObjVal()) if nsols > 0 else None
            dualbound = safe_dualbound(model_obj)
            gap = safe_gap(model_obj)
            if nsols > 0:
                candidate_solution = extract_solution(data, model_obj, vars_)
                platform_ok, platform_error = assign_platforms(data, candidate_solution)
                candidate_platform_result = "ok" if platform_ok else platform_error
                if platform_ok:
                    candidate_audit = audit_solution(data, candidate_solution)
                    candidate_objective = candidate_audit["total_objective"]
        except Exception as exc:
            solve_status = "exception"
            error_message = str(exc)

    publish_candidate = (
        incumbent_ok
        and candidate_solution is not None
        and candidate_audit is not None
        and candidate_audit["ok"]
        and candidate_objective + 1e-8 < incumbent_objective
    )

    final_solution = candidate_solution if publish_candidate else incumbent_solution
    final_audit = candidate_audit if publish_candidate else incumbent_audit

    if publish_candidate:
        write_solution(candidate_solution)

    elapsed = time.time() - start
    log_lines = [
        "## Repair And Performance Phase",
        "",
        "### Commands",
        "",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "### Incumbent Audit From Disk",
        "",
        f"- Artifact reparsed from disk: `{incumbent_solution is not None}`",
        f"- Incumbent feasible after public-only audit: `{incumbent_ok}`",
        f"- Incumbent objective: `{incumbent_objective if incumbent_objective is not None else 'n/a'}`",
        f"- Incumbent theta: `{incumbent_audit['theta'] if incumbent_audit is not None else 'n/a'}`",
        f"- Incumbent violated groups: `{', '.join(incumbent_audit['violated_groups']) if incumbent_audit and incumbent_audit['violated_groups'] else 'none'}`",
        "",
        "### Guarded Improvement Solve",
        "",
        f"- Search time limit seconds: `{f8(search_time_limit)}`",
        f"- SCIP status: `{solve_status}`",
        f"- MIP start accepted: `{mip_start_accepted if mip_start_accepted is not None else 'n/a'}`",
        f"- SCIP incumbent count: `{nsols}`",
        f"- SCIP best objective: `{best_obj if best_obj is not None else 'n/a'}`",
        f"- SCIP dual bound: `{dualbound if dualbound is not None else 'n/a'}`",
        f"- SCIP relative gap: `{gap if gap is not None else 'n/a'}`",
        f"- Candidate platform assignment: `{candidate_platform_result}`",
        f"- Candidate objective: `{candidate_objective if candidate_objective is not None else 'n/a'}`",
        f"- Candidate feasible after public-only audit: `{candidate_audit['ok'] if candidate_audit is not None else 'False'}`",
        f"- Candidate theta: `{candidate_audit['theta'] if candidate_audit is not None else 'n/a'}`",
        f"- Candidate violated groups: `{', '.join(candidate_audit['violated_groups']) if candidate_audit and candidate_audit['violated_groups'] else 'none'}`",
        f"- Strict improvement found: `{publish_candidate}`",
        f"- Published artifact changed: `{publish_candidate}`",
        f"- Final kept objective: `{final_audit['total_objective'] if final_audit is not None else 'n/a'}`",
        f"- Elapsed seconds: `{f8(elapsed)}`",
    ]
    if error_message:
        log_lines.append(f"- Exception: `{error_message}`")
    if incumbent_audit and incumbent_audit["errors"]:
        log_lines.extend(["", "### Incumbent Audit Errors", ""])
        for err in incumbent_audit["errors"][:50]:
            log_lines.append(f"- {err}")
    if candidate_audit and candidate_audit["errors"]:
        log_lines.extend(["", "### Candidate Audit Errors", ""])
        for err in candidate_audit["errors"][:50]:
            log_lines.append(f"- {err}")

    append_log_section(log_lines)


if __name__ == "__main__":
    main()
