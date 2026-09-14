"""Real Git + board transactions: Generation identity survives sequential RUNs."""
import concurrent.futures
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as db
from hermes_cli import kanban_db_dispatch as dispatcher
from hermes_cli import kanban_db_workspace as workspace
from hermes_cli import kanban_generation as gen
from hermes_cli.kanban_generation_worker import source_context, review_prefix


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: home)
    repo = tmp_path / 'repo'
    repo.mkdir()
    gen.git(repo, 'init', '-b', 'main')
    gen.git(repo, 'config', 'user.name', 'Test')
    gen.git(repo, 'config', 'user.email', 'test@example.com')
    gen.git(repo, 'config', 'commit.gpgsign', 'false')
    (repo / 'source.txt').write_text('base\n')
    gen.git(repo, 'add', '.')
    gen.git(repo, 'commit', '-m', 'base')
    gen.git(repo, 'branch', 'loop/001/integration')
    conn = db.connect()
    yield conn, repo, tmp_path, home
    conn.close()


def generation(board, name='001'):
    conn, repo, root, _ = board
    return gen.create_generation(conn, name, repo, 'loop/001/integration', f'loop/001/gen/{name}', root / 'worktrees' / name)


def task(conn, generation_id, role='coder', sha=None):
    tid = kb.create_task(conn, title=role, assignee='worker', initial_status='blocked')
    gen.bind_task(conn, tid, generation_id, role=role, review_sha=sha)
    kb.unblock_task(conn, tid)
    return tid


def test_generation_runs_share_identity_and_exclusive_transaction(board):
    conn, repo, _, _ = board
    first, second = generation(board), generation(board, '002')
    a, b = task(conn, first['generation_id']), task(conn, first['generation_id'])
    other = task(conn, second['generation_id'])
    path = Path(first['worktree_path'])
    assert first['worktree_path'] != second['worktree_path']
    assert first['branch_name'] != second['branch_name']
    assert first['base_loop_sha'] == gen.git(repo, 'rev-parse', 'loop/001/integration')
    db_path = gen._db_path(conn)

    def claim(tid):
        with db.connect_closing(Path(db_path)) as worker:
            return kb.claim_task(worker, tid)

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(claim, (a, b)))
    assert sum(c is not None for c in claims) == 1
    claimed = next(c for c in claims if c)
    waiting = b if claimed.id == a else a
    assert kb.claim_task(conn, waiting) is None
    assert kb.claim_task(conn, other) is not None  # independence, no scheduler added
    (path / 'source.txt').write_text('first run\n')
    gen.git(path, 'commit', '-am', 'RUN-001')
    sha = gen.git(path, 'rev-parse', 'HEAD')
    assert kb.complete_task(conn, claimed.id, expected_run_id=claimed.current_run_id)
    assert path.is_dir()  # per-task cleanup did not consume the Generation
    next_run = kb.claim_task(conn, waiting)
    assert next_run.workspace_path == claimed.workspace_path == str(path)
    assert next_run.branch_name == first['branch_name']
    rows = conn.execute('SELECT * FROM generation_runs WHERE generation_id=? ORDER BY run_id', (first['generation_id'],)).fetchall()
    assert rows[0]['end_sha'] == rows[1]['start_sha'] == sha
    assert len([line for line in gen.git(repo, 'worktree', 'list', '--porcelain').splitlines() if line.startswith('worktree ')]) == 3
    assert gen.git(repo, 'rev-parse', 'main') == gen.git(repo, 'rev-parse', 'loop/001/integration') == first['base_loop_sha']


def test_review_sha_blocks_mutation_and_stale_integration_and_cleanup(board):
    conn, repo, _, _ = board
    g, other = generation(board), generation(board, '002')
    coder = task(conn, g['generation_id'])
    correction = task(conn, g['generation_id'], 'correction')
    code_run = kb.claim_task(conn, coder)
    path = Path(g['worktree_path'])
    (path / 'source.txt').write_text('change\n')
    gen.git(path, 'commit', '-am', 'change')
    sha = gen.git(path, 'rev-parse', 'HEAD')
    assert kb.complete_task(conn, coder)
    reviewer = task(conn, g['generation_id'], 'reviewer', sha)
    review_run = kb.claim_task(conn, reviewer)
    assert source_context(review_run, str(path))['start_sha'] == sha
    from hermes_cli.kanban_generation_worker import worker_environment
    context = source_context(review_run, str(path))
    env = {'HERMES_HOME': str(board[3])}
    worker_environment(env, context)
    assert env['TERMINAL_CWD'] == str(path)
    assert env['HERMES_KANBAN_START_SHA'] == sha
    (board[3] / 'config.yaml').write_text('terminal:\n  backend: ssh\n')
    with pytest.raises(ValueError, match='local terminal'):
        worker_environment(env, context)
    (board[3] / 'config.yaml').unlink()

    assert kb.claim_task(conn, correction) is None
    with pytest.raises(ValueError, match='active RUN'):
        gen.cleanup_generation(conn, g['generation_id'])
    # Unrelated legacy task cleanup and generic pruner cannot delete this tree.
    workspace._cleanup_worktree_workspace('unrelated', str(path), g['branch_name'])
    from hermes_cli.worktree_ops import _reap_prune_verdicts
    _reap_prune_verdicts(str(repo), [(path, 0, True, 'reap', None)], 1)
    assert path.exists()
    unbound = kb.create_task(conn, title="unbound", workspace_kind="dir", workspace_path=str(path), initial_status="blocked")
    with pytest.raises(ValueError, match="explicit Generation binding"):
        workspace.resolve_workspace(kb.get_task(conn, unbound))
    assert kb.complete_task(conn, reviewer)
    assert conn.execute('SELECT end_sha FROM generation_runs WHERE run_id=?', (review_run.current_run_id,)).fetchone()[0] == sha
    assert kb.claim_task(conn, correction).workspace_path == str(path)
    assert kb.complete_task(conn, correction)
    assert gen.integration_check(conn, g['generation_id'])['status'] == 'ready'
    gen.git(repo, 'update-ref', 'refs/heads/loop/001/integration', sha, g['base_loop_sha'])
    assert gen.integration_check(conn, other['generation_id'])['status'] == 'reconciliation_required'
    assert gen.integration_check(conn, g['generation_id'])['status'] == 'reconciliation_required'
    # Corrupting identity cannot turn cleanup into deletion of a sibling.
    with db.write_txn(conn):
        conn.execute('UPDATE generations SET branch_name=? WHERE generation_id=?', ('wrong-owner', g['generation_id']))
    with pytest.raises(ValueError, match='ownership mismatch'):
        gen.cleanup_generation(conn, g['generation_id'])
    assert Path(other['worktree_path']).exists()
    with db.write_txn(conn):
        conn.execute('UPDATE generations SET branch_name=? WHERE generation_id=?', (g['branch_name'], g['generation_id']))
    gen.cleanup_generation(conn, g['generation_id'])
    assert not path.exists()
    assert Path(other['worktree_path']).exists()


@pytest.mark.linux_only
def test_review_worker_cannot_write_source_or_refs(board):
    conn, repo, _, home = board
    g = generation(board)
    tid = task(conn, g['generation_id'], 'reviewer', g['head_sha'])
    run = kb.claim_task(conn, tid)
    ctx = source_context(run, g['worktree_path'])
    prefix = review_prefix(ctx, home)
    for target in (Path(g['worktree_path']) / 'source.txt', repo / 'source.txt', repo / '.git' / 'refs' / 'heads' / 'loop' / '001' / 'integration'):
        result = subprocess.run([*prefix, sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("forbidden")', str(target)], capture_output=True)
        assert result.returncode != 0
        assert b'Read-only file system' in result.stderr
    result = subprocess.run([*prefix, sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("review result")', str(home / 'result')], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert gen.verify(conn, g) == g['head_sha']
    assert kb.complete_task(conn, tid)


def test_live_worker_still_blocks_handoff_after_terminal_state(board):
    conn, _, _, _ = board
    g = generation(board)
    first, second = task(conn, g['generation_id']), task(conn, g['generation_id'])
    run = kb.claim_task(conn, first)
    gen.dispatch_workspace(conn, run)
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    try:
        gen.record_spawn(conn, run.current_run_id, child.pid)
        assert kb.complete_task(conn, first)
        assert gen.get_generation(conn, g['generation_id'])['lease_run_id'] is None
        assert kb.claim_task(conn, second) is None
    finally:
        child.communicate(timeout=10)
    assert kb.claim_task(conn, second).workspace_path == g['worktree_path']


def test_wrong_branch_and_review_integrity_fail_closed(board):
    conn, _, _, _ = board
    g = generation(board)
    tid = task(conn, g['generation_id'], 'reviewer', g['head_sha'])
    run = kb.claim_task(conn, tid)
    path = Path(g['worktree_path'])
    (path / 'source.txt').write_text('unauthorized\n')
    with pytest.raises(ValueError, match='integrity'):
        kb.complete_task(conn, tid)
    assert gen.get_generation(conn, g['generation_id'])['lease_run_id'] == run.current_run_id
    gen.git(path, 'restore', 'source.txt')
    assert kb.complete_task(conn, tid)
    next_task = task(conn, g['generation_id'])
    gen.git(path, 'checkout', '--detach')
    with pytest.raises(ValueError, match='branch mismatch'):
        kb.claim_task(conn, next_task)
    assert kb.get_task(conn, next_task).current_run_id is None


def test_dispatcher_routes_sequential_workers_to_generation(board, monkeypatch):
    conn, repo, _, _ = board
    g = generation(board)
    first, second = task(conn, g['generation_id']), task(conn, g['generation_id'])
    monkeypatch.setattr(dispatcher, '_profile_exists_fn', lambda: None)
    children = []
    observed = []

    def spawn(claimed, workdir, **kwargs):
        assert source_context(claimed, workdir)['generation_id'] == g['generation_id']
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], cwd=workdir, stdin=subprocess.PIPE)
        children.append(child)
        observed.append((claimed.id, workdir))
        return child.pid

    try:
        result = dispatcher.dispatch_once(conn, spawn_fn=spawn, max_in_progress=4)
        assert len(result.spawned) == 1
        assert observed[0][1] == g['worktree_path']
        kb.complete_task(conn, observed[0][0])
        assert not dispatcher.dispatch_once(conn, spawn_fn=spawn, max_in_progress=4).spawned
        children[0].communicate(timeout=10)
        result = dispatcher.dispatch_once(conn, spawn_fn=spawn, max_in_progress=4)
        assert len(result.spawned) == 1
        assert {row[0] for row in observed} == {first, second}
        assert observed[0][1] == observed[1][1]
        assert len([line for line in gen.git(repo, 'worktree', 'list', '--porcelain').splitlines() if line.startswith('worktree ')]) == 2
    finally:
        for child in children:
            if child.poll() is None:
                child.communicate(timeout=10)


def test_existing_review_changes_cycle_retains_generation_lease_and_shas(board):
    conn, _, _, _ = board
    g = generation(board)
    tid = task(conn, g['generation_id'])
    coder = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, reviewer='reviewer', expected_run_id=coder.current_run_id)
    reviewer = kb.claim_review_task(conn, tid)
    assert source_context(reviewer, g['worktree_path'])['role'] == 'reviewer'
    assert kb.claim_task(conn, tid) is None
    assert kb.request_changes(conn, tid, reason='correct this', expected_run_id=reviewer.current_run_id)[0]
    correction = kb.claim_task(conn, tid)
    assert correction.workspace_path == coder.workspace_path == reviewer.workspace_path
    path = Path(g['worktree_path'])
    (path / 'source.txt').write_text('corrected\n')
    gen.git(path, 'commit', '-am', 'correction')
    head = gen.git(path, 'rev-parse', 'HEAD')
    assert kb.request_review(conn, tid, reviewer='reviewer', expected_run_id=correction.current_run_id)
    final_review = kb.claim_review_task(conn, tid)
    assert source_context(final_review, g['worktree_path'])['start_sha'] == head
    assert kb.complete_task(conn, tid)
