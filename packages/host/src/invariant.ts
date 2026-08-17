/**
 * The default Host plugin is intentionally fail-closed: a real Wren gateway
 * must be supplied through `createDataQueryTool` before query execution.
 */
export const invariant = 'wren-data-agent-host requires an injected QueryGateway for real queries'
