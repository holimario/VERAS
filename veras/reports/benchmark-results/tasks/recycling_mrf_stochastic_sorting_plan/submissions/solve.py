#!/usr/bin/env python3

import csv
import json
import math
import os
import tempfile
import time
from collections import defaultdict

from pyscipopt import Model, quicksum


BASE_DIR = "/app"
DATA_DIR = os.path.join(BASE_DIR, "data")
SUBMISSIONS_DIR = os.path.join(BASE_DIR, "submissions")
MODEL_PATH = os.path.join(SUBMISSIONS_DIR, "model.md")
SOLUTION_PATH = os.path.join(SUBMISSIONS_DIR, "solution.csv")
SOLVE_LOG_PATH = os.path.join(SUBMISSIONS_DIR, "solve_log.md")


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def tons_to_units_exact(tons, increment):
    units = int(round(tons / increment))
    if abs(units * increment - tons) > 1e-8:
        raise ValueError(f"{tons} is not aligned to increment {increment}")
    return units


def ceil_units(tons, increment):
    return int(math.ceil(tons / increment - 1e-9))


def floor_units(tons, increment):
    return int(math.floor(tons / increment + 1e-9))


def fmt(value):
    if abs(value) <= 5e-10:
        value = 0.0
    return f"{value:.8f}"


def build_data():
    lines_raw = read_csv(os.path.join(DATA_DIR, "sorting_lines.csv"))
    rec_raw = read_csv(os.path.join(DATA_DIR, "line_commodity_recovery.csv"))
    batches_raw = read_csv(os.path.join(DATA_DIR, "inbound_batches.csv"))
    comm_raw = read_csv(os.path.join(DATA_DIR, "commodities.csv"))
    scenarios_raw = read_csv(os.path.join(DATA_DIR, "scenarios.csv"))
    quality_raw = read_csv(os.path.join(DATA_DIR, "scenario_batch_quality.csv"))
    with open(os.path.join(DATA_DIR, "config.json"), encoding="utf-8") as handle:
        cfg = json.load(handle)

    increment = float(cfg["ton_increment"])
    tolerance = float(cfg["feasibility_tolerance"])

    lines = {}
    line_ids = []
    by_family = defaultdict(list)
    container_lines = []
    for row in lines_raw:
        line_id = row["line_id"]
        line_ids.append(line_id)
        family = row["family"]
        line = {
            "line_id": line_id,
            "family": family,
            "capacity_tons": float(row["capacity_tons"]),
            "min_open_tons": float(row["min_open_tons"]),
            "fixed_cost": float(row["fixed_cost"]),
            "variable_cost_per_ton": float(row["variable_cost_per_ton"]),
            "base_labor_hours": float(row["base_labor_hours"]),
            "labor_hours_per_ton": float(row["labor_hours_per_ton"]),
            "max_labor_hours": float(row["max_labor_hours"]),
            "min_labor_hours": float(row["min_labor_hours"]),
            "min_qc_tests": int(row["min_qc_tests"]),
            "max_qc_tests": int(row["max_qc_tests"]),
            "accepts_streams": set(row["accepts_streams"].split(";")),
            "wet_stream_allowed": int(row["wet_stream_allowed"]),
        }
        lines[line_id] = line
        by_family[family].append(line_id)
        if family in {"metal", "glass_metal", "mixed"}:
            container_lines.append(line_id)

    commodities = {}
    commodity_ids = []
    priority_commodities = []
    for row in comm_raw:
        commodity_id = row["commodity_id"]
        commodity_ids.append(commodity_id)
        commodity = {
            "price_per_ton": float(row["price_per_ton"]),
            "contract_floor_tons": float(row["contract_floor_tons"]),
            "contract_max_tons": float(row["contract_max_tons"]),
            "shortfall_penalty_per_ton": float(row["shortfall_penalty_per_ton"]),
            "priority_contract": int(row["priority_contract"]),
        }
        commodities[commodity_id] = commodity
        if commodity["priority_contract"]:
            priority_commodities.append(commodity_id)

    batches = {}
    batch_ids = []
    for row in batches_raw:
        batch_id = row["batch_id"]
        batch_ids.append(batch_id)
        priority = int(row["municipal_priority"])
        batch = {
            "supplier_zone": row["supplier_zone"],
            "stream": row["stream"],
            "available_tons": float(row["available_tons"]),
            "wet_stream": int(row["wet_stream"]),
            "high_contamination_risk": int(row["high_contamination_risk"]),
            "must_process_rate": float(row["must_process_rate"]),
            "municipal_priority": priority,
            "risky": row["stream"] == "mixed" or int(row["high_contamination_risk"]) == 1,
            "unprocessed_penalty_per_ton": float(
                cfg["unprocessed_priority_penalty_per_ton"]
                if priority
                else cfg["unprocessed_regular_penalty_per_ton"]
            ),
        }
        batches[batch_id] = batch

    scenarios = {}
    scenario_ids = []
    for row in scenarios_raw:
        scenario_id = row["scenario_id"]
        scenario_ids.append(scenario_id)
        scenarios[scenario_id] = {
            "probability": float(row["probability"]),
            "volume_multiplier": float(row["volume_multiplier"]),
            "demand_multiplier": float(row["demand_multiplier"]),
            "contamination_multiplier": float(row["contamination_multiplier"]),
            "labor_multiplier": float(row["labor_multiplier"]),
            "recovery_multiplier": float(row["recovery_multiplier"]),
        }

    quality = {}
    for row in quality_raw:
        scenario_id = row["scenario_id"]
        batch_id = row["batch_id"]
        shares = {
            "OCC": float(row["occ_share"]),
            "MIXPAPER": float(row["mixed_paper_share"]),
            "PET": float(row["pet_share"]),
            "HDPE": float(row["hdpe_share"]),
            "ALUM": float(row["alum_share"]),
        }
        quality[(scenario_id, batch_id)] = {
            "available_tons": float(row["available_tons"]),
            "available_units": tons_to_units_exact(float(row["available_tons"]), increment),
            "shares": shares,
            "residue_share": float(row["residue_share"]),
        }

    recovery = {}
    for row in rec_raw:
        recovery[(row["line_id"], row["commodity_id"])] = float(row["recovery_efficiency"])

    eligible_pairs = []
    eligible_lines_by_batch = defaultdict(list)
    eligible_batches_by_line = defaultdict(list)
    risky_pairs = []
    for batch_id in batch_ids:
        batch = batches[batch_id]
        for line_id in line_ids:
            line = lines[line_id]
            if batch["stream"] not in line["accepts_streams"]:
                continue
            if batch["wet_stream"] and not line["wet_stream_allowed"]:
                continue
            eligible_pairs.append((batch_id, line_id))
            eligible_lines_by_batch[batch_id].append(line_id)
            eligible_batches_by_line[line_id].append(batch_id)
            if batch["risky"]:
                risky_pairs.append((batch_id, line_id))

    capacity_units = {}
    risky_qc_required = {}
    sales_caps = {}
    min_open_units = {}
    for line_id in line_ids:
        min_open_units[line_id] = ceil_units(lines[line_id]["min_open_tons"], increment)

    for scenario_id in scenario_ids:
        scen = scenarios[scenario_id]
        for line_id in line_ids:
            cap_tons = lines[line_id]["capacity_tons"] / scen["labor_multiplier"]
            capacity_units[(scenario_id, line_id)] = floor_units(cap_tons, increment)
            risky_qc_required[(scenario_id, line_id)] = int(
                math.ceil(
                    lines[line_id]["min_qc_tests"] * scen["contamination_multiplier"] - 1e-9
                )
            )
        for commodity_id in commodity_ids:
            sales_caps[(scenario_id, commodity_id)] = (
                commodities[commodity_id]["contract_max_tons"] * scen["demand_multiplier"]
            )

    process_ub = {}
    recovery_coef = {}
    recovered_total_coef = {}
    must_process_units = {}
    for scenario_id in scenario_ids:
        rec_mult = scenarios[scenario_id]["recovery_multiplier"]
        for batch_id in batch_ids:
            avail_units = quality[(scenario_id, batch_id)]["available_units"]
            must_process_units[(scenario_id, batch_id)] = int(
                math.ceil(batches[batch_id]["must_process_rate"] * avail_units - 1e-9)
            )
            shares = quality[(scenario_id, batch_id)]["shares"]
            for line_id in eligible_lines_by_batch[batch_id]:
                ub = min(avail_units, capacity_units[(scenario_id, line_id)])
                process_ub[(scenario_id, batch_id, line_id)] = ub
                total = 0.0
                for commodity_id in commodity_ids:
                    coef = (
                        increment
                        * shares[commodity_id]
                        * rec_mult
                        * recovery[(line_id, commodity_id)]
                    )
                    recovery_coef[(scenario_id, batch_id, line_id, commodity_id)] = coef
                    total += coef
                recovered_total_coef[(scenario_id, batch_id, line_id)] = total

    return {
        "cfg": cfg,
        "increment": increment,
        "tolerance": tolerance,
        "lines": lines,
        "line_ids": line_ids,
        "fiber_lines": list(by_family["fiber"]),
        "plastic_lines": list(by_family["plastic"]),
        "container_lines": container_lines,
        "batches": batches,
        "batch_ids": batch_ids,
        "commodities": commodities,
        "commodity_ids": commodity_ids,
        "priority_commodities": priority_commodities,
        "scenarios": scenarios,
        "scenario_ids": scenario_ids,
        "quality": quality,
        "recovery": recovery,
        "eligible_pairs": eligible_pairs,
        "eligible_lines_by_batch": dict(eligible_lines_by_batch),
        "eligible_batches_by_line": dict(eligible_batches_by_line),
        "risky_pairs": risky_pairs,
        "capacity_units": capacity_units,
        "risky_qc_required": risky_qc_required,
        "sales_caps": sales_caps,
        "process_ub": process_ub,
        "recovery_coef": recovery_coef,
        "recovered_total_coef": recovered_total_coef,
        "must_process_units": must_process_units,
        "min_open_units": min_open_units,
    }


def build_model(data, time_limit_seconds):
    model = Model("recycling_mrf_stochastic_sorting")
    model.setParam("display/verblevel", 0)
    model.setParam("limits/gap", 0.0005)
    model.setParam("limits/time", time_limit_seconds)
    model.setParam("parallel/maxnthreads", 1)
    model.setParam("randomization/permutationseed", 0)
    model.setParam("randomization/randomseedshift", 0)

    increment = data["increment"]
    cfg = data["cfg"]
    lines = data["lines"]
    line_ids = data["line_ids"]
    batches = data["batches"]
    batch_ids = data["batch_ids"]
    commodity_ids = data["commodity_ids"]
    scenarios = data["scenarios"]
    scenario_ids = data["scenario_ids"]

    open_line = {
        line_id: model.addVar(vtype="B", name=f"open[{line_id}]") for line_id in line_ids
    }
    labor_hours = {
        line_id: model.addVar(lb=0.0, vtype="C", name=f"labor[{line_id}]")
        for line_id in line_ids
    }
    qc_tests = {
        line_id: model.addVar(lb=0.0, vtype="I", name=f"qc[{line_id}]") for line_id in line_ids
    }
    process_units = {}
    for scenario_id in scenario_ids:
        for batch_id in batch_ids:
            for line_id in data["eligible_lines_by_batch"][batch_id]:
                ub = data["process_ub"][(scenario_id, batch_id, line_id)]
                process_units[(scenario_id, batch_id, line_id)] = model.addVar(
                    lb=0.0,
                    ub=float(ub),
                    vtype="I",
                    name=f"process[{scenario_id},{batch_id},{line_id}]",
                )

    risky_use = {}
    for scenario_id in scenario_ids:
        for batch_id, line_id in data["risky_pairs"]:
            if data["process_ub"][(scenario_id, batch_id, line_id)] <= 0:
                continue
            req = data["risky_qc_required"][(scenario_id, line_id)]
            if req <= 0:
                continue
            risky_use[(scenario_id, batch_id, line_id)] = model.addVar(
                vtype="B", name=f"risky_use[{scenario_id},{batch_id},{line_id}]"
            )

    sell = {
        (scenario_id, commodity_id): model.addVar(
            lb=0.0, vtype="C", name=f"sell[{scenario_id},{commodity_id}]"
        )
        for scenario_id in scenario_ids
        for commodity_id in commodity_ids
    }
    shortfall = {
        (scenario_id, commodity_id): model.addVar(
            lb=0.0, vtype="C", name=f"short[{scenario_id},{commodity_id}]"
        )
        for scenario_id in scenario_ids
        for commodity_id in commodity_ids
    }
    tail = model.addVar(lb=0.0, vtype="C", name="tail")

    model.addCons(
        quicksum(open_line[line_id] for line_id in data["fiber_lines"])
        >= int(cfg["required_open_fiber_lines"]),
        name="required_fiber_lines",
    )
    model.addCons(
        quicksum(open_line[line_id] for line_id in data["plastic_lines"])
        >= int(cfg["required_open_plastic_lines"]),
        name="required_plastic_lines",
    )
    model.addCons(
        quicksum(open_line[line_id] for line_id in data["container_lines"])
        >= int(cfg["required_open_container_lines"]),
        name="required_container_lines",
    )

    for line_id in line_ids:
        line = lines[line_id]
        model.addCons(
            labor_hours[line_id] >= line["min_labor_hours"] * open_line[line_id],
            name=f"labor_lb[{line_id}]",
        )
        model.addCons(
            labor_hours[line_id] <= line["max_labor_hours"] * open_line[line_id],
            name=f"labor_ub[{line_id}]",
        )
        model.addCons(
            qc_tests[line_id] >= line["min_qc_tests"] * open_line[line_id],
            name=f"qc_lb[{line_id}]",
        )
        model.addCons(
            qc_tests[line_id] <= line["max_qc_tests"] * open_line[line_id],
            name=f"qc_ub[{line_id}]",
        )

    model.addCons(
        quicksum(labor_hours[line_id] for line_id in line_ids)
        <= float(cfg["total_labor_hours_budget"]),
        name="total_labor_budget",
    )
    model.addCons(
        quicksum(qc_tests[line_id] for line_id in line_ids) <= int(cfg["total_qc_tests"]),
        name="total_qc_budget",
    )

    for scenario_id in scenario_ids:
        for line_id in line_ids:
            line_process = quicksum(
                process_units[(scenario_id, batch_id, line_id)]
                for batch_id in data["eligible_batches_by_line"].get(line_id, [])
            )
            model.addCons(
                line_process <= data["capacity_units"][(scenario_id, line_id)] * open_line[line_id],
                name=f"capacity[{scenario_id},{line_id}]",
            )
            model.addCons(
                line_process >= data["min_open_units"][line_id] * open_line[line_id],
                name=f"min_open_tons[{scenario_id},{line_id}]",
            )
            model.addCons(
                lines[line_id]["base_labor_hours"] * open_line[line_id]
                + lines[line_id]["labor_hours_per_ton"] * increment * line_process
                <= labor_hours[line_id],
                name=f"labor_support[{scenario_id},{line_id}]",
            )

        for batch_id in batch_ids:
            batch_process = quicksum(
                process_units[(scenario_id, batch_id, line_id)]
                for line_id in data["eligible_lines_by_batch"][batch_id]
            )
            model.addCons(
                batch_process <= data["quality"][(scenario_id, batch_id)]["available_units"],
                name=f"availability[{scenario_id},{batch_id}]",
            )
            if data["must_process_units"][(scenario_id, batch_id)] > 0:
                model.addCons(
                    batch_process >= data["must_process_units"][(scenario_id, batch_id)],
                    name=f"must_process[{scenario_id},{batch_id}]",
                )

        for batch_id, line_id in data["risky_pairs"]:
            key = (scenario_id, batch_id, line_id)
            if key not in risky_use:
                continue
            ub = data["process_ub"][key]
            model.addCons(
                process_units[key] <= ub * risky_use[key],
                name=f"risky_link[{scenario_id},{batch_id},{line_id}]",
            )
            model.addCons(
                qc_tests[line_id] >= data["risky_qc_required"][(scenario_id, line_id)] * risky_use[key],
                name=f"risky_qc[{scenario_id},{batch_id},{line_id}]",
            )

        for commodity_id in commodity_ids:
            recovered = quicksum(
                data["recovery_coef"][(scenario_id, batch_id, line_id, commodity_id)]
                * process_units[(scenario_id, batch_id, line_id)]
                for batch_id, line_id in data["eligible_pairs"]
            )
            model.addCons(
                sell[(scenario_id, commodity_id)] <= recovered,
                name=f"recovered_sales[{scenario_id},{commodity_id}]",
            )
            model.addCons(
                sell[(scenario_id, commodity_id)] <= data["sales_caps"][(scenario_id, commodity_id)],
                name=f"sales_cap[{scenario_id},{commodity_id}]",
            )
            model.addCons(
                shortfall[(scenario_id, commodity_id)]
                >= data["commodities"][commodity_id]["contract_floor_tons"]
                - sell[(scenario_id, commodity_id)],
                name=f"shortfall_def[{scenario_id},{commodity_id}]",
            )

        model.addCons(
            tail
            >= quicksum(
                shortfall[(scenario_id, commodity_id)]
                for commodity_id in data["priority_commodities"]
            ),
            name=f"tail[{scenario_id}]",
        )

    expected_margin = 0.0
    for scenario_id in scenario_ids:
        prob = scenarios[scenario_id]["probability"]
        revenue = quicksum(
            data["commodities"][commodity_id]["price_per_ton"] * sell[(scenario_id, commodity_id)]
            for commodity_id in commodity_ids
        )
        variable_cost = quicksum(
            lines[line_id]["variable_cost_per_ton"] * increment * process_units[(scenario_id, batch_id, line_id)]
            for batch_id, line_id in data["eligible_pairs"]
        )
        contract_penalty = quicksum(
            data["commodities"][commodity_id]["shortfall_penalty_per_ton"]
            * shortfall[(scenario_id, commodity_id)]
            for commodity_id in commodity_ids
        )
        unprocessed_penalty = quicksum(
            batches[batch_id]["unprocessed_penalty_per_ton"]
            * (
                data["quality"][(scenario_id, batch_id)]["available_tons"]
                - increment
                * quicksum(
                    process_units[(scenario_id, batch_id, line_id)]
                    for line_id in data["eligible_lines_by_batch"][batch_id]
                )
            )
            for batch_id in batch_ids
        )
        residue = quicksum(
            (
                increment - data["recovered_total_coef"][(scenario_id, batch_id, line_id)]
            )
            * process_units[(scenario_id, batch_id, line_id)]
            for batch_id, line_id in data["eligible_pairs"]
        )
        expected_margin += prob * (
            revenue
            - variable_cost
            - contract_penalty
            - unprocessed_penalty
            - float(cfg["residue_disposal_cost_per_ton"]) * residue
        )

    first_stage_cost = (
        quicksum(lines[line_id]["fixed_cost"] * open_line[line_id] for line_id in line_ids)
        + quicksum(float(cfg["labor_cost_per_hour"]) * labor_hours[line_id] for line_id in line_ids)
        + quicksum(float(cfg["qc_test_cost"]) * qc_tests[line_id] for line_id in line_ids)
        + float(cfg["tail_contract_shortfall_penalty"]) * tail
    )
    model.setObjective(expected_margin - first_stage_cost, "maximize")

    return {
        "model": model,
        "open_line": open_line,
        "labor_hours": labor_hours,
        "qc_tests": qc_tests,
        "process_units": process_units,
        "risky_use": risky_use,
        "sell": sell,
        "shortfall": shortfall,
        "tail": tail,
    }


def extract_solution(data, model_bundle):
    model = model_bundle["model"]
    if model.getNSols() <= 0 or model.getBestSol() is None:
        raise RuntimeError(f"SCIP finished with status {model.getStatus()} and no incumbent solution")
    sol = model.getBestSol()

    open_line = {}
    labor_hours = {}
    qc_tests = {}
    process_tons = {}
    sell = {}

    max_integrality_residual = 0.0
    for line_id in data["line_ids"]:
        y_val = model.getSolVal(sol, model_bundle["open_line"][line_id])
        q_val = model.getSolVal(sol, model_bundle["qc_tests"][line_id])
        open_line[line_id] = int(round(y_val))
        qc_tests[line_id] = int(round(q_val))
        labor_hours[line_id] = model.getSolVal(sol, model_bundle["labor_hours"][line_id])
        max_integrality_residual = max(
            max_integrality_residual,
            abs(open_line[line_id] - y_val),
            abs(qc_tests[line_id] - q_val),
        )

    for key, var in model_bundle["process_units"].items():
        raw = model.getSolVal(sol, var)
        units = int(round(raw))
        max_integrality_residual = max(max_integrality_residual, abs(units - raw))
        if units > 0:
            process_tons[key] = units * data["increment"]

    for key, var in model_bundle["sell"].items():
        value = model.getSolVal(sol, var)
        if value > 1e-9:
            sell[key] = value

    return {
        "open_line": open_line,
        "labor_hours": labor_hours,
        "qc_tests": qc_tests,
        "process_tons": process_tons,
        "sell": sell,
        "integrality_residual": max_integrality_residual,
    }


def write_solution_csv(path, data, solution):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["row_type", "line_id", "scenario_id", "batch_id", "commodity_id", "value"])

        for line_id in data["line_ids"]:
            writer.writerow(["open_line", line_id, "", "", "", str(solution["open_line"][line_id])])
            writer.writerow(["labor_hours", line_id, "", "", "", fmt(solution["labor_hours"][line_id])])
            writer.writerow(["qc_tests", line_id, "", "", "", str(solution["qc_tests"][line_id])])

        for scenario_id in data["scenario_ids"]:
            for batch_id in data["batch_ids"]:
                for line_id in data["eligible_lines_by_batch"][batch_id]:
                    tons = solution["process_tons"].get((scenario_id, batch_id, line_id), 0.0)
                    if tons > 1e-9:
                        writer.writerow(
                            ["process", line_id, scenario_id, batch_id, "", fmt(tons)]
                        )

        for scenario_id in data["scenario_ids"]:
            for commodity_id in data["commodity_ids"]:
                tons = solution["sell"].get((scenario_id, commodity_id), 0.0)
                if tons > 1e-9:
                    writer.writerow(["sell", "", scenario_id, "", commodity_id, fmt(tons)])


def parsed_to_solution(parsed):
    return {
        "open_line": {key: int(round(val)) for key, val in parsed["open_line"].items()},
        "labor_hours": dict(parsed["labor_hours"]),
        "qc_tests": {key: int(round(val)) for key, val in parsed["qc_tests"].items()},
        "process_tons": {
            key: val for key, val in parsed["process_tons"].items() if val > 1e-9
        },
        "sell": {key: val for key, val in parsed["sell"].items() if val > 1e-9},
        "integrality_residual": 0.0,
    }


def audit_is_clean(validation):
    return not validation["errors"] and not validation["violated_groups"]


def apply_mip_start(data, model_bundle, parsed):
    model = model_bundle["model"]
    start_sol = model.createOrigSol()

    for line_id in data["line_ids"]:
        model.setSolVal(
            start_sol,
            model_bundle["open_line"][line_id],
            float(int(round(parsed["open_line"].get(line_id, 0.0)))),
        )
        model.setSolVal(
            start_sol,
            model_bundle["labor_hours"][line_id],
            float(parsed["labor_hours"].get(line_id, 0.0)),
        )
        model.setSolVal(
            start_sol,
            model_bundle["qc_tests"][line_id],
            float(int(round(parsed["qc_tests"].get(line_id, 0.0)))),
        )

    for key, var in model_bundle["process_units"].items():
        tons = parsed["process_tons"].get(key, 0.0)
        model.setSolVal(start_sol, var, tons / data["increment"])

    for key, var in model_bundle["sell"].items():
        model.setSolVal(start_sol, var, parsed["sell"].get(key, 0.0))

    for key, var in model_bundle["shortfall"].items():
        scenario_id, commodity_id = key
        sold = max(0.0, parsed["sell"].get((scenario_id, commodity_id), 0.0))
        short = max(0.0, data["commodities"][commodity_id]["contract_floor_tons"] - sold)
        model.setSolVal(start_sol, var, short)

    tail_value = 0.0
    for scenario_id in data["scenario_ids"]:
        priority_shortfall = 0.0
        for commodity_id in data["priority_commodities"]:
            sold = max(0.0, parsed["sell"].get((scenario_id, commodity_id), 0.0))
            priority_shortfall += max(
                0.0, data["commodities"][commodity_id]["contract_floor_tons"] - sold
            )
        tail_value = max(tail_value, priority_shortfall)
    model.setSolVal(start_sol, model_bundle["tail"], tail_value)

    risky_use_vars = model_bundle.get("risky_use", {})
    for key, var in risky_use_vars.items():
        tons = parsed["process_tons"].get(key, 0.0)
        model.setSolVal(start_sol, var, 1.0 if tons > 1e-9 else 0.0)

    return model.addSol(start_sol, free=True)


def parse_solution_csv(data, path):
    parsed = {
        "open_line": {},
        "labor_hours": {},
        "qc_tests": {},
        "process_tons": defaultdict(float),
        "sell": defaultdict(float),
    }
    errors = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        expected = ["row_type", "line_id", "scenario_id", "batch_id", "commodity_id", "value"]
        if header != expected:
            errors.append(f"Header mismatch: expected {expected}, found {header}")
            return parsed, errors

        seen_first_stage = set()
        for idx, row in enumerate(reader, start=2):
            row_type = row["row_type"]
            line_id = row["line_id"]
            scenario_id = row["scenario_id"]
            batch_id = row["batch_id"]
            commodity_id = row["commodity_id"]
            try:
                value = float(row["value"])
            except ValueError:
                errors.append(f"Row {idx}: non-numeric value {row['value']!r}")
                continue

            if row_type == "open_line":
                key = (row_type, line_id)
                if key in seen_first_stage:
                    errors.append(f"Row {idx}: duplicate first-stage row for {key}")
                seen_first_stage.add(key)
                if scenario_id or batch_id or commodity_id:
                    errors.append(f"Row {idx}: open_line must leave scenario, batch, commodity blank")
                if line_id not in data["lines"]:
                    errors.append(f"Row {idx}: unknown line {line_id}")
                    continue
                parsed["open_line"][line_id] = value
            elif row_type == "labor_hours":
                key = (row_type, line_id)
                if key in seen_first_stage:
                    errors.append(f"Row {idx}: duplicate first-stage row for {key}")
                seen_first_stage.add(key)
                if scenario_id or batch_id or commodity_id:
                    errors.append(f"Row {idx}: labor_hours must leave scenario, batch, commodity blank")
                if line_id not in data["lines"]:
                    errors.append(f"Row {idx}: unknown line {line_id}")
                    continue
                parsed["labor_hours"][line_id] = value
            elif row_type == "qc_tests":
                key = (row_type, line_id)
                if key in seen_first_stage:
                    errors.append(f"Row {idx}: duplicate first-stage row for {key}")
                seen_first_stage.add(key)
                if scenario_id or batch_id or commodity_id:
                    errors.append(f"Row {idx}: qc_tests must leave scenario, batch, commodity blank")
                if line_id not in data["lines"]:
                    errors.append(f"Row {idx}: unknown line {line_id}")
                    continue
                parsed["qc_tests"][line_id] = value
            elif row_type == "process":
                if not line_id or not scenario_id or not batch_id or commodity_id:
                    errors.append(f"Row {idx}: process must specify line, scenario, batch and blank commodity")
                    continue
                if line_id not in data["lines"] or scenario_id not in data["scenarios"] or batch_id not in data["batches"]:
                    errors.append(f"Row {idx}: invalid process identifiers")
                    continue
                parsed["process_tons"][(scenario_id, batch_id, line_id)] += value
            elif row_type == "sell":
                if line_id or batch_id or not scenario_id or not commodity_id:
                    errors.append(f"Row {idx}: sell must specify scenario, commodity and blank line, batch")
                    continue
                if scenario_id not in data["scenarios"] or commodity_id not in data["commodities"]:
                    errors.append(f"Row {idx}: invalid sell identifiers")
                    continue
                parsed["sell"][(scenario_id, commodity_id)] += value
            else:
                errors.append(f"Row {idx}: unknown row_type {row_type}")

    for line_id in data["line_ids"]:
        for row_type in ("open_line", "labor_hours", "qc_tests"):
            if (row_type, line_id) not in seen_first_stage:
                errors.append(f"Missing required first-stage row ({row_type}, {line_id})")
    return parsed, errors


def validate_solution(data, path):
    parsed, errors = parse_solution_csv(data, path)
    tolerance = data["tolerance"]
    increment = data["increment"]

    max_residual = 0.0
    violated_groups = set()

    def note(group, residual, scale=1.0, detail=None):
        nonlocal max_residual
        if residual <= tolerance:
            return
        norm = residual / max(scale, 1.0)
        max_residual = max(max_residual, norm)
        violated_groups.add(group)
        if detail:
            errors.append(detail)

    open_line = {}
    labor_hours = {}
    qc_tests = {}
    integrality_residual = 0.0
    for line_id in data["line_ids"]:
        open_val = parsed["open_line"].get(line_id, 0.0)
        labor_val = parsed["labor_hours"].get(line_id, 0.0)
        qc_val = parsed["qc_tests"].get(line_id, 0.0)
        open_line[line_id] = int(round(open_val))
        qc_tests[line_id] = int(round(qc_val))
        labor_hours[line_id] = labor_val
        integrality_residual = max(
            integrality_residual,
            abs(open_val - open_line[line_id]),
            abs(qc_val - qc_tests[line_id]),
        )
        note(
            "open_binary",
            abs(open_val - open_line[line_id]),
            1.0,
            f"open_line[{line_id}] is not integral/binary: {open_val}",
        )
        if open_line[line_id] not in {0, 1}:
            note(
                "open_binary",
                1.0,
                1.0,
                f"open_line[{line_id}] is not 0/1 after rounding: {open_val}",
            )
        note(
            "qc_integrality",
            abs(qc_val - qc_tests[line_id]),
            1.0,
            f"qc_tests[{line_id}] is not integral: {qc_val}",
        )
        if qc_val < -tolerance:
            note("qc_nonnegative", -qc_val, 1.0, f"qc_tests[{line_id}] is negative: {qc_val}")
        if labor_val < -tolerance:
            note(
                "labor_nonnegative",
                -labor_val,
                1.0,
                f"labor_hours[{line_id}] is negative: {labor_val}",
            )

    process_tons = defaultdict(float)
    process_units = defaultdict(int)
    for key, tons in parsed["process_tons"].items():
        scenario_id, batch_id, line_id = key
        if (batch_id, line_id) not in data["eligible_pairs"]:
            note(
                "eligibility",
                tons,
                1.0,
                f"Process on ineligible arc {(scenario_id, batch_id, line_id)}: {tons}",
            )
        units = tons / increment
        rounded_units = int(round(units))
        integrality_residual = max(integrality_residual, abs(units - rounded_units))
        note(
            "process_increment",
            abs(units - rounded_units),
            1.0,
            f"Process value {tons} for {(scenario_id, batch_id, line_id)} is not on the 0.1-ton grid",
        )
        if tons < -tolerance:
            note(
                "process_nonnegative",
                -tons,
                1.0,
                f"Negative process value {tons} for {(scenario_id, batch_id, line_id)}",
            )
        process_tons[key] = tons
        process_units[key] = rounded_units

    for key, tons in parsed["sell"].items():
        if tons < -tolerance:
            note("sell_nonnegative", -tons, 1.0, f"Negative sell value {tons} for {key}")

    note("family_fiber", max(0, int(data["cfg"]["required_open_fiber_lines"]) - sum(open_line[l] for l in data["fiber_lines"])))
    note(
        "family_plastic",
        max(0, int(data["cfg"]["required_open_plastic_lines"]) - sum(open_line[l] for l in data["plastic_lines"])),
    )
    note(
        "family_container",
        max(0, int(data["cfg"]["required_open_container_lines"]) - sum(open_line[l] for l in data["container_lines"])),
    )
    note(
        "labor_budget",
        max(0.0, sum(labor_hours.values()) - float(data["cfg"]["total_labor_hours_budget"])),
        float(data["cfg"]["total_labor_hours_budget"]),
    )
    note(
        "qc_budget",
        max(0.0, sum(qc_tests.values()) - int(data["cfg"]["total_qc_tests"])),
        float(data["cfg"]["total_qc_tests"]),
    )

    shortfall = {}
    objective = 0.0
    for line_id in data["line_ids"]:
        line = data["lines"][line_id]
        note(
            "labor_lb",
            max(0.0, line["min_labor_hours"] * open_line[line_id] - labor_hours[line_id]),
            line["min_labor_hours"] + 1.0,
        )
        note(
            "labor_ub",
            max(0.0, labor_hours[line_id] - line["max_labor_hours"] * open_line[line_id]),
            line["max_labor_hours"] + 1.0,
        )
        note(
            "qc_lb",
            max(0.0, line["min_qc_tests"] * open_line[line_id] - qc_tests[line_id]),
            line["min_qc_tests"] + 1.0,
        )
        note(
            "qc_ub",
            max(0.0, qc_tests[line_id] - line["max_qc_tests"] * open_line[line_id]),
            line["max_qc_tests"] + 1.0,
        )
        objective -= line["fixed_cost"] * open_line[line_id]
        objective -= float(data["cfg"]["labor_cost_per_hour"]) * labor_hours[line_id]
        objective -= float(data["cfg"]["qc_test_cost"]) * qc_tests[line_id]

    tail_candidate = 0.0
    for scenario_id in data["scenario_ids"]:
        prob = data["scenarios"][scenario_id]["probability"]
        priority_shortfall = 0.0
        line_processed_tons = defaultdict(float)
        batch_processed_tons = defaultdict(float)
        recovered_by_commodity = defaultdict(float)
        scenario_margin = 0.0

        for batch_id, line_id in data["eligible_pairs"]:
            tons = process_tons.get((scenario_id, batch_id, line_id), 0.0)
            line_processed_tons[line_id] += tons
            batch_processed_tons[batch_id] += tons
            if tons > 0 and data["batches"][batch_id]["risky"]:
                req = data["risky_qc_required"][(scenario_id, line_id)]
                note(
                    "risky_qc",
                    max(0.0, req - qc_tests[line_id]),
                    max(req, 1.0),
                    f"Risky processing requires {req} QC tests on {(scenario_id, batch_id, line_id)} but has {qc_tests[line_id]}",
                )
            scenario_margin -= data["lines"][line_id]["variable_cost_per_ton"] * tons
            scenario_margin -= float(data["cfg"]["residue_disposal_cost_per_ton"]) * (
                tons
                - sum(
                    data["recovery_coef"][(scenario_id, batch_id, line_id, commodity_id)] * process_units[(scenario_id, batch_id, line_id)]
                    for commodity_id in data["commodity_ids"]
                )
            )
            for commodity_id in data["commodity_ids"]:
                recovered_by_commodity[commodity_id] += (
                    data["recovery_coef"][(scenario_id, batch_id, line_id, commodity_id)]
                    * process_units[(scenario_id, batch_id, line_id)]
                )

        for line_id in data["line_ids"]:
            cap_tons = data["capacity_units"][(scenario_id, line_id)] * increment
            note(
                "capacity",
                max(0.0, line_processed_tons[line_id] - cap_tons * open_line[line_id]),
                cap_tons + 1.0,
            )
            note(
                "min_open",
                max(0.0, data["min_open_units"][line_id] * increment * open_line[line_id] - line_processed_tons[line_id]),
                data["min_open_units"][line_id] * increment + 1.0,
            )
            required_labor = data["lines"][line_id]["base_labor_hours"] * open_line[line_id] + data["lines"][line_id]["labor_hours_per_ton"] * line_processed_tons[line_id]
            note(
                "labor_support",
                max(0.0, required_labor - labor_hours[line_id]),
                required_labor + 1.0,
            )

        for batch_id in data["batch_ids"]:
            avail = data["quality"][(scenario_id, batch_id)]["available_tons"]
            processed = batch_processed_tons[batch_id]
            note(
                "availability",
                max(0.0, processed - avail),
                avail + 1.0,
            )
            must = data["must_process_units"][(scenario_id, batch_id)] * increment
            if must > 0:
                note(
                    "must_process",
                    max(0.0, must - processed),
                    must + 1.0,
                )
            scenario_margin -= data["batches"][batch_id]["unprocessed_penalty_per_ton"] * (avail - processed)

        for commodity_id in data["commodity_ids"]:
            sold = parsed["sell"].get((scenario_id, commodity_id), 0.0)
            note(
                "sales_recovery",
                max(0.0, sold - recovered_by_commodity[commodity_id]),
                recovered_by_commodity[commodity_id] + 1.0,
            )
            note(
                "sales_cap",
                max(0.0, sold - data["sales_caps"][(scenario_id, commodity_id)]),
                data["sales_caps"][(scenario_id, commodity_id)] + 1.0,
            )
            sold = max(0.0, sold)
            short = max(0.0, data["commodities"][commodity_id]["contract_floor_tons"] - sold)
            shortfall[(scenario_id, commodity_id)] = short
            if commodity_id in data["priority_commodities"]:
                priority_shortfall += short
            scenario_margin += data["commodities"][commodity_id]["price_per_ton"] * sold
            scenario_margin -= data["commodities"][commodity_id]["shortfall_penalty_per_ton"] * short

        objective += prob * scenario_margin
        tail_candidate = max(tail_candidate, priority_shortfall)

    objective -= float(data["cfg"]["tail_contract_shortfall_penalty"]) * tail_candidate

    return {
        "parsed": parsed,
        "errors": errors,
        "objective": objective,
        "max_residual": max_residual,
        "violated_groups": sorted(violated_groups),
        "integrality_residual": integrality_residual,
        "tail_value": tail_candidate,
        "checked_groups": [
            "schema",
            "first_stage_rows",
            "open_line_binary",
            "labor_qc_nonnegativity_and_bounds",
            "family_open_requirements",
            "facility_budgets",
            "eligibility",
            "process_increment_grid",
            "scenario_line_capacity",
            "scenario_line_min_open",
            "scenario_line_labor_support",
            "scenario_batch_availability",
            "scenario_must_process",
            "scenario_risky_qc_gating",
            "scenario_sales_vs_recovery",
            "scenario_sales_caps",
        ],
        "has_hard_violation": bool(errors or violated_groups),
    }


def write_log(lines):
    with open(SOLVE_LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


def main():
    os.makedirs(SUBMISSIONS_DIR, exist_ok=True)
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"Missing required model file: {MODEL_PATH}")

    start = time.time()
    data = build_data()

    env_limit = os.getenv("ORCLAW_SOLVE_TIME_LIMIT_SECONDS")
    time_limit_seconds = 295.0
    if env_limit:
        time_limit_seconds = min(time_limit_seconds, float(env_limit))

    log_lines = [
        "# Solve Log",
        "",
        "## Commands",
        "",
        "- `PYTHONPATH=/opt/or-deps python /app/submissions/solve.py`",
        "",
        "## Run Summary",
        "",
        f"- Start time (unix): `{start:.3f}`",
        f"- SCIP relative gap target: `0.0005`",
        f"- Time limit used (seconds): `{time_limit_seconds:.3f}`",
        f"- Instance size: `{len(data['line_ids'])}` lines, `{len(data['batch_ids'])}` batches, `{len(data['scenario_ids'])}` scenarios, `{len(data['commodity_ids'])}` commodities",
    ]

    incumbent_exists = os.path.exists(SOLUTION_PATH)
    incumbent_validation = None
    incumbent_clean = False
    incumbent_objective = None
    incumbent_parsed = None
    if incumbent_exists:
        incumbent_validation = validate_solution(data, SOLUTION_PATH)
        incumbent_clean = audit_is_clean(incumbent_validation)
        incumbent_objective = incumbent_validation["objective"]
        incumbent_parsed = incumbent_validation["parsed"]

    log_lines.extend(
        [
            "",
            "## Incumbent Audit",
            "",
            f"- Incumbent file present: `{'yes' if incumbent_exists else 'no'}`",
        ]
    )
    if incumbent_exists:
        log_lines.extend(
            [
                f"- Incumbent recomputed objective: `{incumbent_objective:.8f}`",
                f"- Incumbent max normalized hard-constraint residual: `{incumbent_validation['max_residual']:.8e}`",
                f"- Incumbent violated constraint groups: `{len(incumbent_validation['violated_groups'])}`",
                f"- Incumbent integrality residual: `{incumbent_validation['integrality_residual']:.8e}`",
                f"- Independent audit clean: `{'yes' if incumbent_clean else 'no'}`",
                f"- Independent audit coverage: `{', '.join(incumbent_validation['checked_groups'])}`",
            ]
        )
        if incumbent_validation["violated_groups"]:
            log_lines.append(
                f"- Incumbent violated groups: `{', '.join(incumbent_validation['violated_groups'])}`"
            )
        if incumbent_validation["errors"]:
            log_lines.append("- Incumbent audit notes:")
            for error in incumbent_validation["errors"]:
                log_lines.append(f"  - {error}")
        else:
            log_lines.append("- Incumbent audit notes: none")

    if incumbent_exists and not incumbent_clean:
        write_log(log_lines)
        raise RuntimeError("Existing incumbent failed the independent audit; restoration is required before performance optimization")

    model_bundle = build_model(data, time_limit_seconds)
    model = model_bundle["model"]
    warm_start_stored = False
    objective_cutoff = None
    improvement_tol = 1e-8
    if incumbent_clean and incumbent_parsed is not None:
        warm_start_stored = apply_mip_start(data, model_bundle, incumbent_parsed)
        objective_cutoff = incumbent_objective + improvement_tol
        model.setObjlimit(objective_cutoff)

    optimize_start = time.time()
    model.optimize()
    solve_seconds = time.time() - optimize_start
    status = str(model.getStatus())
    candidate_model_obj = model.getObjVal() if model.getNSols() > 0 and model.getBestSol() is not None else None
    dual_bound = model.getDualbound()
    gap = model.getGap()

    log_lines.extend(
        [
            "",
            "## Improvement Attempt",
            "",
            f"- Warm start used: `{'yes' if incumbent_clean else 'no'}`",
            f"- Warm start accepted into SCIP storage: `{'yes' if warm_start_stored else 'no'}`",
            f"- Objective cutoff for strict improvement: `{objective_cutoff:.8f}`" if objective_cutoff is not None else "- Objective cutoff for strict improvement: `none`",
            f"- SCIP status: `{status}`",
            f"- Elapsed solve wall time (seconds): `{solve_seconds:.3f}`",
            f"- Candidate incumbent count in SCIP: `{model.getNSols()}`",
            f"- Candidate model objective: `{candidate_model_obj:.8f}`" if candidate_model_obj is not None else "- Candidate model objective: `none`",
            f"- Candidate dual bound: `{dual_bound:.8f}`",
            f"- Candidate relative gap: `{gap:.8f}`",
        ]
    )

    published = False
    publication_reason = "No candidate solution was available."
    candidate_validation = None
    candidate_solution = None
    candidate_path = None
    if model.getNSols() > 0 and model.getBestSol() is not None:
        candidate_solution = extract_solution(data, model_bundle)
        temp_handle = tempfile.NamedTemporaryFile(
            mode="w",
            dir=SUBMISSIONS_DIR,
            prefix="candidate_",
            suffix=".csv",
            delete=False,
            encoding="utf-8",
            newline="",
        )
        candidate_path = temp_handle.name
        temp_handle.close()
        write_solution_csv(candidate_path, data, candidate_solution)
        candidate_validation = validate_solution(data, candidate_path)

        candidate_clean = audit_is_clean(candidate_validation)
        candidate_objective = candidate_validation["objective"]
        log_lines.extend(
            [
                f"- Candidate recomputed objective from exported artifact: `{candidate_objective:.8f}`",
                f"- Candidate max normalized hard-constraint residual: `{candidate_validation['max_residual']:.8e}`",
                f"- Candidate violated constraint groups: `{len(candidate_validation['violated_groups'])}`",
                f"- Candidate integrality residual: `{max(candidate_solution['integrality_residual'], candidate_validation['integrality_residual']):.8e}`",
                f"- Candidate audit clean: `{'yes' if candidate_clean else 'no'}`",
            ]
        )
        if candidate_validation["violated_groups"]:
            log_lines.append(
                f"- Candidate violated groups: `{', '.join(candidate_validation['violated_groups'])}`"
            )
        if candidate_validation["errors"]:
            log_lines.append("- Candidate audit notes:")
            for error in candidate_validation["errors"]:
                log_lines.append(f"  - {error}")
        else:
            log_lines.append("- Candidate audit notes: none")

        if candidate_clean and (
            not incumbent_clean or candidate_objective > incumbent_objective + improvement_tol
        ):
            os.replace(candidate_path, SOLUTION_PATH)
            published = True
            publication_reason = "Published candidate because it passed the independent audit and strictly improved the incumbent objective."
        else:
            os.unlink(candidate_path)
            if not candidate_clean:
                publication_reason = "Kept incumbent because the candidate failed the independent audit."
            elif incumbent_clean and candidate_objective <= incumbent_objective + improvement_tol:
                publication_reason = "Kept incumbent because the candidate did not strictly improve the recomputed objective."
            else:
                publication_reason = "Kept incumbent."

    log_lines.extend(
        [
            "",
            "## Publication Decision",
            "",
            f"- Final artifact published: `{'candidate' if published else 'incumbent'}`",
            f"- Decision reason: {publication_reason}",
        ]
    )

    if published:
        final_validation = validate_solution(data, SOLUTION_PATH)
    elif incumbent_exists:
        final_validation = incumbent_validation
    else:
        final_validation = candidate_validation

    if final_validation is not None:
        log_lines.extend(
            [
                f"- Final recomputed objective: `{final_validation['objective']:.8f}`",
                f"- Final max normalized hard-constraint residual: `{final_validation['max_residual']:.8e}`",
                f"- Final violated constraint groups: `{len(final_validation['violated_groups'])}`",
            ]
        )
        if final_validation["violated_groups"]:
            log_lines.append(
                f"- Final violated groups: `{', '.join(final_validation['violated_groups'])}`"
            )

    write_log(log_lines)

    if final_validation is None or not audit_is_clean(final_validation):
        raise RuntimeError("Final solution artifact failed the independent audit")


if __name__ == "__main__":
    main()
