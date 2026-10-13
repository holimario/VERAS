"""Per-task VERAS orchestration."""

from __future__ import annotations

import json
import shutil
import time
import tomllib
from pathlib import Path
from typing import Any

from .audits import (
    combined_trace,
    control_decision,
    copy_submissions,
    public_audit,
    regresses,
    restore_submissions,
    retry_delay,
    run_local_solver,
    safe_state,
    semantic_audit,
    semantic_regresses,
    thread_id,
    transport_failure_kind,
    viable_candidate,
)
from .benchmark_api import OR_BENCH, benchmark
from .constants import DEFAULT_CHALLENGER_FRACTION, HARNESS_NAME, MIN_INTERVENTION_SECONDS, RUNTIME_NOTE
from .prompts import challenger_prompt, performance_prompt, recovery_prompt
from .protocols import stage_protocol
from .runtime import agent_messages, run_resilient_agent

def make_run_task(
    *,
    reserve_fraction: float,
    min_recovery_seconds: float,
    local_solver_cap_seconds: float,
    max_transport_retries: int,
    challenger_cap_seconds: float = 180.0,
    performance_addendum: str = "",
):
    def run_task(
        harness: str,
        task: str,
        output: Path,
        model: dict[str, str],
        timeout_scale: float,
        force: bool,
    ) -> dict[str, Any]:
        run_dir = (output / harness / task).resolve()
        status_path = run_dir / "status.json"
        if status_path.exists() and not force:
            old = json.loads(status_path.read_text())
            if benchmark.reusable_result(harness, run_dir, old):
                return old
        config = tomllib.loads((OR_BENCH / "harbor_tasks" / task / "task.toml").read_text())
        app = benchmark.sanitize_task(task, run_dir)
        root = app.parent
        for path in (root / "state", root / "state" / "home", root / "tmp"):
            path.mkdir(exist_ok=True)
        forbidden = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.name in benchmark.FORBIDDEN_PARTS
        ]
        if forbidden:
            raise RuntimeError(f"sanitized root contains hidden paths: {forbidden}")
        solution_env = config.get("solution", {}).get("env", {})
        verifier_env = benchmark.safe_environment(model["api_key"], solution_env)
        status = {
            "schema_version": 1,
            "status": "running",
            "harness": harness,
            "harness_release": benchmark.VERSIONS[harness],
            "task": task,
            "model": model["model"],
            "credential_persisted": False,
            "hidden_assets_copied": False,
            "isolation": "sanitized-copy+proot",
            "started_at": benchmark.now(),
            "steps": [],
        }
        benchmark.write_json(status_path, status)
        try:
            for index, step in enumerate(benchmark.task_steps(task, config)):
                if index and step["workdir"].is_dir():
                    shutil.copytree(step["workdir"], app, dirs_exist_ok=True)
                state = root / "state" / f"{index + 1:02d}-{step['name']}"
                before = benchmark.manifest(app)
                benchmark.write_json(state / "manifest.before.json", before)
                base_instruction = step["instruction"].read_text(encoding="utf-8")
                instruction = (
                    base_instruction
                    + RUNTIME_NOTE
                    + stage_protocol(task, app, base_instruction)
                )
                command, agent_env, stdout_name, cwd = benchmark.harness_command(
                    harness, model, root, state, instruction
                )
                agent_env.update({str(key): str(value) for key, value in solution_env.items()})
                total_budget = step["agent_timeout"] * timeout_scale
                step_started = time.monotonic()

                def remaining_budget() -> float:
                    return max(0.0, total_budget - (time.monotonic() - step_started))

                reserve = min(
                    max(min_recovery_seconds, total_budget * reserve_fraction),
                    max(0.0, total_budget - 60.0),
                )
                initial_budget = total_budget - reserve
                initial_events = state / stdout_name
                initial_stderr = state / "harness.stderr.log"
                initial = benchmark.run_process(
                    command,
                    agent_env,
                    initial_events,
                    initial_stderr,
                    initial_budget,
                    cwd,
                )

                initial_audit = public_audit(app, instruction)
                benchmark.write_json(state / "public-audit.initial.json", initial_audit)
                initial_observation = semantic_audit(task, app, initial_audit)
                benchmark.write_json(
                    state / "semantic-observation.initial.json", initial_observation
                )
                phases: list[dict[str, Any]] = [{"name": "solve", **initial}]
                event_paths = [initial_events]
                session_id = thread_id(initial_events)
                initial_failure_kind = transport_failure_kind(
                    initial, initial_stderr
                )
                initial_transport_failure = initial_failure_kind != "none"
                initial_cooldown_seconds = 0.0
                if initial_failure_kind == "rate-limit":
                    initial_cooldown_seconds = retry_delay(
                        initial_failure_kind, 1, remaining_budget()
                    )
                    if initial_cooldown_seconds:
                        time.sleep(initial_cooldown_seconds)
                current_audit = initial_audit
                current_observation = initial_observation
                decisions = [
                    control_decision(
                        app,
                        instruction,
                        current_audit,
                        remaining_budget(),
                        current_observation,
                    )
                ]
                intervention_snapshot = state / "pre-intervention-submissions"
                copy_submissions(app, intervention_snapshot)
                local_solver_result: dict[str, Any] | None = None
                local_solver_accepted = False
                recovery_results: list[dict[str, Any]] = []
                recovery_accepted = False
                challenger_results: list[dict[str, Any]] = []
                challenger_report = ""
                performance_results: list[dict[str, Any]] = []
                performance_accepted = False
                rollback_reasons: list[str] = []

                if (
                    initial_failure_kind != "daily-limit"
                    and decisions[-1]["action"] == "execute-current-stage-solver"
                ):
                    local_budget = min(
                        local_solver_cap_seconds,
                        max(0.0, remaining_budget() - min_recovery_seconds),
                    )
                    local_solver_result, _ = run_local_solver(
                        root,
                        state,
                        instruction,
                        agent_env,
                        local_budget,
                    )
                    if local_solver_result is not None:
                        phases.append({"name": "local-solver", **local_solver_result})
                        candidate_audit = public_audit(app, instruction)
                        regressed, reasons = regresses(current_audit, candidate_audit)
                        if regressed:
                            restore_submissions(app, intervention_snapshot)
                            rollback_reasons.extend(
                                f"local-solver: {reason}" for reason in reasons
                            )
                        else:
                            current_audit = candidate_audit
                            current_observation = semantic_audit(
                                task, app, current_audit
                            )
                            local_solver_accepted = viable_candidate(
                                current_audit, current_observation
                            )
                        benchmark.write_json(
                            state / "public-audit.local-solver.json", current_audit
                        )
                        benchmark.write_json(
                            state / "semantic-observation.local-solver.json",
                            current_observation,
                        )
                        decisions.append(
                            control_decision(
                                app,
                                instruction,
                                current_audit,
                                remaining_budget(),
                                current_observation,
                            )
                        )

                semantic_unsafe = bool(
                    current_observation.get("supported")
                    and current_observation.get("safe") is not True
                )
                if (
                    initial_failure_kind != "daily-limit"
                    and
                    (not current_audit["primary_target_valid"] or semantic_unsafe)
                    and remaining_budget() >= MIN_INTERVENTION_SECONDS
                ):
                    recovery_snapshot = state / "pre-recovery-submissions"
                    copy_submissions(app, recovery_snapshot)
                    pre_recovery_audit = current_audit
                    pre_recovery_observation = current_observation

                    def restored_safely() -> bool:
                        observed_audit = public_audit(app, instruction)
                        observed_semantics = semantic_audit(task, app, observed_audit)
                        return safe_state(observed_audit, observed_semantics)

                    recovery_budget = min(
                        remaining_budget(),
                        max(
                            MIN_INTERVENTION_SECONDS,
                            remaining_budget() * 0.72,
                        ),
                    )
                    recovery_results, recovery_events = run_resilient_agent(
                        root=root,
                        state=state,
                        environment=agent_env,
                        session_id=session_id,
                        prompt=recovery_prompt(current_audit, current_observation),
                        budget=recovery_budget,
                        event_stem="recovery",
                        max_attempts=max_transport_retries,
                        stop_condition=restored_safely,
                    )
                    for result in recovery_results:
                        phases.append(
                            {"name": f"recovery-{result['attempt']}", **result}
                        )
                    event_paths.extend(recovery_events)
                    candidate_audit = public_audit(app, instruction)
                    candidate_observation = semantic_audit(task, app, candidate_audit)
                    structural_regressed, reasons = regresses(
                        pre_recovery_audit, candidate_audit
                    )
                    semantic_worse, semantic_reasons = semantic_regresses(
                        pre_recovery_observation, candidate_observation
                    )
                    reasons.extend(semantic_reasons)
                    if structural_regressed or semantic_worse:
                        restore_submissions(app, recovery_snapshot)
                        current_audit = pre_recovery_audit
                        current_observation = pre_recovery_observation
                        rollback_reasons.extend(
                            f"agent-restoration: {reason}" for reason in reasons
                        )
                    else:
                        current_audit = candidate_audit
                        current_observation = candidate_observation
                        recovery_accepted = viable_candidate(
                            current_audit, current_observation
                        )
                    decisions.append(
                        control_decision(
                            app,
                            instruction,
                            current_audit,
                            remaining_budget(),
                            current_observation,
                        )
                    )

                if (
                    initial_failure_kind != "daily-limit"
                    and
                    viable_candidate(current_audit, current_observation)
                    and remaining_budget() >= MIN_INTERVENTION_SECONDS
                ):
                    needs_challenger = not current_observation.get(
                        "coverage_complete", False
                    )
                    if needs_challenger:
                        challenger_snapshot = state / "pre-challenger-submissions"
                        copy_submissions(app, challenger_snapshot)
                        challenge_budget = min(
                            challenger_cap_seconds,
                            max(
                                MIN_INTERVENTION_SECONDS,
                                remaining_budget() * DEFAULT_CHALLENGER_FRACTION,
                            ),
                        )
                        challenger_results, challenger_events = run_resilient_agent(
                            root=root,
                            state=state,
                            environment=agent_env,
                            session_id=None,
                            prompt=challenger_prompt(
                                task, app, current_audit, current_observation
                            ),
                            budget=challenge_budget,
                            event_stem="challenger",
                            max_attempts=min(2, max_transport_retries),
                            stop_condition=None,
                        )
                        for result in challenger_results:
                            phases.append(
                                {"name": f"challenger-{result['attempt']}", **result}
                            )
                        event_paths.extend(challenger_events)
                        challenger_report = "\n\n".join(
                            agent_messages(path) for path in challenger_events
                        ).strip()
                        # The challenger is an observer, never an actuator.
                        restore_submissions(app, challenger_snapshot)
                        current_audit = public_audit(app, instruction)
                        current_observation = semantic_audit(
                            task, app, current_audit
                        )
                        benchmark.write_json(
                            state / "challenger-report.json",
                            {
                                "report": challenger_report,
                                "attempts": len(challenger_results),
                                "candidate_restored_after_observation": True,
                            },
                        )

                    if remaining_budget() >= MIN_INTERVENTION_SECONDS:
                        performance_snapshot = state / "pre-performance-submissions"
                        copy_submissions(app, performance_snapshot)
                        pre_performance_audit = current_audit
                        pre_performance_observation = current_observation
                        performance_results, performance_events = run_resilient_agent(
                            root=root,
                            state=state,
                            environment=agent_env,
                            session_id=session_id,
                            prompt=performance_prompt(
                                task,
                                current_audit,
                                current_observation,
                                challenger_report,
                                performance_addendum=performance_addendum,
                            ),
                            budget=remaining_budget(),
                            event_stem="performance",
                            max_attempts=max_transport_retries,
                            stop_condition=None,
                        )
                        for result in performance_results:
                            phases.append(
                                {"name": f"performance-{result['attempt']}", **result}
                            )
                        event_paths.extend(performance_events)
                        candidate_audit = public_audit(app, instruction)
                        candidate_observation = semantic_audit(
                            task, app, candidate_audit
                        )
                        structural_regressed, reasons = regresses(
                            pre_performance_audit, candidate_audit
                        )
                        semantic_worse, semantic_reasons = semantic_regresses(
                            pre_performance_observation, candidate_observation
                        )
                        reasons.extend(semantic_reasons)
                        if (
                            structural_regressed
                            or semantic_worse
                            or not viable_candidate(
                                candidate_audit, candidate_observation
                            )
                        ):
                            restore_submissions(app, performance_snapshot)
                            current_audit = pre_performance_audit
                            current_observation = pre_performance_observation
                            rollback_reasons.extend(
                                f"agent-performance: {reason}"
                                for reason in (reasons or ["candidate became unsafe"])
                            )
                        else:
                            current_audit = candidate_audit
                            current_observation = candidate_observation
                            performance_accepted = True
                        decisions.append(
                            control_decision(
                                app,
                                instruction,
                                current_audit,
                                remaining_budget(),
                                current_observation,
                            )
                        )

                final_audit = current_audit
                final_observation = current_observation

                benchmark.write_json(state / "public-audit.final.json", final_audit)
                benchmark.write_json(
                    state / "semantic-observation.final.json", final_observation
                )
                benchmark.write_json(
                    state / "control-decision.json",
                    {
                        "harness_name": HARNESS_NAME,
                        "policy": "counterexample-guided-guarded-performance",
                        "session_id": session_id,
                        "total_budget_seconds": total_budget,
                        "initial_budget_seconds": initial_budget,
                        "reserved_recovery_seconds": reserve,
                        "initial_transport_failure": initial_transport_failure,
                        "initial_transport_failure_kind": initial_failure_kind,
                        "initial_cooldown_seconds": initial_cooldown_seconds,
                        "local_solver_used": local_solver_result is not None,
                        "local_solver_accepted": local_solver_accepted,
                        "recovery_attempts": len(recovery_results),
                        "recovery_accepted": recovery_accepted,
                        "challenger_attempts": len(challenger_results),
                        "challenger_report_present": bool(challenger_report),
                        "performance_attempts": len(performance_results),
                        "performance_accepted": performance_accepted,
                        "critic_used": bool(challenger_results),
                        "decisions": decisions,
                        "rollback_reasons": rollback_reasons,
                        "initial_audit": initial_audit,
                        "initial_observation": initial_observation,
                        "final_audit": final_audit,
                        "final_observation": final_observation,
                    },
                )
                effective_result = initial
                if local_solver_accepted and local_solver_result is not None:
                    effective_result = local_solver_result
                if recovery_accepted and recovery_results:
                    effective_result = recovery_results[-1]
                if performance_accepted and performance_results:
                    effective_result = performance_results[-1]
                agent = {
                    "started_at": initial["started_at"],
                    "finished_at": phases[-1]["finished_at"],
                    "wall_seconds": round(time.monotonic() - step_started, 3),
                    "timeout_seconds": total_budget,
                    "timed_out": any(bool(item["timed_out"]) for item in phases),
                    "return_code": effective_result["return_code"],
                    "command": initial["command"],
                    "phases": phases,
                    "trace": combined_trace(state, event_paths),
                }
                after = benchmark.manifest(app)
                benchmark.write_json(state / "manifest.after.json", after)
                benchmark.write_json(
                    state / "file_changes.json", benchmark.manifest_diff(before, after)
                )
                verifier = benchmark.run_verifier(
                    app,
                    step["tests"],
                    state,
                    step["verifier_timeout"] * timeout_scale,
                    verifier_env,
                )
                reward = verifier["reward_details"]
                gate = step["min_reward"] is None or float(
                    reward.get("scalar_reward", 0.0)
                ) >= float(step["min_reward"])
                step_result = {
                    "index": index + 1,
                    "name": step["name"],
                    "instruction_source": str(step["instruction"].relative_to(OR_BENCH)),
                    "agent": agent,
                    "verifier": verifier,
                    "min_reward": step["min_reward"],
                    "gate_passed": gate,
                    "veras_controller": {
                        "harness_name": HARNESS_NAME,
                        "policy": "counterexample-guided-guarded-performance",
                        "initial_transport_failure": initial_transport_failure,
                        "initial_transport_failure_kind": initial_failure_kind,
                        "initial_cooldown_seconds": initial_cooldown_seconds,
                        "local_solver_used": local_solver_result is not None,
                        "local_solver_accepted": local_solver_accepted,
                        "recovery_attempts": len(recovery_results),
                        "recovery_accepted": recovery_accepted,
                        "challenger_attempts": len(challenger_results),
                        "challenger_report_present": bool(challenger_report),
                        "performance_attempts": len(performance_results),
                        "performance_accepted": performance_accepted,
                        "critic_used": bool(challenger_results),
                        "decisions": decisions,
                        "initial_observation": initial_observation,
                        "final_observation": final_observation,
                        "rollback_reasons": rollback_reasons,
                    },
                }
                benchmark.write_json(state / "result.json", step_result)
                status["steps"].append(step_result)
                benchmark.write_json(status_path, status)
                if not gate:
                    status["stop_reason"] = f"min_reward gate failed at {step['name']}"
                    break
            status["status"] = "complete"
        except Exception as error:
            import traceback

            status.update(
                {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                    "traceback": traceback.format_exc(),
                }
            )
        status["finished_at"] = benchmark.now()
        status["redacted_secret_occurrences"] = benchmark.redact_tree(
            run_dir, model["api_key"]
        )
        benchmark.write_json(status_path, status)
        return status

    return run_task
