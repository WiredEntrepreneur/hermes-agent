"""Bounded, non-authoritative result claims from an external worker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0"
MAX_TOTAL_BYTES = 32_768
MAX_SUMMARY_LENGTH = 2_000
MAX_LIST_ENTRIES = 20
MAX_ENTRY_LENGTH = 1_000

_LIST_FIELDS = ("artifacts", "evidence", "findings", "changed_files", "tests")
_ALLOWED_FIELDS = {"schema_version", "outcome", "summary", *_LIST_FIELDS, "continuation"}
_PATH_FIELDS = ("artifacts", "changed_files")


class WorkerOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    BLOCKED = "BLOCKED"


class InvalidWorkerResult(ValueError):
    """A fixed-code validation failure safe to persist without raw output."""


@dataclass(frozen=True)
class WorkerResult:
    schema_version: str
    outcome: WorkerOutcome
    summary: str
    artifacts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    continuation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "summary": self.summary,
        }
        for field in _LIST_FIELDS:
            entries = list(getattr(self, field))
            if entries:
                value[field] = entries
        if self.continuation is not None:
            value["continuation"] = self.continuation
        return value


def parse_worker_result(response: str | None) -> dict[str, Any]:
    """Parse exactly one bounded JSON object; never repair model output."""
    if not isinstance(response, str):
        raise InvalidWorkerResult("response_not_string")
    if len(response.encode("utf-8")) > MAX_TOTAL_BYTES:
        raise InvalidWorkerResult("result_too_large")
    try:
        value = json.loads(response)
    except (json.JSONDecodeError, RecursionError):
        raise InvalidWorkerResult("malformed_json") from None
    if not isinstance(value, dict):
        raise InvalidWorkerResult("top_level_not_object")
    return value


def _bounded_string(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InvalidWorkerResult(f"{field}_wrong_type")
    normalized = value.strip()
    if not normalized:
        raise InvalidWorkerResult(f"{field}_empty")
    if len(normalized) > limit:
        raise InvalidWorkerResult(f"{field}_too_large")
    return normalized


def _bounded_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidWorkerResult(f"{field}_wrong_type")
    if len(value) > MAX_LIST_ENTRIES:
        raise InvalidWorkerResult(f"{field}_too_many_entries")
    return tuple(_bounded_string(item, f"{field}_entry", MAX_ENTRY_LENGTH) for item in value)


def _workspace_path(claim: str, workspace: Path, field: str) -> str:
    if urlsplit(claim).scheme:
        raise InvalidWorkerResult(f"{field}_outside_workspace")
    candidate = Path(claim)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (workspace / candidate).resolve()
    try:
        relative = resolved.relative_to(workspace)
    except ValueError:
        raise InvalidWorkerResult(f"{field}_outside_workspace") from None
    if not resolved.exists():
        raise InvalidWorkerResult(f"{field}_not_found")
    return relative.as_posix()


def validate_worker_result(value: dict[str, Any], workspace: str) -> WorkerResult:
    """Validate schema, resource limits, and local path claims deterministically."""
    unexpected = set(value) - _ALLOWED_FIELDS
    if unexpected:
        raise InvalidWorkerResult("unexpected_fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise InvalidWorkerResult("unsupported_schema_version")
    raw_outcome = value.get("outcome")
    if not isinstance(raw_outcome, str):
        raise InvalidWorkerResult("outcome_missing_or_wrong_type")
    try:
        outcome = WorkerOutcome(raw_outcome)
    except ValueError:
        raise InvalidWorkerResult("unknown_outcome") from None
    summary = _bounded_string(value.get("summary"), "summary", MAX_SUMMARY_LENGTH)

    lists = {
        field: _bounded_list(value[field], field) if field in value else ()
        for field in _LIST_FIELDS
    }
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise InvalidWorkerResult("workspace_not_found")
    for field in _PATH_FIELDS:
        lists[field] = tuple(_workspace_path(item, root, field) for item in lists[field])

    continuation = value.get("continuation")
    if continuation is not None:
        continuation = _bounded_string(continuation, "continuation", MAX_ENTRY_LENGTH)
    result = WorkerResult(
        schema_version=SCHEMA_VERSION,
        outcome=outcome,
        summary=summary,
        continuation=continuation,
        **lists,
    )
    normalized_size = len(
        json.dumps(result.as_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if normalized_size > MAX_TOTAL_BYTES:
        raise InvalidWorkerResult("result_too_large")
    return result


def parse_and_validate_worker_result(response: str | None, workspace: str) -> WorkerResult:
    return validate_worker_result(parse_worker_result(response), workspace)
