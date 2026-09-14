# Generation source isolation (v1)

A Generation owns one local Git branch and one linked worktree, created from an
explicit LOOP branch SHA. Kanban's existing task attempts are RUNs. Binding several
cards to a Generation, or using one card's implement/review/changes cycle, reuses
that same worktree. Existing unbound Kanban tasks retain their workspace behavior.

## Setup

Use the runtime's Python environment from the Hermes checkout. Create cards blocked,
bind them, then unblock them through the existing Kanban CLI. Generation setup is an
explicit operator action; no core model tool or automatic scheduler is added.

```sh
python -m hermes_cli.kanban_generation_cli --board example create gen-003 \
  --repo-root /absolute/repository \
  --loop-branch loop/001/integration \
  --branch-name loop/001/gen/003 \
  --worktree-path /absolute/worktrees/loop-001/gen-003
python -m hermes_cli.kanban_generation_cli --board example bind gen-003 t_example --role coder
python -m hermes_cli.kanban_generation_cli --board example show gen-003
```

Git forbids a branch named `loop/001` alongside `loop/001/gen/003`: a ref cannot
be both a file and a directory. `loop/001/integration` avoids that conflict.
The repository, LOOP, Generation branch, and worktree are explicit; there is no
fallback to the dispatcher's checkout. Existing branches/worktrees are not adopted.
Generation IDs are board-local simple identifiers. A worktree's Git administration
directory carries an owner marker binding its canonical path, branch, Generation ID,
and board DB path. Worktrees cannot be shared across boards.

For a separate review card, bind with `--role reviewer --review-sha <full SHA>`.
For the existing same-card review cycle, `request_review` closes the coder RUN and
the reviewer claim binds to that RUN's recorded end SHA. `request_changes` returns
the card to its original work role. Reviewers use the same worktree under an
exclusive lease. Use a coder/correction role for explicitly authorized edits.

## Ownership and traceability

`generations` stores source identity, `base_loop_sha`, the last accepted worktree
HEAD, state, and lease owner. `generation_tasks` binds cards and roles.
`generation_runs` associates existing `task_runs.id` values with role and exact
start/end SHA; `show` joins assignee, task, timestamps, outcome, status, and PID.
No new RUN identifier system or RUN Git branch/worktree is introduced.

The existing `BEGIN IMMEDIATE` claim transaction checks the Generation and acquires
its lease together with the task RUN. A partial unique index independently prevents
two running rows per Generation. A second claim stays queued. Completion, review,
block, archive, crash, timeout, and reclaim use the shared RUN closure path.
A terminal RUN releases its logical lease, but a live spawned PID still blocks
handoff and cleanup. A launch-in-progress flag closes the fast-worker race between
completion and recording its PID. Leases are not stolen on a timer.

Every claim and spawn verifies ownership, branch, and clean source at the expected
HEAD. Successful completion/review handoff requires clean, committed source; a
review must also finish at its starting SHA. Failed/aborted dirty work is preserved
and quarantined as `reconciliation_required` rather than passed to another RUN.

Native Generation workers require a local terminal backend and reject a conflicting
profile terminal.cwd; they cannot silently use a remote/default checkout.

Native and Antigravity review commands run through Bubblewrap with read-only source,
all registered repository worktrees, and Git administration data. The worker's home
remains writable for runtime state, with source mounts made read-only afterward.
`/tmp` is private. PID namespaces prevent access through host `/proc` paths.
Missing Bubblewrap or disabled namespaces reject the launch; there is no writable
review fallback. Automated reviews therefore require Linux with working Bubblewrap.
These mounts protect local source; they do not revoke network credentials or create
a hostile-worker security boundary. Normal coding workers are routed to the verified
Generation checkout and instructed not to modify LOOP/main or other checkouts.

## Integration and cleanup

```sh
python -m hermes_cli.kanban_generation_cli --board example check-integration gen-003
python -m hermes_cli.kanban_generation_cli --board example cleanup gen-003
```

`check-integration` compares the recorded LOOP SHA with the current local LOOP ref
and verifies the Generation HEAD. It reports `busy`, `ready`, or
`reconciliation_required`; only `ready` exits zero. A moved LOOP is persistently
marked for reconciliation. This is a preflight guard, not a merge operation or a
reservation of the LOOP ref. The integration owner must revalidate the parent at
its actual ref mutation. No merge/rebase engine or remote GitHub integration is added.

Cleanup verifies the owner marker, requires no active lease/launch/live worker, a
clean worktree, and proof its HEAD is reachable from the local LOOP. It invokes
non-forced `git worktree remove`, retains the branch/history, and marks the Generation
removed. Individual task cleanup and the existing worktree pruners preserve marked
Generation worktrees, even when an unrelated task points at one.

## Deliberate limits and recovery

This is a single-host, single-board-owner protocol, not distributed locking.
PID reuse may conservatively delay handoff. Workers must finish descendant processes
before reporting completion; unmanaged host processes do not participate in leases.
Unknown launch failures retain the launch flag because an unrecorded child could
still exist. An operator must investigate such launches before clearing that state.
A crash during Git creation can leave an unadopted branch/worktree; it is preserved
for inspection, not automatically deleted or reused. Relocating a board DB or
worktree requires deliberate ownership reconciliation. No automated reconciliation
or recovery override is exposed in v1.

No per-RUN worktrees/branches, parallel RUN DAGs, generalized Generation scheduling,
distributed lock service, automatic integration, or automatic Generation GC is added.
