# SemaRail access control (alpha)

SemaRail Core now has a local control-plane API for service accounts, API keys,
policy bindings, and audit events. It is the first implementation stage of the
multi-user architecture. The endpoints bind to the same loopback server as the
Semantic Console and require an administrator bearer token.

Set a bootstrap token of at least 32 characters before starting Core:

```powershell
$env:SEMARAIL_API_TOKEN = semarail token create
semarail start --project C:\path\to\semantic-project
```

Use that token only to bootstrap managed service accounts. A created API key is
shown once and its plaintext is not stored.

## Policy example

The following rule lets a service account execute queries against one explicit
physical table. Its permitted rows come from the trusted `regionCodes` attribute
stored on that account.

```json
{
  "schemaVersion": 1,
  "projects": ["sales-project"],
  "tools": ["semantic:read", "query:execute", "query:cancel"],
  "limits": {"maxRows": 200, "timeoutMs": 10000},
  "tables": {
    "public.sales": {
      "effect": "allow",
      "tenantField": "organization_id",
      "rows": [
        {
          "field": "region_code",
          "operator": "in",
          "valueFrom": "subject.attributes.regionCodes"
        }
      ],
      "columns": {
        "allow": ["order_id", "organization_id", "region_code", "amount"],
        "deny": []
      }
    }
  }
}
```

If account A has `{"regionCodes":["CN-JIA"]}` and account B has
`{"regionCodes":["CN-YI"]}`, the same SQL is executed with different mandatory
bound parameters. Updating either account's attributes or the bound policy takes
effect on its next request.

## Administrative endpoints

- `GET/POST /api/v1/access/service-accounts`
- `PUT /api/v1/access/service-accounts/{id}`
- `PUT /api/v1/access/service-accounts/{id}/status`
- `POST /api/v1/access/service-accounts/{id}/keys`
- `POST /api/v1/access/credentials/{id}/rotate`
- `POST /api/v1/access/credentials/{id}/revoke`
- `GET/POST /api/v1/access/policies`
- `PUT /api/v1/access/policies/{id}`
- `POST /api/v1/access/policy-bindings`
- `GET /api/v1/access/audit`

Key rotation atomically creates a replacement and revokes the old credential.
Policy updates increment the policy version. Disabling a subject and revoking a
key are effective immediately.

## Current boundary

This alpha API is intended for a trusted local administrator. A visual policy
editor, remote HTTP MCP, employee/OIDC login, DingTalk identity mapping, and
PostgreSQL RLS are not yet implemented. Do not expose the loopback control API as
an internet-facing service without the planned production transport and session
hardening.
