"""FIX-006B: organizational grouping (``task_groups``) vs execution dependency
(``task_links``).

The contract: ``task_links`` answers "what must complete before this runs?"
and is the ONLY edge the scheduler reads; ``task_groups`` answers "what does
this task belong to?" and is scheduling-inert by construction. The two axes
coexist and never leak into each other.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_groups as kbg


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


# --- 1. GROUPING DOES NOT BLOCK -------------------------------------------

def test_grouping_does_not_block(conn):
    epic = kb.create_task(conn, title="EPIC", assignee="planner", initial_status="running")
    member = kb.create_task(conn, title="RUN", assignee="coder", groups=[epic])
    # Otherwise-eligible member stays eligible: NOT demoted to todo by the group.
    assert kb.get_task(conn, member).status == "ready"
    assert kbg.group_ids(conn, member) == [epic]
    # And it can actually be claimed while the container is still open.
    assert kb.claim_task(conn, member) is not None


def test_grouping_at_creation_and_after_the_fact(conn):
    epic = kb.create_task(conn, title="EPIC", assignee="planner")
    # Post-hoc membership, like link_tasks is post-hoc.
    member = kb.create_task(conn, title="RUN", assignee="coder")
    assert kbg.add_task_group(conn, epic, member) is True
    # Idempotent: a duplicate add is a no-op, not an error.
    assert kbg.add_task_group(conn, epic, member) is False
    assert kbg.group_ids(conn, member) == [epic]
    # Multiple memberships are allowed (V1 does not impose single-group).
    other = kb.create_task(conn, title="LOOP", assignee="planner")
    assert kbg.add_task_group(conn, other, member) is True
    assert kbg.group_ids(conn, member) == sorted([epic, other])
    # Unknown container is refused.
    with pytest.raises(ValueError, match="unknown task"):
        kbg.add_task_group(conn, "t_nope", member)
    # Self-grouping is refused.
    with pytest.raises(ValueError, match="itself"):
        kbg.add_task_group(conn, epic, epic)


# --- 2. DEPENDENCY STILL BLOCKS -------------------------------------------

def test_dependency_still_blocks(conn):
    dep = kb.create_task(conn, title="DEP", assignee="planner", initial_status="running")
    child = kb.create_task(conn, title="CHILD", assignee="coder", parents=[dep])
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None
    kb.complete_task(conn, dep)  # recomputes readiness itself
    assert kb.get_task(conn, child).status == "ready"


# --- 3. BOTH AXES COEXIST ---------------------------------------------------

def test_both_axes_coexist(conn):
    epic = kb.create_task(conn, title="EPIC", assignee="planner", initial_status="running")
    dep = kb.create_task(conn, title="DEP", assignee="planner", initial_status="running")
    task = kb.create_task(conn, title="RUN", assignee="coder", parents=[dep], groups=[epic])
    # Only the parent gates: todo while dep is open, even though the group is open too.
    assert kb.get_task(conn, task).status == "todo"
    kb.complete_task(conn, dep)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task).status == "ready"
    # Grouping survived the whole lifecycle untouched.
    assert kbg.group_ids(conn, task) == [epic]
    assert kb.parent_ids(conn, task) == [dep]


# --- 4. REMOVE GROUP DOES NOT ALTER DEPENDENCY -----------------------------

def test_remove_group_does_not_alter_dependency(conn):
    epic = kb.create_task(conn, title="EPIC", assignee="planner", initial_status="running")
    child = kb.create_task(conn, title="RUN", assignee="coder", parents=[epic], groups=[epic])
    assert kb.get_task(conn, child).status == "todo"
    assert kbg.remove_task_group(conn, epic, child) is True
    # Dependency edge intact, child still gated.
    assert kb.parent_ids(conn, child) == [epic]
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None
    # Removing a non-membership is a deterministic False, not an error.
    assert kbg.remove_task_group(conn, epic, child) is False


# --- 5. UNLINK DEPENDENCY DOES NOT REMOVE GROUP ----------------------------

def test_unlink_dependency_does_not_remove_group(conn):
    epic = kb.create_task(conn, title="EPIC", assignee="planner", initial_status="running")
    child = kb.create_task(conn, title="RUN", assignee="coder", parents=[epic], groups=[epic])
    assert kb.unlink_tasks(conn, epic, child) is True
    # Dependency gone (child promoted), membership intact.
    assert kb.parent_ids(conn, child) == []
    assert kb.get_task(conn, child).status == "ready"
    assert kbg.group_ids(conn, child) == [epic]


# --- 6. REOPEN INVALIDATION IGNORES GROUPING --------------------------------

def _reopen_done(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
                     (task_id,))


def test_reopen_invalidation_ignores_grouping(conn):
    epic = kb.create_task(conn, title="EPIC", assignee="planner")
    member = kb.create_task(conn, title="RUN", assignee="coder", groups=[epic])
    # A true dependency descendant of the same epic, for contrast.
    dep_child = kb.create_task(conn, title="DEPC", assignee="coder", parents=[epic])
    for tid in (epic, member, dep_child):
        assert kb.complete_task(conn, tid)
    _reopen_done(conn, epic)
    result = kb.invalidate_descendants_for_parent_reopen(conn, epic, author="op")
    invalidated = {entry["id"] for entry in result["invalidated"]}
    # The dependency descendant is demoted...
    assert dep_child in invalidated
    assert kb.get_task(conn, dep_child).status == "todo"
    # ...but the group member is untouched.
    assert member not in invalidated
    assert kb.get_task(conn, member).status == "done"


# --- 7. CYCLE DETECTION REMAINS DEPENDENCY-ONLY -----------------------------

def test_cycle_detection_remains_dependency_only(conn):
    a = kb.create_task(conn, title="A", assignee="p")
    b = kb.create_task(conn, title="B", assignee="p")
    c = kb.create_task(conn, title="C", assignee="p")
    # Grouping a pair that also depends on each other's container must not
    # trip the dependency cycle check: task_links stays a DAG.
    kb.link_tasks(conn, a, b)
    kbg.add_task_group(conn, a, b)
    kbg.add_task_group(conn, b, a)  # symmetric grouping is inert
    # A real dependency cycle is still refused.
    with pytest.raises(ValueError, match="cycle"):
        kb.link_tasks(conn, b, a)
    # Group membership never counts as a link, and symmetric grouping is inert.
    assert kb.parent_ids(conn, b) == [a]
    assert kbg.group_ids(conn, b) == [a]
    assert kbg.group_ids(conn, a) == [b]


# --- 8. GENERATION COMPATIBILITY --------------------------------------------

@pytest.fixture
def gen_board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = tmp_path / "repo"
    repo.mkdir()
    from hermes_cli import kanban_generation as gen
    gen.git(repo, "init", "-b", "main")
    gen.git(repo, "config", "user.name", "Test")
    gen.git(repo, "config", "user.email", "test@example.com")
    gen.git(repo, "config", "commit.gpgsign", "false")
    (repo / "source.txt").write_text("base\n")
    gen.git(repo, "add", ".")
    gen.git(repo, "commit", "-m", "base")
    gen.git(repo, "branch", "loop/001/integration")
    conn = kbc.connect()
    yield conn, repo, gen
    conn.close()


def test_generation_bound_task_can_also_be_grouped(gen_board):
    conn, repo, gen = gen_board
    g = gen.create_generation(conn, "001", repo, "loop/001/integration",
                              "loop/001/gen/001", repo.parent / "wt-001")
    card = kb.create_task(conn, title="GEN-CARD", assignee="planner", initial_status="running")
    run1 = kb.create_task(conn, title="RUN-1", assignee="worker", initial_status="blocked")
    run2 = kb.create_task(conn, title="RUN-2", assignee="worker", initial_status="blocked")
    gen.bind_task(conn, run1, g["generation_id"], role="coder")
    gen.bind_task(conn, run2, g["generation_id"], role="coder")
    kb.unblock_task(conn, run1)
    kb.unblock_task(conn, run2)
    # Group both RUNs under the Generation card: pure metadata.
    assert kbg.add_task_group(conn, card, run1) is True
    assert kbg.add_task_group(conn, card, run2) is True
    # Lease semantics unchanged: one RUN at a time per Generation...
    first = kb.claim_task(conn, run1)
    assert first is not None
    assert kb.claim_task(conn, run2) is None
    # ...and grouping neither granted nor blocked anything.
    assert kbg.group_ids(conn, run1) == [card]
    assert kbg.group_ids(conn, run2) == [card]
    assert first.workspace_path == g["worktree_path"]
    # Removing the group does not disturb the binding or the lease.
    assert kbg.remove_task_group(conn, card, run1) is True
    assert gen.task_generation(conn, run1)["generation_id"] == g["generation_id"]
    assert gen.get_generation(conn, g["generation_id"])["lease_run_id"] == first.current_run_id
    # Worktree/branch identity untouched.
    assert kb.get_task(conn, run1).workspace_path == g["worktree_path"]


# --- 9. NO TENANT / NOTIFY INHERITANCE --------------------------------------

def test_grouping_copies_no_tenant_or_notify(conn):
    from hermes_cli import kanban_db_notify as kbn

    epic = kb.create_task(conn, title="EPIC", assignee="planner", tenant="acme",
                          initial_status="running")
    kbn.add_notify_sub(conn, task_id=epic, platform="telegram", chat_id="42",
                       chat_type="dm", thread_id="", user_id=None, user_id_alt=None,
                       notifier_profile="gw")
    member = kb.create_task(conn, title="RUN", assignee="coder", groups=[epic])
    # No tenant inheritance (dependency-only behavior).
    assert kb.get_task(conn, member).tenant is None
    # No subscription inheritance.
    assert kbn.list_notify_subs(conn, member) == []
    # Same for the post-hoc verb.
    member2 = kb.create_task(conn, title="RUN2", assignee="coder")
    kbg.add_task_group(conn, epic, member2)
    assert kb.get_task(conn, member2).tenant is None
    assert kbn.list_notify_subs(conn, member2) == []
    # Contrast: a real parent link DOES inherit both.
    child = kb.create_task(conn, title="CHILD", assignee="coder", parents=[epic])
    assert kb.get_task(conn, child).tenant == "acme"
    assert len(kbn.list_notify_subs(conn, child)) == 1


# --- 10. DELETE CLEANUP -------------------------------------------------------

def test_delete_either_side_leaves_no_orphans(conn):
    g = kb.create_task(conn, title="G", assignee="p")
    m = kb.create_task(conn, title="M", assignee="p", groups=[g])
    # Delete the container: its membership rows vanish.
    assert kb.delete_task(conn, g) is True
    assert conn.execute(
        "SELECT COUNT(*) FROM task_groups WHERE group_id = ? OR task_id = ?", (g, g)
    ).fetchone()[0] == 0
    # Delete the member: same, from the other side.
    g2 = kb.create_task(conn, title="G2", assignee="p")
    m2 = kb.create_task(conn, title="M2", assignee="p", groups=[g2])
    assert kb.delete_task(conn, m2) is True
    assert conn.execute(
        "SELECT COUNT(*) FROM task_groups WHERE group_id = ? OR task_id = ?", (g2, g2)
    ).fetchone()[0] == 0
    # Archived-task purge goes through the same relation path.
    g3 = kb.create_task(conn, title="G3", assignee="p")
    m3 = kb.create_task(conn, title="M3", assignee="p", groups=[g3])
    kb.archive_task(conn, m3)
    assert kb.delete_archived_task(conn, m3) is True
    assert conn.execute(
        "SELECT COUNT(*) FROM task_groups WHERE group_id = ? OR task_id = ?", (g3, g3)
    ).fetchone()[0] == 0


# --- 11. TRANSFER -------------------------------------------------------------

def test_grouping_survives_board_transfer(tmp_path, monkeypatch):
    def _use(name: str) -> Path:
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(root))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
        for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                    "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_BOARD"):
            monkeypatch.delenv(var, raising=False)
        kb._INITIALIZED_PATHS.clear()
        return root

    from hermes_cli import kanban_transfer as kt

    _use("source")
    kb.create_board("alpha", name="Alpha")
    with kbc.connect_closing(board="alpha") as conn:
        epic = kb.create_task(conn, title="EPIC", assignee="planner")
        member = kb.create_task(conn, title="RUN", assignee="coder", groups=[epic])
        kbg.add_task_group(conn, epic, member)
        ids = {"epic": epic, "member": member}

    archive = tmp_path / "alpha.tar.gz"
    exported = kt.export_board("alpha", str(archive))
    assert exported["counts"]["task_groups"] == 1

    _use("dest")
    imported = kt.import_board(str(archive), "alpha")
    assert imported["counts"]["task_groups"] == 1
    with kbc.connect_closing(board="alpha") as conn:
        assert kbg.group_ids(conn, ids["member"]) == [ids["epic"]]
        assert kbg.grouped_task_ids(conn, ids["epic"]) == [ids["member"]]


# --- 12. CLI / API READABILITY -------------------------------------------------

def _parse(kanban_args: list[str]):
    import argparse
    from hermes_cli.kanban_parser import build_parser

    # Same throwaway-wrapper pattern as kanban.run_slash: parse on the kanban
    # parser itself (the "kanban" choice is the wrapper's, not an argv token).
    wrap = argparse.ArgumentParser(prog="hermes", add_help=False)
    parser = build_parser(wrap.add_subparsers(dest="_top"))
    parser.exit_on_error = False
    return parser.parse_args(list(kanban_args))


def test_cli_group_surface_answers_membership(tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban as kanban_cli

    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        epic = kb.create_task(conn, title="EPIC", assignee="planner")
        member = kb.create_task(conn, title="RUN", assignee="coder")
        kbg.add_task_group(conn, epic, member)

    # group add (idempotent) / group list / list --group / show --json.
    assert kanban_cli.kanban_command(_parse(["group", "add", epic, member])) == 0
    assert kanban_cli.kanban_command(_parse(["group", "list", epic])) == 0
    out = capsys.readouterr().out
    assert member in out and "1 member" in out

    assert kanban_cli.kanban_command(_parse(["list", "--group", epic])) == 0
    assert member in capsys.readouterr().out

    assert kanban_cli.kanban_command(_parse(["show", member, "--json"])) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"] == [epic]
    assert payload["parents"] == []  # dependency axis stays empty

    # group remove, then membership is gone.
    assert kanban_cli.kanban_command(_parse(["group", "remove", epic, member])) == 0
    with kbc.connect_closing() as conn:
        assert kbg.group_ids(conn, member) == []
        # ...and the dependency axis was never involved.
        assert kb.parent_ids(conn, member) == []


def test_delegated_child_guard_covers_group_mutations(monkeypatch):
    """``group add/remove`` are CLI mutations (refused in delegated child
    contexts); ``group list`` is read-only and stays allowed."""
    import argparse
    from hermes_cli import kanban as kanban_cli

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    for action in ("add", "remove"):
        args = argparse.Namespace(kanban_action="group", group_action=action,
                                  group_id="g", task_id="t")
        assert kanban_cli._is_delegated_child_cli_mutation(args) is True
    args = argparse.Namespace(kanban_action="group", group_action="list", group_id="g")
    assert kanban_cli._is_delegated_child_cli_mutation(args) is False
    # Outside a delegated context the same args are not refusals.
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    args = argparse.Namespace(kanban_action="group", group_action="add",
                              group_id="g", task_id="t")
    assert kanban_cli._is_delegated_child_cli_mutation(args) is False
