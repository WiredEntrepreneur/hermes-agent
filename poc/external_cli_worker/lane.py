"""Generic supervisor lane: transport execution plus fenced callback ownership."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Callable, Optional

from hermes_cli import kanban_db as kb

from .lifecycle import complete_marker_task, fail_closed

POC_PROMPT = "Return exactly HERMES_AGY_ROUNDTRIP_OK and perform no other work."


def _supervise(adapter, db_path: str, task_id: str, run_id: int, workspace: str, prompt: str) -> None:
    result = adapter.run(prompt, workspace)
    if not complete_marker_task(Path(db_path), task_id, run_id, result, adapter):
        fail_closed(Path(db_path), task_id, run_id, result)


class ExternalCliWorkerLane:
    """Spawn a monitorable supervisor; adapters only implement ``run``."""

    def __init__(self, adapter, *, db_path: Path | None = None, prompt: str = POC_PROMPT,
                 process_factory: Callable[..., multiprocessing.Process] = multiprocessing.Process):
        self.adapter = adapter
        self.db_path = db_path
        self.prompt = prompt
        self.process_factory = process_factory

    def spawn(self, task, workspace: str, board: str | None = None) -> Optional[int]:
        if task.current_run_id is None:
            raise ValueError("claimed task has no current run id")
        db_path = self.db_path or kb.kanban_db_path(board=board)
        process = self.process_factory(
            target=_supervise,
            args=(self.adapter, str(db_path), task.id, int(task.current_run_id), workspace, self.prompt),
        )
        process.start()
        return process.pid
