"""Resilient Codex process execution and event parsing."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .audits import retry_delay, thread_id, transport_failure_kind
from .benchmark_api import benchmark

def agent_messages(events: Path) -> str:
    messages: list[str] = []
    if not events.is_file():
        return ""
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = row.get("item")
        if (
            row.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
    return "\n\n".join(messages)


def resume_command(root: Path, session_id: str, prompt: str) -> list[str]:
    return [
        *benchmark.proot_prefix(root),
        "/opt/codex",
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        session_id,
        prompt,
    ]


def fresh_command(root: Path, prompt: str) -> list[str]:
    return [
        *benchmark.proot_prefix(root),
        "/opt/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        "/app",
        prompt,
    ]


def run_resilient_agent(
    *,
    root: Path,
    state: Path,
    environment: dict[str, str],
    session_id: str | None,
    prompt: str,
    budget: float,
    event_stem: str,
    max_attempts: int,
    stop_condition: Callable[[], bool] | None,
) -> tuple[list[dict[str, Any]], list[Path]]:
    """Retry only explicit transport failures, always within the phase budget."""
    results: list[dict[str, Any]] = []
    events: list[Path] = []
    started = time.monotonic()
    active_session_id = session_id
    for attempt in range(1, max_attempts + 1):
        remaining = budget - (time.monotonic() - started)
        if remaining < 30.0:
            break
        suffix = "" if attempt == 1 else f"-{attempt:02d}"
        event_path = state / f"{event_stem}{suffix}.events.jsonl"
        stderr_path = state / f"{event_stem}{suffix}.stderr.log"
        command = (
            resume_command(root, active_session_id, prompt)
            if active_session_id
            else fresh_command(root, prompt)
        )
        result = benchmark.run_process(
            command,
            environment,
            event_path,
            stderr_path,
            remaining,
        )
        result["attempt"] = attempt
        failure_kind = transport_failure_kind(result, stderr_path)
        result["transport_failure_kind"] = failure_kind
        result["transient_transport_failure"] = failure_kind != "none"
        result["quota_circuit_open"] = failure_kind == "daily-limit"
        results.append(result)
        events.append(event_path)
        active_session_id = active_session_id or thread_id(event_path)
        if stop_condition is not None and stop_condition():
            break
        if (
            result["return_code"] == 0
            or failure_kind == "none"
            or failure_kind == "daily-limit"
        ):
            break
        remaining = budget - (time.monotonic() - started)
        delay = retry_delay(failure_kind, attempt, remaining)
        result["retry_delay_seconds"] = delay
        if delay > 0:
            time.sleep(delay)
    return results, events
