"""Public-data audits and controller decision logic."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .benchmark_api import benchmark
from .constants import DAILY_LIMIT, MIN_INTERVENTION_SECONDS, RATE_LIMIT, TRANSIENT_TRANSPORT
from .observers import observe_submission
from .protocols import submission_references, target_candidates, validate_target

def python_syntax(path: Path) -> bool:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        return True
    except (OSError, UnicodeError, SyntaxError):
        return False


def public_audit(app: Path, instruction: str) -> dict[str, Any]:
    references = [app / value for value in submission_references(instruction)]
    targets = target_candidates(app, instruction)
    target_results = [
        {
            "path": str(path.relative_to(app)),
            "exists": path.is_file(),
            "valid": result[0],
            "status": result[1],
            "records": result[2],
        }
        for path in targets
        for result in [validate_target(path)]
    ]
    python_files = sorted((app / "submissions").glob("*.py"))
    required_results = [
        {
            "path": str(path.relative_to(app)),
            "exists": path.is_file() and path.stat().st_size > 0,
        }
        for path in references
        if "template" not in path.name.lower()
    ]
    syntax_results = [
        {"path": str(path.relative_to(app)), "valid": python_syntax(path)}
        for path in python_files
    ]
    primary = target_results[0] if target_results else None
    return {
        "targets": target_results,
        "primary_target": primary,
        "primary_target_valid": bool(primary and primary["valid"]),
        "primary_target_exists": bool(primary and primary["exists"]),
        "required": required_results,
        "python_syntax": syntax_results,
        "target_valid": any(item["valid"] for item in target_results),
        "target_exists": any(item["exists"] for item in target_results),
        "required_present": sum(item["exists"] for item in required_results),
        "required_total": len(required_results),
        "python_valid": all(item["valid"] for item in syntax_results),
        "python_count": len(syntax_results),
    }


def semantic_audit(
    task: str, app: Path, artifact_audit: dict[str, Any]
) -> dict[str, Any]:
    primary = artifact_audit.get("primary_target") or {}
    artifact = app / primary["path"] if primary.get("path") else None
    return observe_submission(task, app, artifact)


def safe_state(
    artifact_audit: dict[str, Any], observation: dict[str, Any]
) -> bool:
    """Return true only when an independent semantic observer found no violation."""
    return bool(
        artifact_audit["primary_target_valid"]
        and observation.get("supported")
        and observation.get("safe") is True
    )


def viable_candidate(
    artifact_audit: dict[str, Any], observation: dict[str, Any]
) -> bool:
    """A structurally valid candidate may be retained even when coverage is partial."""
    return bool(
        artifact_audit["primary_target_valid"]
        and (
            not observation.get("supported")
            or observation.get("safe") is True
        )
    )


def observation_rank(observation: dict[str, Any]) -> tuple[float, float, int]:
    if not observation.get("supported"):
        return (float("inf"), float("inf"), 2**31 - 1)
    if observation.get("safe") is True:
        return (0.0, 0.0, 0)
    return (
        float(observation.get("max_normalized_residual") or 0.0),
        float(observation.get("total_normalized_violation_mass") or 0.0),
        int(observation.get("violation_count") or 0),
    )


def semantic_regresses(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if before.get("supported") and not after.get("supported"):
        reasons.append("semantic observer support was lost")
    elif before.get("supported") and after.get("supported"):
        if observation_rank(after) > observation_rank(before):
            reasons.append("independent semantic residual tuple worsened")
    return bool(reasons), reasons


def solver_candidates(app: Path, instruction: str) -> list[Path]:
    """Return only solver scripts explicitly required by the current step."""
    candidates = [
        app / value
        for value in submission_references(instruction)
        if Path(value).suffix.lower() == ".py"
        and "template" not in Path(value).name.lower()
    ]
    return [
        path
        for path in dict.fromkeys(candidates)
        if path.is_file() and python_syntax(path)
    ]


def transient_transport_failure(result: dict[str, Any], stderr_path: Path) -> bool:
    return transport_failure_kind(result, stderr_path) != "none"


def transport_failure_kind(result: dict[str, Any], stderr_path: Path) -> str:
    if result.get("return_code") == 0 or not stderr_path.is_file():
        return "none"
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    if DAILY_LIMIT.search(stderr):
        return "daily-limit"
    if RATE_LIMIT.search(stderr):
        return "rate-limit"
    if TRANSIENT_TRANSPORT.search(stderr):
        return "transient"
    return "none"


def retry_delay(failure_kind: str, attempt: int, remaining: float) -> float:
    if failure_kind == "daily-limit":
        return 0.0
    if failure_kind == "rate-limit":
        requested = min(45.0 * attempt, 120.0)
    else:
        requested = min(10.0 * (2 ** (attempt - 1)), 60.0)
    return min(requested, max(0.0, remaining - MIN_INTERVENTION_SECONDS))


def local_solver_command(root: Path, script: Path) -> list[str]:
    relative = script.relative_to(root / "app")
    return [
        *benchmark.proot_prefix(root),
        "/usr/bin/env",
        "PYTHONPATH=/opt/or-deps",
        "PATH=/opt/or-deps/bin:/usr/local/bin:/usr/bin:/bin",
        "python",
        f"/app/{relative}",
    ]


def run_local_solver(
    root: Path,
    state: Path,
    instruction: str,
    environment: dict[str, str],
    budget: float,
) -> tuple[dict[str, Any] | None, Path | None]:
    candidates = solver_candidates(root / "app", instruction)
    if not candidates or budget < 30.0:
        return None, None
    script = candidates[0]
    stderr = state / "local-solver.stderr.log"
    result = benchmark.run_process(
        local_solver_command(root, script),
        environment,
        state / "local-solver.stdout.log",
        stderr,
        budget,
    )
    result["script"] = str(script.relative_to(root / "app"))
    return result, stderr


def regresses(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if before["primary_target_valid"] and not after["primary_target_valid"]:
        reasons.append("valid primary decision artifact became invalid")
    if before["primary_target_exists"] and not after["primary_target_exists"]:
        reasons.append("primary decision artifact was removed")
    if before["python_valid"] and not after["python_valid"]:
        reasons.append("valid Python source became syntactically invalid")
    if after["required_present"] < before["required_present"]:
        reasons.append("required output files were removed")
    return bool(reasons), reasons


def copy_submissions(app: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    source = app / "submissions"
    if source.exists():
        shutil.copytree(source, destination)
    else:
        destination.mkdir(parents=True)


def restore_submissions(app: Path, snapshot: Path) -> None:
    target = app / "submissions"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(snapshot, target)


def redact_tree(root: Path, secret: str) -> int:
    """Redact credentials while ignoring unreadable zero-byte bind placeholders."""
    needle, count = secret.encode(), 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size == 0 or size > 128 * 1024 * 1024:
            continue
        payload = path.read_bytes()
        if needle in payload:
            count += payload.count(needle)
            path.write_bytes(payload.replace(needle, b"[REDACTED]"))
    return count


def thread_id(events: Path) -> str | None:
    if not events.is_file():
        return None
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "thread.started" and row.get("thread_id"):
            return str(row["thread_id"])
    return None


def combined_trace(state: Path, event_paths: list[Path]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    usage: dict[str, int] = {}
    for path in event_paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(row.get("type", "unknown"))
            counts[kind] = counts.get(kind, 0) + 1
            candidate = row.get("usage")
            if not isinstance(candidate, dict):
                continue
            for key, value in candidate.items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value
    summary = {
        "event_logs": [str(path.relative_to(state)) for path in event_paths if path.is_file()],
        "event_counts": counts,
        "usage": usage,
    }
    benchmark.write_json(state / "trace_summary.json", summary)
    return summary


def control_decision(
    app: Path,
    instruction: str,
    audit: dict[str, Any],
    remaining_seconds: float,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose a bounded intervention from directly observable public state."""
    candidates = solver_candidates(app, instruction)
    triggers: list[str] = []
    primary = audit.get("primary_target") or {}
    if not audit["primary_target_exists"]:
        triggers.append("primary-artifact-missing")
    elif not audit["primary_target_valid"]:
        triggers.append(f"primary-artifact-invalid:{primary.get('status', 'unknown')}")
    if not audit["python_valid"]:
        triggers.append("submitted-python-syntax-invalid")
    if candidates:
        triggers.append("current-stage-solver-executable")
    semantic_unsafe = bool(
        observation
        and observation.get("supported")
        and observation.get("safe") is not True
    )
    if semantic_unsafe:
        groups = sorted(
            {
                item.get("group", "unknown")
                for item in observation.get("violations", [])
            }
        )
        triggers.extend(f"semantic-violation:{group}" for group in groups)

    if remaining_seconds < MIN_INTERVENTION_SECONDS:
        mode = "terminal"
        action = "hold-best-candidate"
    elif not audit["primary_target_valid"] and candidates:
        mode = "restoration"
        action = "execute-current-stage-solver"
    elif not audit["primary_target_valid"] or semantic_unsafe:
        mode = "restoration"
        action = "resume-agent-restoration"
    elif observation and observation.get("supported"):
        mode = "performance"
        action = "resume-agent-performance"
    else:
        mode = "unverified-incumbent"
        action = "challenge-then-improve"
    return {
        "mode": mode,
        "action": action,
        "triggers": triggers,
        "remaining_seconds": round(max(0.0, remaining_seconds), 3),
        "solver_candidates": [str(path.relative_to(app)) for path in candidates],
        "primary_target": primary,
        "semantic_observer": (observation or {}).get("observer"),
        "semantic_safe": (observation or {}).get("safe"),
        "semantic_violation_count": (observation or {}).get("violation_count", 0),
        "max_normalized_residual": (observation or {}).get(
            "max_normalized_residual"
        ),
        "observer_coverage_complete": (observation or {}).get(
            "coverage_complete", False
        ),
    }
