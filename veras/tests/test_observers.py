import csv
import json
import tempfile
import unittest
from pathlib import Path

from veras_harness.observers import normalized_violation_mass, observe_submission, report


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class SemanticObserverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Path(self.temp.name)
        (self.app / "data").mkdir()
        (self.app / "submissions").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_report_integrates_temporal_violation_mass(self):
        interval = {
            "group": "energy-window-capacity",
            "message": "overload",
            "normalized_residual": 0.5,
            "evidence": {"start_min": 10.0, "end_min": 30.0},
        }
        structural = {
            "group": "part-assignment",
            "message": "missing",
            "normalized_residual": 2.0,
            "evidence": {"part_id": "P001"},
        }
        self.assertEqual(normalized_violation_mass(interval), 10.0)
        observation = report("test", ["x"], [interval, structural])
        self.assertEqual(observation["total_normalized_violation_mass"], 12.0)

    def test_online_bus_observer_rejects_trip_row_payload_fields(self):
        plan = self.app / "submissions" / "initial_plan.csv"
        fields = [
            "row_type", "item_id", "bus_id", "trip_id", "charger_id",
            "start_min", "end_min", "energy_kwh", "decision",
        ]
        write_csv(plan, fields, [{
            "row_type": "trip", "item_id": "T1", "bus_id": "B1",
            "trip_id": "T1", "decision": "operate",
        }])
        result = observe_submission(
            "online_electric_bus_depot_replanning", self.app, plan
        )
        self.assertFalse(result["safe"])
        self.assertEqual(
            result["violations"][0]["group"], "trip-row-nullability"
        )

    def test_microgrid_observer_checks_reserve_against_ramp(self):
        write_csv(
            self.app / "data" / "generators.csv",
            ["generator_id", "max_kw", "ramp_up_kw"],
            [{"generator_id": "G1", "max_kw": 100, "ramp_up_kw": 20}],
        )
        plan = self.app / "submissions" / "solution.csv"
        fields = [
            "row_type", "scenario_id", "hour", "asset_id", "commit",
            "power_kw", "charge_kw", "discharge_kw", "soc_kwh",
            "curtail_kw", "shed_kw", "reserve_kw",
        ]
        write_csv(plan, fields, [{
            "row_type": "generator", "scenario_id": "S1", "hour": 3,
            "asset_id": "G1", "commit": 1, "power_kw": 50,
            "reserve_kw": 25,
        }])
        result = observe_submission(
            "robust_microgrid_islanding_dispatch", self.app, plan
        )
        self.assertFalse(result["safe"])
        self.assertEqual(
            result["violations"][0]["group"],
            "generator-reserve-ramp-headroom",
        )

    def test_evtol_observer_checks_turnaround_readiness(self):
        write_csv(
            self.app / "data" / "aircraft.csv",
            ["aircraft_id", "home_vertiport", "turnaround_slots"],
            [{"aircraft_id": "A1", "home_vertiport": "V1", "turnaround_slots": 2}],
        )
        write_csv(
            self.app / "data" / "scenario_aircraft_availability.csv",
            ["scenario_id", "aircraft_id", "release_slot"],
            [{"scenario_id": "S1", "aircraft_id": "A1", "release_slot": 1}],
        )
        plan = self.app / "submissions" / "solution.csv"
        fields = [
            "row_type", "scenario_id", "aircraft_id", "request_id",
            "origin", "destination", "start_slot", "end_slot", "energy_kwh",
            "quantity", "decision",
        ]
        write_csv(plan, fields, [
            {"row_type": "flight", "scenario_id": "S1", "aircraft_id": "A1", "request_id": "R1", "origin": "V1", "destination": "V2", "start_slot": 1, "end_slot": 3, "energy_kwh": 10, "quantity": 1, "decision": "serve"},
            {"row_type": "charge", "scenario_id": "S1", "aircraft_id": "A1", "origin": "V2", "start_slot": 4, "end_slot": 5, "energy_kwh": 5, "decision": "charge"},
        ])
        result = observe_submission("evtol_vertiport_recovery", self.app, plan)
        self.assertFalse(result["safe"])
        self.assertEqual(
            result["violations"][0]["group"],
            "aircraft-readiness-turnaround",
        )

    def test_sterile_observer_rejects_negative_exported_time(self):
        plan = self.app / "submissions" / "solution.csv"
        write_csv(
            plan,
            ["row_type", "start_min", "wash_start", "assembly_start", "sterilizer_start"],
            [{"row_type": "cart", "start_min": -5}],
        )
        result = observe_submission(
            "sterile_processing_robust_schedule", self.app, plan
        )
        self.assertFalse(result["safe"])
        self.assertEqual(result["violations"][0]["group"], "nonnegative-time")

    def test_petroleum_observer_replays_service_and_station_reserve(self):
        data = self.app / "data"
        (data / "config.json").write_text(
            json.dumps({"max_kl_per_active_stop": 10}), encoding="utf-8"
        )
        write_csv(data / "periods.csv", ["period_id", "day_index"], [
            {"period_id": "D1", "day_index": 1},
        ])
        write_csv(data / "products.csv", ["product_id"], [{"product_id": "P"}])
        write_csv(data / "terminals.csv", ["terminal_id"], [{"terminal_id": "T"}])
        write_csv(data / "stations.csv", ["station_id"], [{"station_id": "S"}])
        write_csv(
            data / "depots.csv",
            [
                "depot_id", "initial_P", "cap_P", "reserve_P",
            ],
            [{"depot_id": "DEP", "initial_P": 10, "cap_P": 20, "reserve_P": 2}],
        )
        write_csv(
            data / "station_tanks.csv",
            [
                "station_id", "product_id", "tank_capacity_kl",
                "initial_inventory_kl", "emergency_reserve_kl",
                "daily_service_floor",
            ],
            [{
                "station_id": "S", "product_id": "P", "tank_capacity_kl": 10,
                "initial_inventory_kl": 2, "emergency_reserve_kl": 1,
                "daily_service_floor": 0.8,
            }],
        )
        write_csv(
            data / "terminal_depot_lanes.csv",
            ["terminal_id", "depot_id", "product_id", "max_daily_kl"],
            [{"terminal_id": "T", "depot_id": "DEP", "product_id": "P", "max_daily_kl": 10}],
        )
        write_csv(
            data / "terminal_depot_commitments.csv",
            ["period_id", "terminal_id", "depot_id", "product_id", "minimum_commitment_kl"],
            [],
        )
        write_csv(
            data / "terminal_product_capacity.csv",
            ["period_id", "terminal_id", "product_id", "max_load_kl"],
            [{"period_id": "D1", "terminal_id": "T", "product_id": "P", "max_load_kl": 10}],
        )
        write_csv(
            data / "scenario_terminal_capacity.csv",
            ["scenario_id", "period_id", "terminal_id", "product_id", "capacity_multiplier"],
            [{"scenario_id": "SC", "period_id": "D1", "terminal_id": "T", "product_id": "P", "capacity_multiplier": 1}],
        )
        write_csv(
            data / "depot_station_lanes.csv",
            ["depot_id", "station_id", "product_id", "max_daily_kl"],
            [{"depot_id": "DEP", "station_id": "S", "product_id": "P", "max_daily_kl": 10}],
        )
        write_csv(
            data / "scenario_lane_impacts.csv",
            ["scenario_id", "period_id", "depot_id", "station_id", "product_id", "capacity_multiplier"],
            [{"scenario_id": "SC", "period_id": "D1", "depot_id": "DEP", "station_id": "S", "product_id": "P", "capacity_multiplier": 1}],
        )
        write_csv(
            data / "depot_fleet.csv",
            ["depot_id", "period_id", "max_outbound_kl", "max_active_stops"],
            [{"depot_id": "DEP", "period_id": "D1", "max_outbound_kl": 10, "max_active_stops": 1}],
        )
        write_csv(
            data / "depot_product_shift_capacity.csv",
            ["depot_id", "period_id", "product_id", "max_product_outbound_kl"],
            [{"depot_id": "DEP", "period_id": "D1", "product_id": "P", "max_product_outbound_kl": 10}],
        )
        write_csv(data / "scenarios.csv", ["scenario_id"], [{"scenario_id": "SC"}])
        write_csv(
            data / "scenario_demands.csv",
            ["scenario_id", "period_id", "station_id", "product_id", "demand_kl"],
            [{"scenario_id": "SC", "period_id": "D1", "station_id": "S", "product_id": "P", "demand_kl": 5}],
        )
        write_csv(
            data / "scenario_emergency_caps.csv",
            ["scenario_id", "product_id", "max_emergency_kl"],
            [{"scenario_id": "SC", "product_id": "P", "max_emergency_kl": 10}],
        )

        plan = self.app / "submissions" / "solution.csv"
        fields = [
            "row_type", "scenario_id", "period_id", "origin_id",
            "destination_id", "product_id", "quantity",
        ]
        valid_delivery = {
            "row_type": "delivery", "scenario_id": "SC", "period_id": "D1",
            "origin_id": "DEP", "destination_id": "S", "product_id": "P",
            "quantity": 4,
        }
        write_csv(plan, fields, [valid_delivery])
        result = observe_submission("petroleum_terminal_dro_inventory", self.app, plan)
        self.assertTrue(result["safe"])

        # The official capacity check is on ending inventory after same-day demand.
        write_csv(
            plan,
            fields,
            [
                {
                    "row_type": "inbound", "scenario_id": "", "period_id": "D1",
                    "origin_id": "T", "destination_id": "DEP", "product_id": "P",
                    "quantity": 1,
                },
                {**valid_delivery, "quantity": 9},
            ],
        )
        result = observe_submission("petroleum_terminal_dro_inventory", self.app, plan)
        self.assertTrue(result["safe"])

        write_csv(plan, fields, [{**valid_delivery, "quantity": 0}])
        result = observe_submission("petroleum_terminal_dro_inventory", self.app, plan)
        self.assertFalse(result["safe"])
        self.assertEqual(result["metrics"]["actual_violation_count"], 2)
        self.assertEqual(
            {item["group"] for item in result["violations"]},
            {"station-service-floor", "station-reserve"},
        )

    def test_liner_counts_intermediate_transfers_not_service_changes(self):
        write_csv(
            self.app / "data" / "bookings.csv",
            [
                "booking_id",
                "origin_port",
                "destination_port",
                "teu",
                "earliest_load_day",
                "due_arrival_day",
            ],
            [
                {
                    "booking_id": "B1",
                    "origin_port": "A",
                    "destination_port": "D",
                    "teu": 2,
                    "earliest_load_day": 1,
                    "due_arrival_day": 8,
                }
            ],
        )
        write_csv(
            self.app / "data" / "vessel_legs.csv",
            ["leg_id", "service", "from_port", "to_port", "depart_day", "arrive_day", "teu_capacity", "reefer_plug_capacity", "dg1_limit"],
            [
                {"leg_id": "L1", "service": "S", "from_port": "A", "to_port": "B", "depart_day": 1, "arrive_day": 2, "teu_capacity": 99, "reefer_plug_capacity": 99, "dg1_limit": 99},
                {"leg_id": "L2", "service": "S", "from_port": "B", "to_port": "C", "depart_day": 3, "arrive_day": 4, "teu_capacity": 99, "reefer_plug_capacity": 99, "dg1_limit": 99},
                {"leg_id": "L3", "service": "S", "from_port": "C", "to_port": "D", "depart_day": 5, "arrive_day": 6, "teu_capacity": 99, "reefer_plug_capacity": 99, "dg1_limit": 99},
            ],
        )
        write_csv(
            self.app / "data" / "booking_path_rules.csv",
            ["booking_id", "max_transship_count", "requires_direct", "allowed_services"],
            [{"booking_id": "B1", "max_transship_count": 1, "requires_direct": 0, "allowed_services": "S"}],
        )
        plan = self.app / "submissions" / "initial_plan.csv"
        write_csv(
            plan,
            ["row_type", "item_id", "decision", "leg_id", "quantity_teu"],
            [
                {"row_type": "booking_decision", "item_id": "B1", "decision": "accept"},
                {"row_type": "booking_leg", "item_id": "B1", "decision": "load", "leg_id": "L1", "quantity_teu": 2},
                {"row_type": "booking_leg", "item_id": "B1", "decision": "load", "leg_id": "L2", "quantity_teu": 2},
                {"row_type": "booking_leg", "item_id": "B1", "decision": "load", "leg_id": "L3", "quantity_teu": 2},
            ],
        )
        result = observe_submission(
            "online_liner_slot_empty_repositioning", self.app, plan
        )
        self.assertFalse(result["safe"])
        self.assertEqual(result["violation_count"], 1)
        self.assertEqual(result["violations"][0]["group"], "max-transshipments")

        write_csv(
            self.app / "data" / "booking_path_rules.csv",
            ["booking_id", "max_transship_count", "requires_direct", "allowed_services"],
            [{"booking_id": "B1", "max_transship_count": 2, "requires_direct": 0, "allowed_services": "S"}],
        )
        self.assertTrue(
            observe_submission(
                "online_liner_slot_empty_repositioning", self.app, plan
            )["safe"]
        )

    def test_liner_empty_move_day_matches_leg_departure(self):
        data = self.app / "data"
        write_csv(
            data / "bookings.csv",
            ["booking_id", "origin_port", "destination_port", "teu"],
            [],
        )
        write_csv(
            data / "booking_path_rules.csv",
            ["booking_id", "max_transship_count", "requires_direct", "allowed_services"],
            [],
        )
        write_csv(
            data / "vessel_legs.csv",
            [
                "leg_id", "service", "from_port", "to_port", "depart_day",
                "arrive_day", "teu_capacity", "reefer_plug_capacity", "dg1_limit",
            ],
            [{
                "leg_id": "L1", "service": "S", "from_port": "A", "to_port": "B",
                "depart_day": 1, "arrive_day": 2, "teu_capacity": 10,
                "reefer_plug_capacity": 10, "dg1_limit": 10,
            }],
        )
        plan = self.app / "submissions" / "plan.csv"
        fields = [
            "row_type", "item_id", "decision", "leg_id", "port_id", "day",
            "container_type", "quantity_teu",
        ]
        move = {
            "row_type": "empty_move", "item_id": "E1", "decision": "move",
            "leg_id": "L1", "day": 3, "container_type": "STD", "quantity_teu": 2,
        }
        write_csv(plan, fields, [move])
        result = observe_submission(
            "online_liner_slot_empty_repositioning", self.app, plan
        )
        self.assertEqual(
            [item["group"] for item in result["violations"]],
            ["empty-move-departure-day"],
        )

        write_csv(plan, fields, [{**move, "day": 1}])
        self.assertTrue(
            observe_submission(
                "online_liner_slot_empty_repositioning", self.app, plan
            )["safe"]
        )

    def test_liner_observer_enforces_event_acceptance_and_protected_slots(self):
        data = self.app / "data"
        booking_fields = [
            "booking_id", "origin_port", "destination_port", "container_type",
            "teu", "earliest_load_day", "due_arrival_day", "priority",
            "hazmat_class", "medical_flag",
        ]
        write_csv(data / "bookings.csv", booking_fields, [
            {"booking_id": "B1", "origin_port": "A", "destination_port": "D", "container_type": "REEFER", "teu": 5, "earliest_load_day": 1, "due_arrival_day": 3, "priority": "contract", "hazmat_class": "", "medical_flag": 1},
            {"booking_id": "B2", "origin_port": "A", "destination_port": "D", "container_type": "STD", "teu": 5, "earliest_load_day": 1, "due_arrival_day": 3, "priority": "contract", "hazmat_class": "", "medical_flag": 0},
            {"booking_id": "B3", "origin_port": "A", "destination_port": "D", "container_type": "STD", "teu": 5, "earliest_load_day": 1, "due_arrival_day": 3, "priority": "contract", "hazmat_class": "", "medical_flag": 0},
            {"booking_id": "B4", "origin_port": "A", "destination_port": "D", "container_type": "STD", "teu": 1, "earliest_load_day": 1, "due_arrival_day": 3, "priority": "spot", "hazmat_class": "", "medical_flag": 0},
        ])
        write_csv(
            data / "vessel_legs.csv",
            ["leg_id", "service", "from_port", "to_port", "depart_day", "arrive_day", "teu_capacity", "reefer_plug_capacity", "dg1_limit"],
            [{"leg_id": "L1", "service": "S", "from_port": "A", "to_port": "D", "depart_day": 1, "arrive_day": 2, "teu_capacity": 30, "reefer_plug_capacity": 10, "dg1_limit": 10}],
        )
        write_csv(
            data / "booking_path_rules.csv",
            ["booking_id", "max_transship_count", "requires_direct", "allowed_services"],
            [{"booking_id": booking_id, "max_transship_count": 0, "requires_direct": 1, "allowed_services": "S"} for booking_id in ("B1", "B2", "B3", "B4")],
        )
        write_csv(data / "special_booking_rules.csv", ["rule_id", "booking_id", "require_accept", "max_legs"], [
            {"rule_id": "MEDICAL", "booking_id": "B1", "require_accept": 1, "max_legs": 1},
        ])
        write_csv(data / "service_rules.csv", ["rule_id", "priority", "min_accept_rate"], [
            {"rule_id": "CONTRACT", "priority": "contract", "min_accept_rate": 0.82},
            {"rule_id": "SPOT", "priority": "spot", "min_accept_rate": 0.0},
        ])
        write_csv(data / "protected_slots.csv", ["rule_id", "leg_id", "priority", "min_teu"], [
            {"rule_id": "PROTECTED", "leg_id": "L1", "priority": "contract", "min_teu": 10},
        ])
        plan = self.app / "submissions" / "plan.csv"
        fields = ["row_type", "item_id", "decision", "leg_id", "quantity_teu"]
        write_csv(plan, fields, [
            {"row_type": "booking_decision", "item_id": "B1", "decision": "reject"},
            {"row_type": "booking_decision", "item_id": "B2", "decision": "accept"},
            {"row_type": "booking_leg", "item_id": "B2", "decision": "load", "leg_id": "L1", "quantity_teu": 5},
            {"row_type": "booking_decision", "item_id": "B3", "decision": "reject"},
            {"row_type": "booking_decision", "item_id": "B4", "decision": "accept"},
            {"row_type": "booking_leg", "item_id": "B4", "decision": "load", "leg_id": "L1", "quantity_teu": 1},
        ])
        result = observe_submission("online_liner_slot_empty_repositioning", self.app, plan)
        groups = {item["group"] for item in result["violations"]}
        self.assertIn("mandatory-medical-booking", groups)
        self.assertIn("special-booking-acceptance", groups)
        self.assertIn("priority-min-acceptance", groups)
        self.assertIn("protected-slot-minimum", groups)

        rows = []
        for booking_id, teu in (("B1", 5), ("B2", 5), ("B3", 5)):
            rows.extend([
                {"row_type": "booking_decision", "item_id": booking_id, "decision": "accept"},
                {"row_type": "booking_leg", "item_id": booking_id, "decision": "load", "leg_id": "L1", "quantity_teu": teu},
            ])
        rows.append({"row_type": "booking_decision", "item_id": "B4", "decision": "reject"})
        write_csv(plan, fields, rows)
        self.assertTrue(
            observe_submission("online_liner_slot_empty_repositioning", self.app, plan)["safe"]
        )

    def test_liner_observer_recomputes_customs_timing_and_capacity(self):
        data = self.app / "data"
        booking_fields = [
            "booking_id", "origin_port", "destination_port", "container_type",
            "teu", "earliest_load_day", "due_arrival_day", "priority",
            "hazmat_class", "medical_flag",
        ]
        write_csv(data / "bookings.csv", booking_fields, [
            {"booking_id": "B1", "origin_port": "A", "destination_port": "B", "container_type": "REEFER", "teu": 4, "earliest_load_day": 1, "due_arrival_day": 2, "priority": "spot", "hazmat_class": "", "medical_flag": 0},
            {"booking_id": "B2", "origin_port": "A", "destination_port": "B", "container_type": "REEFER", "teu": 4, "earliest_load_day": 1, "due_arrival_day": 2, "priority": "spot", "hazmat_class": "", "medical_flag": 0},
        ])
        write_csv(
            data / "vessel_legs.csv",
            ["leg_id", "service", "from_port", "to_port", "depart_day", "arrive_day", "teu_capacity", "reefer_plug_capacity", "dg1_limit"],
            [{"leg_id": "L1", "service": "S", "from_port": "A", "to_port": "B", "depart_day": 1, "arrive_day": 2, "teu_capacity": 20, "reefer_plug_capacity": 20, "dg1_limit": 20}],
        )
        write_csv(
            data / "booking_path_rules.csv",
            ["booking_id", "max_transship_count", "requires_direct", "allowed_services", "customs_group", "cold_chain_preclear"],
            [
                {"booking_id": "B1", "max_transship_count": 0, "requires_direct": 1, "allowed_services": "S", "customs_group": "cold_chain", "cold_chain_preclear": 0},
                {"booking_id": "B2", "max_transship_count": 0, "requires_direct": 1, "allowed_services": "S", "customs_group": "cold_chain", "cold_chain_preclear": 1},
            ],
        )
        customs_fields = [
            "port_id", "day", "customs_group", "max_teu",
            "max_medical_reefer_teu", "inspection_cost_per_teu",
        ]
        write_csv(data / "customs_inspection_windows.csv", customs_fields, [
            {"port_id": "A", "day": 0, "customs_group": "cold_chain", "max_teu": 4, "max_medical_reefer_teu": 4, "inspection_cost_per_teu": 0},
            {"port_id": "A", "day": 1, "customs_group": "cold_chain", "max_teu": 3, "max_medical_reefer_teu": 4, "inspection_cost_per_teu": 0},
            {"port_id": "B", "day": 2, "customs_group": "cold_chain", "max_teu": 8, "max_medical_reefer_teu": 8, "inspection_cost_per_teu": 0},
        ])
        plan = self.app / "submissions" / "plan.csv"
        fields = ["row_type", "item_id", "decision", "leg_id", "port_id", "day", "container_type", "quantity_teu"]
        write_csv(plan, fields, [
            {"row_type": "booking_decision", "item_id": "B1", "decision": "accept"},
            {"row_type": "booking_leg", "item_id": "B1", "decision": "load", "leg_id": "L1", "quantity_teu": 4},
            {"row_type": "booking_decision", "item_id": "B2", "decision": "accept"},
            {"row_type": "booking_leg", "item_id": "B2", "decision": "load", "leg_id": "L1", "quantity_teu": 4},
        ])
        result = observe_submission("online_liner_slot_empty_repositioning", self.app, plan)
        customs = [item for item in result["violations"] if item["group"] == "customs-capacity"]
        self.assertEqual(len(customs), 1)
        self.assertEqual(customs[0]["evidence"]["day"], 1)
        self.assertEqual(result["metrics"]["customs_loads"]["A|0|cold_chain"], 4)
        self.assertEqual(result["metrics"]["customs_loads"]["A|1|cold_chain"], 4)
        self.assertEqual(result["metrics"]["customs_loads"]["B|2|cold_chain"], 8)

        rows = read_csv(data / "customs_inspection_windows.csv")
        rows[1]["max_teu"] = 4
        write_csv(data / "customs_inspection_windows.csv", customs_fields, rows)
        self.assertTrue(
            observe_submission("online_liner_slot_empty_repositioning", self.app, plan)["safe"]
        )

    def test_liner_observer_recomputes_gate_and_inventory_flow(self):
        data = self.app / "data"
        write_csv(data / "bookings.csv", [
            "booking_id", "origin_port", "destination_port", "container_type",
            "teu", "earliest_load_day", "due_arrival_day", "priority",
            "hazmat_class", "medical_flag",
        ], [
            {"booking_id": "B1", "origin_port": "A", "destination_port": "B", "container_type": "STD", "teu": 3, "earliest_load_day": 1, "due_arrival_day": 2, "priority": "spot", "hazmat_class": "", "medical_flag": 0},
        ])
        write_csv(data / "vessel_legs.csv", [
            "leg_id", "service", "from_port", "to_port", "depart_day",
            "arrive_day", "teu_capacity", "reefer_plug_capacity", "dg1_limit",
        ], [
            {"leg_id": "L1", "service": "S", "from_port": "A", "to_port": "B", "depart_day": 1, "arrive_day": 2, "teu_capacity": 10, "reefer_plug_capacity": 10, "dg1_limit": 10},
        ])
        write_csv(data / "booking_path_rules.csv", [
            "booking_id", "max_transship_count", "requires_direct", "allowed_services",
        ], [
            {"booking_id": "B1", "max_transship_count": 0, "requires_direct": 1, "allowed_services": "S"},
        ])
        write_csv(data / "ports.csv", [
            "port_id", "initial_std", "initial_reefer", "yard_capacity",
            "reefer_plug_capacity", "gate_out_default", "gate_in_default",
        ], [
            {"port_id": "A", "initial_std": 2, "initial_reefer": 0, "yard_capacity": 10, "reefer_plug_capacity": 10, "gate_out_default": 10, "gate_in_default": 10},
            {"port_id": "B", "initial_std": 0, "initial_reefer": 0, "yard_capacity": 10, "reefer_plug_capacity": 10, "gate_out_default": 10, "gate_in_default": 10},
        ])
        limit_fields = [
            "port_id", "day", "gate_out_limit", "gate_in_limit",
            "yard_capacity", "reefer_plug_capacity",
        ]
        write_csv(data / "port_day_limits.csv", limit_fields, [
            {"port_id": "A", "day": 1, "gate_out_limit": 3, "gate_in_limit": 10, "yard_capacity": 10, "reefer_plug_capacity": 10},
            {"port_id": "B", "day": 2, "gate_out_limit": 10, "gate_in_limit": 3, "yard_capacity": 10, "reefer_plug_capacity": 10},
        ])
        write_csv(data / "lease_options.csv", [
            "port_id", "day", "container_type", "max_teu", "lease_cost_per_teu",
        ], [
            {"port_id": "A", "day": 1, "container_type": "STD", "max_teu": 1, "lease_cost_per_teu": 1},
        ])
        plan = self.app / "submissions" / "plan.csv"
        fields = ["row_type", "item_id", "decision", "leg_id", "port_id", "day", "container_type", "quantity_teu"]
        feasible_rows = [
            {"row_type": "booking_decision", "item_id": "B1", "decision": "accept"},
            {"row_type": "booking_leg", "item_id": "B1", "decision": "load", "leg_id": "L1", "quantity_teu": 3},
            {"row_type": "lease", "item_id": "Q1", "decision": "lease", "port_id": "A", "day": 1, "container_type": "STD", "quantity_teu": 1},
        ]
        write_csv(plan, fields, feasible_rows)
        self.assertTrue(
            observe_submission("online_liner_slot_empty_repositioning", self.app, plan)["safe"]
        )

        tight_limits = read_csv(data / "port_day_limits.csv")
        tight_limits[0]["gate_out_limit"] = 2
        write_csv(data / "port_day_limits.csv", limit_fields, tight_limits)
        result = observe_submission("online_liner_slot_empty_repositioning", self.app, plan)
        self.assertIn("gate-out-capacity", {item["group"] for item in result["violations"]})

        tight_limits[0]["gate_out_limit"] = 3
        write_csv(data / "port_day_limits.csv", limit_fields, tight_limits)
        write_csv(plan, fields, feasible_rows[:-1])
        result = observe_submission("online_liner_slot_empty_repositioning", self.app, plan)
        self.assertIn("inventory-availability", {item["group"] for item in result["violations"]})

    def test_flood_observer_rejects_metric_closure_boat_hop(self):
        data = self.app / "data"
        (data / "config.json").write_text(json.dumps({
            "depot_id": "D0", "drone_capacity_kg": 18, "drone_endurance_minutes": 58,
            "boat_capacity_kg": 96, "boat_trip_limit_minutes": 115,
            "boat_setup_minutes": 10, "boat_unload_minutes_per_shelter": 4,
        }))
        write_csv(data / "staging_points.csv", ["node_id", "launch_clearance", "boat_ramp"], [
            {"node_id": "D0", "launch_clearance": "good", "boat_ramp": 0},
            {"node_id": "R", "launch_clearance": "good", "boat_ramp": 1},
        ])
        write_csv(data / "shelters.csv", ["shelter_id", "area_type", "nearest_dry_node", "demand_kg"], [
            {"shelter_id": "H1", "area_type": "flooded", "nearest_dry_node": "", "demand_kg": 20},
            {"shelter_id": "H2", "area_type": "flooded", "nearest_dry_node": "", "demand_kg": 20},
        ])
        write_csv(data / "drone_options.csv", ["shelter_id", "launch_node", "flight_minutes", "payload_feasible"], [])
        write_csv(data / "road_edges.csv", ["from_node", "to_node", "usable_after_flood"], [
            {"from_node": "D0", "to_node": "R", "usable_after_flood": 1},
        ])
        write_csv(data / "boat_travel.csv", ["from_id", "to_id", "minutes"], [
            {"from_id": "R", "to_id": "H1", "minutes": 10},
            {"from_id": "R", "to_id": "H2", "minutes": 10},
        ])
        write_csv(data / "weather_windows.csv", ["boat_multiplier"], [{"boat_multiplier": 1.1}])
        solution = self.app / "submissions" / "solution.csv"
        fields = ["row_type", "sequence", "node_id", "trip_id", "mode", "shelter_id", "launch_node"]
        write_csv(solution, fields, [
            {"row_type": "truck_stop", "sequence": 1, "node_id": "D0"},
            {"row_type": "truck_stop", "sequence": 2, "node_id": "R"},
            {"row_type": "truck_stop", "sequence": 3, "node_id": "D0"},
            {"row_type": "delivery", "sequence": 1, "trip_id": "BT1", "mode": "boat", "shelter_id": "H1", "launch_node": "R"},
            {"row_type": "delivery", "sequence": 2, "trip_id": "BT1", "mode": "boat", "shelter_id": "H2", "launch_node": "R"},
        ])
        result = observe_submission("flood_relief_multimodal_routing", self.app, solution)
        water = [item for item in result["violations"] if item["group"] == "boat-water-continuity"]
        self.assertEqual(len(water), 1)
        self.assertEqual(water[0]["evidence"]["from_id"], "H1")
        self.assertEqual(water[0]["evidence"]["to_id"], "H2")

        write_csv(data / "boat_travel.csv", ["from_id", "to_id", "minutes"], [
            {"from_id": "R", "to_id": "H1", "minutes": 10},
            {"from_id": "H1", "to_id": "H2", "minutes": 8},
            {"from_id": "R", "to_id": "H2", "minutes": 10},
        ])
        self.assertTrue(
            observe_submission("flood_relief_multimodal_routing", self.app, solution)["safe"]
        )

        write_csv(data / "staging_points.csv", ["node_id", "launch_clearance", "boat_ramp"], [
            {"node_id": "D0", "launch_clearance": "good", "boat_ramp": 0},
            {"node_id": "M", "launch_clearance": "good", "boat_ramp": 0},
            {"node_id": "R", "launch_clearance": "good", "boat_ramp": 1},
        ])
        write_csv(data / "road_edges.csv", ["from_node", "to_node", "usable_after_flood"], [
            {"from_node": "D0", "to_node": "M", "usable_after_flood": 1},
            {"from_node": "M", "to_node": "R", "usable_after_flood": 1},
        ])
        self.assertTrue(
            observe_submission("flood_relief_multimodal_routing", self.app, solution)["safe"]
        )

    def test_medical_observer_aggregates_actual_window_load(self):
        data = self.app / "data"
        (data / "config.json").write_text(
            json.dumps(
                {
                    "curfew_start_min": 1000,
                    "curfew_end_min": 1100,
                    "load_energy_kwh_per_kg_km": 0.0,
                    "max_open_facilities": 2,
                    "minimum_facility_throughput_kg": 1.0,
                }
            )
        )
        write_csv(
            data / "clinics.csv",
            ["clinic_id", "node_id", "red_kg", "yellow_kg", "service_min", "earliest_min", "latest_min", "max_red_onboard_min", "max_yellow_onboard_min", "priority_class"],
            [
                {"clinic_id": "H1", "node_id": "H1", "red_kg": 40, "yellow_kg": 20, "service_min": 0, "earliest_min": 0, "latest_min": 1000, "max_red_onboard_min": 1000, "max_yellow_onboard_min": 1000, "priority_class": "routine"},
                {"clinic_id": "H2", "node_id": "H2", "red_kg": 45, "yellow_kg": 15, "service_min": 0, "earliest_min": 0, "latest_min": 1000, "max_red_onboard_min": 1000, "max_yellow_onboard_min": 1000, "priority_class": "routine"},
            ],
        )
        vehicle_fields = ["vehicle_id", "start_node", "end_node", "capacity_kg", "red_compartment_kg", "yellow_compartment_kg", "battery_kwh", "start_battery_kwh", "shift_start_min", "shift_end_min"]
        write_csv(data / "vehicles.csv", vehicle_fields, [
            {"vehicle_id": "V1", "start_node": "D", "end_node": "D", "capacity_kg": 200, "red_compartment_kg": 200, "yellow_compartment_kg": 200, "battery_kwh": 100, "start_battery_kwh": 100, "shift_start_min": 0, "shift_end_min": 1000},
            {"vehicle_id": "V2", "start_node": "D", "end_node": "D", "capacity_kg": 200, "red_compartment_kg": 200, "yellow_compartment_kg": 200, "battery_kwh": 100, "start_battery_kwh": 100, "shift_start_min": 0, "shift_end_min": 1000},
        ])
        write_csv(data / "vehicle_waste_permits.csv", ["vehicle_id", "allow_routine", "max_isolation_pickups"], [
            {"vehicle_id": "V1", "allow_routine": 1, "max_isolation_pickups": 0},
            {"vehicle_id": "V2", "allow_routine": 1, "max_isolation_pickups": 0},
        ])
        write_csv(data / "treatment_facilities.csv", ["facility_id", "node_id", "daily_capacity_kg", "red_capacity_kg", "yellow_capacity_kg", "latest_receive_min"], [
            {"facility_id": "F", "node_id": "F", "daily_capacity_kg": 500, "red_capacity_kg": 500, "yellow_capacity_kg": 500, "latest_receive_min": 1000}
        ])
        write_csv(data / "facility_waste_rules.csv", ["facility_id", "waste_type", "accepted"], [
            {"facility_id": "F", "waste_type": "red", "accepted": 1},
            {"facility_id": "F", "waste_type": "yellow", "accepted": 1},
        ])
        write_csv(data / "facility_receiving_windows.csv", ["facility_id", "window_id", "start_min", "end_min", "max_unloads", "max_kg"], [
            {"facility_id": "F", "window_id": "early", "start_min": 0, "end_min": 100, "max_unloads": 3, "max_kg": 100}
        ])
        write_csv(data / "incompatible_pickup_pairs.csv", ["clinic_a", "clinic_b"], [])
        write_csv(data / "chargers.csv", ["charger_id", "node_id", "rate_kwh_per_min", "queue_delay_min", "daily_kwh_cap", "session_limit"], [])
        arc_fields = ["from_node", "to_node", "distance_km", "travel_min", "empty_energy_kwh", "loaded_curfew_flag"]
        write_csv(data / "travel_energy_arcs.csv", arc_fields, [
            {"from_node": "D", "to_node": "H1", "distance_km": 1, "travel_min": 10, "empty_energy_kwh": 1, "loaded_curfew_flag": 0},
            {"from_node": "H1", "to_node": "F", "distance_km": 1, "travel_min": 10, "empty_energy_kwh": 1, "loaded_curfew_flag": 0},
            {"from_node": "D", "to_node": "H2", "distance_km": 1, "travel_min": 10, "empty_energy_kwh": 1, "loaded_curfew_flag": 0},
            {"from_node": "H2", "to_node": "F", "distance_km": 1, "travel_min": 10, "empty_energy_kwh": 1, "loaded_curfew_flag": 0},
            {"from_node": "F", "to_node": "D", "distance_km": 1, "travel_min": 10, "empty_energy_kwh": 1, "loaded_curfew_flag": 0},
        ])
        solution = self.app / "submissions" / "solution.csv"
        rows = []
        for vehicle_id, clinic_id in (("V1", "H1"), ("V2", "H2")):
            rows.extend([
                {"row_type": "start", "vehicle_id": vehicle_id, "sequence": 1, "node_id": "D", "clinic_id": "", "facility_id": "", "charger_id": "", "charge_kwh": ""},
                {"row_type": "pickup", "vehicle_id": vehicle_id, "sequence": 2, "node_id": clinic_id, "clinic_id": clinic_id, "facility_id": "", "charger_id": "", "charge_kwh": ""},
                {"row_type": "unload", "vehicle_id": vehicle_id, "sequence": 3, "node_id": "F", "clinic_id": "", "facility_id": "F", "charger_id": "", "charge_kwh": ""},
                {"row_type": "end", "vehicle_id": vehicle_id, "sequence": 4, "node_id": "D", "clinic_id": "", "facility_id": "", "charger_id": "", "charge_kwh": ""},
            ])
        write_csv(solution, ["row_type", "vehicle_id", "sequence", "node_id", "clinic_id", "facility_id", "charger_id", "charge_kwh"], rows)
        result = observe_submission("electric_medical_waste_lrp", self.app, solution)
        groups = [item["group"] for item in result["violations"]]
        self.assertIn("facility-window-kg-cap", groups)
        window_violation = next(item for item in result["violations"] if item["group"] == "facility-window-kg-cap")
        self.assertEqual(window_violation["evidence"]["actual"], 120.0)

        for row in rows:
            row["sequence"] = int(row["sequence"]) - 1
        write_csv(solution, ["row_type", "vehicle_id", "sequence", "node_id", "clinic_id", "facility_id", "charger_id", "charge_kwh"], rows)
        invalid_sequence = observe_submission(
            "electric_medical_waste_lrp", self.app, solution
        )
        self.assertFalse(invalid_sequence["safe"])
        self.assertIn(
            "route-sequence",
            {item["group"] for item in invalid_sequence["violations"]},
        )

    def test_aerospace_observer_recomputes_packing_and_interval_resources(self):
        data = self.app / "data"
        (data / "config.json").write_text(json.dumps({
            "boundary_epsilon_cm": 1e-6,
            "coupon_panel_inspection_minutes": 5,
            "planning_horizon_min": 1000,
        }))
        part_fields = [
            "part_id", "program_id", "recipe_id", "fixture_type", "width_cm",
            "height_cm", "rotate_ok", "thickness_mm", "release_min",
            "max_out_time_min", "due_min", "priority_weight", "vacuum_ports",
            "thermocouples", "requires_ultra_uniform", "preferred_zone",
            "non_round_quality_index", "ndt_required", "coupon_panels",
            "cooling_class",
        ]
        write_csv(data / "parts.csv", part_fields, [
            {"part_id": "P1", "program_id": "G", "recipe_id": "R", "fixture_type": "F", "width_cm": 10, "height_cm": 10, "rotate_ok": 1, "thickness_mm": 2, "release_min": 0, "max_out_time_min": 500, "due_min": 900, "priority_weight": 1, "vacuum_ports": 1, "thermocouples": 1, "requires_ultra_uniform": 0, "preferred_zone": "center", "non_round_quality_index": 1, "ndt_required": 0, "coupon_panels": 0, "cooling_class": "standard"},
            {"part_id": "P2", "program_id": "G", "recipe_id": "R", "fixture_type": "F", "width_cm": 10, "height_cm": 10, "rotate_ok": 1, "thickness_mm": 2, "release_min": 0, "max_out_time_min": 500, "due_min": 900, "priority_weight": 1, "vacuum_ports": 1, "thermocouples": 1, "requires_ultra_uniform": 0, "preferred_zone": "any", "non_round_quality_index": 1, "ndt_required": 0, "coupon_panels": 0, "cooling_class": "standard"},
        ])
        write_csv(data / "autoclaves.csv", ["autoclave_id", "width_cm", "height_cm", "vacuum_ports", "thermocouple_ports", "energy_kw", "operator_skill", "uniformity_class"], [
            {"autoclave_id": "A", "width_cm": 100, "height_cm": 100, "vacuum_ports": 10, "thermocouple_ports": 10, "energy_kw": 10, "operator_skill": "S", "uniformity_class": "standard"},
        ])
        write_csv(data / "recipes.csv", ["recipe_id", "resin_family", "cure_min", "load_fixed_min", "load_per_part_min", "unload_min", "energy_multiplier", "thermal_group"], [
            {"recipe_id": "R", "resin_family": "X", "cure_min": 100, "load_fixed_min": 10, "load_per_part_min": 1, "unload_min": 5, "energy_multiplier": 1, "thermal_group": "T"},
        ])
        write_csv(data / "autoclave_recipe_eligibility.csv", ["autoclave_id", "recipe_id", "process_multiplier", "max_thickness_mm", "supports_ultra_uniform"], [
            {"autoclave_id": "A", "recipe_id": "R", "process_multiplier": 1, "max_thickness_mm": 10, "supports_ultra_uniform": 1},
        ])
        write_csv(data / "recipe_batch_rules.csv", ["recipe_id", "max_parts_per_batch", "max_area_fraction", "max_programs_per_batch", "max_coupon_panels", "ndt_minutes_fixed", "ndt_minutes_per_part", "cooling_fixed_min", "cooling_per_part_min"], [
            {"recipe_id": "R", "max_parts_per_batch": 2, "max_area_fraction": 0.8, "max_programs_per_batch": 1, "max_coupon_panels": 0, "ndt_minutes_fixed": 10, "ndt_minutes_per_part": 1, "cooling_fixed_min": 1, "cooling_per_part_min": 0},
        ])
        write_csv(data / "vacuum_manifold_limits.csv", ["autoclave_id", "side", "max_vacuum_ports"], [
            {"autoclave_id": "A", "side": "left", "max_vacuum_ports": 5},
            {"autoclave_id": "A", "side": "right", "max_vacuum_ports": 5},
        ])
        write_csv(data / "fixtures.csv", ["fixture_type", "capacity", "setup_buffer_min"], [
            {"fixture_type": "F", "capacity": 2, "setup_buffer_min": 0},
        ])
        write_csv(data / "setup_times.csv", ["from_resin_family", "to_resin_family", "setup_min"], [
            {"from_resin_family": "X", "to_resin_family": "X", "setup_min": 0.1},
        ])
        write_csv(data / "maintenance_windows.csv", ["autoclave_id", "start_min", "end_min", "reason"], [])
        write_csv(data / "operator_calendar.csv", ["skill", "start_min", "end_min", "capacity"], [
            {"skill": "S", "start_min": 0, "end_min": 1000, "capacity": 2},
        ])
        write_csv(data / "energy_tariffs.csv", ["start_min", "end_min", "tariff_per_kwh", "max_active_autoclaves", "label"], [
            {"start_min": 0, "end_min": 1000, "tariff_per_kwh": 1, "max_active_autoclaves": 1, "label": "all"},
        ])
        write_csv(data / "cooling_lane_capacity.csv", ["start_min", "end_min", "capacity"], [
            {"start_min": 0, "end_min": 1000, "capacity": 2},
        ])
        write_csv(data / "post_cure_inspection_capacity.csv", ["resource", "start_min", "end_min", "capacity"], [
            {"resource": "ndt_cell", "start_min": 0, "end_min": 1000, "capacity": 1},
        ])
        write_csv(data / "program_commitments.csv", ["program_id", "min_on_time_fraction", "commitment_due_min", "penalty_if_short"], [
            {"program_id": "G", "min_on_time_fraction": 1, "commitment_due_min": 1000, "penalty_if_short": 0},
        ])
        solution = self.app / "submissions" / "solution.json"
        solution.write_text(json.dumps({"batches": [
            {"batch_id": "B1", "autoclave_id": "A", "recipe_id": "R", "start_min": 0, "parts": [{"part_id": "P1", "x_cm": 0.1, "y_cm": 10, "rotated": False}]},
            {"batch_id": "B2", "autoclave_id": "A", "recipe_id": "R", "start_min": 50, "parts": [{"part_id": "P2", "x_cm": 20, "y_cm": 20, "rotated": False}]},
        ]}))

        result = observe_submission(
            "aerospace_composite_autoclave_scheduling", self.app, solution
        )
        groups = {item["group"] for item in result["violations"]}
        self.assertIn("placement-zone-center", groups)
        self.assertIn("autoclave-nonoverlap", groups)
        self.assertIn("energy-window-capacity", groups)
        self.assertEqual(result["metrics"]["assigned_part_count"], 2)
        self.assertEqual(
            result["metrics"]["conflict_batch_context"]["B1"]["autoclave_id"],
            "A",
        )
        self.assertEqual(
            result["metrics"]["conflict_batch_context"]["B1"][
                "same_machine_neighbors"
            ],
            ["B2"],
        )

        # Eight-decimal exports can round a computed busy end just below the next
        # start. Keep cleanup clearance separate and use the robust center band.
        rounded_busy_end = round(10 + 1 + 0.45 + 0.022 + 100 + 5, 8)
        solution.write_text(json.dumps({"batches": [
            {"batch_id": "B1", "autoclave_id": "A", "recipe_id": "R", "start_min": 0, "parts": [{"part_id": "P1", "x_cm": 9.9, "y_cm": 10.1, "rotated": False}]},
            {"batch_id": "B2", "autoclave_id": "A", "recipe_id": "R", "start_min": rounded_busy_end, "parts": [{"part_id": "P2", "x_cm": 20, "y_cm": 20, "rotated": False}]},
        ]}))
        boundary_result = observe_submission(
            "aerospace_composite_autoclave_scheduling", self.app, solution
        )
        boundary_groups = {
            item["group"] for item in boundary_result["violations"]
        }
        self.assertIn("placement-zone-center", boundary_groups)
        self.assertIn("resin-cleanup-separation", boundary_groups)
        self.assertNotIn("autoclave-nonoverlap", boundary_groups)
        self.assertNotIn("energy-window-capacity", boundary_groups)
        self.assertEqual(
            boundary_result["metrics"]["temporal_clearance_margin_min"], 0.0
        )
        self.assertEqual(
            boundary_result["metrics"]["recommended_temporal_clearance_margin_min"],
            1e-2,
        )
        self.assertEqual(
            boundary_result["metrics"]["center_clearance_fraction"], 0.10
        )
        self.assertEqual(
            boundary_result["metrics"]["center_clearance_extra_cm"], 2.0
        )
        self.assertEqual(
            boundary_result["metrics"]["setup_clearance_margin_min"], 0.1
        )
        self.assertEqual(
            boundary_result["metrics"]["cooling_class_extra_minutes"]["slow"],
            8.0,
        )

        # Starting exactly at release + max_out_time is public-feasible and must
        # not create a zero-residual out-time violation from a safety margin.
        solution.write_text(json.dumps({"batches": [
            {
                "batch_id": "B1",
                "autoclave_id": "A",
                "recipe_id": "R",
                "start_min": 500,
                "parts": [
                    {"part_id": "P1", "x_cm": 10.1, "y_cm": 10.1, "rotated": False},
                    {"part_id": "P2", "x_cm": 20.1, "y_cm": 20.1, "rotated": False},
                ],
            },
        ]}))
        exact_out_time = observe_submission(
            "aerospace_composite_autoclave_scheduling", self.app, solution
        )
        self.assertNotIn(
            "part-out-time",
            {item["group"] for item in exact_out_time["violations"]},
        )


if __name__ == "__main__":
    unittest.main()
