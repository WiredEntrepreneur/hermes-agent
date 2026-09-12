import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing

from poc.external_cli_worker.lifecycle import (
    complete_marker_task, handle_exact_response_result, handle_marker_result,
    handle_worker_result, sanitized_metadata,
)
from poc.external_cli_worker.result import ExternalCliResult, ResultStatus


class Adapter:
    model = "gemini-3.8-flash-medium"
    effort = "medium"
    mode = "plan"
    provider = "gemini"
    sandbox_enabled = True
    output_format = "json"


def _claimed(tmp_path):
    db = tmp_path / "kanban.db"
    with connect_closing(db) as conn:
        task_id = kb.create_task(conn, title="marker", assignee="gemini-worker", workspace_kind="scratch", workspace_path=str(tmp_path), initial_status="blocked")
        assert kb.unblock_task(conn, task_id)
        task = kb.claim_task(conn, task_id)
    return db, task_id, task.current_run_id


def _worker_response(outcome, summary="bounded result", **fields):
    return json.dumps({
        "schema_version": "1.0", "outcome": outcome, "summary": summary, **fields,
    })


def _handle(tmp_path, outcome, summary="bounded result", **fields):
    db, task_id, run_id = _claimed(tmp_path)
    result = ExternalCliResult(
        ResultStatus.SUCCESS, _worker_response(outcome, summary, **fields),
        "conversation", 1.25, 2,
        {"input_tokens": 10, "total_tokens": 15, "secret": "not durable"},
    )
    changed = handle_worker_result(
        task_id, run_id, result, Adapter(),
        {"db_path": str(db), "workspace": str(tmp_path), "worker": "gemini-worker"},
    )
    return db, task_id, run_id, changed


def test_correct_marker_completes_current_run_and_metadata_has_no_environment(tmp_path):
    db, task_id, run_id = _claimed(tmp_path)
    result = ExternalCliResult(ResultStatus.SUCCESS, "\nHERMES_AGY_ROUNDTRIP_OK\n", "cid", 1.2, 1, {"total_tokens": 4, "secret": "nope"})
    assert complete_marker_task(db, task_id, run_id, result, Adapter())
    with connect_closing(db) as conn:
        assert kb.get_task(conn, task_id).status == "done"
    metadata = sanitized_metadata(result, Adapter())
    assert "secret" not in metadata.get("usage", {})
    assert not ({"env", "stderr", "authorization"} & metadata.keys())


def test_wrong_marker_produces_truthful_failure_evidence(tmp_path):
    db, task_id, run_id = _claimed(tmp_path)
    wrong = ExternalCliResult(ResultStatus.SUCCESS, "DIFFERENT_MARKER_B")
    assert handle_marker_result(task_id, run_id, wrong, Adapter(), {"db_path": str(db)})
    with connect_closing(db) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute("SELECT outcome, summary FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert task.status == "blocked"
        assert run["outcome"] == "blocked"
        assert "CONTRACT_MISMATCH" in run["summary"]
        assert "SUCCESS" not in run["summary"]


def test_stale_run_cannot_complete_newer_run(tmp_path):
    db, task_id, stale_run_id = _claimed(tmp_path)
    with connect_closing(db) as conn:
        assert kb.block_task(
            conn, task_id, reason="supersede test run", kind="capability",
            expected_run_id=stale_run_id,
        )
        assert kb.unblock_task(conn, task_id)
        newer = kb.claim_task(conn, task_id)
        assert newer.current_run_id != stale_run_id
    right = ExternalCliResult(ResultStatus.SUCCESS, "HERMES_AGY_ROUNDTRIP_OK")
    assert not complete_marker_task(db, task_id, stale_run_id, right, Adapter())
    with connect_closing(db) as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert task.current_run_id == newer.current_run_id


def test_exact_response_handler_preserves_stale_run_fencing(tmp_path):
    db, task_id, stale_run_id = _claimed(tmp_path)
    with connect_closing(db) as conn:
        assert kb.block_task(conn, task_id, reason="supersede", kind="capability", expected_run_id=stale_run_id)
        assert kb.unblock_task(conn, task_id)
        newer = kb.claim_task(conn, task_id)
    assert not handle_exact_response_result(
        task_id, stale_run_id, ExternalCliResult(ResultStatus.SUCCESS, "EXPECTED"), Adapter(),
        {"db_path": str(db), "expected_response": "EXPECTED"},
    )
    with connect_closing(db) as conn:
        assert kb.get_task(conn, task_id).current_run_id == newer.current_run_id


def test_completed_worker_result_completes_and_persists_only_sanitized_evidence(tmp_path):
    changed = tmp_path / "math_utils.py"
    changed.write_text("def clamp(value, lower, upper): ...\n")
    secret = "sk-proj-abc123def456ghi789jkl012"
    db, task_id, run_id, applied = _handle(
        tmp_path, "COMPLETED", f"implemented clamp; accidental {secret}",
        changed_files=["math_utils.py"], tests=["3 passed"], evidence=["inspected math_utils.py"],
    )
    assert applied
    with connect_closing(db) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute("SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    metadata = json.loads(run["metadata"])
    assert task.status == "done"
    assert run["outcome"] == "completed"
    assert secret not in run["summary"]
    assert secret not in json.dumps(metadata)
    assert metadata["worker_result"]["changed_files"] == ["math_utils.py"]
    assert metadata["worker_result"]["tests"] == ["3 passed"]
    assert metadata["transport_status"] == "SUCCESS"
    assert not ({"stderr", "env", "authorization"} & metadata.keys())


def test_blocked_worker_result_uses_fenced_needs_input_block(tmp_path):
    db, task_id, run_id, applied = _handle(tmp_path, "BLOCKED", "need operator input")
    assert applied
    with connect_closing(db) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute("SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert run["outcome"] == "blocked"
    assert json.loads(run["metadata"])["worker_result"]["outcome"] == "BLOCKED"


def test_review_required_uses_existing_review_transition(tmp_path):
    db, task_id, run_id, applied = _handle(tmp_path, "REVIEW_REQUIRED", "please inspect patch")
    assert applied
    with connect_closing(db) as conn:
        assert kb.get_task(conn, task_id).status == "review"
        run = conn.execute("SELECT outcome, metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["outcome"] == "review_requested"
    assert json.loads(run["metadata"])["worker_result"]["outcome"] == "REVIEW_REQUIRED"


def test_failed_outcomes_never_complete_and_use_existing_block_kinds(tmp_path):
    for index, (outcome, kind) in enumerate((
        ("FAILED_RETRYABLE", "transient"),
        ("FAILED_TERMINAL", "capability"),
    )):
        workspace = tmp_path / str(index)
        workspace.mkdir()
        db, task_id, run_id, applied = _handle(workspace, outcome)
        assert applied
        with connect_closing(db) as conn:
            task = kb.get_task(conn, task_id)
            run = conn.execute("SELECT outcome, metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert task.status == "blocked"
        assert task.block_kind == kind
        assert run["outcome"] == "blocked"
        assert json.loads(run["metadata"])["worker_result"]["outcome"] == outcome


def test_invalid_worker_result_and_transport_failure_cannot_complete(tmp_path):
    cases = (
        ExternalCliResult(ResultStatus.SUCCESS, "not-json"),
        ExternalCliResult(ResultStatus.MODEL_FAILURE, None, stderr="secret raw stderr"),
    )
    for index, result in enumerate(cases):
        workspace = tmp_path / str(index)
        workspace.mkdir()
        db, task_id, run_id = _claimed(workspace)
        assert handle_worker_result(
            task_id, run_id, result, Adapter(),
            {"db_path": str(db), "workspace": str(workspace), "worker": "gemini-worker"},
        )
        with connect_closing(db) as conn:
            task = kb.get_task(conn, task_id)
            run = conn.execute("SELECT summary, metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert task.status == "blocked"
        assert "stderr" not in json.dumps(json.loads(run["metadata"]))


def test_invalid_claims_never_gain_lifecycle_authority(tmp_path):
    invalid_claims = (
        {"schema_version": "1.0", "outcome": "UNKNOWN", "summary": "claim"},
        {"schema_version": "9.0", "outcome": "COMPLETED", "summary": "claim"},
        {"schema_version": "1.0", "outcome": "COMPLETED", "summary": "claim", "task_id": "other"},
        {"schema_version": "1.0", "outcome": "COMPLETED", "summary": "claim", "run_id": 999},
        {"schema_version": "1.0", "outcome": "COMPLETED", "summary": "claim", "command": "do something"},
        {"schema_version": "1.0", "outcome": "COMPLETED", "summary": "claim", "changed_files": ["../escape"]},
    )
    for index, claim in enumerate(invalid_claims):
        workspace = tmp_path / str(index)
        workspace.mkdir()
        db, task_id, run_id = _claimed(workspace)
        assert handle_worker_result(
            task_id, run_id, ExternalCliResult(ResultStatus.SUCCESS, json.dumps(claim)), Adapter(),
            {"db_path": str(db), "workspace": str(workspace), "worker": "gemini-worker"},
        )
        with connect_closing(db) as conn:
            task = kb.get_task(conn, task_id)
            run = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert task.status == "blocked"
        metadata = json.loads(run["metadata"])
        assert metadata["worker_result_status"] == "INVALID_WORKER_RESULT"
        assert "command" not in metadata


def test_valid_stale_completed_result_cannot_mutate_superseding_run(tmp_path):
    db, task_id, stale_run_id = _claimed(tmp_path)
    with connect_closing(db) as conn:
        assert kb.block_task(
            conn, task_id, reason="superseded", kind="capability",
            expected_run_id=stale_run_id,
        )
        assert kb.unblock_task(conn, task_id)
        newer = kb.claim_task(conn, task_id)
    applied = handle_worker_result(
        task_id, stale_run_id,
        ExternalCliResult(ResultStatus.SUCCESS, _worker_response("COMPLETED")),
        Adapter(),
        {"db_path": str(db), "workspace": str(tmp_path), "worker": "gemini-worker"},
    )
    assert not applied
    with connect_closing(db) as conn:
        task = kb.get_task(conn, task_id)
    assert task.status == "running"
    assert task.current_run_id == newer.current_run_id
