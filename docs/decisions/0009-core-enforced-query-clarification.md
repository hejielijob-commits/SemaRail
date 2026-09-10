# 0009 — Core-enforced query clarification

## Status

Accepted for the first clarification release.

## Context

An Agent can recognize that a natural-language question is ambiguous, but an
execution boundary must not trust every client to ask the required business
questions. Metric choice, time range, statistical grain, and business definition
may be mandatory or require explicit confirmation. Older clients must not bypass
those requirements by calling `query.run` directly.

## Decision

Published business rules may include a versioned query-confirmation document for
specific semantic models. Each condition has one of the four supported kinds, a
type or allowed-value set, an optional default, and a `requiresConfirmation`
flag. The normal validation, draft, publish, snapshot, and rollback workflow owns
these records.

The authenticated `query.prepare` Core RPC and `semarail_prepare_query` MCP tool
accept a question, candidate semantic SQL, Agent-extracted structured conditions,
and condition names already confirmed by the user. Core resolves the models,
merges their published rules, and returns `ready`, `needs_clarification`, or
`blocked`. Related missing items are returned together with short questions,
allowed choices where applicable, and stable reason codes. A default is applied
only when confirmation is not required and is returned so the Agent can disclose
it in the answer.

A ready preparation is bound server-side to the subject, organization, project,
candidate SQL digest, structured conditions, and rule versions for 30 minutes.
`query.run` independently resolves the current rules and requires a matching ready
preparation whenever they apply. Changed SQL, rules, ownership, project, or expiry
invalidates it. With no applicable rule, existing queries remain compatible.

Waiting for clarification is normal conversation state and is not recorded as a
product failure. User answers affect only this preparation and never mutate access
policy or global business rules.

## Boundary

Core validates structured conditions and the execution flow. The Agent still owns
open-ended ambiguity recognition and extraction of the user's answer. This design
does not claim that Core has verified all semantic SQL against natural-language
intent, and it introduces no Core-hosted model.

## Consequences

- Agents can use ordinary conversation turns for clarification.
- New and old execution clients receive the same fail-closed requirement.
- Preparation records contain no result rows, credentials, or full conversation.
- Published rule history makes each ready decision explainable and replayable.
