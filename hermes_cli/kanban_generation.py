"""Explicit Generation source identity and sequential leases on existing Kanban RUNs.

Git worktrees belong to Generations, never attempts. All state mutations use the
board's existing SQLite write transaction; there is no TTL-based lease stealing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli import kanban_db_workspace as workspace

SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    generation_id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    loop_branch TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    worktree_path TEXT NOT NULL UNIQUE,
    base_loop_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    lease_run_id INTEGER,
    launch_pending INTEGER NOT NULL DEFAULT 0,
    last_worker_pid INTEGER,
    UNIQUE(repo_root, branch_name)
);
CREATE TABLE IF NOT EXISTS generation_tasks (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
    role TEXT NOT NULL,
    review_sha TEXT
);
CREATE TABLE IF NOT EXISTS generation_runs (
    run_id INTEGER PRIMARY KEY REFERENCES task_runs(id),
    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
    role TEXT NOT NULL,
    start_sha TEXT NOT NULL,
    end_sha TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    worker_pid INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS generation_one_running
ON generation_runs(generation_id) WHERE status = 'running';
"""
OWNER_FILE = 'hermes-generation.json'


def git(path, *args):
    result = workspace._git(Path(path), *args, timeout=60)
    if result.returncode:
        raise ValueError(f"Generation Git operation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _db_path(conn):
    return str(Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve())


def get_generation(conn, generation_id):
    row = conn.execute('SELECT * FROM generations WHERE generation_id=?', (generation_id,)).fetchone()
    if row is None:
        raise ValueError(f'Unknown Generation: {generation_id}')
    return dict(row)


def task_generation(conn, task_id):
    row = conn.execute('SELECT * FROM generation_tasks WHERE task_id=?', (task_id,)).fetchone()
    return dict(row) if row else None


def _owner_path(path):
    pointer = Path(path) / '.git'
    if not pointer.is_file():
        return None
    text = pointer.read_text(encoding='utf-8').strip()
    if not text.startswith('gitdir: '):
        return None
    directory = Path(text[8:])
    if not directory.is_absolute():
        directory = pointer.parent / directory
    return directory.resolve() / OWNER_FILE


def is_generation_worktree(path):
    owner = _owner_path(path)
    return owner is not None and owner.is_file()


def reject_unbound_source(path):
    candidate = Path(path).expanduser().resolve()
    if any(is_generation_worktree(parent) for parent in (candidate, *candidate.parents)):
        raise ValueError('Source requires an explicit Generation binding and lease')


def _identity(conn, generation):
    return {key: generation[key] for key in ('generation_id', 'branch_name', 'worktree_path')} | {'db_path': _db_path(conn)}


def verify(conn, generation, *, clean=True):
    path = Path(generation['worktree_path'])
    if not path.is_dir() or not workspace._is_linked_worktree_checkout(path):
        raise ValueError('Generation worktree missing; shared-checkout fallback forbidden')
    owner = _owner_path(path)
    if owner is None or not owner.is_file() or json.loads(owner.read_text()) != _identity(conn, generation):
        raise ValueError('Generation worktree ownership mismatch')
    if workspace._git_common_dir(path) != workspace._git_common_dir(Path(generation['repo_root'])):
        raise ValueError('Generation repository mismatch')
    if workspace._git_current_branch(path) != generation['branch_name']:
        raise ValueError('Generation branch mismatch; fallback forbidden')
    if clean and git(path, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Generation source is dirty; commit or reconcile before another RUN')
    return git(path, 'rev-parse', 'HEAD')


def create_generation(conn, generation_id, repo_root, loop_branch, branch_name, worktree_path):
    """Create once from an explicit local LOOP ref; never adopt an existing checkout."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', generation_id):
        raise ValueError('Generation ID must be a simple identifier')
    repo = Path(repo_root).expanduser().resolve(strict=True)
    target = Path(worktree_path).expanduser()
    if not target.is_absolute():
        raise ValueError('Generation worktree path must be absolute')
    target = target.resolve()
    git(repo, 'check-ref-format', '--branch', loop_branch)
    git(repo, 'check-ref-format', '--branch', branch_name)
    if branch_name in {loop_branch, 'main', 'master'}:
        raise ValueError('Generation branch must be distinct from LOOP/main')
    with write_txn(conn):
        base = git(repo, 'rev-parse', '--verify', f'refs/heads/{loop_branch}^{{commit}}')
        if target.exists() or workspace._git_branch_exists(repo, branch_name):
            raise ValueError('Generation target or branch already exists; refusing adoption')
        conn.execute('INSERT INTO generations(generation_id,repo_root,loop_branch,branch_name,worktree_path,base_loop_sha,head_sha) VALUES(?,?,?,?,?,?,?)',
                     (generation_id, str(repo), loop_branch, branch_name, str(target), base, base))
        target.parent.mkdir(parents=True, exist_ok=True)
        git(repo, 'worktree', 'add', '-b', branch_name, str(target), base)
        generation = get_generation(conn, generation_id)
        _owner_path(target).write_text(json.dumps(_identity(conn, generation)), encoding='utf-8')
        verify(conn, generation)
    return generation


def _busy(generation):
    from hermes_cli.kanban_db_dispatch import _pid_alive
    return (generation['lease_run_id'] is not None or generation['launch_pending']
            or (generation['last_worker_pid'] is not None and _pid_alive(generation['last_worker_pid'])))


def bind_task(conn, task_id, generation_id, *, role='coder', review_sha=None):
    """Bind an unclaimed task explicitly. Different task cards may share a Generation."""
    if role not in {'coder', 'reviewer', 'correction'}:
        raise ValueError('role must be coder, reviewer, or correction')
    with write_txn(conn):
        generation = get_generation(conn, generation_id)
        if generation['state'] != 'active' or _busy(generation):
            raise ValueError('Generation is not idle and active')
        head = verify(conn, generation)
        if role == 'reviewer' and review_sha != head:
            raise ValueError('Reviewer requires the exact current Generation SHA')
        row = conn.execute('SELECT current_run_id,claim_lock,status FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or row['current_run_id'] is not None or row['claim_lock'] or row['status'] == 'running':
            raise ValueError('Task must exist and be unclaimed')
        conn.execute('INSERT INTO generation_tasks VALUES(?,?,?,?)', (task_id, generation_id, role, review_sha))
        conn.execute("UPDATE tasks SET workspace_kind='worktree',workspace_path=?,branch_name=? WHERE id=?",
                     (generation['worktree_path'], generation['branch_name'], task_id))


def prepare_run(conn, task_id, source_status):
    """Called inside the claim transaction, before changing the task or opening a RUN."""
    binding = task_generation(conn, task_id)
    if binding is None:
        return None
    generation = get_generation(conn, binding['generation_id'])
    if generation['state'] != 'active' or _busy(generation):
        return False
    head = verify(conn, generation)
    if head != generation['head_sha']:
        raise ValueError('Generation HEAD moved outside a RUN; reconciliation required')
    role = 'reviewer' if source_status == 'review' else binding['role']
    if role == 'reviewer':
        expected = binding['review_sha']
        if source_status == 'review':
            row = conn.execute('SELECT g.end_sha FROM generation_runs g JOIN task_runs r ON r.id=g.run_id WHERE r.task_id=? ORDER BY r.id DESC LIMIT 1', (task_id,)).fetchone()
            expected = row['end_sha'] if row else expected
        if expected != head:
            raise ValueError('Review SHA is stale or missing; reconciliation required')
    return generation['generation_id'], role, head


def open_run(conn, run_id, prepared):
    if prepared is None:
        return
    generation_id, role, head = prepared
    conn.execute('INSERT INTO generation_runs(run_id,generation_id,role,start_sha) VALUES(?,?,?,?)',
                 (run_id, generation_id, role, head))
    conn.execute('UPDATE generations SET lease_run_id=?,last_worker_pid=NULL WHERE generation_id=?', (run_id, generation_id))


def finish_run(conn, run_id, status):
    """Close the logical lease; live PID/pending-launch guards still prevent overlap."""
    row = conn.execute('SELECT * FROM generation_runs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        return
    generation = get_generation(conn, row['generation_id'])
    if generation['lease_run_id'] != run_id:
        raise ValueError('Generation RUN ownership mismatch')
    end_sha = None
    state = generation['state']
    try:
        end_sha = verify(conn, generation)
        if row['role'] == 'reviewer' and end_sha != row['start_sha']:
            raise ValueError('Reviewer changed Generation HEAD')
    except (ValueError, OSError, json.JSONDecodeError):
        if status in {"done", "completed", "review", "review_requested"}:
            raise ValueError("Generation source integrity failed; completion/review rejected") from None
        # Still close crashed/failed runs. Never make their source reusable.
        state = 'reconciliation_required'
    conn.execute('UPDATE generation_runs SET end_sha=?,status=? WHERE run_id=?', (end_sha, status, run_id))
    conn.execute('UPDATE generations SET lease_run_id=NULL,head_sha=COALESCE(?,head_sha),state=? WHERE generation_id=?',
                 (end_sha, state, generation['generation_id']))


def dispatch_workspace(conn, task):
    binding = task_generation(conn, task.id)
    if binding is None:
        return None
    generation = get_generation(conn, binding['generation_id'])
    if generation['lease_run_id'] != task.current_run_id:
        raise ValueError('Generation lease is not owned by this RUN')
    verify(conn, generation)
    if task.workspace_path != generation['worktree_path'] or task.branch_name != generation['branch_name']:
        raise ValueError('Task source identity differs from Generation')
    with write_txn(conn):
        conn.execute('UPDATE generations SET launch_pending=1 WHERE generation_id=? AND lease_run_id=?',
                     (generation['generation_id'], task.current_run_id))
    task._generation_expected = True
    return Path(generation['worktree_path']), generation['branch_name']


def record_spawn(conn, run_id, pid):
    """Use the captured RUN ID even if a fast worker has already completed."""
    with write_txn(conn):
        row = conn.execute('SELECT generation_id FROM generation_runs WHERE run_id=?', (run_id,)).fetchone()
        if row:
            if not pid:
                raise ValueError('Generation worker must return a monitorable PID')
            conn.execute('UPDATE generation_runs SET worker_pid=? WHERE run_id=?', (pid, run_id))
            conn.execute('UPDATE generations SET last_worker_pid=?,launch_pending=0 WHERE generation_id=?', (pid, row['generation_id']))


def integration_check(conn, generation_id):
    """Local acceptance boundary only; does not merge, rebase, or move any ref."""
    with write_txn(conn):
        generation = get_generation(conn, generation_id)
        if _busy(generation):
            return {'status': 'busy', 'generation_id': generation_id}
        if generation['state'] != 'active':
            return {'status': generation['state'], 'generation_id': generation_id}
        head = verify(conn, generation)
        current = git(generation['repo_root'], 'rev-parse', f"refs/heads/{generation['loop_branch']}")
        status = 'ready' if current == generation['base_loop_sha'] and head == generation['head_sha'] else 'reconciliation_required'
        if status != 'ready':
            conn.execute('UPDATE generations SET state=? WHERE generation_id=?', (status, generation_id))
        return {'status': status, 'generation_id': generation_id, 'base_loop_sha': generation['base_loop_sha'],
                'current_loop_sha': current, 'head_sha': head}


def cleanup_generation(conn, generation_id):
    """Explicit cleanup only after local LOOP contains the complete Generation."""
    with write_txn(conn):
        generation = get_generation(conn, generation_id)
        if _busy(generation):
            raise ValueError('Generation still has an active RUN or worker')
        head = verify(conn, generation)
        git(generation['repo_root'], 'merge-base', '--is-ancestor', head, f"refs/heads/{generation['loop_branch']}")
        git(generation['repo_root'], 'worktree', 'remove', generation['worktree_path'])
        conn.execute("UPDATE generations SET state='removed' WHERE generation_id=?", (generation_id,))
        # Keep the Generation branch and history; no forced deletion.
