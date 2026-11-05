# @hejielijob/semarail-core

Standalone SemaRail Core distribution. It contains the local Semantic Console,
semantic runtime, MCP servers, and governed-query policy boundary.

The Console includes **Quality → Evaluation center** for isolated test projects.
It manages versioned suites and Agent profiles, deterministic scoring, independent
answer review, evidence retention and historical baseline comparisons. Running
an Agent requires the separate `semarail-eval-runner` project; Core never starts
Codex. See the repository's `docs/evaluation-center.md` for setup and acceptance.

This alpha package is currently built from the SemaRail repository:

```powershell
pnpm package:core
npm install --global .\dist\hejielijob-semarail-core-0.1.0-alpha.7.tgz
$env:SEMARAIL_API_TOKEN = semarail token create
$env:SEMARAIL_DATABASE_URL = "postgresql://readonly_user:password@localhost:5432/database"
semarail start --project C:\path\to\semantic-project
```

Open `http://127.0.0.1:48763` for the Console. Run `semarail status` from an
environment with the bootstrap token to verify the authenticated v1 handshake.
Do not pass that bootstrap token to an Agent; create a scoped service-account
key in Access Control for each Agent integration.

Configure `SEMARAIL_IDENTITY_PROVIDERS` to enable DingTalk or generic OIDC
employee sign-in, then run `semarail auth login --provider <id>`. The browser
callback returns no bearer token; the CLI stores a bounded SemaRail session and
uses the same current table/column/row policy as service-account API keys. Full
configuration and tenant-boundary requirements are documented in the repository
at `docs/access-control.md`.

Core binds to loopback only in this alpha. Keep the bearer token and database
DSN in the Core process environment; do not place them in agent prompts or
browser storage.
