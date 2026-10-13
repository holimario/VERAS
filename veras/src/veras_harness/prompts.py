"""Recovery, challenger, and guarded-performance prompts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .protocols import task_family_protocol

def recovery_prompt(
    audit: dict[str, Any], observation: dict[str, Any]
) -> str:
    return f"""A measurable feasibility event fired: the current-stage artifact is missing, structurally invalid, or violates an independently recomputed public constraint. Continue from the existing files and session; do not restart the analysis.

Operate only in RESTORATION mode. First run or complete the current-stage solver with `PYTHONPATH=/opt/or-deps python`. If the exact model has no incumbent, construct an elastic Phase-I model that lexicographically minimizes maximum normalized hard-constraint violation, then total violation, then distance from the best candidate. For integer candidates, use a repair MIP or local-branching projection rather than hand-editing coupled rows. Change the search operator after a repeated stall. Do not spend this recovery budget improving the objective until a solver-confirmed feasible artifact exists.

Read the exact exported artifact independently against public data. Preserve earlier-stage frozen decisions, use conservative numeric margins, and leave executable solver code plus the strongest artifact in /app/submissions. A self-authored checker alone is not proof of validity.

<current_public_artifact_audit>
{json.dumps(audit, ensure_ascii=False, indent=2)}
</current_public_artifact_audit>

<independent_public_constraint_observer>
{json.dumps(observation, ensure_ascii=False, indent=2)}
</independent_public_constraint_observer>

The observer is independent of your solver and has priority over claims in your
solve log. Repair every reported violation and rerun the exact exported artifact
through an independent calculation before finishing."""


def challenger_prompt(
    task: str,
    app: Path,
    audit: dict[str, Any],
    observation: dict[str, Any],
) -> str:
    return f"""Act as an independent adversarial verifier for `{task}`. Do not modify any
file. Use only `/app/PROBLEM_STATEMENT.md`, `/app/data`, the current-stage artifact,
and prior-stage public artifacts. Reparse the exact exported rows; do not trust the
solver model, solve log, or a checker written by the producing agent.

Construct a constraint coverage matrix and actively search for a concrete
counterexample in each family: schema/nullability and key coverage; identifier
domains; frozen/nonanticipative decisions; conservation and inventory; time,
location, readiness, setup, and non-overlap; capacities and scenario derates;
reserves/ramps; and objective accounting. Focus first on these empirically risky
public constraints:
{chr(10).join(f'- {item}' for item in task_family_protocol(task, app))}

Return a concise report in your final message. For every violation give the exact
row/key, public rule, observed value, required bound, and a repair suggestion. If
you find none, explicitly list the families you actually recomputed and the ones
you could not cover. This report is diagnostic evidence, not proof of feasibility.

<structural_audit>
{json.dumps(audit, ensure_ascii=False, indent=2)}
</structural_audit>
<existing_observer>
{json.dumps(observation, ensure_ascii=False, indent=2)}
</existing_observer>"""


def performance_prompt(
    task: str,
    audit: dict[str, Any],
    observation: dict[str, Any],
    challenger_report: str = "",
    *,
    performance_addendum: str = "",
) -> str:
    challenge = challenger_report.strip() or "No separate challenger report was available."
    return f"""Continue from the existing session and files for `{task}`. A complete
candidate now exists, so use the remaining time as a guarded repair-and-performance
phase. Keep the current submissions as the rollback incumbent.

First address every concrete issue in the independent report below. Then reparse
the exact artifact from disk and recompute all public hard constraints independently
of the optimization model. Only after that audit has zero violations, optimize the
true stated objective. Use the incumbent as a MIP start and prefer a bounded exact
solve, local branching, or large-neighborhood search. Publish a changed artifact
only when its recomputed objective strictly improves and all hard checks still pass;
otherwise leave the incumbent untouched. Record incumbent objective, candidate
objective, bound/gap, and audit result in the solve log.

{performance_addendum}<public_challenger_report>
{challenge[-16000:]}
</public_challenger_report>
<current_structural_audit>
{json.dumps(audit, ensure_ascii=False, indent=2)}
</current_structural_audit>
<current_semantic_observer>
{json.dumps(observation, ensure_ascii=False, indent=2)}
</current_semantic_observer>"""
