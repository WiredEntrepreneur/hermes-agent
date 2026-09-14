from types import SimpleNamespace

from hermes_state import SessionDB


def test_provider_calls_preserve_distinct_requested_and_returned_identity(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.record_provider_call(
            "turn-1:api:1", "session-1", turn_id="turn-1", task_id="task-1",
            task_run_id="9", worker_identity="deepseek-worker",
            configured_provider="openrouter", configured_model="qwen/qwen3-coder",
            response_provider="DeepInfra", response_model="qwen/qwen3-coder-480b-a35b",
            input_tokens=120, output_tokens=30, cache_read_tokens=20,
            reasoning_tokens=4, finish_reason="stop", provider_response_id="resp-1",
            started_at=10.0, completed_at=11.0,
        )
        db.record_provider_call(
            "turn-1:api:2", "session-1", turn_id="turn-1", task_id="task-1",
            configured_provider="openrouter", configured_model="qwen/qwen3-coder",
            response_model=None, finish_reason="stop", started_at=12.0, completed_at=13.0,
        )
        rows = db.provider_calls("session-1")
    finally:
        db.close()

    assert [row["provider_call_id"] for row in rows] == ["turn-1:api:1", "turn-1:api:2"]
    assert rows[0]["configured_model"] == "qwen/qwen3-coder"
    assert rows[0]["response_model"] == "qwen/qwen3-coder-480b-a35b"
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"], rows[0]["finish_reason"]) == (120, 30, "stop")
    assert rows[0]["task_run_id"] == "9" and rows[0]["worker_identity"] == "deepseek-worker"
    assert rows[1]["response_model"] is None


def test_length_response_is_persisted_before_truncation_handling_without_hooks_or_metrics(
    tmp_path, monkeypatch,
):
    from agent.turn_response_check import _persist_provider_call

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "41")
    db = SessionDB(tmp_path / "state.db")
    agent = SimpleNamespace(
        _session_db=db, _session_db_created=False, session_id="legacy-local",
        provider="custom", model="local/llama", api_mode="chat_completions",
        agent_identity="local-worker", _ensure_db_session=lambda: None,
    )
    response = SimpleNamespace(
        id="chatcmpl-local", model=None, provider=None,
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=8, total_tokens=58),
    )
    try:
        _persist_provider_call(
            agent, response, finish_reason="length", api_request_id="turn-x:api:1",
            effective_task_id="task-x", turn_id="turn-x", api_start_time=20.0, api_duration=2.5,
        )
        rows = db.provider_calls("legacy-local")
    finally:
        db.close()

    assert len(rows) == 1
    assert rows[0]["configured_model"] == "local/llama" and rows[0]["response_model"] is None
    assert rows[0]["input_tokens"] == 50 and rows[0]["output_tokens"] == 8
    assert rows[0]["finish_reason"] == "length" and rows[0]["truncated"] == 1
    assert rows[0]["task_run_id"] == "41" and rows[0]["provider_response_id"] == "chatcmpl-local"


def test_usage_normalization_failure_keeps_identity_audit_fail_soft(tmp_path, monkeypatch, caplog):
    from agent import turn_response_check

    def normalization_failure(*args, **kwargs):
        raise ValueError("unusual provider usage")

    monkeypatch.setattr(turn_response_check, "normalize_usage", normalization_failure)
    db = SessionDB(tmp_path / "state.db")
    agent = SimpleNamespace(
        _session_db=db, _session_db_created=False, session_id="fail-soft",
        provider="openrouter", model="requested/model", api_mode="chat_completions",
        agent_identity="worker", _ensure_db_session=lambda: None,
    )
    response = SimpleNamespace(
        id="response-1", model="returned/model", provider="upstream",
        usage=SimpleNamespace(provider_specific="malformed"),
    )
    try:
        turn_response_check._persist_provider_call(
            agent, response, finish_reason="stop", api_request_id="turn-y:api:1",
            effective_task_id="task-y", turn_id="turn-y", api_start_time=30.0, api_duration=1.0,
        )
        rows = db.provider_calls("fail-soft")
    finally:
        db.close()

    assert "Provider-call usage normalization failed" in caplog.text
    assert len(rows) == 1
    assert rows[0]["configured_model"] == "requested/model"
    assert rows[0]["response_model"] == "returned/model" and rows[0]["finish_reason"] == "stop"
    assert all(rows[0][column] is None for column in (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    ))
