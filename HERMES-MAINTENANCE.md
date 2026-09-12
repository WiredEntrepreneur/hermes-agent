# Hermes Maintenance Policy

## Ownership Model

- `upstream` = `NousResearch/hermes-agent`
- `origin` = `WiredEntrepreneur/hermes-agent`
- `main` mirrors upstream `main`
- `bytesphere/runtime` is the production Bytesphere runtime branch
- Never run a blind `hermes update` against the production runtime
- Hermes upgrades must be deliberately merged or rebased into `bytesphere/runtime`
- Run the relevant regression suite and a live Gemini worker smoke test before accepting an upgraded runtime
- Known-good recovery tag: `bytesphere-hermes-gemini-v1`

## Upgrade Rule

Treat Hermes as a controlled dependency.

For every upstream upgrade:

1. Fetch the latest `upstream/main`.
2. Keep `origin/main` synchronized with upstream.
3. Integrate the new upstream baseline into `bytesphere/runtime`.
4. Review conflicts and preserve the Bytesphere runtime invariants.
5. Run the relevant Hermes/Kanban regression tests.
6. Run a live Gemini external-worker smoke test.
7. Accept the upgrade only after verification.
8. Push the verified `bytesphere/runtime` state to the Bytesphere fork.

Do not allow an automated Hermes update to replace the accepted production runtime without this verification process.
