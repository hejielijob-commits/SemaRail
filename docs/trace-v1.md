# Codex desktop Trace v1

## Scope

A Session is a Codex chat; a Trace is one main Agent turn. The Console quality
section contains **Agent traces**, separate from **Issues and feedback**. It
reconstructs the main Agent, subagents, tools, and verified Core requests from
persisted events. There is no automatic root-cause diagnosis or repair.

The optional adapter is the separate plugin project
[semarail-codex-trace](https://github.com/hejielijob-commits/semarail-codex-trace).
It uses the seven Codex lifecycle hooks documented at
[Codex Hooks](https://learn.chatgpt.com/docs/hooks). Projects must be explicitly
enabled. Existing chats are not backfilled, and transcripts are never read.

## Enable collection

Install the adapter using a personal or organization plugin marketplace and
review and trust its current Hooks in Codex. Follow the
[plugin Hook trust workflow](https://developers.openai.com/plugins/build/plugins).
Start a new desktop chat after installation.

Configure `~/.semarail/trace-projects.json`:

```json
{
  "projects": [{
    "root": "D:\\projects\\my-project",
    "enabled": true,
    "endpoint": "http://127.0.0.1:48763",
    "authFile": "C:\\Users\\me\\.semarail\\session.json"
  }]
}
```

Use an employee session from `semarail auth login`, or set `tokenEnv` to an
environment variable name containing a managed credential. Never put credential
values in the configuration. Remote origins require HTTPS. The session origin
must match the configured origin.

The writer needs project-scoped `trace:write`. Use the **same SemaRail subject**
for the adapter and the MCP bridge; foreign organization, project, or subject
Core links are rejected. Console readers require project-scoped `console:admin`.
Bootstrap administrator credentials cannot write agent events.

## Data and API

Only allowlisted identifiers, event types, timestamps, model names, statuses,
and explicitly available counters are accepted. Prompts, model input/output,
tool arguments/result bodies, final answers, and transcripts are not Trace
fields. Unknown fields and versions fail closed. Existing successful Core
diagnostics remain metadata-only; existing failed evidence retention rules apply.

Diagnostic schema migrations version Trace, Span, events, and Core associations
independently from audit tables. Event schema version is `1`.

| Route | Permission | Purpose |
| --- | --- | --- |
| `POST /api/v1/traces/events` | `trace:write` | Append up to 100 events for one source session and turn |
| `GET /api/v1/traces?limit=50&cursor=…` | `console:admin` | Newest-first pagination |
| `GET /api/v1/traces/{id}` | `console:admin` | Ordered events, persisted spans, verified Core metadata |
| `GET /api/v1/traces/by-core/{coreTraceId}` | `console:admin` | Resolve the issue-to-Agent-Trace link |

Events are idempotent within a scoped source session and turn. Reusing an event
ID with different contents returns `409`. Terminal state uses timestamp and
event-ID ordering, so arrival order does not change replay. Unavailable Core
diagnostics remain unverified; ambiguous or conflicting claims do not produce
an arbitrary link. Trace events expire 30 days after ingestion and are deleted
by periodic HTTP-server maintenance, including when the server is idle.

Core RPC v3 returns each independently generated Core `traceId` on success and
failure. RPC v1/v2 behavior is preserved. MCP responses carry `_meta.traceId`
outside structured tool content. Core diagnostic phases record actual
authentication, policy, and runtime boundaries, including failed phases.
Instrumentation failures do not change the query outcome.

## Reading a Trace

Agent and tool timestamps are Hook **observation times**. Background Hook
scheduling contributes to these intervals; they are not exact model or tool
execution timers. Reversed or missing intervals show **Not collected**. Core
durations are measured inside Core.

The Console shows parallel intervals, execution status, Core phases, and issue
links. Missing completion events differ from still-running spans. SubagentStop
without an explicit outcome means **Completed · outcome unknown**. Tool ownership
is **Unknown owner** unless an explicit Agent ID establishes it. LLM request
duration and Token usage are **Not collected** when Hooks supply no such events;
neither is estimated.

## Verification

Server tests cover version compatibility, duplicate and out-of-order events,
scope isolation, forged and ambiguous associations, content rejection, physical
retention cleanup, and fail-open instrumentation. The UI fixture in
`apps/semantic-console/web/src/components/fixtures/persisted-trace.json` covers
refresh reconstruction, overlapping subagents, a failed tool and linked Core
issue, and missing completion. Trace UI tests also cover issue-to-Trace
navigation, interrupted turns, and stale detail response protection.

Desktop acceptance must use a new enabled chat with actual SemaRail MCP calls
and a subagent, then confirm their persisted Trace and issue navigation in the
Console. Also run an unenabled project and verify no Trace is created. Unit
fixtures do not substitute for this desktop check.

During the local desktop check on 2026-09-30, successful MCP responses supplied
Core correlation. The desktop emitted independent subagent turn IDs; adapter
0.1.3 uses explicit spawn-to-Agent routing and a local metadata-only outbox.
Collector activity prunes expired seven-day routing metadata; unresolved event
rows are bounded to 4,096. Unrecognized
follow-up child turns are not assigned to an earlier parent turn.

The initial failed MCP check had no PostToolUse completion. Investigation found
that the stdio bridge discarded the Core correlation when it redacted an
incomplete error resource descriptor. The bridge now preserves a validated v3
request-matched correlation independently from redacted error details; an
official MCP SDK regression test confirms the failure still has `isError=true`
and `_meta.traceId`. A subsequent desktop call confirmed that the failed MCP
response carries the Core ID, while this desktop version still omits its
PostToolUse event. The independent adapter includes an optional stdio MCP
observer for this case: it preserves protocol bytes and results, and emits
completion only after an exact opted-in PreToolUse registration matches the
Codex call/session/turn/Agent identifiers. Observation and delivery run in the
background. No tool arguments or result bodies are retained.

The native collaboration tool can return a canonical task path instead of an
Agent ID. Its path-to-Agent enrichment reads only the thread ID, canonical Agent
path and parent thread ID from the observed local Codex thread metadata schema
(`state_5.sqlite`). It matches the recorded spawn path and parent exactly and
does not read rollouts, transcripts, prompts, previews or titles. This schema
dependency is explicit; missing or conflicting metadata leaves the association
unresolved. Only an observed initial SubagentStart turn can bind this relation;
a Stop-only event cannot assign a reused Agent's follow-up turn to an older spawn.

The final 0.1.3 desktop check on 2026-09-30 used enabled chat
`01a0f273-8b09-7dc2-88d7-7820a8d779d0`. Its one main turn persisted as
`atr_57cdec559ebc47198fcfcb86baf693b0`, with 13 events, the main Agent,
subagent `01a0f275-99a7-73b2-b36e-1ee5f3c5defa`, both MCP calls, and two
verified Core diagnostics. The failed child tool links to Core
`trace-be8183d3e8414f1c873e43a94aea00a5` and issue
`fb_16591bb01e0f4e77b3f019b0514a562f` (`authorization`). The by-Core API
resolves that same Agent Trace, and the issue detail exposes the same Core ID.
Main model and Token fields remain uncollected rather than borrowing the child's
model. Codex's hooks/list reports all seven 0.1.3 Hooks enabled and trusted.

The full verification run passed 230 server tests, 133 UI tests, 17 contract
tests, and 29 adapter/proxy tests. The final per-Core issue-link change then
passed all 14 focused Trace API tests and all five Trace UI tests; typecheck,
production build, and distribution packaging also passed.

The rendered Console check is complete. After loading the final production
bundle (`index-hktQTlUe.js`), the same 13 persisted events reconstructed the
Session, main turn, Agent/subagent, tools, and two Core diagnostics. Both a full
page reload with scoped credential re-entry and the Trace refresh action
preserved this reconstruction. LLM duration and Token usage remain explicitly
uncollected; the child completion has an unknown outcome rather than an inferred
success. The failed tool's Core node has exactly one issue button for
`fb_16591bb01e0f4e77b3f019b0514a562f`; the successful tool has none. Clicking the
failed Span's button opens that issue with Core
`trace-be8183d3e8414f1c873e43a94aea00a5`, and the issue's **Open trace** action
returns `atr_57cdec559ebc47198fcfcb86baf693b0`. Issue IDs are taken from each
verified, scoped Core diagnostic, never guessed from the Trace's issue list.

The unenabled projectless desktop chat
`01a0f231-83a2-7a01-abba-8320c8784229` called the actual MCP validation and
model-listing tools. The Trace API returned no Trace for that session. This
confirms opt-in isolation for this local acceptance environment. The final 0.1.3
projectless chat `01a0f273-dd12-7083-a748-454a78729ba3` also completed both real
MCP calls, including the expected failure, and returned zero session Traces.

Any failed request with no completion evidence must remain visibly incomplete.
Do not infer Core association from timing, tool name, or a nearby diagnostic.
