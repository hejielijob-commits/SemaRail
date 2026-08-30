# Third-party notices

This file identifies third-party software that SemaRail uses or distributes
through its documented Python and JavaScript installation paths. It does not
change the repository license in [`LICENSE`](LICENSE), and it does not relicense
any third-party software as MIT.

## SemaRail source

Source and documentation authored in this repository are released under the
MIT License in [`LICENSE`](LICENSE), except for the third-party components
listed below. No per-file copyright header is added to ordinary SemaRail source
files: the repository-level license is the authoritative notice for those
files.

## WrenAI runtime and Wren Core

SemaRail's semantic adapter is based on and adapted to the public APIs and
project format of the upstream [WrenAI](https://github.com/Canner/WrenAI)
project. The adapter keeps the upstream runtime as a separate Python
dependency; this repository does not vendor a WrenAI source tree.

- `wrenai==0.13.2`, selected by the `wren` extras in
  `python/sidecar/pyproject.toml` and `apps/semantic-console/pyproject.toml`,
  declares Apache License 2.0 in its package metadata.
- `wren-core-py`, pulled in by `wrenai`, declares Apache License 2.0 in its
  package metadata.
- The upstream repository's Apache license text is available at
  [`LICENSE-APACHE-2.0`](https://github.com/Canner/WrenAI/blob/main/LICENSE-APACHE-2.0).
  The upstream repository's root [`LICENSE`](https://github.com/Canner/WrenAI/blob/main/LICENSE)
  describes the applicable license by path.

The SemaRail-owned integration boundary is documented in
`python/sidecar/sidecar/wren_adapter.py` and
`python/sidecar/sidecar/semantic_service.py`. When the WrenAI package is
redistributed as part of another deployment, retain the upstream package's
license and attribution notices.

## Python runtime components

The following direct runtime dependencies are declared by the Python packages
in this repository. Their licenses remain theirs when installed alongside
SemaRail:

| Component | Declaration in this repository | License | Upstream |
| --- | --- | --- | --- |
| SQLGlot | `sqlglot>=29` | MIT | [tobymao/sqlglot](https://github.com/tobymao/sqlglot) |
| Model Context Protocol Python SDK | `mcp[cli]==1.28.1` | MIT | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |
| PyYAML | `PyYAML>=6` | MIT | [yaml/pyyaml](https://github.com/yaml/pyyaml) |
| Psycopg | `psycopg[binary]>=3.1` | LGPL-3.0-or-later | [psycopg/psycopg](https://github.com/psycopg/psycopg) |
| MySQL Connector/Python | `mysql-connector-python>=8.0` | GPL-2.0 with the MySQL FOSS License Exception | [mysql/mysql-connector-python](https://github.com/mysql/mysql-connector-python) |

The MySQL Connector/Python license is especially relevant to deployments that
redistribute the Console with its MySQL driver. Follow the connector's own
license and FOSS exception terms; the SemaRail MIT license does not replace
them.

## JavaScript and browser components

The Semantic Console web application uses React, React DOM, Phosphor Icons,
XYFlow, i18next, react-i18next, CodeMirror, and the `@uiw/react-codemirror`
integration. These packages declare MIT licenses in their package metadata.
The exact versions are locked by `pnpm-lock.yaml` and their package license
files must remain available when a built Console artifact is redistributed.

The Client artifact bundles Apache ECharts `5.6.0`. Its Apache license and
notice are kept in:

- [`packages/client/licenses/echarts/LICENSE`](packages/client/licenses/echarts/LICENSE)
- [`packages/client/licenses/echarts/NOTICE`](packages/client/licenses/echarts/NOTICE)

The Host staging step copies the web runtime license files into
`semantic-console-web/licenses/` in the generated package artifact. Do not
remove those files when packaging or redistributing the Host artifact.

## Attribution boundary

SemaRail's own code, API contracts, MCP facade, policy layer, Console, and
Harness adapter are project-owned work under the repository MIT License. The
WrenAI runtime is an upstream dependency and semantic foundation under Apache
License 2.0; the two licenses apply to their respective works. Product names,
logos, and trademarks are not granted by an open-source license.
