import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Check, Copy, Key, LockKey, Plus, ShieldCheck, UsersThree, WarningCircle } from "@phosphor-icons/react";
import { api } from "../api/client";
import type { AccessAuditEvent, AccessPolicy, IssuedApiKey, ServiceAccount, UserAccount } from "../types";
import { Badge, Button, EmptyState, Field, InlineNotice, LoadingRows, SectionHeading, Select, TextArea, TextInput } from "./ui";
import "./access-control.css";

type Locale = "en-US" | "zh-CN";
type Tab = "accounts" | "employees" | "policies" | "audit";

const copy = {
  "en-US": {
    eyebrow: "Security", title: "Access control", description: "Manage agent identities, API keys, policy bindings, and authorization audit events.",
    locked: "Administrator authentication required", lockedBody: "Enter the bootstrap administrator token. It remains only in this page's memory and is never saved by the browser.", token: "Administrator token", unlock: "Open access control",
    accounts: "Service accounts", employees: "Employees", policies: "Policies", audit: "Audit", refresh: "Refresh", create: "Create account", accountName: "Account name", regions: "Region codes", regionsHint: "Comma-separated trusted attributes, for example CN-JIA,CN-YI.",
    active: "Active", disabled: "Disabled", noAccounts: "No service accounts", noAccountsBody: "Create a non-human identity before connecting an agent.", credentials: "API keys", noKeys: "No API keys issued", issue: "Issue key", rotate: "Rotate", revoke: "Revoke", enable: "Enable", disable: "Disable",
    bind: "Bind policy", unbind: "Unbind", choosePolicy: "Choose a policy", bound: "Bound policies", shownOnce: "Copy this key now", shownOnceBody: "SemaRail stores only its hash. Closing this notice permanently hides the plaintext.", copied: "Copied", closeKey: "I saved the key",
    policyName: "Policy name", policyDocument: "Policy document (JSON)", createPolicy: "Create policy", savePolicy: "Save policy", version: "Version", invalidJson: "Policy document must be valid JSON.", noPolicies: "No policies", noPoliciesBody: "Create a version-one policy, then bind it to a service account.",
    noEmployees: "No employees have signed in", noEmployeesBody: "An employee appears here after completing OIDC or DingTalk authorization.", identity: "External identity", saveAccess: "Save access attributes", employeeNumber: "Employee number",
    event: "Event", actor: "Actor", decision: "Decision", resource: "Resource", copyKey: "Copy", noAudit: "No audit events", noAuditBody: "Authenticated operations will appear here without query data or credentials.", authFailed: "Access control could not be opened", operationFailed: "Operation failed",
  },
  "zh-CN": {
    eyebrow: "安全", title: "访问控制", description: "管理 Agent 身份、API Key、策略绑定和授权审计事件。",
    locked: "需要管理员认证", lockedBody: "输入启动时使用的管理员 Token。它只保存在当前页面内存中，浏览器不会保存。", token: "管理员 Token", unlock: "进入访问控制",
    accounts: "服务账号", employees: "员工", policies: "权限策略", audit: "审计", refresh: "刷新", create: "创建账号", accountName: "账号名称", regions: "地区代码", regionsHint: "受信任属性，使用英文逗号分隔，例如 CN-JIA,CN-YI。",
    active: "启用", disabled: "停用", noAccounts: "暂无服务账号", noAccountsBody: "为 Agent 创建一个非人类身份后再进行连接。", credentials: "API Key", noKeys: "尚未签发 API Key", issue: "签发密钥", rotate: "轮换", revoke: "撤销", enable: "启用", disable: "停用",
    bind: "绑定策略", unbind: "解绑", choosePolicy: "选择策略", bound: "已绑定策略", shownOnce: "请立即复制该密钥", shownOnceBody: "SemaRail 只保存哈希；关闭提示后将无法再次查看明文。", copied: "已复制", closeKey: "我已保存密钥",
    policyName: "策略名称", policyDocument: "策略文档（JSON）", createPolicy: "创建策略", savePolicy: "保存策略", version: "版本", invalidJson: "策略文档必须是有效 JSON。", noPolicies: "暂无策略", noPoliciesBody: "创建 v1 策略，然后将其绑定到服务账号。",
    noEmployees: "暂无员工登录", noEmployeesBody: "员工完成 OIDC 或钉钉授权后会显示在这里。", identity: "外部身份", saveAccess: "保存访问属性", employeeNumber: "工号",
    event: "事件", actor: "主体", decision: "决策", resource: "资源", copyKey: "复制", noAudit: "暂无审计事件", noAuditBody: "认证后的操作会显示在这里，但不会记录查询数据或凭据。", authFailed: "无法进入访问控制", operationFailed: "操作失败",
  },
} as const;

const policyTemplate = (datasourceId = "") => JSON.stringify({
  schemaVersion: 1,
  ...(datasourceId ? { datasourceId } : {}),
  projects: ["sales-project"],
  tools: ["project:validate", "semantic:read", "query:plan", "query:execute", "query:cancel"],
  limits: { maxRows: 200, timeoutMs: 10000 },
  tables: {
    "public.sales": {
      effect: "allow",
      rows: [{ field: "region_code", operator: "in", valueFrom: "subject.attributes.regionCodes" }],
      columns: { allow: ["order_id", "region_code", "amount"], deny: [] },
    },
  },
}, null, 2);

function formatDate(value: string | null | undefined, locale: Locale) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function BoundPolicies({ ids, policies, unbindLabel, onUnbind }: { ids: string[]; policies: AccessPolicy[]; unbindLabel: string; onUnbind: (id: string) => void }) {
  if (!ids.length) return <span>—</span>;
  return <div className="access-bound-list">{ids.map((id) => {
    const name = policies.find((item) => item.id === id)?.name ?? id;
    return <span key={id}>{name}<button aria-label={`${unbindLabel} ${name}`} onClick={() => onUnbind(id)}>{unbindLabel}</button></span>;
  })}</div>;
}

export default function AccessControl({ locale, onAuthenticated, adminToken = "", activeDatasourceId = "" }: { locale: Locale; onAuthenticated?: () => void; adminToken?: string; activeDatasourceId?: string }) {
  const c = copy[locale];
  const [tokenInput, setTokenInput] = useState("");
  const [token, setToken] = useState(adminToken);
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [users, setUsers] = useState<UserAccount[]>([]);
  const [policies, setPolicies] = useState<AccessPolicy[]>([]);
  const [audit, setAudit] = useState<AccessAuditEvent[]>([]);
  const [tab, setTab] = useState<Tab>("accounts");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [accountName, setAccountName] = useState("");
  const [regions, setRegions] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [selectedUserId, setSelectedUserId] = useState("");
  const [employeeRegions, setEmployeeRegions] = useState("");
  const [employeePolicyId, setEmployeePolicyId] = useState("");
  const [bindingPolicyId, setBindingPolicyId] = useState("");
  const [issued, setIssued] = useState<IssuedApiKey | null>(null);
  const [copied, setCopied] = useState(false);
  const [policyName, setPolicyName] = useState("");
  const [policyId, setPolicyId] = useState("");
  const [policyJson, setPolicyJson] = useState(() => policyTemplate(activeDatasourceId));

  const selected = accounts.find((item) => item.id === selectedId) ?? accounts[0];
  const selectedUser = users.find((item) => item.id === selectedUserId) ?? users[0];
  const selectedPolicy = policies.find((item) => item.id === policyId);
  const availablePolicies = policies.filter((item) => !selected?.policyIds.includes(item.id));
  const availableEmployeePolicies = policies.filter((item) => !selectedUser?.policyIds.includes(item.id));
  const activeCredentials = useMemo(() => selected?.credentials.filter((item) => !item.revokedAt) ?? [], [selected]);

  useEffect(() => {
    const values = selectedUser?.attributes.regionCodes;
    setEmployeeRegions(Array.isArray(values) ? values.filter((item): item is string => typeof item === "string").join(",") : "");
  }, [selectedUser?.id, selectedUser?.attributes.regionCodes]);

  useEffect(() => {
    if (adminToken) void loadAll(adminToken);
  }, [adminToken]);

  useEffect(() => {
    if (!policyId) setPolicyJson(policyTemplate(activeDatasourceId));
  }, [activeDatasourceId, policyId]);

  async function loadAll(adminToken = token) {
    if (!adminToken) return;
    setBusy(true); setError("");
    try {
      const [accountResult, userResult, policyResult, auditResult] = await Promise.all([
        api.getServiceAccounts(adminToken), api.getUsers(adminToken), api.getAccessPolicies(adminToken), api.getAccessAudit(adminToken),
      ]);
      setAccounts(accountResult.items); setUsers(userResult.items); setPolicies(policyResult.items); setAudit(auditResult.items);
      setSelectedId((current) => accountResult.items.some((item) => item.id === current) ? current : accountResult.items[0]?.id ?? "");
      setSelectedUserId((current) => userResult.items.some((item) => item.id === current) ? current : userResult.items[0]?.id ?? "");
      api.setBearerToken(adminToken); setToken(adminToken); setTokenInput(""); onAuthenticated?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : c.authFailed);
    } finally { setBusy(false); }
  }

  async function act(operation: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await operation(); await loadAll(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : c.operationFailed); setBusy(false); }
  }

  async function createAccount(event: FormEvent) {
    event.preventDefault();
    const regionCodes = regions.split(",").map((item) => item.trim()).filter(Boolean);
    await act(async () => {
      const account = await api.createServiceAccount(token, { name: accountName, attributes: { regionCodes } });
      setSelectedId(account.id); setAccountName(""); setRegions("");
    });
  }

  function parsePolicy(): Record<string, unknown> | null {
    try {
      const value: unknown = JSON.parse(policyJson);
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
      return value as Record<string, unknown>;
    } catch { setError(c.invalidJson); return null; }
  }

  async function savePolicy(event: FormEvent) {
    event.preventDefault();
    const document = parsePolicy();
    if (!document) return;
    await act(async () => {
      const saved = selectedPolicy
        ? await api.updateAccessPolicy(token, selectedPolicy.id, document)
        : await api.createAccessPolicy(token, { name: policyName, document });
      setPolicyId(saved.id); setPolicyName("");
    });
  }

  function editPolicy(id: string) {
    setPolicyId(id);
    const policy = policies.find((item) => item.id === id);
    if (policy) setPolicyJson(JSON.stringify(policy.document, null, 2));
  }

  async function showIssued(operation: () => Promise<IssuedApiKey>) {
    setBusy(true); setError("");
    try { const result = await operation(); setIssued(result); setCopied(false); await loadAll(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : c.operationFailed); setBusy(false); }
  }

  if (!token) return <div className="page access-page"><SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} /><section className="panel access-lock"><span><LockKey size={28} weight="duotone" /></span><div><h2>{c.locked}</h2><p>{c.lockedBody}</p><form onSubmit={(event) => { event.preventDefault(); void loadAll(tokenInput.trim()); }}><Field label={c.token} htmlFor="access-admin-token"><TextInput id="access-admin-token" type="password" autoComplete="off" value={tokenInput} onChange={(event) => setTokenInput(event.target.value)} /></Field><Button type="submit" variant="primary" icon={ShieldCheck} loading={busy} disabled={tokenInput.trim().length < 32}>{c.unlock}</Button></form>{error ? <InlineNotice tone="error" title={c.authFailed}>{error}</InlineNotice> : null}</div></section></div>;

  return <div className="page access-page">
    <SectionHeading eyebrow={c.eyebrow} title={c.title} description={c.description} action={<Button size="sm" onClick={() => void loadAll()} loading={busy}>{c.refresh}</Button>} />
    {error ? <InlineNotice tone="error" title={c.operationFailed} onDismiss={() => setError("")}>{error}</InlineNotice> : null}
    {issued ? <section className="access-key-once panel" role="status"><Key size={22} weight="duotone" /><div><h2>{c.shownOnce}</h2><p>{c.shownOnceBody}</p><code>{issued.apiKey}</code><div><Button size="sm" icon={copied ? Check : Copy} onClick={() => void navigator.clipboard.writeText(issued.apiKey).then(() => setCopied(true))}>{copied ? c.copied : c.copyKey}</Button><Button size="sm" variant="ghost" onClick={() => setIssued(null)}>{c.closeKey}</Button></div></div></section> : null}
    <div className="access-tabs" role="tablist">{(["accounts", "employees", "policies", "audit"] as Tab[]).map((item) => <button key={item} role="tab" aria-selected={tab === item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{c[item]}</button>)}</div>
    {busy && !accounts.length && !policies.length ? <div className="panel"><LoadingRows /></div> : null}
    {tab === "accounts" ? <div className="access-account-layout">
      <section className="panel access-list"><form className="access-create" onSubmit={(event) => void createAccount(event)}><Field label={c.accountName}><TextInput required value={accountName} onChange={(event) => setAccountName(event.target.value)} /></Field><Field label={c.regions} hint={c.regionsHint}><TextInput value={regions} onChange={(event) => setRegions(event.target.value)} /></Field><Button type="submit" variant="primary" icon={Plus} loading={busy} disabled={!accountName.trim()}>{c.create}</Button></form><div className="access-list-items">{accounts.map((account) => <button key={account.id} className={selected?.id === account.id ? "active" : ""} onClick={() => setSelectedId(account.id)}><span><UsersThree size={17} /><strong>{account.name}</strong></span><Badge tone={account.status === "active" ? "green" : "amber"} dot>{account.status === "active" ? c.active : c.disabled}</Badge><small>{account.attributes.regionCodes instanceof Array ? account.attributes.regionCodes.join(" · ") : "—"}</small></button>)}</div>{!accounts.length ? <EmptyState icon={UsersThree} title={c.noAccounts} body={c.noAccountsBody} /> : null}</section>
      {selected ? <section className="panel access-detail"><header><div><p className="panel-kicker">{selected.type}</p><h2>{selected.name}</h2><code>{selected.id}</code></div><Button size="sm" variant={selected.status === "active" ? "danger" : "secondary"} onClick={() => void act(() => api.setServiceAccountStatus(token, selected.id, selected.status === "active" ? "disabled" : "active"))}>{selected.status === "active" ? c.disable : c.enable}</Button></header><div className="access-bind"><div><strong>{c.bound}</strong><BoundPolicies ids={selected.policyIds} policies={policies} unbindLabel={c.unbind} onUnbind={(id) => void act(() => api.unbindAccessPolicy(token, selected.id, id))} /></div><Select value={bindingPolicyId} onChange={(event) => setBindingPolicyId(event.target.value)}><option value="">{c.choosePolicy}</option>{availablePolicies.map((policy) => <option key={policy.id} value={policy.id}>{policy.name}</option>)}</Select><Button size="sm" disabled={!bindingPolicyId} onClick={() => void act(async () => { await api.bindAccessPolicy(token, selected.id, bindingPolicyId); setBindingPolicyId(""); })}>{c.bind}</Button></div><div className="access-credential-heading"><h3>{c.credentials}</h3><Button size="sm" icon={Key} onClick={() => void showIssued(() => api.issueServiceAccountKey(token, selected.id, "console"))}>{c.issue}</Button></div>{activeCredentials.length ? <div className="access-credentials">{selected.credentials.map((credential) => <article key={credential.id} className={credential.revokedAt ? "revoked" : ""}><div><strong>{credential.label}</strong><code>{credential.id}</code><small>{formatDate(credential.lastUsedAt ?? credential.createdAt, locale)}</small></div>{!credential.revokedAt ? <span><Button size="sm" variant="ghost" onClick={() => void showIssued(() => api.rotateCredential(token, credential.id))}>{c.rotate}</Button><Button size="sm" variant="danger" onClick={() => void act(() => api.revokeCredential(token, credential.id))}>{c.revoke}</Button></span> : <Badge tone="red">{c.revoke}</Badge>}</article>)}</div> : <p className="access-muted">{c.noKeys}</p>}</section> : null}
    </div> : null}
    {tab === "employees" ? <div className="access-account-layout">
      <section className="panel access-list"><div className="access-list-items access-list-items-flush">{users.map((user) => { const identity = user.identities[0]; return <button key={user.id} className={selectedUser?.id === user.id ? "active" : ""} onClick={() => setSelectedUserId(user.id)}><span><UsersThree size={17} /><strong>{user.name}</strong></span><Badge tone={user.status === "active" ? "green" : "amber"} dot>{user.status === "active" ? c.active : c.disabled}</Badge><small>{identity ? `${identity.provider} · ${String(identity.profile.employeeNumber ?? identity.externalSubject)}` : "—"}</small></button>; })}</div>{!users.length ? <EmptyState icon={UsersThree} title={c.noEmployees} body={c.noEmployeesBody} /> : null}</section>
      {selectedUser ? <section className="panel access-detail"><header><div><p className="panel-kicker">{selectedUser.type}</p><h2>{selectedUser.name}</h2><code>{selectedUser.id}</code></div><Button size="sm" variant={selectedUser.status === "active" ? "danger" : "secondary"} onClick={() => void act(() => api.setUserStatus(token, selectedUser.id, selectedUser.status === "active" ? "disabled" : "active"))}>{selectedUser.status === "active" ? c.disable : c.enable}</Button></header><div className="access-employee-identity"><strong>{c.identity}</strong>{selectedUser.identities.map((identity) => <span key={`${identity.provider}:${identity.externalSubject}`}><Badge tone="blue">{identity.provider}</Badge><code>{String(identity.profile.employeeNumber ?? identity.externalSubject)}</code><small>{formatDate(identity.lastLoginAt, locale)}</small></span>)}</div><div className="access-employee-policy"><Field label={c.regions} hint={c.regionsHint} htmlFor="employee-region-codes"><TextInput id="employee-region-codes" value={employeeRegions} onChange={(event) => setEmployeeRegions(event.target.value)} /></Field><Button size="sm" variant="primary" onClick={() => void act(() => api.updateUser(token, selectedUser.id, { attributes: { ...selectedUser.attributes, regionCodes: employeeRegions.split(",").map((item) => item.trim()).filter(Boolean) } }))}>{c.saveAccess}</Button></div><div className="access-bind"><div><strong>{c.bound}</strong><BoundPolicies ids={selectedUser.policyIds} policies={policies} unbindLabel={c.unbind} onUnbind={(id) => void act(() => api.unbindAccessPolicy(token, selectedUser.id, id))} /></div><Select value={employeePolicyId} onChange={(event) => setEmployeePolicyId(event.target.value)}><option value="">{c.choosePolicy}</option>{availableEmployeePolicies.map((policy) => <option key={policy.id} value={policy.id}>{policy.name}</option>)}</Select><Button size="sm" disabled={!employeePolicyId} onClick={() => void act(async () => { await api.bindAccessPolicy(token, selectedUser.id, employeePolicyId); setEmployeePolicyId(""); })}>{c.bind}</Button></div></section> : null}
    </div> : null}
    {tab === "policies" ? <div className="access-policy-layout"><section className="panel access-policy-list"><Button size="sm" icon={Plus} onClick={() => { setPolicyId(""); setPolicyJson(policyTemplate(activeDatasourceId)); }}>{c.createPolicy}</Button>{policies.map((policy) => <button key={policy.id} className={policyId === policy.id ? "active" : ""} onClick={() => editPolicy(policy.id)}><strong>{policy.name}</strong><span>{c.version} {policy.version}</span></button>)}{!policies.length ? <EmptyState icon={ShieldCheck} title={c.noPolicies} body={c.noPoliciesBody} /> : null}</section><form className="panel access-policy-editor" onSubmit={(event) => void savePolicy(event)}>{!selectedPolicy ? <Field label={c.policyName} htmlFor="access-policy-name"><TextInput id="access-policy-name" required value={policyName} onChange={(event) => setPolicyName(event.target.value)} /></Field> : <div className="access-policy-title"><h2>{selectedPolicy.name}</h2><Badge tone="blue">v{selectedPolicy.version}</Badge></div>}<Field label={c.policyDocument} htmlFor="access-policy-document"><TextArea id="access-policy-document" spellCheck={false} value={policyJson} onChange={(event) => setPolicyJson(event.target.value)} /></Field><Button type="submit" variant="primary" loading={busy} disabled={!selectedPolicy && !policyName.trim()}>{selectedPolicy ? c.savePolicy : c.createPolicy}</Button></form></div> : null}
    {tab === "audit" ? <section className="panel access-audit">{audit.length ? <div className="access-audit-table"><div className="head"><span>{c.event}</span><span>{c.actor}</span><span>{c.decision}</span><span>{c.resource}</span></div>{audit.map((event) => <div key={event.id}><span><strong>{event.action}</strong><small>{formatDate(event.occurredAt, locale)}</small></span><code>{event.subjectId ?? "—"}</code><Badge tone={event.decision === "allowed" ? "green" : event.decision === "denied" ? "red" : "amber"}>{event.decision}</Badge><span>{event.resource ?? "—"}</span></div>)}</div> : <EmptyState icon={WarningCircle} title={c.noAudit} body={c.noAuditBody} />}</section> : null}
  </div>;
}
