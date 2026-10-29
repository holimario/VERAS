#!/usr/bin/env python3
"""Remove transport diagnostics and private runtime metadata from trajectories."""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any


TRANSPORT_MARKERS = (
    "reconnecting...",
    "503 service unavailable",
    "retrying sampling request",
    "sampling_error=",
    "resource capacity",
    "业务申请资源不足",
    "平台申请扩容",
)
PROCESS_MARKERS = (
    "ps -o pid,etime,pcpu,pmem,cmd",
    "ps -eo pid,etime,pcpu,pmem,cmd",
    "ps -o pid,etime,%cpu,%mem,cmd",
    "ps -eo pid,etime,%cpu,%mem,cmd",
    "/benchmark/runtimes/proot/",
    "/workspace/project:/opt/dsh -w /app /opt/codex exec",
)
PRIVATE_KEYS = {
    "base_url",
    "credential_source",
    "initial_cooldown_seconds",
    "initial_transport_failure",
    "initial_transport_failure_kind",
    "model_provider",
    "quota_circuit_open",
    "retry_delay_seconds",
    "transient_transport_failure",
    "transport_failure_kind",
}
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
PRIVATE_WORKSPACE = re.compile(r"/workspace/(?:tmp/)?(?:[A-Za-z0-9_.-]+/){1,2}[A-Za-z0-9_.-]+")
PRIVATE_HOME = re.compile(r"/(?:mlx_devbox/users/[^/\s]+|home/[^/\s]+)(?:/[^\s\"'<>:]*)?")
RUNTIME_BINARY = re.compile(r"/benchmark/runtimes/[^\s\"'<>:,]+")
RUNTIME_DEPS = re.compile(r"/benchmark/\.local-deps")
EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TIMELINE_BLOCK = re.compile(
    r'<details class="timeline-item [^"]+"[^>]*>.*?</details>', re.DOTALL
)


def contains_transport_error(text: str) -> bool:
    lowered = html.unescape(text).lower()
    return any(marker in lowered for marker in TRANSPORT_MARKERS)


def contains_process_listing(text: str) -> bool:
    decoded = html.unescape(text).lower()
    return any(marker in decoded for marker in PROCESS_MARKERS)


def sanitize_text(text: str) -> str:
    text = UUID.sub("<REDACTED_ID>", text)
    text = PRIVATE_WORKSPACE.sub("/workspace/project", text)
    text = PRIVATE_HOME.sub("<REDACTED_PATH>", text)
    text = RUNTIME_BINARY.sub("<RUNTIME_BINARY>", text)
    text = RUNTIME_DEPS.sub("<RUNTIME_DEPENDENCIES>", text)
    text = EMAIL.sub("<REDACTED_EMAIL>", text)
    return text


def sanitize_value(value: Any, parent: str | None = None) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if (
                key in PRIVATE_KEYS
                or (parent == "harness_release" and key == "commit")
                or (parent == "event_counts" and key == "error")
            ):
                continue
            clean[key] = sanitize_value(item, key)
        return clean
    if isinstance(value, list):
        return [sanitize_value(item, parent) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_json(path: Path) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps(sanitize_value(value), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def sanitize_jsonl(path: Path) -> None:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if contains_transport_error(line) or contains_process_listing(line):
            continue
        rows.append(json.dumps(sanitize_value(value), ensure_ascii=False, separators=(",", ":")))
    path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")


def sanitize_log(path: Path) -> None:
    lines = [
        sanitize_text(line)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not contains_transport_error(line)
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def format_trajectory_presentation(text: str) -> str:
    text = re.sub(
        r'(<html\b[^>]*\blang=)["\'][^"\']*["\']',
        r'\1"en"',
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = text.replace(
        "Sanitized VERAS benchmark trajectory", "VERAS benchmark trajectory"
    )
    text = text.replace(
        "Official verifier outcome and complete sanitized Codex event streams, grouped by evaluated stage and controller phase.",
        "Task results and agent execution logs, grouped by stage and controller phase.",
    )
    text = text.replace("Final sanitized submissions:", "Final submissions:")
    text = text.replace("Open complete sanitized JSONL", "Open JSONL")
    return re.sub(
        r'<div class="notice"><b>Privacy boundary:</b>.*?</div>\s*',
        "",
        text,
        flags=re.DOTALL,
    )


def sanitize_html(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = TIMELINE_BLOCK.sub(
        lambda match: ""
        if contains_transport_error(match.group(0))
        or contains_process_listing(match.group(0))
        or "/workspace/project/run_benchmark.py --output /benchmark-runs" in html.unescape(match.group(0))
        else match.group(0),
        text,
    )
    text = sanitize_text(text)
    text = re.sub(
        r"Sanitized VERAS trajectory · Hard\d+",
        "Sanitized VERAS benchmark trajectory",
        text,
    )
    text = text.replace(
        '<a href="../../index.html">Back to trajectory index</a><a href="../../../benchmark-results/index.html">Benchmark results</a>',
        '<a href="../../index.html">Back to benchmark results</a>',
    )

    event_counts = {
        event.name: sum(1 for line in event.read_text(encoding="utf-8").splitlines() if line.strip())
        for event in path.parent.rglob("*.events.jsonl")
    }
    text = re.sub(
        r'(<b>([^<]+\.events\.jsonl)</b><span class="muted">)\d+ items',
        lambda match: f"{match.group(1)}{event_counts.get(match.group(2), 0)} items",
        text,
    )
    timeline_items = text.count('class="timeline-item ')
    text = re.sub(
        r"(\d+ streams · )\d+( items)",
        lambda match: f"{match.group(1)}{timeline_items}{match.group(2)}",
        text,
        count=1,
    )
    text = format_trajectory_presentation(text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def sanitize_tree(root: Path) -> None:
    paths = sorted(root.rglob("*"))
    for path in paths:
        if path.is_dir():
            continue
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            path.unlink()
        elif path.suffix == ".jsonl":
            sanitize_jsonl(path)
        elif path.suffix == ".json":
            sanitize_json(path)
        elif path.suffix == ".html":
            continue
        elif path.suffix == ".log":
            sanitize_log(path)
        elif path.suffix in {".md", ".py", ".csv", ".sol"}:
            path.write_text(
                sanitize_text(path.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8",
            )

    for path in paths:
        if path.is_file() and path.suffix == ".html":
            sanitize_html(path)

    for cache in sorted(root.rglob("__pycache__"), reverse=True):
        cache.rmdir()

    metadata_path = root / "data" / "trajectory-metadata.json"
    tasks_root = root / "tasks"
    if metadata_path.is_file() and tasks_root.is_dir():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        records = {row["task"]: row for row in metadata.get("records", [])}
        for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
            row = records.get(task_dir.name)
            if row is None:
                continue
            row["event_streams"] = len(list(task_dir.rglob("*.events.jsonl")))
            page = task_dir / "index.html"
            row["timeline_items"] = (
                page.read_text(encoding="utf-8").count('class="timeline-item ')
                if page.is_file()
                else 0
            )
        metadata["event_streams"] = sum(row.get("event_streams", 0) for row in records.values())
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    sanitize_tree(args.root)


if __name__ == "__main__":
    main()
