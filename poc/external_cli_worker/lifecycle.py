"""Fenced host policy for validated external worker result claims."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing

from .result import ExternalCliResult
from .worker_result import (
    InvalidWorkerResult, WorkerOutcome, WorkerResult, parse_and_validate_worker_result,
)

# Compatibility for the transport-only generations; normal dispatch does not use it.
EXPECTED_MARKER = "HERMES_AGY_ROUNDTRIP_OK"
POC_PROMPT = f"Return exactly {EXPECTED_MARKER} and perform no other work."


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def sanitized_metadata(
    result: ExternalCliResult, adapter, worker_result: WorkerResult | None = None,
    *, worker: str | None = None, invalid_reason: str | None = None,
) -> dict:
    """Return only bounded transport fields and validated/redacted result data."""
    metadata = {
        "worker": worker,
        "worker_lane": "external-cli",
        "provider": getattr(adapter, "provider", "gemini"),
        "adapter": "antigravity",
        "model": adapter.model,
        "effort": adapter.effort,
        "mode": adapter.mode,
        "sandbox": bool(getattr(adapter, "sandbox_enabled", True)),
        "output": getattr(adapter, "output_format", "json"),
        "transport_status": result.status.value,
    }
    if isinstance(result.conversation_id, str) and len(result.conversation_id) <= 256:
        metadata["conversation_id"] = result.conversation_id
    if (
        isinstance(result.duration_seconds, (int, float))
        and not isinstance(result.duration_seconds, bool)
        and math.isfinite(result.duration_seconds)
        and 0 <= result.duration_seconds <= 86_400
    ):
        metadata["duration_seconds"] = result.duration_seconds
    if (
        isinstance(result.num_turns, int) and not isinstance(result.num_turns, bool)
        and 0 <= result.num_turns <= 1_000_000
    ):
        metadata["num_turns"] = result.num_turns
    if result.usage:
        allowed_usage = {
            "input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens",
        }
        metadata["usage"] = {
            key: value for key, value in result.usage.items()
            if key in allowed_usage
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1_000_000_000_000
        }
    if worker_result is not None:
        metadata["worker_result"] = worker_result.as_dict()
    if invalid_reason is not None:
        metadata["worker_result_status"] = "INVALID_WORKER_RESULT"
        metadata["validation_error"] = invalid_reason
    return _redact({key: value for key, value in metadata.items() if value is not None})


def _block(
    db_path: Path, task_id: str, run_id: int, *, reason: str, kind: str,
    metadata: dict,
) -> bool:
    with connect_closing(db_path) as conn:
        return bool(kb.block_task(
            conn, task_id, reason=_redact(reason), kind=kind,
            expected_run_id=run_id, metadata=metadata,
        ))


def fail_closed(
    db_path: Path, task_id: str, run_id: int, failure_status: str,
    *, metadata: dict, detail: str | None = None,
) -> bool:
    reason = f"external-cli {failure_status}"
    if detail:
        reason += f": {detail}"
    return _block(
        db_path, task_id, run_id, reason=reason, kind="capability", metadata=metadata,
    )


def apply_worker_lifecycle(
    db_path: Path, task_id: str, run_id: int, worker_result: WorkerResult,
    metadata: dict,
) -> bool:
    """Map the bounded enum to existing fenced Hermes lifecycle primitives."""
    summary = _redact(worker_result.summary)
    with connect_closing(db_path) as conn:
        if worker_result.outcome is WorkerOutcome.COMPLETED:
            return kb.complete_task(
                conn, task_id, result="External CLI worker reported COMPLETED",
                summary=summary, metadata=metadata, expected_run_id=run_id,
            )
        if worker_result.outcome is WorkerOutcome.REVIEW_REQUIRED:
            return bool(kb.request_review(
                conn, task_id, summary=summary, metadata=metadata,
                expected_run_id=run_id,
            ))
        if worker_result.outcome is WorkerOutcome.BLOCKED:
            kind = "needs_input"
        elif worker_result.outcome is WorkerOutcome.FAILED_RETRYABLE:
            kind = "transient"
        else:
            kind = "capability"
        return bool(kb.block_task(
            conn, task_id,
            reason=f"external-cli {worker_result.outcome.value}: {summary}",
            kind=kind, expected_run_id=run_id, metadata=metadata,
        ))


def handle_worker_result(
    task_id: str, run_id: int, result: ExternalCliResult, adapter, context: dict,
) -> bool:
    """Separate transport success from result parsing, then apply host policy."""
    db_path = Path(context["db_path"])
    worker = context.get("worker")
    if not result.succeeded:
        metadata = sanitized_metadata(result, adapter, worker=worker)
        return fail_closed(
            db_path, task_id, run_id, result.status.value, metadata=metadata,
        )
    try:
        worker_result = parse_and_validate_worker_result(result.response, context["workspace"])
    except InvalidWorkerResult as exc:
        metadata = sanitized_metadata(
            result, adapter, worker=worker, invalid_reason=str(exc),
        )
        return fail_closed(
            db_path, task_id, run_id, "INVALID_WORKER_RESULT",
            metadata=metadata, detail=str(exc),
        )
    metadata = sanitized_metadata(result, adapter, worker_result, worker=worker)
    return apply_worker_lifecycle(db_path, task_id, run_id, worker_result, metadata)


def complete_marker_task(
    db_path: Path, task_id: str, run_id: int, result: ExternalCliResult, adapter,
) -> bool:
    """Legacy marker helper retained only for older transport compatibility tests."""
    if not result.succeeded or result.response is None or result.response.strip() != EXPECTED_MARKER:
        return False
    with connect_closing(db_path) as conn:
        return kb.complete_task(
            conn, task_id, result="External CLI marker validated",
            summary="Antigravity marker PoC completed",
            metadata=sanitized_metadata(result, adapter), expected_run_id=run_id,
        )


def handle_marker_result(
    task_id: str, run_id: int, result: ExternalCliResult, adapter, context: dict,
) -> bool:
    return handle_exact_response_result(
        task_id, run_id, result, adapter,
        {**context, "expected_response": EXPECTED_MARKER},
    )


def handle_exact_response_result(
    task_id: str, run_id: int, result: ExternalCliResult, adapter, context: dict,
) -> bool:
    """Legacy exact-response policy; normal production dispatch does not call it."""
    db_path = Path(context["db_path"])
    if not result.succeeded:
        return fail_closed(
            db_path, task_id, run_id, result.status.value,
            metadata=sanitized_metadata(result, adapter),
        )
    if result.response is None or result.response.strip() != context["expected_response"]:
        return fail_closed(
            db_path, task_id, run_id, "CONTRACT_MISMATCH",
            metadata=sanitized_metadata(result, adapter), detail="unexpected_response",
        )
    with connect_closing(db_path) as conn:
        return kb.complete_task(
            conn, task_id, result="External CLI exact response validated",
            summary="Antigravity external worker completed",
            metadata=sanitized_metadata(result, adapter), expected_run_id=run_id,
        )
