# Changelog

All notable user-visible changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial DeepSeek Harness Host, Client, Contract, and Bundle packages.
- Governed PostgreSQL query sidecar and conversation-native result views.
- Local Semantic Console for Wren project management.
- MySQL datasource metadata, connection testing, schema browsing, and model import in the Semantic Console.
- English project screenshots and a public-project README.
- Security policy, contribution guide, GitHub issue forms, pull-request template, and continuous integration checks.
- Agent-neutral Wren MCP setup documentation and a credential-free native MCP end-to-end CI gate.
- A stdio `dsh_governed_query` MCP tool that reuses the Harness query policy,
  limits, error contract, and database cancellation path.

### Changed

- Renamed the GitHub repository to `dsh-data-agent` to match the independent project branding.
- Rebranded the management interface as DSH Data Agent while retaining clear WrenAI attribution.
- Datasource selection now shows only drivers configured in the running Python environment.
- Positioned DeepSeek Harness as an enhanced adapter while using Wren's native MCP server as the standard integration for other agents.
