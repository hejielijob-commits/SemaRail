# Changelog

All notable user-visible changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Independently installable `@hejielijob/semarail-core` and thin
  `@hejielijob/dsh-semarail-plugin` distributions.
- An authenticated, versioned HTTP v1 boundary for Core handshake, semantic
  context, governed query execution, and cancellation.
- PostgreSQL, MySQL, SQLite, ClickHouse, and DuckDB Console adapters for
  connection testing, schema browsing, and model import.
- DingTalk and generic OIDC employee sign-in with one-time browser/device
  authorization, short-lived revocable SemaRail sessions, immutable provider
  identities, external-organization allowlisting, employee administration, and
  the same table/column/row policies used by service accounts.

### Changed

- DeepSeek Harness now connects to an independently running SemaRail Core;
  project selection, credentials, Python lifecycle, and policy limits no longer
  live in the recommended Harness plugin.
- The original `@hejielijob/dsh-wren-data-agent` package remains available as a
  legacy all-in-one compatibility artifact during migration.
- User-facing Console, documentation, API, and GitHub template copy now uses
  SemaRail naming consistently; obsolete Harness screenshots were removed.
- The Semantic Console browser override now uses
  `semarail.semantic-console-url` while reading the previous key as a migration
  fallback.
- DeepSeek Harness now reads a dedicated, scoped service-account key from
  `SEMARAIL_HARNESS_TOKEN`; the Core bootstrap administrator token is no longer
  the adapter default.
- Console project administration and access-control administration use
  independent capabilities, and policy unbinding or employee disablement takes
  effect on the next request without reviving old sessions.
- Physical table row/column enforcement now resolves aliases by SQL lexical
  scope and keeps other-project policies out of the current project's compiled
  data policy.

## [0.1.0-alpha.1] - 2026-08-30

### Added

- Initial DeepSeek Harness Host, Client, Contract, and Bundle packages.
- Governed PostgreSQL query sidecar and conversation-native result views.
- Local SemaRail Semantic Console for semantic project management.
- MySQL datasource metadata, connection testing, schema browsing, and model import in the Semantic Console.
- English project screenshots and a public-project README.
- Security policy, contribution guide, GitHub issue forms, pull-request template, and continuous integration checks.
- Agent-neutral SemaRail MCP setup documentation and a credential-free native MCP end-to-end CI gate.
- A stdio `semarail_governed_query` MCP tool that reuses the SemaRail governed-query policy,
  limits, error contract, and database cancellation path.
- An MCP Integration page in the Semantic Console with readiness, copyable
  stdio commands, a secret-free client configuration, and explicit MySQL scope.
- A stable, read-only SemaRail semantic MCP contract for project validation,
  model discovery, context retrieval, and dry query planning.
- A single-tarball DeepSeek Harness distribution and `pnpm package:plugin`
  command for local or GitHub Release installation.
- Automatic, versioned private Python runtime initialization for the Harness
  Sidecar and Semantic Console, with concurrent-start locking and safe errors.

### Changed

- Renamed the GitHub repository to `SemaRail` to match the independent project branding.
- Renamed the product and management interface to SemaRail, with WrenAI attribution isolated to the README's upstream-foundation and license sections.
- Datasource selection now shows only drivers configured in the running Python environment.
- Positioned DeepSeek Harness as an optional adapter while using SemaRail MCP servers as the standard integration for other agents.
- Replaced the `semarail-mcp` upstream-proxy behavior with a thin SemaRail-owned
  `SemanticService`; it continues to use WrenAI project structures directly and
  does not introduce an intermediate semantic format.
- Collapsed the Harness distribution boundary from four separately installed
  workspace packages to one dual-face Host and Client Bundle.
