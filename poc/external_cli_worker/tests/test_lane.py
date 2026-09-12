import os

import poc.external_cli_worker.lane as lane_module
from poc.external_cli_worker.lane import ExternalCliWorkerLane
from poc.external_cli_worker.result import ExternalCliResult, ResultStatus


class Adapter:
    def run(self, prompt, workspace):
        return ExternalCliResult(ResultStatus.SUCCESS, "adapter result")


class Task:
    id = "t_test"
    current_run_id = 42


def _handler(*args):
    return None


def test_spawn_returns_live_supervisor_pid(tmp_path):
    lane = ExternalCliWorkerLane(Adapter(), prompt="generic prompt", result_handler=_handler)
    pid = lane.spawn(Task(), str(tmp_path))
    assert isinstance(pid, int) and pid > 0
    os.kill(pid, 0)


def test_lane_has_no_marker_policy_and_delivers_normalized_result_context(tmp_path):
    assert not hasattr(lane_module, "POC_PROMPT")
    assert not hasattr(lane_module, "EXPECTED_MARKER")
    assert not hasattr(lane_module, "complete_marker_task")
    assert not hasattr(lane_module, "fail_closed")
    received = []

    def handler(*args):
        received.append(args)

    lane_module._supervise(
        Adapter(), handler, "t_1", 7, str(tmp_path), "transport input", {"receipt": 1},
    )
    task_id, run_id, result, adapter, context = received[0]
    assert (task_id, run_id, result.response, context) == ("t_1", 7, "adapter result", {"receipt": 1})
    assert isinstance(adapter, Adapter)
