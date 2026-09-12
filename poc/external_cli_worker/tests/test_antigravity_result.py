import json
from pathlib import Path

from poc.external_cli_worker.antigravity import AntigravityAdapter
from poc.external_cli_worker.result import ResultStatus


def _fake_agy(tmp_path: Path, body: str, code: int = 0) -> str:
    program = tmp_path / "agy"
    program.write_text("#!/bin/sh\nprintf '%s' '" + body.replace("'", "'\\''") + "'\nexit " + str(code) + "\n")
    program.chmod(0o755)
    return str(program)


def test_argv_is_pinned_and_adapter_uses_workspace_and_no_shell(tmp_path, monkeypatch):
    adapter = AntigravityAdapter(_fake_agy(tmp_path, json.dumps({"status": "SUCCESS", "response": "ok"})))
    calls = {}
    import subprocess
    real_run = subprocess.run
    def spy(*args, **kwargs):
        calls.update(kwargs)
        return real_run(*args, **kwargs)
    monkeypatch.setattr(subprocess, "run", spy)
    result = adapter.run("hello; never shell", str(tmp_path))
    assert result.status is ResultStatus.SUCCESS
    assert calls["cwd"] == str(tmp_path)
    assert calls["shell"] is False
    assert adapter.build_argv("x") == [adapter.executable, "--new-project", "--model", "gemini-3.8-flash-medium", "--effort", "medium", "--mode", "accept-edits", "--sandbox", "--output-format", "json", "--print", "x"]


def test_result_failures_fail_closed(tmp_path):
    cases = [
        ("not-json", 0, ResultStatus.INVALID_JSON),
        (json.dumps({"status": "FAILED", "response": "ok"}), 0, ResultStatus.MODEL_FAILURE),
        (json.dumps({"status": "SUCCESS"}), 0, ResultStatus.MODEL_FAILURE),
        (json.dumps({"status": "SUCCESS", "response": "ok"}), 7, ResultStatus.CLI_ERROR),
    ]
    for body, code, expected in cases:
        result = AntigravityAdapter(_fake_agy(tmp_path, body, code)).run("p", str(tmp_path))
        assert result.status is expected


def test_missing_executable_is_a_cli_error_not_a_success(tmp_path):
    result = AntigravityAdapter(str(tmp_path / "missing-agy")).run("p", str(tmp_path))
    assert result.status is ResultStatus.CLI_ERROR
