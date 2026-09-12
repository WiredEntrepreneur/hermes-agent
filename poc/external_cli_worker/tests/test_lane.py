import os
import time

from poc.external_cli_worker.lane import ExternalCliWorkerLane


class SlowAdapter:
    def run(self, prompt, workspace):
        time.sleep(1)
        raise RuntimeError("test supervisor only")


class Task:
    id = "t_test"
    current_run_id = 42


def test_spawn_returns_live_supervisor_pid(tmp_path):
    lane = ExternalCliWorkerLane(SlowAdapter(), db_path=tmp_path / "never-opened.db")
    pid = lane.spawn(Task(), str(tmp_path))
    assert isinstance(pid, int) and pid > 0
    os.kill(pid, 0)
