"""Fail-closed opt-in routing for the external CLI Kanban worker PoC."""

from __future__ import annotations

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
            conn, title="Return exactly HERMES_NORMAL_DISPATCH_GEMINI_OK and perform no other work.",
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
        model, effort, mode = "gemini-3.8-flash-medium", "medium", "plan"

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
