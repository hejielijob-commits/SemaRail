# SemaRail access control (alpha)

SemaRail Core has a local control-plane API for service accounts, externally
authenticated employees, API keys, short-lived sessions, policy bindings, and
audit events. Both employee sessions and service-account keys resolve to the
same `Subject` and are evaluated by the same current policy on every request.

Set a bootstrap token of at least 32 characters before starting Core:

```powershell
$env:SEMARAIL_API_TOKEN = semarail token create
semarail start --project C:\path\to\semantic-project
```

Use that token only to bootstrap managed service accounts. A created API key is
shown once and its plaintext is not stored.

Open **Access control** in the Semantic Console to manage employees, service
accounts, trusted subject attributes, API keys, policy documents, bindings, and
audit events. The administrator token is held only in the current tab's JavaScript
memory: it is sent in the `Authorization` header, is not written to browser storage,
and is cleared when the page is reloaded or the tab is closed. The unlock dialog
resolves `console:admin` and `access:admin` independently: the same credential is
used only for the capabilities it actually has. Most Console REST routes require
`console:admin`, while subject, credential, policy, and audit routes require
`access:admin`.

## Policy example

The following rule lets an employee or service account execute queries against
one explicit physical table. Its permitted rows come from the trusted
`regionCodes` attribute stored on that Subject by an administrator.

```json
{
  "schemaVersion": 1,
  "datasourceId": "server-issued-datasource-id",
  "projects": ["sales-project"],
  "tools": ["project:validate", "semantic:read", "query:plan", "query:execute", "query:cancel"],
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

`datasourceId` is the opaque ID returned by the server's datasource API, not a
display name or database URL. It binds every table in this document unless an
individual table supplies its own `datasourceId`. Existing schema-version-1
policies without either binding remain readable for migration, but non-bootstrap
data-facing requests fail closed until they are updated with an ID for the active datasource.
When two datasources expose the same `schema.table`, only rules bound to the
currently active datasource participate in authorization or policy compilation.

If employee A has `{"regionCodes":["CN-JIA"]}` and employee B has
`{"regionCodes":["CN-YI"]}`, the same SQL is executed with different mandatory
bound parameters. Updating either employee's attributes or the bound policy takes
effect on its next request.

## Employee sign-in

Identity providers are server-side configuration. Keep `clientSecret` in a
secret manager or process environment; never place it in a project file,
browser storage, agent prompt, or MCP configuration. The DingTalk configuration
maps exactly one allowlisted DingTalk corporation to one SemaRail organization:

```powershell
$env:SEMARAIL_IDENTITY_PROVIDERS = @'
{
  "dingtalk": {
    "type": "dingtalk",
    "label": "Company DingTalk",
    "clientId": "<app client id>",
    "clientSecret": "<secret from a secret manager>",
    "redirectUri": "http://127.0.0.1:48763/api/v1/auth/callback/dingtalk",
    "organizationId": "default",
    "allowedOrganizationExternalIds": ["<DingTalk corpId>"]
  }
}
'@
semarail start --project C:\path\to\semantic-project
```

The redirect URI must exactly match the callback registered in DingTalk. The
default delegated scopes are `openid`, `corpid`, and `Contact.User.Read`.
SemaRail requires `unionId` as the stable external subject and verifies the
returned `corpId` before provisioning the user. The login profile API does not
provide a guaranteed employee number. Retrieving DingTalk `job_number` requires
separate enterprise-wide directory permissions, and the value can be missing or
duplicated, so it is display metadata only and is never an identity or policy key.

A generic OIDC provider must either use a tenant-specific issuer
(`singleTenantIssuer: true`) or configure one `organizationClaim` and exactly
one `allowedOrganizationExternalIds` value. Reusing a provider display id with
different endpoints/client configuration creates a different immutable provider
instance, preventing an equal `sub` from taking over an existing Subject.

```json
{
  "company-oidc": {
    "type": "oidc",
    "label": "Company SSO",
    "clientId": "<client id>",
    "clientSecret": "<secret>",
    "authorizationEndpoint": "https://identity.example.com/oauth2/authorize",
    "tokenEndpoint": "https://identity.example.com/oauth2/token",
    "userinfoEndpoint": "https://identity.example.com/oauth2/userinfo",
    "redirectUri": "https://semarail.example.com/api/v1/auth/callback/company-oidc",
    "organizationId": "default",
    "organizationClaim": "tenant_id",
    "allowedOrganizationExternalIds": ["company-tenant"]
  }
}
```

For a provider whose endpoints are already dedicated to one tenant, omit the
last two fields and set `"singleTenantIssuer": true`. OIDC scopes must include
`openid`; only selected profile claims are retained, and provider groups/roles
are never copied into trusted SemaRail authorization attributes.

Employees sign in from an MCP client host with:

```powershell
semarail auth login --provider dingtalk --endpoint http://127.0.0.1:48763
semarail auth status
semarail auth logout
```

The CLI opens the browser authorization page and polls with a high-entropy one-time
device code. After the provider callback, the browser displays a separate eight-character
confirmation code that must be entered into the CLI before it can receive a session.
Forwarding the complete authorization URL and device code therefore cannot transfer the
browser user's session without that user's explicit post-callback confirmation. The
confirmation code is stored only as a bounded-attempt digest and consumed with the login.
The CLI stores only the resulting short-lived SemaRail session. Provider access/refresh
tokens are neither returned nor persisted. On Windows the session
file receives a user-only ACL; on POSIX it is mode `0600`. Override its location
with `--session-file` or `SEMARAIL_AUTH_FILE`.

First login creates an active user with no policy and no trusted authorization
attributes. It therefore fails closed for data access until an administrator
sets attributes such as `regionCodes` and binds a policy. Disabling the user or
changing attributes/policies applies to the next request without issuing a new
session.

## Administrative endpoints

- `GET/POST /api/v1/access/service-accounts`
- `GET /api/v1/access/users`
- `PUT /api/v1/access/users/{id}`
- `PUT /api/v1/access/users/{id}/status`
- `PUT /api/v1/access/service-accounts/{id}`
- `PUT /api/v1/access/service-accounts/{id}/status`
- `POST /api/v1/access/service-accounts/{id}/keys`
- `POST /api/v1/access/credentials/{id}/rotate`
- `POST /api/v1/access/credentials/{id}/revoke`
- `GET/POST /api/v1/access/policies`
- `PUT /api/v1/access/policies/{id}`
- `POST /api/v1/access/policy-bindings`
- `DELETE /api/v1/access/policy-bindings/{subjectId}/{policyId}`
- `GET /api/v1/access/audit`

The public identity/device routes are `GET /api/v1/auth/providers`, `POST
/api/v1/auth/device/start`, `GET /api/v1/auth/callback/{provider}`, and `POST
/api/v1/auth/device/token`. `GET /api/v1/auth/me` and `POST
/api/v1/auth/logout` require the employee session bearer token.

Key rotation atomically creates a replacement and revokes the old credential.
Policy updates increment the policy version. Binding or unbinding a policy is
effective on the next request. Disabling an employee permanently revokes every
existing employee session, so re-enabling the account requires a new login;
revoking a key is also effective immediately.

The Console resolves `console:admin` and `access:admin` independently for the
current project. A Console administrator can edit the semantic project without
receiving identity/key-management permission, while an Access administrator can
manage subjects and policies without automatically receiving project editing
permission.

## Current boundary

The alpha distribution includes an authenticated, stateless Streamable HTTP MCP
endpoint at `/mcp`, started with `semarail mcp serve`. It verifies the same API
keys and routes every tool through the same current Runtime/PolicyEngine as Core.

The management API and the rest of the current Console remain intended for a
trusted administrator on the Core host. Employee/OIDC/DingTalk login and unified
policy enforcement are implemented; database-native PostgreSQL RLS and a
production multi-tenant control-plane store remain follow-up defense-in-depth
work. Non-loopback MCP requires an explicit allowed Host and TLS termination;
do not expose the loopback Console API as an internet-facing service.
