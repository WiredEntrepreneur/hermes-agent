"""Generic external-CLI process supervision and normalized result delivery."""

from __future__ import annotations

import multiprocessing
from typing import Any, Callable, Optional


def _supervise(adapter, result_handler, task_id: str, run_id: int, workspace: str,
               prompt: str, result_context: Any) -> None:
    result = adapter.run(prompt, workspace)
    result_handler(task_id, run_id, result, adapter, result_context)


class ExternalCliWorkerLane:
    """Spawn a monitorable supervisor; adapters only implement ``run``."""

    def __init__(self, adapter, *, prompt: str, result_handler: Callable, result_context: Any = None,
                 process_factory: Callable[..., multiprocessing.Process] = multiprocessing.Process):
        self.adapter = adapter
        self.prompt = prompt
        self.result_handler = result_handler
        self.result_context = result_context
        self.process_factory = process_factory

    def spawn(self, task, workspace: str, board: str | None = None) -> Optional[int]:
        if task.current_run_id is None:
            raise ValueError("claimed task has no current run id")
        process = self.process_factory(
            target=_supervise,
            args=(self.adapter, self.result_handler, task.id, int(task.current_run_id),
                  workspace, self.prompt, self.result_context),
        )
        process.start()
        return process.pid
