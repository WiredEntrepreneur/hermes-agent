# External CLI worker lane PoC

`ExternalCliWorkerLane` is a transport-neutral dispatcher `spawn_fn` target. It returns a real supervisor PID immediately, invokes its adapter in the dispatcher-resolved workspace, then delivers the normalized result and run context to an injected result handler. It contains no marker or Kanban lifecycle policy.

The adapter calls `agy` with an argv array, `gemini-3.8-flash-medium`, medium effort, accept-edits mode, sandbox, and JSON output. Its reasoning child is passed through `delegated_child_subprocess_env`: it deliberately cannot inherit Kanban mutation authority.

Normal dispatch supplies the task's existing title/body inside a bounded engineering prompt. The model must return WorkerResult V1 JSON. `worker_result.py` parses and validates the claim; `lifecycle.py` alone maps its bounded outcome enum to fenced Kanban calls. Accepted raw and normalized JSON are capped at 32,768 UTF-8 bytes, summaries at 2,000 characters, optional lists at 20 entries, and entries/continuations at 1,000 characters. Unknown fields are rejected, local file claims must exist inside the assigned workspace, and no returned value supplies task identity, run identity, commands, or lifecycle function names.

The old exact-marker helpers remain only for transport-generation compatibility. Production dispatch does not use them.
