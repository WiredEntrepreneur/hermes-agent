"""Fenced, deterministic Hermes lifecycle handoff for the marker-only PoC."""

from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing

from .result import ExternalCliResult

EXPECTED_MARKER = "HERMES_AGY_ROUNDTRIP_OK"


def sanitized_metadata(result: ExternalCliResult, adapter) -> dict:
    metadata = {
        "worker_lane": "external-cli", "adapter": "antigravity", "model": adapter.model,
        "effort": adapter.effort, "mode": adapter.mode, "status": result.status.value,
    }
    for name in ("conversation_id", "duration_seconds", "num_turns"):
        value = getattr(result, name)
        if value is not None:
            metadata[name] = value
    if result.usage:
        # Only structured token accounting, never process env/stdout/stderr.
        allowed_usage = {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
        metadata["usage"] = {k: v for k, v in result.usage.items() if k in allowed_usage and isinstance(v, (int, float))}
    return metadata


def complete_marker_task(db_path: Path, task_id: str, run_id: int, result: ExternalCliResult, adapter) -> bool:
    if not result.succeeded or result.response.strip() != EXPECTED_MARKER:
        return False
    with connect_closing(db_path) as conn:
        return kb.complete_task(
            conn, task_id, result="External CLI marker validated", summary="Antigravity marker PoC completed",
            metadata=sanitized_metadata(result, adapter), expected_run_id=run_id,
        )


def fail_closed(db_path: Path, task_id: str, run_id: int, result: ExternalCliResult) -> bool:
    """Leave durable failure evidence without ever converting a bad result to done."""
    with connect_closing(db_path) as conn:
        return bool(kb.block_task(conn, task_id, reason=f"external-cli {result.status.value}", kind="capability", expected_run_id=run_id))
