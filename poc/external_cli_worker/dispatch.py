"""Fail-closed external worker route selection for normal Kanban dispatch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .antigravity import AntigravityAdapter
from .lane import ExternalCliWorkerLane
from .lifecycle import handle_worker_result
from .worker_result import (
    MAX_ENTRY_LENGTH, MAX_LIST_ENTRIES, MAX_SUMMARY_LENGTH, MAX_TOTAL_BYTES,
)


@dataclass(frozen=True)
class ExternalWorkerRoute:
    """A configured route, with ``spawn`` absent for invalid config."""

    configured: bool
    spawn: Optional[Callable]


def _task_instruction(task) -> str:
    """Use the title/body intent already owned by the Kanban task."""
    title = (task.title or "").strip()
    body = (task.body or "").strip()
    if body and body != title:
        return f"TITLE:\n{title}\n\nDETAILS:\n{body}"
    return body or title


def _worker_prompt(instruction: str) -> str:
    return f"""You are a bounded engineering worker.

Perform the task below within the supplied workspace and constraints.

TASK:
{instruction}

Return ONLY one JSON object conforming to WorkerResult schema v1:
{{
  "schema_version": "1.0",
  "outcome": "COMPLETED | REVIEW_REQUIRED | FAILED_RETRYABLE | FAILED_TERMINAL | BLOCKED",
  "summary": "non-empty description of what actually happened",
  "artifacts": [],
  "evidence": [],
  "findings": [],
  "changed_files": [],
  "tests": [],
  "continuation": null
}}

Only schema_version, outcome, and summary are required. Optional list fields contain strings.
COMPLETED means the task succeeded. REVIEW_REQUIRED means bounded work finished but needs review.
FAILED_RETRYABLE means the same work may succeed on another attempt. FAILED_TERMINAL means retrying
the unchanged task will not succeed. BLOCKED means external action or a prerequisite is required.

Bounds: the full JSON must be at most {MAX_TOTAL_BYTES} UTF-8 bytes; summary at most
{MAX_SUMMARY_LENGTH} characters; each list at most {MAX_LIST_ENTRIES} entries; every list entry
and continuation at most {MAX_ENTRY_LENGTH} characters.

Do not include markdown fences or commentary outside the JSON. Do not invent task IDs, run IDs,
lifecycle fields, commands, files, tests, or evidence. Do not report paths outside the workspace.
The result is a claim that the host will validate; it does not control Hermes lifecycle.
"""


def _antigravity_spawn(
    task,
    workspace: str,
    *,
    board: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> Optional[int]:
    instruction = _task_instruction(task)
    if not instruction:
        raise ValueError("external antigravity lane requires a non-empty Kanban task instruction")
    from hermes_cli import kanban_db as kb

    lane = ExternalCliWorkerLane(
        AntigravityAdapter(model=model, effort=effort),
        prompt=_worker_prompt(instruction),
        result_handler=handle_worker_result,
        result_context={
            "db_path": str(kb.kanban_db_path(board=board)),
            "workspace": workspace,
            "worker": task.assignee,
        },
    )
    return lane.spawn(task, workspace, board=board)


# This is intentionally a tiny, code-owned registry.  Configuration selects
# an adapter identifier only; it never supplies executable paths or argv.
_ADAPTER_REGISTRY: dict[str, Callable] = {"antigravity": _antigravity_spawn}

_ANTIGRAVITY_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ANTIGRAVITY_EFFORTS = frozenset({"low", "medium", "high"})


def resolve_external_worker_route(assignee: str, config: Mapping[str, Any]) -> ExternalWorkerRoute:
    """Resolve only a valid, explicitly opted-in external worker route.

    A present but malformed entry is still ``configured`` so the dispatcher
    will not silently fall back to a native profile.
    """
    lanes = config.get("external_worker_lanes") if isinstance(config, Mapping) else None
    if not isinstance(lanes, Mapping) or assignee not in lanes:
        return ExternalWorkerRoute(configured=False, spawn=None)
    entry = lanes[assignee]
    if not isinstance(entry, Mapping):
        return ExternalWorkerRoute(configured=True, spawn=None)
    if entry.get("type") != "external_cli":
        return ExternalWorkerRoute(configured=True, spawn=None)
    adapter = entry.get("adapter")
    spawn = _ADAPTER_REGISTRY.get(adapter)
    if spawn is None:
        return ExternalWorkerRoute(configured=True, spawn=None)

    model = entry.get("model", AntigravityAdapter.default_model)
    effort = entry.get("effort", AntigravityAdapter.default_effort)
    if not isinstance(model, str):
        return ExternalWorkerRoute(configured=True, spawn=None)
    if not isinstance(effort, str):
        return ExternalWorkerRoute(configured=True, spawn=None)
    model = model.strip()
    effort = effort.strip()
    if _ANTIGRAVITY_MODEL_RE.fullmatch(model) is None:
        return ExternalWorkerRoute(configured=True, spawn=None)
    if effort not in _ANTIGRAVITY_EFFORTS:
        return ExternalWorkerRoute(configured=True, spawn=None)

    if adapter == "antigravity":

        def configured_spawn(task, workspace: str, *, board: str | None = None):
            return spawn(
                task,
                workspace,
                board=board,
                model=model,
                effort=effort,
            )

        return ExternalWorkerRoute(configured=True, spawn=configured_spawn)

    return ExternalWorkerRoute(configured=True, spawn=spawn)


def configured_route_for_assignee(assignee: str) -> ExternalWorkerRoute:
    """Read the normal Hermes config; config errors leave routing disabled."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        kanban = config.get("kanban", {}) if isinstance(config, Mapping) else {}
        return resolve_external_worker_route(assignee, kanban)
    except Exception:
        return ExternalWorkerRoute(configured=False, spawn=None)
