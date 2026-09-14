"""HERMES-FIX-002: bounded finish_reason=length recovery.

A provider output cap (``finish_reason="length"`` from hitting max output tokens)
is NOT successful completion and is NOT a licence to regenerate the whole answer
indefinitely — the runtime, not the model, owns continuation policy. These tests
pin the deterministic bound: a genuine output-cap truncation gets at most
``MAX_LENGTH_CONTINUATIONS`` (default 1) automatic continuations before the turn
ends with an OUTPUT_BUDGET_EXCEEDED-class partial result, so repeated truncation
of an oversized deliverable (the t_22551762 incident shape) can never fan a single
answer into an unbounded provider-call loop while resending a growing prompt.

Cases (see the work order):
  A  finish_reason=stop            -> normal behaviour unchanged.
  B  length once, then stop        -> exactly one controlled continuation, success.
  C  length repeatedly             -> continuation stops deterministically, no loop.
  D  truncated tool-call arguments -> tool is NOT executed (fail closed).
  E  oversized write_file truncates repeatedly -> no full-regeneration amplification.
  F  bound hit, then a fresh task  -> counter reset; the next turn runs normally.
  G  prompt-filled-window logic    -> still ends on the first truncation (unchanged).
  REGRESSION  the t_22551762 12-turn shape now terminates within the bound.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import FINISH_REASON_LENGTH


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = False
        a.save_trajectories = False
        return a


def _length_text(content):
    """A normal-id (NOT partial-stream stub) output-cap text truncation."""
    from tests.run_agent.test_run_agent import _mock_response

    return _mock_response(content=content, finish_reason=FINISH_REASON_LENGTH)


def _stop_text(content):
    from tests.run_agent.test_run_agent import _mock_response

    return _mock_response(content=content, finish_reason="stop")


def _length_write_file(chunk):
    """An oversized write_file tool call truncated by the output cap — the
    t_22551762 write-amplification shape. finish_reason=length WITH tool_calls."""
    from tests.run_agent.test_run_agent import _mock_response, _mock_tool_call

    return _mock_response(
        content="",
        finish_reason=FINISH_REASON_LENGTH,
        tool_calls=[_mock_tool_call(
            name="write_file",
            arguments='{"path": "benchmark.md", "content": "' + chunk,  # deliberately unterminated
        )],
    )


def _run(agent, message, history=None):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message, conversation_history=history)


def _calls(agent):
    return agent.client.chat.completions.create.call_count


# --------------------------------------------------------------------------- A
def test_case_a_stop_is_unchanged(loop_agent):
    loop_agent.client.chat.completions.create.side_effect = [
        _stop_text("Here is the complete answer."),
    ]
    result = _run(loop_agent, "answer briefly")

    assert result["completed"] is True
    assert "complete answer" in result["final_response"]
    assert _calls(loop_agent) == 1


# --------------------------------------------------------------------------- B
def test_case_b_one_length_then_stop_completes(loop_agent):
    loop_agent.client.chat.completions.create.side_effect = [
        _length_text("First half of the answer. "),
        _stop_text("And the concluding half."),
    ]
    result = _run(loop_agent, "write a report")

    assert result["completed"] is True
    # Exactly one controlled continuation: original + 1.
    assert _calls(loop_agent) == 2
    # The continuation stitches onto the first fragment rather than replacing it.
    assert "First half of the answer." in result["final_response"]
    assert "And the concluding half." in result["final_response"]


# --------------------------------------------------------------------------- C
def test_case_c_repeated_length_stops_deterministically(loop_agent):
    # Ten consecutive output-cap truncations; the loop must NOT consume them all.
    loop_agent.client.chat.completions.create.side_effect = [
        _length_text(f"chunk {i} ") for i in range(10)
    ]
    result = _run(loop_agent, "write an enormous document")

    assert result["completed"] is False
    assert result["partial"] is True
    assert "OUTPUT_BUDGET_EXCEEDED" in (result.get("error") or "")
    # Bounded to original + 1 continuation regardless of how many truncations follow.
    assert _calls(loop_agent) == 2
    # The partial we did receive is preserved, not silently discarded.
    assert "chunk 0" in result["final_response"]


# --------------------------------------------------------------------------- D
def test_case_d_truncated_tool_call_is_not_executed(loop_agent):
    loop_agent.client.chat.completions.create.side_effect = [
        _length_write_file("line one\\nline two"),
        _length_write_file("line one\\nline two\\nline three"),
    ]
    with (
        patch("model_tools.handle_function_call") as dispatch,
        patch.object(loop_agent, "_invoke_tool") as invoke,
    ):
        result = _run(loop_agent, "write the benchmark file")

    # Fail closed: incomplete/truncated tool arguments must never execute.
    dispatch.assert_not_called()
    invoke.assert_not_called()
    assert result["completed"] is False
    assert "OUTPUT_BUDGET_EXCEEDED" in (result.get("error") or "")
    # Original + exactly one bounded retry, then refuse.
    assert _calls(loop_agent) == 2


# --------------------------------------------------------------------------- E
def test_case_e_oversized_write_file_no_amplification(loop_agent):
    # The exact incident vector: an oversized write_file that keeps truncating.
    # Without the bound this regenerated the enormous payload from the beginning
    # on every retry (with an exponentially boosted max_tokens), fanning one
    # deliverable into many provider calls.
    loop_agent.client.chat.completions.create.side_effect = [
        _length_write_file(f"benchmark section {i}, " * 50) for i in range(12)
    ]
    with (
        patch("model_tools.handle_function_call") as dispatch,
        patch.object(loop_agent, "_invoke_tool") as invoke,
    ):
        result = _run(loop_agent, "produce the full benchmark answer")

    dispatch.assert_not_called()
    invoke.assert_not_called()
    assert _calls(loop_agent) == 2, (
        "Repeated oversized write_file truncation must be bounded to the original "
        "request plus one automatic continuation, not a regeneration loop."
    )
    assert result["completed"] is False


# --------------------------------------------------------------------------- F
def test_case_f_counter_resets_between_tasks(loop_agent):
    # Turn 1 exhausts the bound.
    loop_agent.client.chat.completions.create.side_effect = [
        _length_text(f"chunk {i} ") for i in range(6)
    ]
    result1 = _run(loop_agent, "first oversized task")
    assert result1["partial"] is True
    assert _calls(loop_agent) == 2

    # Turn 2 is an INDEPENDENT task: one truncation then success. It must get its
    # own fresh continuation budget, proving the counter did not persist.
    loop_agent.client.chat.completions.create.side_effect = [
        _length_text("second task, part one. "),
        _stop_text("second task, done."),
    ]
    result2 = _run(loop_agent, "second unrelated task", history=result1["messages"])

    assert result2["completed"] is True, (
        "A fresh task must recover with its own continuation budget, not inherit "
        "the previous task's exhausted counter."
    )
    assert "second task, part one." in result2["final_response"]
    assert "second task, done." in result2["final_response"]


# --------------------------------------------------------------------------- G
def test_case_g_prompt_filled_window_still_ends_on_first_truncation(loop_agent):
    """The context-window short-circuit (compression territory) must be
    unaffected by the continuation bound: a prompt that filled the window ends
    on the FIRST truncation and names the window, never entering continuation."""
    loop_agent.context_compressor.context_length = 32768

    def _filled(content):
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        return SimpleNamespace(
            id="resp",
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content=content),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=SimpleNamespace(
                prompt_tokens=32638, completion_tokens=40, total_tokens=32678,
            ),
        )

    loop_agent.client.chat.completions.create.side_effect = [_filled("partial ")]
    result = _run(loop_agent, "summarize everything")

    assert _calls(loop_agent) == 1
    assert result["partial"] is True
    assert "context window" in result["final_response"].lower()
    assert "OUTPUT_BUDGET_EXCEEDED" not in (result.get("error") or "")


# ------------------------------------------------------------------ REGRESSION
def test_regression_t_22551762_provider_call_count_is_bounded(loop_agent):
    """Fixture modelling the t_22551762 failure shape: the model repeatedly
    narrates 'I'll write the full benchmark answer' and hits finish_reason=length
    each time. The provider-side evidence was ~12 API requests for one deliverable.

    With the bound, the same never-ending truncation sequence terminates within
    the defined policy — the original request plus at most one automatic length
    continuation — and the assertion is explicitly on the provider-call count.
    """
    # Stage far more truncations than the bound would ever consume.
    loop_agent.client.chat.completions.create.side_effect = [
        _length_text("I'll write the full benchmark answer as the deliverable. ")
        for _ in range(12)
    ]
    result = _run(loop_agent, "run the B3 architecture analysis")

    assert _calls(loop_agent) <= 2, (
        f"Repeated finish_reason=length must terminate within the bounded "
        f"continuation policy (original + 1); saw {_calls(loop_agent)} calls."
    )
    assert result["completed"] is False
    assert "OUTPUT_BUDGET_EXCEEDED" in (result.get("error") or "")


def test_bound_default_is_one():
    """The shipped default is a single automatic continuation."""
    from agent import turn_truncation

    assert turn_truncation.MAX_LENGTH_CONTINUATIONS == 1
