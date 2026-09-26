import json
import tempfile
import unittest
from pathlib import Path

from veras_harness import runner


class PublicAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Path(self.temp.name)
        (self.app / "submissions").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_csv_template_and_python_syntax(self):
        submissions = self.app / "submissions"
        (submissions / "solution_template.csv").write_text("id,value\n")
        (submissions / "solution.csv").write_text("id,value\na,1\n")
        (submissions / "solve.py").write_text("print('ok')\n")
        audit = runner.public_audit(
            self.app,
            "Write /app/submissions/solution.csv and /app/submissions/solve.py",
        )
        self.assertTrue(audit["target_valid"])
        self.assertTrue(audit["python_valid"])
        self.assertEqual(audit["required_present"], 2)

    def test_structural_regression_is_detected(self):
        before = {
            "target_valid": True,
            "target_exists": True,
            "primary_target_valid": True,
            "primary_target_exists": True,
            "python_valid": True,
            "required_present": 3,
        }
        after = {
            "target_valid": False,
            "target_exists": True,
            "primary_target_valid": False,
            "primary_target_exists": True,
            "python_valid": False,
            "required_present": 2,
        }
        rejected, reasons = runner.regresses(before, after)
        self.assertTrue(rejected)
        self.assertEqual(len(reasons), 3)

    def test_thread_parsing(self):
        events = self.app / "events.jsonl"
        rows = [
            {"type": "thread.started", "thread_id": "abc"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "audit result"},
            },
        ]
        events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertEqual(runner.thread_id(events), "abc")

    def test_redaction_skips_unreadable_zero_byte_placeholders(self):
        placeholder = self.app / "codex"
        secret_file = self.app / "events.jsonl"
        placeholder.touch()
        placeholder.chmod(0)
        secret_file.write_text("prefix-secret-suffix")
        try:
            count = runner.redact_tree(self.app, "secret")
        finally:
            placeholder.chmod(0o600)
        self.assertEqual(count, 1)
        self.assertEqual(secret_file.read_text(), "prefix-[REDACTED]-suffix")

    def test_primary_target_does_not_fall_back_to_previous_step(self):
        submissions = self.app / "submissions"
        (submissions / "step3.csv").write_text("id,value\na,1\n")
        audit = runner.public_audit(
            self.app,
            "Write /app/submissions/step4.csv using /app/submissions/step3.csv.",
        )
        self.assertFalse(audit["primary_target_valid"])
        self.assertTrue(audit["target_valid"])
        self.assertEqual(audit["primary_target"]["path"], "submissions/step4.csv")

    def test_solver_candidates_are_scoped_to_current_step(self):
        submissions = self.app / "submissions"
        old_solver = submissions / "solve_initial.py"
        current_solver = submissions / "solve_step2.py"
        old_solver.write_text("print('old')\n")
        current_solver.write_text("print('current')\n")
        candidates = runner.solver_candidates(
            self.app,
            "Write /app/submissions/solve_step2.py and "
            "/app/submissions/step2_replan.csv; preserve initial_plan.csv.",
        )
        self.assertEqual(candidates, [current_solver])

    def test_stage_protocol_names_only_primary_current_artifact(self):
        protocol = runner.stage_protocol(
            "test_task",
            self.app,
            "Write /app/submissions/step2_replan.csv while preserving "
            "/app/submissions/initial_plan.csv.",
        )
        self.assertIn("`/app/submissions/step2_replan.csv`", protocol)
        self.assertNotIn("`/app/submissions/initial_plan.csv`", protocol)

    def test_stage_protocol_contains_control_and_math_mechanisms(self):
        protocol = runner.stage_protocol(
            "test_schedule", self.app, "Write /app/submissions/solution.csv."
        )
        for phrase in (
            "RESTORATION",
            "PERFORMANCE",
            "Lyapunov-like",
            "elastic Phase-I",
            "repair MIP",
            "constraint tightening",
            "local branching",
        ):
            self.assertIn(phrase, protocol)

    def test_control_challenges_an_unobserved_valid_incumbent(self):
        submissions = self.app / "submissions"
        (submissions / "solution.csv").write_text("id,value\na,1\n")
        instruction = "Write /app/submissions/solution.csv."
        audit = runner.public_audit(self.app, instruction)
        decision = runner.control_decision(
            self.app, instruction, audit, remaining_seconds=120.0
        )
        self.assertEqual(decision["mode"], "unverified-incumbent")
        self.assertEqual(decision["action"], "challenge-then-improve")

    def test_control_optimizes_an_observer_safe_incumbent(self):
        submissions = self.app / "submissions"
        (submissions / "solution.csv").write_text("id,value\na,1\n")
        instruction = "Write /app/submissions/solution.csv."
        audit = runner.public_audit(self.app, instruction)
        observation = {
            "supported": True,
            "safe": True,
            "coverage_complete": True,
            "observer": "test-observer",
            "violation_count": 0,
            "max_normalized_residual": 0.0,
        }
        decision = runner.control_decision(
            self.app, instruction, audit, 120.0, observation
        )
        self.assertEqual(decision["mode"], "performance")
        self.assertEqual(decision["action"], "resume-agent-performance")

    def test_control_executes_solver_only_when_artifact_is_invalid(self):
        submissions = self.app / "submissions"
        (submissions / "solve.py").write_text("print('solve')\n")
        instruction = (
            "Write /app/submissions/solve.py and "
            "/app/submissions/solution.csv."
        )
        audit = runner.public_audit(self.app, instruction)
        decision = runner.control_decision(
            self.app, instruction, audit, remaining_seconds=120.0
        )
        self.assertEqual(decision["mode"], "restoration")
        self.assertEqual(decision["action"], "execute-current-stage-solver")
        self.assertIn("primary-artifact-missing", decision["triggers"])

    def test_control_restores_structurally_valid_but_unsafe_artifact(self):
        submissions = self.app / "submissions"
        (submissions / "solution.csv").write_text("id,value\na,1\n")
        instruction = "Write /app/submissions/solution.csv."
        audit = runner.public_audit(self.app, instruction)
        observation = {
            "supported": True,
            "safe": False,
            "observer": "test-observer",
            "violation_count": 1,
            "max_normalized_residual": 0.25,
            "violations": [{"group": "capacity"}],
        }
        decision = runner.control_decision(
            self.app,
            instruction,
            audit,
            remaining_seconds=120.0,
            observation=observation,
        )
        self.assertEqual(decision["mode"], "restoration")
        self.assertEqual(decision["action"], "resume-agent-restoration")
        self.assertIn("semantic-violation:capacity", decision["triggers"])

    def test_veras_reserves_thirty_percent_by_default(self):
        self.assertEqual(runner.HARNESS_NAME, "VERAS")
        self.assertEqual(runner.DEFAULT_RESERVE_FRACTION, 0.30)

    def test_invalid_solver_is_not_executed(self):
        solver = self.app / "submissions" / "solve.py"
        solver.write_text("def broken(:\n")
        self.assertEqual(
            runner.solver_candidates(
                self.app, "Run /app/submissions/solve.py to create solution.csv"
            ),
            [],
        )

    def test_transient_transport_detection_requires_failed_process(self):
        stderr = self.app / "stderr.log"
        stderr.write_text("unexpected status 503 Service Unavailable")
        self.assertTrue(
            runner.transient_transport_failure({"return_code": 1}, stderr)
        )
        self.assertFalse(
            runner.transient_transport_failure({"return_code": 0}, stderr)
        )

    def test_transport_circuit_breaker_distinguishes_daily_and_qpm_limits(self):
        stderr = self.app / "stderr.log"
        stderr.write_text("Daily Limits exceeded")
        self.assertEqual(
            runner.transport_failure_kind({"return_code": 1}, stderr),
            "daily-limit",
        )
        self.assertEqual(runner.retry_delay("daily-limit", 1, 500.0), 0.0)
        stderr.write_text("429 Too Many Requests: QPM limit exceeded")
        self.assertEqual(
            runner.transport_failure_kind({"return_code": 1}, stderr),
            "rate-limit",
        )
        self.assertGreaterEqual(runner.retry_delay("rate-limit", 1, 500.0), 45.0)

    def test_semantic_regression_rejects_a_new_violation(self):
        before = {
            "supported": True,
            "safe": True,
            "max_normalized_residual": 0.0,
            "total_normalized_violation_mass": 0.0,
            "violation_count": 0,
        }
        after = {
            "supported": True,
            "safe": False,
            "max_normalized_residual": 0.1,
            "total_normalized_violation_mass": 0.1,
            "violation_count": 1,
        }
        regressed, reasons = runner.semantic_regresses(before, after)
        self.assertTrue(regressed)
        self.assertTrue(reasons)

    def test_agent_messages_extracts_challenger_report(self):
        events = self.app / "challenger.events.jsonl"
        rows = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "second"}},
        ]
        events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertEqual(runner.agent_messages(events), "first\n\nsecond")


if __name__ == "__main__":
    unittest.main()
