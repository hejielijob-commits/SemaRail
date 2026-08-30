# 0001: Out-of-tree plugin boundary

## Status

Accepted.

## Decision

The optional DeepSeek Harness integration ships as an independently installable
thin plugin. It mounts Host and Client adapters through `cordis.patch.yml` and
connects to an independently running, authenticated SemaRail Core. No source
file in DeepSeek Harness is changed or overlaid.

The supported Harness baseline is `>=0.1.0-rc.10 <0.2.0`. Compatibility is
isolated behind the Host tool adapter and Client slot adapter; the current
Desktop 2.0.2 runtime ships the later `0.1.1-rc.2` package line and is the
real installation target used for release acceptance.

This decision applies to the optional Harness adapter. The agent-neutral
integration boundary is defined separately in
[`0002-native-mcp-agent-boundary.md`](0002-native-mcp-agent-boundary.md).

## Consequences

- Installation and removal use the official `dsh plugin --profile` lifecycle.
- Host and Client loadability must be tested from built package artifacts.
- The plugin cannot rely on undocumented imports from Harness applications.
- Semantic runtime and Python provisioning belong to SemaRail Core, not the
  recommended Harness plugin.
