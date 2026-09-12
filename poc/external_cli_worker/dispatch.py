"""Fail-closed external worker route selection for normal Kanban dispatch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .antigravity import AntigravityAdapter
from .lane import ExternalCliWorkerLane
from .lifecycle import handle_exact_response_result


_EXACT_RESPONSE_INSTRUCTION = re.compile(
    r"^Return exactly (?P<response>\S+) and perform no other work\.$"
)


@dataclass(frozen=True)
class ExternalWorkerRoute:
    """A configured route, with ``spawn`` absent for invalid config."""

    configured: bool
    spawn: Optional[Callable]


def _task_instruction(task) -> str:
    """Use bounded task content, preferring its detailed body when present."""
    return (task.body or task.title or "").strip()


def _expected_response(instruction: str) -> Optional[str]:
    match = _EXACT_RESPONSE_INSTRUCTION.fullmatch(instruction)
    return match.group("response") if match else None


def _antigravity_spawn(task, workspace: str, *, board: str | None = None) -> Optional[int]:
    instruction = _task_instruction(task)
    # The task title is the durable, operator-visible exact-output contract;
    # its optional body remains the bounded worker instruction.  With no body,
    # the title deliberately serves both roles for the minimal PoC flow.
    expected_response = _expected_response((task.title or "").strip())
    if expected_response is None:
        raise ValueError(
            "external antigravity lane requires task instruction: "
            "'Return exactly <token> and perform no other work.'"
        )
    from hermes_cli import kanban_db as kb

    lane = ExternalCliWorkerLane(
        AntigravityAdapter(),
        prompt=instruction,
        result_handler=handle_exact_response_result,
        result_context={
            "db_path": str(kb.kanban_db_path(board=board)),
            "expected_response": expected_response,
        },
    )
    return lane.spawn(task, workspace, board=board)


# This is intentionally a tiny, code-owned registry.  Configuration selects
# an adapter identifier only; it never supplies executable paths or argv.
_ADAPTER_REGISTRY: dict[str, Callable] = {"antigravity": _antigravity_spawn}


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
    return ExternalWorkerRoute(configured=True, spawn=_ADAPTER_REGISTRY.get(adapter))


def configured_route_for_assignee(assignee: str) -> ExternalWorkerRoute:
    """Read the normal Hermes config; config errors leave routing disabled."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        kanban = config.get("kanban", {}) if isinstance(config, Mapping) else {}
        return resolve_external_worker_route(assignee, kanban)
    except Exception:
        return ExternalWorkerRoute(configured=False, spawn=None)
