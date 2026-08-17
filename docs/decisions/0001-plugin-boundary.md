# 0001: Out-of-tree plugin boundary

## Status

Accepted.

## Decision

The Data Agent ships as an independently installable DeepSeek Harness Bundle.
The Bundle mounts Host and Client plugins through `cordis.patch.yml`. No source
file in DeepSeek Harness is changed or overlaid.

The first supported Harness baseline is exactly `0.1.0-rc.7`. Compatibility is
isolated behind the Host tool adapter and Client slot adapter so a later Harness
upgrade can be validated without changing the Wren protocol.

## Consequences

- Installation and removal use the official `dsh plugin --profile` lifecycle.
- Host and Client loadability must be tested from built package artifacts.
- The plugin cannot rely on undocumented imports from Harness applications.
- Wren and Python runtime provisioning remain explicit deployment concerns.

