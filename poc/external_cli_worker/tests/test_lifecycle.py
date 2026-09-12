from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing

from poc.external_cli_worker.lifecycle import complete_marker_task, sanitized_metadata
from poc.external_cli_worker.result import ExternalCliResult, ResultStatus


class Adapter:
    model = "gemini-3.8-flash-medium"
    effort = "medium"
    mode = "plan"


def _claimed(tmp_path):
    db = tmp_path / "kanban.db"
    with connect_closing(db) as conn:
        task_id = kb.create_task(conn, title="marker", assignee="gemini-worker", workspace_kind="scratch", workspace_path=str(tmp_path), initial_status="blocked")
        assert kb.unblock_task(conn, task_id)
        task = kb.claim_task(conn, task_id)
    return db, task_id, task.current_run_id


def test_correct_marker_completes_current_run_and_metadata_has_no_environment(tmp_path):
    db, task_id, run_id = _claimed(tmp_path)
    result = ExternalCliResult(ResultStatus.SUCCESS, "\nHERMES_AGY_ROUNDTRIP_OK\n", "cid", 1.2, 1, {"total_tokens": 4, "secret": "nope"})
    assert complete_marker_task(db, task_id, run_id, result, Adapter())
    with connect_closing(db) as conn:
        assert kb.get_task(conn, task_id).status == "done"
    metadata = sanitized_metadata(result, Adapter())
    assert "secret" not in metadata.get("usage", {})
    assert not ({"env", "stderr", "authorization"} & metadata.keys())


def test_wrong_marker_and_stale_run_cannot_complete(tmp_path):
    db, task_id, run_id = _claimed(tmp_path)
    wrong = ExternalCliResult(ResultStatus.SUCCESS, "DIFFERENT_MARKER_B")
    assert not complete_marker_task(db, task_id, run_id, wrong, Adapter())
    right = ExternalCliResult(ResultStatus.SUCCESS, "HERMES_AGY_ROUNDTRIP_OK")
    assert not complete_marker_task(db, task_id, run_id + 99, right, Adapter())
    with connect_closing(db) as conn:
        assert kb.get_task(conn, task_id).status == "running"
