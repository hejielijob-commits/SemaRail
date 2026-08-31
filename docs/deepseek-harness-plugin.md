# DeepSeek Harness plugin migration

SemaRail Core and the DeepSeek Harness integration are now separate installable artifacts:

- `@hejielijob/semarail-core` owns the semantic project, Console, Python runtime, MCP servers, credentials, and query policy.
- `@hejielijob/dsh-semarail-plugin` is a thin Harness Host/Client adapter. It calls Core over authenticated loopback HTTP and renders Chart, Table, and SQL results.
- `@hejielijob/dsh-wren-data-agent` is the legacy all-in-one compatibility package.

## Migrate from the all-in-one package

Build the split artifacts while npm publication is pending:

```powershell
pnpm install
pnpm package:split
npm install --global .\dist\hejielijob-semarail-core-0.1.0-alpha.3.tgz
```

Generate a bootstrap token for Core only and keep it out of every Agent process:

```powershell
$env:SEMARAIL_API_TOKEN = semarail token create
$env:SEMARAIL_DATABASE_URL = "postgresql://readonly_user:password@localhost:5432/database"
semarail start --project C:\path\to\semantic-project
```

Open Access Control, create a dedicated Harness service account, bind a least-privilege policy for the current project and its permitted tables/rows, then issue a one-time API key. In the environment that launches DeepSeek Harness, store that `sr_live_...` key under the dedicated variable, remove the legacy package, and add the thin plugin:

```powershell
$env:SEMARAIL_HARNESS_TOKEN = "<scoped sr_live_... key>"
dsh plugin --profile web remove @hejielijob/dsh-wren-data-agent
dsh plugin --profile web add .\dist\hejielijob-dsh-semarail-plugin-0.1.0-alpha.3.tgz
```

Do not enable the legacy and thin packages together: both register the same conversation tools. The thin plugin defaults to `http://127.0.0.1:48763`; configure `semarailEndpoint` only when Core uses a different local port or a trusted HTTPS endpoint.

The adapter defaults to `SEMARAIL_HARNESS_TOKEN`; set `authTokenEnv` only when a deployment uses another dedicated secret variable. The adapter sends questions, semantic SQL, chart intent, and cancellation IDs. It cannot choose Core's project, credential source, row/byte limits, or execution timeout. The bearer token is read from the named environment variable and is never placed in Harness configuration or browser state. Never reuse the Core bootstrap token for an Agent.
