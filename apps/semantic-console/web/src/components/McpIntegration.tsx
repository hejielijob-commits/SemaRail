import { useState } from "react";
import { Check, Copy, Database, Plug, ShieldCheck, WarningCircle } from "@phosphor-icons/react";
import type { McpIntegrationResponse } from "../types";
import { Badge, Button, EmptyState, LoadingRows, SectionHeading } from "./ui";
import "./mcp-integration.css";

type Locale = "en-US" | "zh-CN";

const copy = {
  "en-US": {
    eyebrow: "Agent access", title: "MCP integration", description: "Connect any MCP-capable agent through SemaRail's authenticated, policy-enforced remote boundary. DeepSeek Harness remains an optional plugin.",
    remote: "Authenticated remote MCP", semantic: "Semantic context", governed: "Governed query", ready: "Ready to configure", setup: "Setup required", configured: "Endpoint configured", defaulted: "Using loopback default",
    remoteBody: "One Streamable HTTP endpoint exposes semantic context and governed query tools. Every request is resolved against the current subject and policy.",
    copied: "Copied", config: "MCP client configuration", configBody: "Set SEMARAIL_TOKEN in the MCP client's private environment, then adapt only the client-specific wrapper if needed.", copyConfig: "Copy configuration", copyUrl: "Copy endpoint",
    authTitle: "Bearer authentication is required", authBody: "Use a service-account key for an Agent or automation. Employees can use a session issued after organizational login. Keys and sessions are never included in this response.",
    secretTitle: "Secrets stay outside configuration", secretBody: "The Console returns no datasource DSN, saved password, API key, or employee session. The token placeholder is resolved only by the MCP client.",
    mysqlTitle: "Governed execution is PostgreSQL-only", mysqlBody: "The semantic server is ready, but the SemaRail Core governed query service cannot execute against the active MySQL datasource yet.",
    missingTitle: "Integration information unavailable", retry: "Retry", endpoint: "Endpoint", transport: "Transport", authentication: "Authentication", tools: "Available tools", readiness: "Readiness",
    serviceAccount: "Service-account key", employeeSession: "Employee login session", localTitle: "Trusted local operator compatibility", localSummary: "Show stdio compatibility notes", localBody: "Direct stdio runs with local operator trust. It does not provide per-user identity or isolation and must not be exposed as a shared employee access path.",
  },
  "zh-CN": {
    eyebrow: "Agent 接入", title: "MCP 集成", description: "通过 SemaRail 经过认证并执行权限策略的远程边界，将当前语义项目提供给任何支持 MCP 的 Agent；DeepSeek Harness 仅作为可选插件。",
    remote: "认证远程 MCP", semantic: "语义上下文", governed: "受控查询", ready: "可以配置", setup: "需要配置", configured: "端点已配置", defaulted: "使用本机默认地址",
    remoteBody: "一个 Streamable HTTP 端点同时提供语义上下文和受控查询工具；每个请求都会根据当前主体和最新策略进行鉴权。",
    copied: "已复制", config: "MCP 客户端配置", configBody: "在 MCP 客户端的私有环境中设置 SEMARAIL_TOKEN；如客户端格式不同，只需调整最外层结构。", copyConfig: "复制配置", copyUrl: "复制端点",
    authTitle: "必须使用 Bearer 认证", authBody: "Agent 或自动化使用服务账号密钥；员工可使用完成组织登录后签发的会话。此响应绝不会包含密钥或会话。",
    secretTitle: "凭据始终留在配置之外", secretBody: "控制台不会返回数据源 DSN、已保存密码、API Key 或员工会话；令牌占位符只由 MCP 客户端解析。",
    mysqlTitle: "受控执行目前仅支持 PostgreSQL", mysqlBody: "语义服务可以使用，但 SemaRail Core 受控查询服务暂不能对当前 MySQL 数据源执行查询。",
    missingTitle: "无法加载集成信息", retry: "重试", endpoint: "端点", transport: "传输方式", authentication: "认证", tools: "可用工具", readiness: "就绪状态",
    serviceAccount: "服务账号密钥", employeeSession: "员工登录会话", localTitle: "可信本地运维兼容模式", localSummary: "查看 stdio 兼容说明", localBody: "Direct stdio 以本地运维信任运行，不提供逐用户身份识别或隔离，不能作为员工共享接入方式暴露。",
  },
} as const;

function CopyButton({ value, label, copiedLabel }: { value: string; label: string; copiedLabel: string }) {
  const [copied, setCopied] = useState(false);
  async function handleCopy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }
  return <Button variant="ghost" size="sm" icon={copied ? Check : Copy} onClick={() => void handleCopy()}>{copied ? copiedLabel : label}</Button>;
}


export default function McpIntegration({ integration, loading, error, locale, onRetry }: { integration: McpIntegrationResponse | null; loading: boolean; error: string | null; locale: Locale; onRetry: () => void }) {
  const c = copy[locale];
  if (loading && !integration) return <div className="page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><div className="panel mcp-loading"><LoadingRows count={5} /></div></div>;
  if (!integration) return <div className="page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><div className="panel"><EmptyState icon={WarningCircle} title={c.missingTitle} body={error ?? c.missingTitle} action={<Button onClick={onRetry}>{c.retry}</Button>} /></div></div>;
  const config = JSON.stringify(integration.clientConfig, null, 2);
  const mysql = integration.readiness.datasourceType === "mysql";
  const ready = integration.readiness.status === "ready";
  const endpointState = integration.readiness.endpointConfiguration === "ready" ? c.configured : c.defaulted;
  return <div className="page mcp-page">
    <SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} action={<Badge tone={ready ? "green" : "amber"} dot>{ready ? c.ready : c.setup}</Badge>} />
    <div className="mcp-overview panel"><div><span>{c.endpoint}</span><strong>{integration.endpoint.url}</strong></div><div><span>{c.transport}</span><strong>Streamable HTTP</strong></div><div><span>{c.authentication}</span><strong>Bearer</strong></div></div>
    {mysql ? <div className="mcp-callout warning"><Database size={20} weight="duotone" /><div><strong>{c.mysqlTitle}</strong><p>{c.mysqlBody}</p></div></div> : null}
    <article className="mcp-server-card panel"><div className="mcp-card-heading"><span className={`mcp-card-icon ${ready ? "ready" : "pending"}`}><Plug size={20} /></span><div><div className="mcp-title-line"><h2>{c.remote}</h2><Badge tone={integration.readiness.endpointConfiguration === "ready" ? "green" : "blue"} dot>{endpointState}</Badge></div><p>{c.remoteBody}</p></div></div><dl className="mcp-meta"><div><dt>{c.semantic}</dt><dd>{integration.readiness.semanticContext === "ready" ? c.ready : c.setup}</dd></div><div><dt>{c.governed}</dt><dd>{integration.readiness.governedQuery === "ready" ? c.ready : c.setup}</dd></div><div><dt>{c.tools}</dt><dd>{integration.tools.length}</dd></div></dl><div className="mcp-endpoint"><code>{integration.endpoint.url}</code><CopyButton value={integration.endpoint.url} label={c.copyUrl} copiedLabel={c.copied} /></div></article>
    <div className="mcp-callout"><ShieldCheck size={20} weight="duotone" /><div><strong>{c.authTitle}</strong><p>{c.authBody}</p><p className="mcp-auth-kinds"><span>{c.serviceAccount}</span><span>{c.employeeSession}</span></p></div></div>
    <section className="mcp-config panel"><div className="mcp-config-heading"><div><p className="panel-kicker">JSON</p><h2>{c.config}</h2><p>{c.configBody}</p></div><CopyButton value={config} label={c.copyConfig} copiedLabel={c.copied} /></div><pre><code>{config}</code></pre><div className="mcp-callout"><ShieldCheck size={20} weight="duotone" /><div><strong>{c.secretTitle}</strong><p>{c.secretBody}</p></div></div></section>
    <details className="mcp-local panel"><summary>{c.localSummary}</summary><div><h2>{c.localTitle}</h2><Badge tone="amber">stdio · trusted operator</Badge><p>{c.localBody}</p></div></details>
  </div>;
}
