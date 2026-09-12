"""Fail-closed opt-in routing for the external CLI Kanban worker PoC."""

from __future__ import annotations

from types import SimpleNamespace

from poc.external_cli_worker.dispatch import ExternalWorkerRoute, resolve_external_worker_route


def _config(lanes):
    return {"kanban": {"external_worker_lanes": lanes}}


def test_external_lane_config_is_disabled_by_default_and_never_accepts_commands():
    assert resolve_external_worker_route("gemini-worker", {}) == ExternalWorkerRoute(False, None)
    route = resolve_external_worker_route(
        "gemini-worker",
        {"external_worker_lanes": {"gemini-worker": {
            "type": "external_cli", "adapter": "antigravity", "command": ["not", "used"],
        }}},
    )
    assert route.configured and route.spawn is not None


def test_antigravity_spawn_uses_normal_task_intent_and_worker_result_policy(monkeypatch, tmp_path):
    import poc.external_cli_worker.dispatch as external
    from poc.external_cli_worker.lifecycle import handle_worker_result

    seen = {}

    class FakeLane:
        def __init__(self, adapter, **kwargs):
            seen.update(adapter=adapter, **kwargs)

        def spawn(self, task, workspace, board=None):
            seen.update(task=task, workspace=workspace, board=board)
            return 4321

    monkeypatch.setattr(external, "ExternalCliWorkerLane", FakeLane)
    monkeypatch.setattr(external, "AntigravityAdapter", lambda: object())
    monkeypatch.setattr("hermes_cli.kanban_db.kanban_db_path", lambda board=None: tmp_path / "kanban.db")
    task = SimpleNamespace(
        id="t_general", current_run_id=7, assignee="gemini-worker",
        title="Implement clamp", body="Add tests and run them.",
    )
    assert external._antigravity_spawn(task, str(tmp_path), board="work") == 4321
    assert "Implement clamp" in seen["prompt"]
    assert "Add tests and run them." in seen["prompt"]
    assert "WorkerResult schema v1" in seen["prompt"]
    assert "Return exactly" not in seen["prompt"]
    assert seen["result_handler"] is handle_worker_result
    assert seen["result_context"]["workspace"] == str(tmp_path)


def test_unknown_or_malformed_external_lane_is_configured_but_fails_closed():
    for entry in (
        {"type": "external_cli", "adapter": "unknown"},
        {"type": "external_cli", "command": "anything"},
        "agy --unsafe",
    ):
        route = resolve_external_worker_route(
            "gemini-worker", {"external_worker_lanes": {"gemini-worker": entry}},
        )
        assert route == ExternalWorkerRoute(True, None)


def test_normal_dispatch_selects_configured_external_lane_before_placeholder_profile(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    import poc.external_cli_worker.dispatch as external

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen = {}

    def external_spawn(task, resolved_workspace, *, board=None):
        seen.update(task_id=task.id, run_id=task.current_run_id, workspace=resolved_workspace, board=board)
        return 424242

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: _config({
        "gemini-worker": {"type": "external_cli", "adapter": "antigravity"},
    }))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name == "gemini-worker")
    monkeypatch.setitem(external._ADAPTER_REGISTRY, "antigravity", external_spawn)
    monkeypatch.setattr(kbd, "_default_spawn", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("native fallback")))

    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn, title="Inspect the assigned repository and implement the bounded utility task.",
            assignee="gemini-worker", workspace_kind="dir", workspace_path=str(workspace), initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)
        result = kbd.dispatch_once(conn, board=None, reconcile_orphans=False)
        task = kb.get_task(conn, task_id)

    assert result.spawned == [(task_id, "gemini-worker", str(workspace))]
    assert seen["task_id"] == task_id
    assert seen["run_id"] == task.current_run_id
    assert seen["workspace"] == str(workspace)
    assert task.worker_pid == 424242


def test_normal_dispatch_can_complete_general_worker_result_with_host_owned_ids(monkeypatch, tmp_path):
    import json

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    import poc.external_cli_worker.dispatch as external
    from poc.external_cli_worker.lifecycle import handle_worker_result
    from poc.external_cli_worker.result import ExternalCliResult, ResultStatus

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "math_utils.py").write_text("def clamp(value, lower, upper): ...\n")

    class Adapter:
        model, effort, mode = "gemini-3.8-flash-medium", "medium", "accept-edits"
        provider, sandbox_enabled, output_format = "gemini", True, "json"

    def external_spawn(task, resolved_workspace, *, board=None):
        response = json.dumps({
            "schema_version": "1.0", "outcome": "COMPLETED",
            "summary": "Implemented and tested clamp.",
            "changed_files": ["math_utils.py"], "tests": ["3 passed"],
        })
        handle_worker_result(
            task.id, task.current_run_id,
            ExternalCliResult(ResultStatus.SUCCESS, response, "cid", 1.0, 1, {"total_tokens": 9}),
            Adapter(),
            {
                "db_path": str(tmp_path / "kanban.db"), "workspace": resolved_workspace,
                "worker": task.assignee,
            },
        )
        return 5252

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: _config({
        "gemini-worker": {"type": "external_cli", "adapter": "antigravity"},
    }))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setitem(external._ADAPTER_REGISTRY, "antigravity", external_spawn)
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn, title="Implement clamp in the assigned repository and run its tests.",
            assignee="gemini-worker", workspace_kind="dir", workspace_path=str(workspace),
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)
        result = kbd.dispatch_once(conn, reconcile_orphans=False)
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT outcome, metadata FROM task_runs WHERE task_id = ?", (task_id,),
        ).fetchone()
    assert result.spawned == [(task_id, "gemini-worker", str(workspace))]
    assert task.status == "done"
    assert run["outcome"] == "completed"
    assert json.loads(run["metadata"])["worker_result"]["outcome"] == "COMPLETED"


def test_native_profiles_and_disabled_gemini_keep_normal_spawn_path(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    native = {"qwen38", "codex-worker", "claude-review", "gemini-worker"}
    seen = []
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: _config({}))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name in native)

    def default_spawn(task, workspace, *, board=None):
        seen.append(task.assignee)
        return 1000 + len(seen)

    monkeypatch.setattr(kbd, "_default_spawn", default_spawn)
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        for assignee in (*sorted(native), "unknown-worker"):
            task_id = kb.create_task(conn, title=assignee, assignee=assignee, initial_status="blocked")
            assert kb.unblock_task(conn, task_id)
        result = kbd.dispatch_once(conn, reconcile_orphans=False)

    assert set(seen) == native
    assert len(result.skipped_nonspawnable) == 1


def test_unknown_adapter_does_not_fall_back_to_native_spawn(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: _config({
        "gemini-worker": {"type": "external_cli", "adapter": "not-registered"},
    }))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "_default_spawn", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback")))
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(conn, title="x", assignee="gemini-worker", initial_status="blocked")
        assert kb.unblock_task(conn, task_id)
        result = kbd.dispatch_once(conn, reconcile_orphans=False)
        assert kb.get_task(conn, task_id).status == "ready"
    assert result.skipped_nonspawnable == [task_id]


def test_external_exact_contract_mismatch_blocks_the_current_run(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from poc.external_cli_worker.lifecycle import handle_exact_response_result
    from poc.external_cli_worker.result import ExternalCliResult, ResultStatus

    class Adapter:
        model, effort, mode = "gemini-3.8-flash-medium", "medium", "accept-edits"

    def external_spawn(task, workspace, *, board=None):
        handle_exact_response_result(
            task.id, task.current_run_id,
            ExternalCliResult(ResultStatus.SUCCESS, "DIFFERENT_B"), Adapter(),
            {"db_path": str(tmp_path / "kanban.db"), "expected_response": "EXPECTED_A"},
        )
        return 8888

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: _config({
        "gemini-worker": {"type": "external_cli", "adapter": "antigravity"},
    }))
    import poc.external_cli_worker.dispatch as external
    monkeypatch.setitem(external._ADAPTER_REGISTRY, "antigravity", external_spawn)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(
            conn, title="Return exactly EXPECTED_A and perform no other work.",
            body="Return exactly DIFFERENT_B and perform no other work.",
            assignee="gemini-worker", initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)
        kbd.dispatch_once(conn, reconcile_orphans=False)
        task = kb.get_task(conn, task_id)
        run = conn.execute("SELECT outcome, summary FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()
    assert task.status == "blocked"
    assert run["outcome"] == "blocked"
    assert "CONTRACT_MISMATCH" in run["summary"]
