# Hermes Linear Channel Plugin — Design Spec

- **Date:** 2026-11-21
- **Status:** Draft for review
- **Tracking:** Linear project *hermes-linear* (formerly Valkyrie) — probe RET-43, spec RET-44, plan RET-45; implementation tasks per the plan (RET-46…RET-67); milestone exit RET-60
- **Repo:** `masha256/hermes-linear`

## 1. Summary

A third-party Hermes **channel plugin** (`kind: platform`) that connects a Hermes profile to Linear's agent API. A Linear ticket **delegated** to the agent becomes **one Hermes session for the whole ticket**: every agent thread, plain human comments, and ticket/project edits feed that one conversation. An **@mention** on a ticket that is not delegated is a one-off, thread-scoped Q&A.

The plugin is installed into a user's own Hermes and connected to the user's own Linear workspace. No Hermes core changes; only the public plugin API is used.

## 2. Goals and non-goals

### Goals

1. One Hermes session per delegated ticket, spanning every thread on that ticket.
2. Native Linear agent UX: sessions, activities (thought / action / response / error / elicitation), stop, plan.
3. Multiple Hermes profiles, each backed by its own Linear OAuth app, behind one listener.
4. First-class plugin: `hermes plugins install`, `config_schema` settings (Desktop form), `hermes linear …` CLI, `hermes plugins validate` clean, eligible for the Hermes plugin catalog.
5. Safe by default: allowlist, default deny, signed webhooks with a replay window.

### Non-goals (milestone 1)

- Polling. Inbound is webhooks only; users expose the listener (Tailscale Funnel assumed).
- Authorization-code OAuth. Client-credentials tokens are sufficient (verified, §3).
- A hosted relay, shared multi-tenant app, or access to anyone else's Hermes.
- Linear documents, initiatives, customers, or project-level agent sessions.
- A durable inbound journal (see §7.1 accepted risk).

## 3. Background: verified Linear behaviour (probe RET-43)

Probed on the test app *Hermes Test* in the Retrograde Capital workspace, 2026-11-21. Recorded payloads are the test fixtures (§11).

| Behaviour | Finding |
|---|---|
| Client-credentials token | `grant_type=client_credentials`, scopes `read,write,app:mentionable,app:assignable`; 30-day token, no refresh token; `viewer` is the app user. Mentions, delegation, webhooks and writes all work. |
| Webhook delivery | One webhook URL per OAuth app; all ticked categories post to it. `Linear-Signature` = hex HMAC-SHA256 of the raw body. Headers include `Linear-Delivery`, `Linear-Event`, `Linear-Timestamp`. |
| App identity in payloads | `AgentSessionEvent`, `AppUserNotification`, `OAuthApp` carry `oauthClientId`. **Data-change events (Issue, Comment, …) do not.** Routing must be by URL path. |
| @mention | `AgentSessionEvent created`. `agentSession.comment` is the mention comment; `previousComments` is that thread; `promptContext` includes issue, project, thread and `<user-request>`. |
| Reply in an agent thread | `AgentSessionEvent prompted` with a `prompt` activity **and** a `Comment create` for the same reply (with `parentId` = thread root). |
| Delegation of a fresh ticket | `created` with an artificial root comment (`"This thread is for an agent session with <app>."`, `botActor: Linear`, no Comment webhook); prompt body `Work on [RET-n](…)`; `previousComments` null. |
| Delegation when the app already has a session on the ticket | **No agent event.** Only `Issue update` with `updatedFrom.delegateId: null` → app user id. |
| Plain top-level comment on a delegated ticket | No agent event; only `Comment create` (no `parentId`). |
| Ordering | `created` can arrive before the `Issue create` it refers to. |
| Field changes | `Issue update` carries the full entity plus `updatedFrom` with the old values (`labelIds`, `stateId`, `description`, `delegateId`, …). |
| Unset fields | `delegateId` is **omitted** (not null) when unset. |
| File uploads | Markdown in description/comment bodies; not Attachment entities. Attachment events are link attachments (PRs, URLs). |
| Own writes | The app's own comments/edits come back as webhooks with the app user as author/actor. |
| `agentSessions` query | Returns sessions of other agents too; always filter by app user. |

Linear constraints the design must respect: return HTTP 200 within **5 s**; first activity within **10 s** of `created` or the session is marked unresponsive; sessions go `stale` after **30 min** without activity (recoverable); OAuth app rate limit **5,000 requests/hour** per app user (2× Business, 5× Enterprise).

## 4. Decisions

1. Python channel plugin; Linear events become gateway messages; the gateway owns sessions, interrupts, approvals, clarify and display settings.
2. One Linear OAuth app per Hermes profile, `actor=app`, client-credentials tokens.
3. Webhooks only; one process-wide listener; per-profile path `/linear/<profile>/webhook`.
4. Delegated ticket → one Hermes session (`chat_id = <issueId>:<generation>`, shared). Non-delegated @mention → thread session (`chat_id = <issueId>`, `thread_id = <agentSessionId>`). @mention on a delegated ticket joins the ticket session.
5. Plain human top-level comments on delegated tickets wake the agent (plugin opens the Linear session). Edits are context only.
6. Context-only events: Issues, Comments (non-session threads), Issue attachments, Projects, Project updates. Label-definition events are not used.
7. Access: allowlist of Linear UUIDs / emails / `*`, or allow-all; default deny; denied users never become context. Bots and integrations are context-only and never wake the agent.
8. Visibility: every tool call is an `action`; commentary is `thought`; heartbeat on long turns; todo → Linear plan.
9. Approvals and clarify as `elicitation` + `select`; who may answer follows `approvals.approvers`.
10. Re-delegation resumes the ticket session unless the ticket is Done/Canceled (then a new generation).
11. Auto-move a human-delegated ticket from Backlog/Todo/Triage to the team's first started state (setting, default on).
12. Linear text is never a gateway command: user-originated messages are built with `allow_gateway_control=False` (a comment `/new` cannot reset the ticket session); only the adapter's own synthesized stop uses gateway control.
13. Settings via `config_schema` (`plugins.entries.linear.settings.*`); secrets via `config_schema` secrets in the profile `.env`; runtime state via `plugin_db("linear")`.

## 5. Architecture

```
Linear ──HTTPS──▶ Tailscale Funnel ──▶ listener (one per process)
                                         │  path → profile, verify, dedup, 200
                                         ▼
                        per-profile LinearAdapter (BasePlatformAdapter)
                ┌──────────────┬─────────┴────────┬───────────────┐
             router         access             store        linear_client
                │                                                 ▲
                ▼                                                 │
        gateway (sessions, turns, approvals) ──▶ renderer ────────┘
```

### Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `listener` | Process-wide aiohttp server. First adapter to connect starts it; adapters register/unregister their profile. Routes `/linear/<profile>/webhook`, verifies, dedups, acks, enqueues. | adapter registry, each profile's webhook secret |
| `linear_client` | Per profile. Client-credentials token lifecycle (renew before expiry and on 401), GraphQL, 429 backoff, cached app user id, user-email cache. | `client_id`, `client_secret` |
| `store` | Per profile, `plugin_db("linear")`. Tables in §5.1. | — |
| `router` | Linear event → gateway `MessageEvent`, context item, or drop. Owns mode rules and reply targets. Pure given store state. | store, access |
| `renderer` | Hermes output → Linear activities / plan / external URL. Pure mapping + a writer. | linear_client, store |
| `access` | Allowlist decision for an actor. Implemented with `OwnAccessPolicyMixin`. | linear_client (email lookup) |
| `setup` | `setup_fn` wizard, `hermes linear setup/status/test` CLI. | all of the above |

Only `listener` and `linear_client` do network I/O. `router` and `renderer` are unit-testable over recorded payloads.

### 5.1 Store schema (per profile)

- `tickets(issue_id PK, identifier, team_id, project_id, mode ∈ {untouched, mention, delegated}, generation INT, state_type, updated_at)`
- `sessions(agent_session_id PK, issue_id, root_comment_id, kind ∈ {delegation, mention, comment, plugin}, plugin_created BOOL, created_at)`
- `turns(message_id PK, agent_session_id, hermes_chat_id, status, created_at)` — `message_id = <agentSessionId>:<activityId>`
- `pending_context(id PK, issue_id, kind, field, old, new, actor, at, consumed_by NULL)`
- `pending_controls(request_id PK, kind ∈ {approval, clarify}, agent_session_id, execution_generation, choices JSON, created_at)`
- `seen_webhooks(webhook_id PK, at)` — 24 h TTL, with an in-memory LRU in front
- `users(user_id PK, email, name, fetched_at)` — 1 h TTL

## 6. Session routing and lifecycle

### 6.1 Modes and keys

Per (profile, ticket): `untouched`, `mention`, or `delegated`.

| Case | `SessionSource` | Effect |
|---|---|---|
| Delegated ticket | `chat_id = <issueId>:<generation>`, no `thread_id`, `shared_session=True` | One shared Hermes session for the ticket |
| @mention, not delegated | `chat_id = <issueId>`, `thread_id = <agentSessionId>` | One session per Linear thread |

Inbound `message_id = <agentSessionId>:<activityId>`; the renderer resolves `reply_to` through `turns` so every turn answers in the Linear session that asked. Turn input is prefixed with `[Linear <identifier> · thread <rootCommentId> · <author name>]` followed by the frozen `prompt` activity body (comments are editable, so they are never re-read).

### 6.2 Turn triggers

1. **`created` from delegating a fresh ticket** → mode `delegated`; generation is 1 for a ticket never delegated before, otherwise as decided in §6.4. First-turn input: `promptContext` + a ticket snapshot (title, state, labels, assignee, project, description ≤ 2,000 chars).
2. **`Issue update` with `delegateId` → app user and no `created`** (ticket already had an app session) → mode `delegated`. Wait 5 s for a `created` on that issue; if none arrives, open a session with `agentSessionCreateOnIssue`, mark it `plugin_created`, and start a turn: "You have been delegated <identifier>" + ticket snapshot.
3. **`created` from an @mention** → if the ticket is delegated: turn in the ticket session with the thread's portion of `promptContext`; otherwise a new thread session with full `promptContext`.
4. **`prompted`**, in order:
   1. `signal: stop` → interrupt (§8.5).
   2. A `pending_controls` row for this agent session → resolve it (§8.4); no new turn.
   3. Otherwise → a turn in the session mapped to this agent session.
5. **Plain top-level human comment on a delegated ticket** → `agentSessionCreateOnComment` on that comment; record the session (`plugin_created`, `root_comment_id`) **before** processing any `created` for it, and match the resulting `created` webhook by session id **or** root comment id so it is ignored regardless of arrival order; start a turn in the ticket session.

### 6.3 Concurrency and ordering

- Turns on one Hermes session run sequentially. A prompt from another thread while a turn is running is **queued**; that agent session immediately gets an ephemeral `thought` ("Queued — finishing current work first"), satisfying the 10 s rule. Only `stop` interrupts. The adapter installs a busy-session handler that enforces queueing.
- Events for an unknown ticket create a placeholder `tickets` row that later events fill in.

### 6.4 Lifecycle

- **Un-delegated** (`delegateId` away from the app user) or **session dismissed**: interrupt any running turn with a final `response`, set mode `untouched`. The Hermes transcript is kept.
- **Re-delegated**: if the ticket's `state.type` is not `completed`/`canceled`, keep the generation (session resumes with memory); otherwise increment the generation (fresh session; the old one remains in Hermes history).
- **Mention → delegated**: starts the ticket session (new key). `promptContext` and the ticket snapshot carry earlier threads.
- **Mention sessions** follow the gateway's normal idle/reset policy.

## 7. Inbound pipeline, access, context

### 7.1 Listener order of operations

1. Path → profile. Unknown profile or channel not enabled → **404** without reading the body.
2. Body > 1 MB → **413**. Read the raw body.
3. Verify `Linear-Signature` (constant-time) with the profile's webhook secret → **401** on failure.
4. `webhookTimestamp` older than 60 s → **401**.
5. Seen delivery (`Linear-Delivery` header; SHA-256 of the raw body if absent) → **200**, drop, INFO log. Never key on payload `webhookId`: it is the webhook config id, constant across deliveries.
6. Payload `oauthClientId` present and ≠ the profile's `client_id` → **401**.
7. **200**, then enqueue to the profile's adapter.

Accepted risk: an event can be lost if the process dies between the 200 and the gateway hand-off (milliseconds). No inbound journal in milestone 1.

### 7.2 Dispatch by type

- `AgentSessionEvent` → router (§6).
- `Issue`, `Comment`, `Attachment`, `Project`, `ProjectUpdate` → context collector (§7.4), except triggers in §6.2 (2) and (5).
- `OAuthApp revoked` → mark the profile's channel fatal; surfaced in `hermes status` / `hermes linear status`.
- `AppUserNotification`, `PermissionChange` → log and ignore.
- **Echo filter:** drop any event whose actor / comment author is the app user.

### 7.3 Access

- Actor: session creator (`created`), prompt author (`prompted`), comment author (Comment), `actor` (Issue/Project edits).
- Allowlist entries: Linear user UUID, email (resolved via `users` cache, GraphQL on miss), or `*`. Display names are never matched.
- Denied actor on a turn trigger → `error` activity with a short refusal naming who can grant access; no turn.
- Denied actor on a context event → dropped.
- Events with `botActor` (integrations, other agents) → context only; never a turn trigger.

### 7.4 Context collector

Kept only for tickets in mode `delegated`:

- `Issue` / `Attachment` events for that ticket.
- `Project` / `ProjectUpdate` events, fanned out to every delegated ticket with that `project_id`.
- `Comment`: top-level by a human → turn trigger (§6.2 (5)); reply in a non-session thread → context; reply in an agent-session thread (`parentId` ∈ `sessions.root_comment_id`) → drop (already arrived as `prompted`).

Item shape: `(issue, kind, field, old → new, actor, at)`. Labels and states use names from the payload; description changes keep the new text truncated to 800 chars.

### 7.5 Digest fold

When a turn starts in a ticket session, unconsumed items are rendered at the top of the input:

```
Since your last turn:
- Status: Todo → In Progress (Mike Machado)
- Labels: +Feature (Mike Machado)
- [Project Valkyrie] status post: "On track, …" (Val)
- +3 earlier changes (priority, description)
```

- Same (kind, field) collapses to one line: earliest old → latest new.
- ~4,000-char cap; oldest items are summarised first.
- Items are marked consumed only after the gateway admits the turn.
- At most 200 pending items per ticket; beyond that, oldest items are merged.
- Context never triggers a turn.

## 8. Outbound rendering

### 8.1 Acknowledgement

After the access check passes on `created`/`prompted`, the adapter itself emits an ephemeral `thought` ("On it" / "Queued — …") **before** enqueueing the turn.

### 8.2 Mapping

| Hermes output | Linear |
|---|---|
| Final reply (`send`) | `response` (markdown as-is; Linear URLs render as mentions) |
| Interim / commentary send | `thought` (see §12 item 2) |
| Typing / working | ephemeral `thought` "Working…", ≤ 1/min; heartbeat every 10 min on long turns |
| Tool start | ephemeral `action` (tool name, short argument preview) — skipped if the tool finishes within 2 s |
| Tool complete | persistent `action` with redacted result preview (≤ 500 chars); failures marked |
| Turn failure | `error` with a short reason |
| Todo updates | `agentSessionUpdate.plan` (full replace; `pending/in_progress/completed/cancelled` → `pending/inProgress/completed/canceled`) |
| Session link | `externalUrls` from `session_url` setting when set |

Tool events come from the plugin's `pre_tool_call` / `post_tool_call` hooks (tool name, args, result, duration), filtered to Linear sessions via `get_session_env("HERMES_SESSION_PLATFORM")`. Adapters only receive tool *starts* through `format_tool_event` and never results, so the adapter returns `None` there and does not parse chat-formatted progress text. `display.platforms.linear` overrides are honoured; default is full verbosity.

### 8.3 Ticket auto-start

When `auto_start` is on and a human (not an automation) delegated the ticket, on the first turn of a generation move a ticket in `backlog`/`unstarted`/`triage` to the team's lowest-position `started` state. The resulting `Issue update` is dropped by the echo filter.

### 8.4 Approvals and clarify

- `send_exec_approval` → `elicitation` + `select`: Allow once (`once`), Allow for this session (`session`), Deny (`deny`). Row in `pending_controls`.
- Answer (`prompted`) → `approval_refusal` check against `approvals.approvers` → `resolve_gateway_approval_card(…, request_id, actor_id=…)`. **Only an exact option value or label is accepted**; any other text re-asks with the options.
- `send_clarify` → `elicitation` (`select` when choices exist); free text accepted; `resolve_gateway_clarify`.
- Expired approval/clarify → `thought` noting expiry.

### 8.5 Stop

`signal: stop` → interrupt via the gateway's normal path; when settled, `response`: "Stopped. Done so far: … / Not done: …" (from the turn's partial output, or a fixed text if none).

### 8.6 Failure handling and budget

- Linear writes retry 3× with backoff and honour 429 `Retry-After`.
- A `response` that still fails is logged and the profile is reported degraded; no comment fallback.
- Budget: a 300-tool-call turn is ≤ ~600 writes, inside 5,000/h.

## 9. Configuration

All settings are declared in the manifest `config_schema` and live at `plugins.entries.linear.settings.<key>` in the profile's `config.yaml` (Desktop: Settings → Plugins → Linear). Secrets are `type: secret`, stored only in the profile `.env`, read with `get_secret()`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `client_id` | str (required) | — | Linear OAuth app client id |
| `client_secret` | secret → `LINEAR_CLIENT_SECRET` | — | |
| `webhook_secret` | secret → `LINEAR_WEBHOOK_SECRET` | — | Linear app webhook signing secret |
| `allowed_users` | list | `[]` | UUIDs, emails, or `*`; list or comma-separated string. Union with `LINEAR_ALLOWED_USERS` (comma-separated) |
| `allow_all_users` | bool | `false` | `LINEAR_ALLOW_ALL_USERS=true` also honoured |
| `auto_start` | bool | `true` | §8.3 |
| `session_url` | str | `""` | Template with `{session_id}` for the Linear "Open" link |
| `listener_host` | str | `127.0.0.1` | Process-wide; read from the default profile only |
| `listener_port` | int | `8650` | Process-wide; read from the default profile only |

Internal constants (not settings): 60 s replay window, 1 MB body cap, 24 h dedup TTL, 5 s delegation wait, 2 s fast-tool skip, 4,000-char digest cap, 200 pending items, 10 min heartbeat.

The platform is registered with `allowed_users_env="LINEAR_ALLOWED_USERS"` and `allow_all_env="LINEAR_ALLOW_ALL_USERS"` so `hermes status` and gateway tooling understand it.

## 10. Setup and CLI

- `hermes plugins install masha256/hermes-linear` → `hermes plugins enable linear`.
- `hermes gateway setup` → Linear, or `hermes linear setup`: explains OAuth app creation (categories, client credentials, scopes), collects `client_id` and secrets, prints `https://<host>/linear/<profile>/webhook` and the `tailscale funnel` command, then runs a live check (token, `viewer`, webhook reachability once the first event arrives).
- `hermes linear status`: per profile — token validity/expiry, app user, webhook URL, last event received, allowlist mode, degraded/fatal state.
- `hermes linear test`: posts a locally signed synthetic event to the listener to verify signature and routing without Linear.

## 11. Testing

- **Unit:** router, renderer, access, digest fold — pure functions over the RET-43 fixtures (IDs scrubbed).
- **Listener:** signature, replay, dedup, unknown profile, `oauthClientId` mismatch, body cap.
- **Contract:** fake Linear GraphQL server — token renew on 401, 429 backoff, activity writes, `agentSessionCreateOnComment` / `OnIssue`.
- **Multi-profile:** two profiles, one listener, same workspace — each sees only its own delegated tickets.
- **Live smoke script** against the Hermes Test app: delegate, two threads, plain comment, edit, approval, stop.
- `hermes plugins validate` clean.

## 12. Verify during implementation

Each item has a defined fallback, so none blocks the plan.

1. `ctx.get_config()` inside a multiplexed secondary profile's adapter returns that profile's settings. **Fallback:** read via `apply_yaml_config_fn` → `PlatformConfig.extra` (per-profile by construction).
2. How the gateway distinguishes interim from final sends in `send()` metadata. **Fallback:** buffer sends per turn; the last one before turn completion is the `response`, earlier ones become `thought`.
3. `agentSessionCreateOnIssue` when the app already has a session on the ticket. **Fallback:** reuse the most recent non-dismissed app session on that ticket.
4. Whether `agentSessionCreateOnComment` emits a `created` webhook. **Handled either way** by the §6.2 (5) match rule.
5. Link-attachment and project status-post payload shapes (not yet observed). **Fallback:** generic item rendering from the entity's title/body fields.
6. Client credentials on an app that has **never** been installed in the workspace (the probe app had been installed earlier via the browser flow). **Fallback:** the setup wizard prints a one-time install URL (`/oauth/authorize?actor=app&scope=…`) for an admin to approve; the plugin still uses client-credentials tokens afterwards.
7. Whether Linear requires a callback URL on an app that only uses client credentials. **Handled:** README tells users to enter any HTTPS URL if asked.
8. Gateway access gate vs email allowlist entries (§7.3). **Resolved by T1 (fallback):** a non-empty `LINEAR_ALLOWED_USERS` bypasses the own-policy path, so the adapter also exposes `resolved_allowlist_user_ids()` (settings + email-resolved UUIDs) and builds every source with `chat_type="dm"`; see `docs/hermes-integration-notes.md` Q4.
9. Busy-session queueing (§6.3). **Resolved by T1 (fallback):** the gateway default is interrupt and the shared runtime bypasses busy handlers, so the adapter keeps its own per-session FIFO released on `on_processing_complete` instead of installing a busy-session handler; see notes Q5.

## 13. Milestone 1 and exit test

Build order follows the tracking tickets: scaffold + client + listener + store → delegated loop (router + renderer) → mention mode → plain comments → digest → access → approvals/clarify → stop → plan/link → docs/publish.

**Exit (dogfood):** a real profile's Linear work runs through the plugin and the hand-built `linear-orion` webhook route is retired. A delegated ticket holds one conversation across ≥ 2 threads, plain comments and edits; stop and approvals work from Linear; the 10 s first response is not missed over one week of real use. The probe logger and its Funnel path are removed.

## 14. Risks

- **Linear agent API changes** (it is evolving; Agent Plans is a preview). Mitigation: isolate in `renderer`/`linear_client`; fixtures make breakage visible.
- **Hermes plugin API drift.** Mitigation: public helpers only; `hermes plugins validate` in CI.
- **Prompt injection via ticket content.** Mitigation: allowlist, denied users excluded from context, approvals for risky commands, bots context-only.
- **Webhook URL changes on profile rename.** Documented; `hermes linear status` prints the current URL.
