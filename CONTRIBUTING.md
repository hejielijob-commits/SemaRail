# Contributing

Thank you for helping improve DSH Data Agent. The project is currently alpha, so focused changes with clear tests are especially valuable.

## Before you start

- Search existing issues before opening a new one.
- Use an issue to discuss large API, protocol, storage-format, or architecture changes before implementation.
- Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md).
- Keep changes inside this repository. The sibling DeepSeek Harness and WrenAI repositories are read-only API references for this project.

## Development setup

Required versions:

- Node.js `^22.19.0 || >=24`
- pnpm `11.x`
- Python `>=3.11`

Install and build the JavaScript workspace:

```powershell
pnpm install --frozen-lockfile
pnpm build
```

Create the Python environment used by the complete local runtime:

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".\python\sidecar[test,wren,mcp]" -e ".\apps\semantic-console[wren]"
```

## Change requirements

- Keep TypeScript ESM and strict; document public exports concisely.
- Keep Python compatible with 3.11+ and type process boundaries.
- Treat model-generated SQL as untrusted and keep credentials out of Client payloads, tool output, fixtures, and logs.
- Add focused tests for behavioral changes.
- Add a durable replay fixture or snapshot for replay-visible Client changes.
- Update README, security notes, or `CHANGELOG.md` when behavior visible to users changes.

## Verification

Run before submitting a pull request:

```powershell
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

For packaging or integration changes, also run:

```powershell
pnpm acceptance
pnpm acceptance:replay --dry-run
```

The real PostgreSQL gate requires an explicitly configured test database; see the README before running it.

## Pull requests

- Keep each pull request focused on one coherent change.
- Explain the problem, design choice, security impact, and verification performed.
- Do not include generated build directories, local state, credentials, or unrelated formatting changes.
- Add an entry under `Unreleased` in `CHANGELOG.md` for user-visible changes.
- Confirm that new dependencies have compatible licenses and that required notices are distributed with built artifacts.
