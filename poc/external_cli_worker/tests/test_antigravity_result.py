import json
import os
from pathlib import Path

from poc.external_cli_worker.antigravity import AntigravityAdapter
from poc.external_cli_worker.result import ResultStatus


def _fake_agy(tmp_path: Path, body: str, code: int = 0) -> str:
    program = tmp_path / "agy"
    program.write_text("#!/bin/sh\nprintf '%s' '" + body.replace("'", "'\\''") + "'\nexit " + str(code) + "\n")
    program.chmod(0o755)
    return str(program)


def test_argv_is_pinned_and_project_bounds_sandboxed_commands(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_home = tmp_path / "home"
    adapter = AntigravityAdapter(_fake_agy(tmp_path, json.dumps({"status": "SUCCESS", "response": "ok"})))
    calls = {}
    import subprocess

    real_run = subprocess.run

    def spy(*args, **kwargs):
        project_id = args[0][args[0].index("--project") + 1]
        project_path = fake_home / ".gemini" / "config" / "projects" / f"{project_id}.json"
        calls.update(kwargs, argv=args[0], project=json.loads(project_path.read_text()))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    result = adapter.run(
        "hello; never shell",
        str(workspace),
        env={**os.environ, "HOME": str(fake_home)},
    )
    assert result.status is ResultStatus.SUCCESS
    assert calls["cwd"] == str(workspace)
    assert calls["shell"] is False
    project_id = calls["project"]["id"]
    assert calls["argv"] == [
        adapter.executable,
        "--project", project_id,
        "--model", "gemini-3.8-flash-medium",
        "--effort", "medium",
        "--mode", "accept-edits",
        "--sandbox",
        "--output-format", "json",
        "--print", "hello; never shell",
    ]
    assert calls["project"]["projectResources"] == {
        "resources": [{"folderUri": workspace.resolve().as_uri()}],
    }
    assert calls["project"]["permissionGrants"] == {
        "permissionGrants": {"allow": ["command(*)"]},
    }
    assert calls["project"]["settings"] == {
        "fileAccessPolicy": "AGENT_SETTING_POLICY_ASK",
        "sandboxMode": True,
        "autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_PROCEED_IN_SANDBOX",
    }
    assert not list((fake_home / ".gemini" / "config" / "projects").iterdir())


def test_result_failures_fail_closed(tmp_path):
    fake_home = tmp_path / "home"
    cases = [
        ("not-json", 0, ResultStatus.INVALID_JSON),
        (json.dumps({"status": "FAILED", "response": "ok"}), 0, ResultStatus.MODEL_FAILURE),
        (json.dumps({"status": "SUCCESS"}), 0, ResultStatus.MODEL_FAILURE),
        (json.dumps({"status": "SUCCESS", "response": "ok"}), 7, ResultStatus.CLI_ERROR),
    ]
    for body, code, expected in cases:
        result = AntigravityAdapter(_fake_agy(tmp_path, body, code)).run(
            "p", str(tmp_path), env={**os.environ, "HOME": str(fake_home)},
        )
        assert result.status is expected
    assert not list((fake_home / ".gemini" / "config" / "projects").iterdir())


def test_missing_executable_is_a_cli_error_not_a_success(tmp_path):
    result = AntigravityAdapter(str(tmp_path / "missing-agy")).run("p", str(tmp_path))
    assert result.status is ResultStatus.CLI_ERROR
