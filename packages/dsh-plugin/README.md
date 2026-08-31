# @hejielijob/dsh-semarail-plugin

Thin DeepSeek Harness Host/Client integration for an independently running
SemaRail Core. It renders Chart, Table, and SQL results but does not embed
Python, the Semantic Console, database credentials, or Core process management.

```powershell
$env:SEMARAIL_HARNESS_TOKEN = "<scoped sr_live_... service-account key>"
dsh plugin --profile web add .\dist\hejielijob-dsh-semarail-plugin-0.1.0-alpha.3.tgz
```

The default Core endpoint is `http://127.0.0.1:48763`. Optional Host settings:

```yaml
- id: semarail-harness-host
  config:
    semarailEndpoint: http://127.0.0.1:48763
    authTokenEnv: SEMARAIL_HARNESS_TOKEN
    timeoutMs: 30000
```

Do not enable this package together with the legacy
`@hejielijob/dsh-wren-data-agent` all-in-one package.

Create the key in SemaRail Access Control after binding a least-privilege
project/table/row policy. Never give the Core bootstrap `SEMARAIL_API_TOKEN` to
Harness or another Agent.
