"""Disposable, operator-run evidence script for the marker-only PoC."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_dispatch import dispatch_once

from .antigravity import AntigravityAdapter
from .lane import ExternalCliWorkerLane
from .lifecycle import POC_PROMPT, handle_marker_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--negative", action="store_true", help="request a deliberately mismatched marker")
    args = parser.parse_args(argv)
    prompt = POC_PROMPT if not args.negative else "Return exactly DIFFERENT_MARKER_B and perform no other work."
    root_path = Path(tempfile.mkdtemp(prefix="hermes-agy-poc-evidence-"))
    try:
        db_path, workspace = root_path / "kanban.db", root_path / "workspace"
        workspace.mkdir()
        # dispatch_once's single-writer lock must resolve the same disposable DB.
        os.environ["HERMES_KANBAN_DB"] = str(db_path)
        with connect_closing(db_path) as conn:
            task_id = kb.create_task(
                conn, title="External CLI marker round trip", body=prompt,
                assignee="gemini-worker", workspace_kind="scratch", workspace_path=str(workspace),
            )
            lane = ExternalCliWorkerLane(
                AntigravityAdapter(), prompt=prompt,
                result_handler=handle_marker_result, result_context={"db_path": str(db_path)},
            )
            dispatched = dispatch_once(conn, spawn_fn=lane.spawn, max_spawn=1)
            task = kb.get_task(conn, task_id)
            run_id = task.current_run_id if task else None
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            time.sleep(0.25)
            with connect_closing(db_path) as conn:
                task = kb.get_task(conn, task_id)
                if task and task.status in {"done", "blocked"}:
                    events = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
                    run = conn.execute("SELECT outcome, summary, metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
                    print(json.dumps({"evidence_root": str(root_path), "task_id": task_id, "run_id": run_id, "status": task.status, "result": task.result,
                                      "spawned": dispatched.spawned,
                                      "run_evidence": {"outcome": run["outcome"], "summary": run["summary"],
                                                       "metadata": json.loads(run["metadata"]) if run["metadata"] else None} if run else None,
                                      "events": [dict(row) for row in events]}, default=str))
                    expected_status = "blocked" if args.negative else "done"
                    return 0 if task.status == expected_status else 1
        print(json.dumps({"evidence_root": str(root_path), "task_id": task_id, "run_id": run_id, "status": "timeout"}))
        return 1
    finally:
        # Deliberately retained: this PoC's DB is its durable task/run/event receipt.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
