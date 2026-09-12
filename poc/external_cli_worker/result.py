"""Small, transport-neutral result contract for external CLI workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ResultStatus(StrEnum):
    SUCCESS = "SUCCESS"
    CLI_ERROR = "CLI_ERROR"
    INVALID_JSON = "INVALID_JSON"
    MODEL_FAILURE = "MODEL_FAILURE"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class ExternalCliResult:
    status: ResultStatus
    response: str | None = None
    conversation_id: str | None = None
    duration_seconds: float | None = None
    num_turns: int | None = None
    usage: dict[str, Any] | None = None
    exit_code: int | None = None
    stderr: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ResultStatus.SUCCESS
