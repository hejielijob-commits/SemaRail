# 0001: Out-of-tree plugin boundary

## Status

Accepted.

## Decision

The Data Agent ships as an independently installable DeepSeek Harness Bundle.
The Bundle mounts Host and Client plugins through `cordis.patch.yml`. No source
file in DeepSeek Harness is changed or overlaid.

The supported Harness baseline is `>=0.1.0-rc.10 <0.2.0`. Compatibility is
isolated behind the Host tool adapter and Client slot adapter; the current
Desktop 2.0.2 runtime ships the later `0.1.1-rc.2` package line and is the
real installation target used for release acceptance.

## Consequences

- Installation and removal use the official `dsh plugin --profile` lifecycle.
- Host and Client loadability must be tested from built package artifacts.
- The plugin cannot rely on undocumented imports from Harness applications.
- Wren and Python runtime provisioning remain explicit deployment concerns.
