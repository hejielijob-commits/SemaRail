# Repository instructions

This repository is an out-of-tree DeepSeek Harness plugin. Never modify files
under the sibling `../deepseek-harness` or `../WrenAI` repositories as part of
this project. Read them only as version-pinned API references.

## Architecture constraints

- Target DeepSeek Harness `>=0.1.0-rc.10 <0.2.0` and Wren `0.13.2`. Compile
  and accept against the current Desktop runtime (`0.1.1-rc.2`) while keeping
  the public peer range compatible with rc.10.
- Integrate through `dsh.bundle`, `cordis.patch.yml`, Cordis plugins, registered
  tools, `tool/result.meta`, and keyed Client tool views.
- Do not add custom Harness session events for the MVP.
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
