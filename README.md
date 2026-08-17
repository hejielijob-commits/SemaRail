# DeepSeek Harness Wren Data Agent

An out-of-tree, installable DeepSeek Harness bundle that uses Wren as the
semantic SQL layer and renders query results inside Harness conversations.

## Non-negotiable boundary

This project extends DeepSeek Harness exclusively through its public Bundle,
Cordis plugin, tool, session presentation metadata, and Client slot APIs. It
does not patch, fork, or modify the DeepSeek Harness repository.

## Target baseline

- DeepSeek Harness `0.1.0-rc.7`
- Wren Python CLI/Core `0.13.2`
- Node.js `^22.19.0 || >=24`
- Python `>=3.11`

## Planned packages

- `packages/contract`: shared JSON-safe Host/Client and Sidecar contracts.
- `packages/host`: tools, Sidecar supervision, policy, and result projection.
- `packages/client`: keyed `data_query` conversation view.
- `packages/bundle`: installable `dsh.bundle` composition.
- `python/sidecar`: supervised Wren process and framed RPC server.

The repository is in the initial contract and loadability milestone.

