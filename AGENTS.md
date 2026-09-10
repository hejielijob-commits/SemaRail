# Repository instructions

This repository contains the standalone SemaRail Core, Semantic Console, MCP
servers, and database adapters. Optional agent integrations belong in separate
plugin repositories and are outside this project's current scope.

## Architecture constraints

- Target Wren `0.13.2` and keep agent integrations behind the stable MCP and
  Core HTTP boundaries.
- Treat all model-generated SQL as untrusted input.
- Keep protocol and presentation payloads JSON-safe and versioned. Unknown
  protocol or schema versions fail closed.
- Keep stdout of the Python Sidecar protocol-only; diagnostics go to stderr.
- Credentials must never enter Client payloads, tool output, session metadata,
  fixtures, or default logs.

## Change quality

- TypeScript is ESM and strict. Public exports require concise contract docs.
- Python supports 3.11+ and uses typed interfaces at process boundaries.
- Behavioral changes require focused tests. Replay-visible UI changes require a
  fixture or snapshot that proves reconstruction from durable tool events.
- Prefer small packages with explicit ownership over cross-package shortcuts.
