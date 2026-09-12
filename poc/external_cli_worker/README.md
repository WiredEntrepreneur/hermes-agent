# External CLI worker lane PoC

`ExternalCliWorkerLane` is a transport-neutral dispatcher `spawn_fn` target. It returns a real supervisor PID immediately, invokes its adapter in the dispatcher-resolved workspace, then delivers the normalized result and run context to an injected result handler. It contains no marker or Kanban lifecycle policy.

The adapter calls `agy` with an argv array, `gemini-3.8-flash-medium`, medium effort, plan mode, sandbox, and JSON output. Its reasoning child is passed through `delegated_child_subprocess_env`: it deliberately cannot inherit Kanban mutation authority. The PoC runner injects the exact-marker policy, which alone performs fenced lifecycle calls.
