# @hejielijob/semarail-core

Standalone SemaRail Core distribution. It contains the local Semantic Console,
semantic runtime, MCP servers, and governed-query policy boundary. It does not
contain a DeepSeek Harness plugin.

This alpha package is currently built from the SemaRail repository:

```powershell
pnpm package:core
npm install --global .\dist\hejielijob-semarail-core-0.1.0-alpha.2.tgz
$env:SEMARAIL_API_TOKEN = semarail token create
$env:SEMARAIL_DATABASE_URL = "postgresql://readonly_user:password@localhost:5432/database"
semarail start --project C:\path\to\semantic-project
```

Open `http://127.0.0.1:48763` for the Console. Run `semarail status` from an
environment with the same token to verify the authenticated v1 handshake.

Core binds to loopback only in this alpha. Keep the bearer token and database
DSN in the Core process environment; do not place them in agent prompts or
browser storage.
