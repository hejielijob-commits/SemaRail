import { useState } from "react";
import { Check, Copy, Database, Plug, ShieldCheck, TerminalWindow, WarningCircle } from "@phosphor-icons/react";
import type { McpIntegrationResponse, McpServerProfile } from "../types";
import { Badge, Button, EmptyState, LoadingRows, SectionHeading } from "./ui";
import "./mcp-integration.css";

type Locale = "en-US" | "zh-CN";

const copy = {
  "en-US": {
    eyebrow: "Agent access", title: "MCP integration", description: "Expose this semantic project to any MCP-capable agent. DeepSeek Harness remains an optional plugin.",
    stdio: "stdio · client managed", semantic: "Semantic context", semanticBody: "Four stable SemaRail tools for project validation, model discovery, context and planning. Database execution is disabled.",
    governed: "Governed query", governedBody: "Safely execute PostgreSQL queries through SemaRail Core query limits, policy checks, and cancellation.", ready: "Ready to configure", setup: "Setup required",
    command: "Launch command", copyCommand: "Copy command", copied: "Copied", config: "MCP client configuration", configBody: "Paste this JSON into an MCP client and adapt only the client-specific wrapper if needed.", copyConfig: "Copy configuration",
    secretTitle: "Credentials stay explicit", secretBody: "The Console never copies saved datasource passwords into this configuration. Replace <POSTGRESQL_DSN> in the agent's private environment.",
    mysqlTitle: "Governed execution is PostgreSQL-only", mysqlBody: "The semantic server is ready, but the SemaRail Core governed query service cannot execute against the active MySQL datasource yet.",
    missingTitle: "Integration information unavailable", retry: "Retry", project: "Project", transport: "Transport", tools: "Agent receives",
    semanticTools: "validate · models · context · plan", governedTools: "semarail_governed_query",
  },
  "zh-CN": {
    eyebrow: "Agent 接入", title: "MCP 集成", description: "将当前语义项目提供给任何支持 MCP 的 Agent；DeepSeek Harness 仅作为可选插件。",
    stdio: "stdio · 由客户端管理", semantic: "语义上下文", semanticBody: "通过四个稳定的 SemaRail 工具提供项目校验、模型发现、上下文和规划；不连接数据库。",
    governed: "受控查询", governedBody: "通过 SemaRail Core 的查询限制、策略校验和取消机制，安全执行 PostgreSQL 查询。", ready: "可以配置", setup: "需要配置",
    command: "启动命令", copyCommand: "复制命令", copied: "已复制", config: "MCP 客户端配置", configBody: "将此 JSON 粘贴到 MCP 客户端；如客户端格式不同，只需调整最外层结构。", copyConfig: "复制配置",
    secretTitle: "凭据保持显式隔离", secretBody: "控制台不会把已保存的数据源密码复制进配置。请在 Agent 的私有环境中替换 <POSTGRESQL_DSN>。",
    mysqlTitle: "受控执行目前仅支持 PostgreSQL", mysqlBody: "语义服务可以使用，但 SemaRail Core 受控查询服务暂不能对当前 MySQL 数据源执行查询。",
    missingTitle: "无法加载集成信息", retry: "重试", project: "项目", transport: "传输方式", tools: "Agent 获得",
    semanticTools: "项目校验 · 模型 · 上下文 · 规划", governedTools: "semarail_governed_query",
  },
} as const;

function shellCommand(profile: McpServerProfile) {
  const quote = (value: string) => /[\s"]/u.test(value) ? `"${value.replaceAll('"', '\\"')}"` : value;
  return [profile.command, ...profile.args].map(quote).join(" ");
}

function CopyButton({ value, label, copiedLabel }: { value: string; label: string; copiedLabel: string }) {
  const [copied, setCopied] = useState(false);
  async function handleCopy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }
  return <Button variant="ghost" size="sm" icon={copied ? Check : Copy} onClick={() => void handleCopy()}>{copied ? copiedLabel : label}</Button>;
}

function ServerCard({ title, body, profile, tools, locale }: { title: string; body: string; profile: McpServerProfile; tools: string; locale: Locale }) {
  const c = copy[locale];
  const ready = profile.status === "ready";
  return <article className="mcp-server-card panel">
    <div className="mcp-card-heading"><span className={`mcp-card-icon ${ready ? "ready" : "pending"}`}>{title === c.semantic ? <Plug size={20} /> : <ShieldCheck size={20} />}</span><div><div className="mcp-title-line"><h2>{title}</h2><Badge tone={ready ? "green" : "amber"} dot>{ready ? c.ready : c.setup}</Badge></div><p>{body}</p></div></div>
    <dl className="mcp-meta"><div><dt>{c.transport}</dt><dd>stdio</dd></div><div><dt>{c.tools}</dt><dd>{tools}</dd></div></dl>
    <div className="mcp-command"><div className="mcp-command-label"><span><TerminalWindow size={15} />{c.command}</span><CopyButton value={shellCommand(profile)} label={c.copyCommand} copiedLabel={c.copied} /></div><code>{shellCommand(profile)}</code></div>
  </article>;
}

export default function McpIntegration({ integration, loading, error, locale, onRetry }: { integration: McpIntegrationResponse | null; loading: boolean; error: string | null; locale: Locale; onRetry: () => void }) {
  const c = copy[locale];
  if (loading && !integration) return <div className="page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><div className="panel mcp-loading"><LoadingRows count={5} /></div></div>;
  if (!integration) return <div className="page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><div className="panel"><EmptyState icon={WarningCircle} title={c.missingTitle} body={error ?? c.missingTitle} action={<Button onClick={onRetry}>{c.retry}</Button>} /></div></div>;
  const config = JSON.stringify(integration.clientConfig, null, 2);
  const mysql = integration.governedQuery.datasourceType === "mysql";
  return <div className="page mcp-page">
    <SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} action={<Badge tone="blue" dot>{c.stdio}</Badge>} />
    <div className="mcp-overview panel"><div><span>{c.project}</span><strong>{integration.projectPath}</strong></div><div><span>{c.transport}</span><strong>stdio</strong></div><div><span>Schema</span><strong>v{integration.schemaVersion}</strong></div></div>
    {mysql ? <div className="mcp-callout warning"><Database size={20} weight="duotone" /><div><strong>{c.mysqlTitle}</strong><p>{c.mysqlBody}</p></div></div> : null}
    <div className="mcp-server-grid"><ServerCard title={c.semantic} body={c.semanticBody} profile={integration.semantic} tools={c.semanticTools} locale={locale} /><ServerCard title={c.governed} body={c.governedBody} profile={integration.governedQuery} tools={c.governedTools} locale={locale} /></div>
    <section className="mcp-config panel"><div className="mcp-config-heading"><div><p className="panel-kicker">JSON</p><h2>{c.config}</h2><p>{c.configBody}</p></div><CopyButton value={config} label={c.copyConfig} copiedLabel={c.copied} /></div><pre><code>{config}</code></pre><div className="mcp-callout"><ShieldCheck size={20} weight="duotone" /><div><strong>{c.secretTitle}</strong><p>{c.secretBody}</p></div></div></section>
  </div>;
}
