"""Generation-aware worker launch: verify the lease and constrain review writes."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from hermes_cli import kanban_generation as generation
from hermes_cli.kanban_db_connect import connect_closing


def source_context(task, workspace):
    owner = generation._owner_path(workspace)
    if owner is None or not owner.is_file():
        if getattr(task, "_generation_expected", False):
            raise ValueError("Generation owner marker disappeared before launch")
        return None
    identity = json.loads(owner.read_text())
    with connect_closing(Path(identity['db_path'])) as conn:
        current = generation.get_generation(conn, identity['generation_id'])
        sha = generation.verify(conn, current)
        if current['lease_run_id'] != task.current_run_id:
            raise ValueError('Worker does not own the Generation lease')
        row = conn.execute('SELECT g.*,r.task_id FROM generation_runs g JOIN task_runs r ON r.id=g.run_id WHERE run_id=?',
                           (task.current_run_id,)).fetchone()
        if row is None or row['task_id'] != task.id or row['start_sha'] != sha:
            raise ValueError('Worker source or RUN identity changed before launch')
        return dict(row) | current


def review_prefix(context, writable_home):
    """Read-only root, with worker state writable and all repository checkouts protected.

    Bubblewrap's private PID namespace prevents /proc/<host-pid>/root access.
    Missing/disabled namespaces fail closed. This is a local filesystem boundary,
    not an authorization system for network services available to the worker.
    """
    if context is None or context['role'] != 'reviewer':
        return []
    executable = shutil.which('bwrap')
    if executable is None:
        raise ValueError('Generation review requires bubblewrap read-only execution')
    home = Path(writable_home).resolve(strict=True)
    common = generation.workspace._git_common_dir(Path(context['worktree_path']))
    records = generation.git(context['worktree_path'], 'worktree', 'list', '--porcelain')
    protected = {Path(line[9:]).resolve() for line in records.splitlines() if line.startswith('worktree ')}
    protected.add(common)
    # Reopening a source subtree as writable would invalidate read-only review.
    if any(home == path or home.is_relative_to(path) for path in protected):
        raise ValueError('Reviewer state directory must be outside repository source')
    prefix = [executable, '--die-with-parent', '--unshare-user', '--unshare-pid',
              '--ro-bind', '/', '/', '--tmpfs', '/tmp',
              '--bind', str(home), str(home), '--dev', '/dev', '--proc', '/proc']
    for path in sorted(protected, key=lambda p: (len(p.parts), str(p))):
        prefix += ['--ro-bind', str(path), str(path)]
    prefix += ['--']
    probe = subprocess.run([*prefix, '/bin/true'], capture_output=True, timeout=15)
    if probe.returncode:
        raise ValueError('Generation review read-only sandbox unavailable')
    return prefix


def worker_environment(env, context):
    if context is None:
        return
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import read_raw_config_readonly

    token = set_hermes_home_override(env['HERMES_HOME'])
    try:
        terminal = read_raw_config_readonly().get('terminal') or {}
    finally:
        reset_hermes_home_override(token)
    backend = terminal.get('backend') or env.get('TERMINAL_ENV') or 'local'
    if backend != 'local':
        raise ValueError('Generation workers require a local terminal backend')
    cwd = terminal.get('cwd')
    if cwd and cwd != '.' and Path(cwd).expanduser().resolve() != Path(context['worktree_path']):
        raise ValueError('Profile terminal.cwd conflicts with the Generation worktree')
    env['HERMES_KANBAN_GENERATION_ID'] = context['generation_id']
    env['HERMES_KANBAN_START_SHA'] = context['start_sha']
    env['HERMES_KANBAN_BRANCH'] = context['branch_name']
    env['HERMES_KANBAN_WORKSPACE'] = context['worktree_path']
    env['TERMINAL_CWD'] = context['worktree_path']
    env['GIT_OPTIONAL_LOCKS'] = '0' if context['role'] == 'reviewer' else env.get('GIT_OPTIONAL_LOCKS', '1')


def review_instruction(context):
    if context is None:
        return ''
    return (f"\nGeneration {context['generation_id']}, RUN {context['run_id']}, "
            f"role {context['role']}, exact start SHA {context['start_sha']}. "
            f"Use only branch {context['branch_name']} in {context['worktree_path']}. "
            'Do not modify LOOP/main or other worktrees. '
            + ('Read-only review: do not edit files or create commits. ' if context['role'] == 'reviewer' else '')
            + 'Finish all child processes before reporting the terminal outcome.\n')
