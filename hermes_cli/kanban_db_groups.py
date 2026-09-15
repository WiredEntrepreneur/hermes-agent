"""Organizational grouping (``task_groups``) — containment without scheduling.

``task_links`` is the execution-dependency edge: it gates dispatch, carries
context handoff, and drives reopen invalidation. ``task_groups`` is a separate
association answering "what does this task belong to?" — an EPIC/LOOP/GENERATION
label. It is scheduling-inert by construction: no dispatcher, ``recompute_ready``,
claim, promotion, cycle-detection, or invalidation path reads this table, and
nothing here mutates ``task_links`` or task status.
"""
from __future__ import annotations

import sqlite3

from hermes_cli.kanban_db_connect import write_txn


def add_task_group(conn: sqlite3.Connection, group_id: str, task_id: str, *,
                   author: str | None = None) -> bool:
    """Record that ``task_id`` belongs to ``group_id``; idempotent.

    Pure metadata: no status change, no ``recompute_ready``, no notify/tenant
    inheritance, no dependency-graph mutation. Returns True when a new row was
    added, False when the membership already existed.
    """
    if group_id == task_id:
        raise ValueError("a task cannot be grouped under itself")
    with write_txn(conn):
        from hermes_cli.kanban_db import _append_event, _missing_task_ids

        missing = _missing_task_ids(conn, [group_id, task_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        cur = conn.execute(
            "INSERT OR IGNORE INTO task_groups (group_id, task_id) VALUES (?, ?)",
            (group_id, task_id),
        )
        added = cur.rowcount > 0
        if added:
            _append_event(conn, task_id, "grouped", {"group": group_id, "by": author})
    return added


def remove_task_group(conn: sqlite3.Connection, group_id: str, task_id: str) -> bool:
    """Remove the membership. Does not touch ``task_links`` or task status."""
    with write_txn(conn):
        from hermes_cli.kanban_db import _append_event

        cur = conn.execute(
            "DELETE FROM task_groups WHERE group_id = ? AND task_id = ?",
            (group_id, task_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _append_event(conn, task_id, "ungrouped", {"group": group_id})
    return removed


def group_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Group containers this task belongs to (sorted)."""
    return [r["group_id"] for r in conn.execute(
        "SELECT group_id FROM task_groups WHERE task_id = ? ORDER BY group_id", (task_id,),
    )]


def grouped_task_ids(conn: sqlite3.Connection, group_id: str) -> list[str]:
    """Members of a group (sorted)."""
    return [r["task_id"] for r in conn.execute(
        "SELECT task_id FROM task_groups WHERE group_id = ? ORDER BY task_id", (group_id,),
    )]
