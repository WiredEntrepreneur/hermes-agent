# External CLI worker lane PoC

`ExternalCliWorkerLane` is a dispatcher `spawn_fn` target.  It returns a real supervisor PID immediately; the supervisor invokes `AntigravityAdapter` in the dispatcher-resolved workspace and uses Hermes `complete_task(..., expected_run_id=...)` only after exact-marker validation.

The adapter calls `agy` with an argv array, `gemini-3.8-flash-medium`, medium effort, plan mode, sandbox, and JSON output. Its reasoning child is passed through `delegated_child_subprocess_env`: it deliberately cannot inherit Kanban mutation authority. The supervisor retains its explicit task/run payload and is the only component that performs lifecycle calls.
