"""Antigravity CLI transport; deliberately contains no Kanban lifecycle code."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Mapping

from agent.delegation_context import delegated_child_subprocess_env

from .result import ExternalCliResult, ResultStatus


class AntigravityAdapter:
    """Run the pinned PoC request through ``agy`` and normalize its response."""

    default_model = "gemini-3.8-flash-medium"
    default_effort = "medium"
    model = default_model
    effort = default_effort
    mode = "accept-edits"
    provider = "gemini"
    sandbox_enabled = True
    output_format = "json"
    command_prefix = ()

    def __init__(
        self,
        executable: str = "/home/rsomarouthu/.local/bin/agy",
        timeout_seconds: int = 120,
        *,
        model: str | None = None,
        effort: str | None = None,
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.model = self.default_model if model is None else model
        self.effort = self.default_effort if effort is None else effort

    def build_argv(self, prompt: str, project_id: str) -> list[str]:
        return [
            self.executable,
            "--project", project_id,
            "--model", self.model,
            "--effort", self.effort,
            "--mode", self.mode,
            "--sandbox",
            "--output-format", "json",
            "--print", prompt,
        ]

    @staticmethod
    def _write_bounded_project(workspace: str, env: Mapping[str, str]) -> tuple[str, Path]:
        """Create an invocation-scoped agy project with sandboxed command grants."""
        project_id = str(uuid.uuid4())
        projects_dir = Path(env.get("HOME", str(Path.home()))) / ".gemini" / "config" / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        project_path = projects_dir / f"{project_id}.json"
        payload = {
            "id": project_id,
            "name": f"hermes-worker-{project_id}",
            "projectResources": {"resources": [{"folderUri": Path(workspace).resolve().as_uri()}]},
            "permissionGrants": {"permissionGrants": {"allow": ["command(*)"]}},
            "settings": {
                "fileAccessPolicy": "AGENT_SETTING_POLICY_ASK",
                "sandboxMode": True,
                "autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_PROCEED_IN_SANDBOX",
            },
            "isWorkspaceOnly": True,
        }
        project_path.write_text(json.dumps(payload), encoding="utf-8")
        return project_id, project_path

    def run(self, prompt: str, workspace: str, *, env: Mapping[str, str] | None = None) -> ExternalCliResult:
        if not shutil.which(self.executable) and not Path(self.executable).is_file():
            return ExternalCliResult(ResultStatus.CLI_ERROR, stderr=f"agy executable not found: {self.executable}")
        # agy is a reasoning child, never a Kanban worker.  The helper removes
        # the task/run authority while retaining ordinary auth/location env.
        child_env = delegated_child_subprocess_env(env or os.environ)
        project_path = None
        try:
            project_id, project_path = self._write_bounded_project(workspace, child_env)
            completed = subprocess.run(
                [*self.command_prefix, *self.build_argv(prompt, project_id)], cwd=workspace, env=child_env,
                capture_output=True, text=True, timeout=self.timeout_seconds, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ExternalCliResult(ResultStatus.TIMEOUT, stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "")
        except OSError as exc:
            return ExternalCliResult(ResultStatus.CLI_ERROR, stderr=str(exc))
        finally:
            if project_path is not None:
                project_path.unlink(missing_ok=True)
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
