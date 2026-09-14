"""Explicit v1 Generation setup/inspection; scheduling remains Kanban dispatch."""
from __future__ import annotations

import argparse
import json

from hermes_cli import kanban_generation as gen
from hermes_cli.kanban_db_connect import connect_closing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('generation_id')
    for name in ('repo-root', 'loop-branch', 'branch-name', 'worktree-path'):
        create.add_argument('--' + name, required=True)
    bind = sub.add_parser('bind')
    bind.add_argument('generation_id')
    bind.add_argument('task_id')
    bind.add_argument('--role', choices=('coder', 'correction', 'reviewer'), default='coder')
    bind.add_argument('--review-sha')
    for verb in ('show', 'check-integration', 'cleanup'):
        sub.add_parser(verb).add_argument('generation_id')
    args = vars(parser.parse_args(argv))
    board, command = args.pop('board'), args.pop('command')
    with connect_closing(board=board) as conn:
        handlers = {'create': gen.create_generation, 'bind': gen.bind_task,
                    'check-integration': gen.integration_check, 'cleanup': gen.cleanup_generation}
        try:
            if command == 'show':
                result = gen.get_generation(conn, args['generation_id'])
                result['runs'] = [dict(row) for row in conn.execute(
                    'SELECT g.*,r.task_id,r.profile AS assignee,r.started_at,r.ended_at,r.outcome '
                    'FROM generation_runs g JOIN task_runs r ON r.id=g.run_id '
                    'WHERE g.generation_id=? ORDER BY g.run_id', (args['generation_id'],))]
            else:
                result = handlers[command](conn, **args)
        except ValueError as exc:
            parser.exit(1, f'{exc}\n')
    print(json.dumps(result, indent=2))
    return 0 if command != 'check-integration' or result['status'] == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
