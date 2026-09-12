"""Antigravity CLI transport; deliberately contains no Kanban lifecycle code."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from agent.delegation_context import delegated_child_subprocess_env

from .result import ExternalCliResult, ResultStatus


class AntigravityAdapter:
    """Run the pinned PoC request through ``agy`` and normalize its response."""

    model = "gemini-3.8-flash-medium"
    effort = "medium"
    mode = "plan"
    provider = "gemini"
    sandbox_enabled = True
    output_format = "json"

    def __init__(self, executable: str = "/home/rsomarouthu/.local/bin/agy", timeout_seconds: int = 120):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def build_argv(self, prompt: str) -> list[str]:
        return [
            self.executable, "--model", self.model, "--effort", self.effort,
            "--mode", self.mode, "--sandbox", "--output-format", "json", "--print", prompt,
        ]

    def run(self, prompt: str, workspace: str, *, env: Mapping[str, str] | None = None) -> ExternalCliResult:
        if not shutil.which(self.executable) and not Path(self.executable).is_file():
            return ExternalCliResult(ResultStatus.CLI_ERROR, stderr=f"agy executable not found: {self.executable}")
        # agy is a reasoning child, never a Kanban worker.  The helper removes
        # the task/run authority while retaining ordinary auth/location env.
        child_env = delegated_child_subprocess_env(env or os.environ)
        try:
            completed = subprocess.run(
                self.build_argv(prompt), cwd=workspace, env=child_env,
                capture_output=True, text=True, timeout=self.timeout_seconds, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ExternalCliResult(ResultStatus.TIMEOUT, stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "")
        except OSError as exc:
            return ExternalCliResult(ResultStatus.CLI_ERROR, stderr=str(exc))
        if completed.returncode != 0:
            return ExternalCliResult(ResultStatus.CLI_ERROR, exit_code=completed.returncode, stderr=completed.stderr)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return ExternalCliResult(ResultStatus.INVALID_JSON, exit_code=completed.returncode, stderr=completed.stderr)
        if not isinstance(payload, dict) or payload.get("status") != "SUCCESS" or "response" not in payload:
            return ExternalCliResult(ResultStatus.MODEL_FAILURE, exit_code=completed.returncode, stderr=completed.stderr)
        response = payload["response"]
        if not isinstance(response, str):
            return ExternalCliResult(ResultStatus.MODEL_FAILURE, exit_code=completed.returncode, stderr=completed.stderr)
        usage = payload.get("usage")
        return ExternalCliResult(
            ResultStatus.SUCCESS, response=response, conversation_id=payload.get("conversation_id"),
            duration_seconds=payload.get("duration_seconds"), num_turns=payload.get("num_turns"),
            usage=usage if isinstance(usage, dict) else None, exit_code=completed.returncode, stderr=completed.stderr,
        )
