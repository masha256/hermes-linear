# Hermes Linear Channel Plugin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship milestone 1 of the Hermes Linear channel plugin: a delegated Linear ticket is one Hermes session; @mentions are thread sessions; native Linear agent activities, approvals, stop and plan.

**Architecture:** A `kind: platform` Hermes plugin. A process-wide aiohttp listener verifies Linear webhooks and hands them to a per-profile `LinearAdapter`. A pure `router` turns events into decisions (start turn, add context, resolve control, interrupt, refuse, …); the adapter executes them against the gateway (`handle_message`) and Linear (`linear_client`). A pure `renderer` maps Hermes output (sends, typing, tool hooks, approvals, clarify) to Linear agent activities. State lives in a per-profile SQLite DB from `plugin_db`.

**Tech Stack:** Python 3.11+ (dev box runs Hermes on 3.14), aiohttp (server + client, already a Hermes dependency), sqlite3 (stdlib), pytest + pytest-asyncio for tests. Hermes public plugin API only.

**Spec:** `docs/superpowers/specs/2026-11-21-hermes-linear-channel-design.md` (read it first; section refs below are `§n`).

**Tracking:** Linear project *hermes-linear* (formerly Valkyrie). One issue per task (named in each task heading, titled `T<n> · <task>`); milestone exit is RET-60. Move an issue to In Progress when its task starts and Done when its commit lands.

## Global Constraints

- Import only modules/names listed in Hermes's public plugin API (`hermes plugins validate` must report no private imports). Allowed modules used here: `gateway.platforms.base`, `gateway.platforms.event`, `gateway.platforms._shared`, `gateway.platforms.helpers`, `gateway.config`, `gateway.platforms.access_policy_mixin`, `gateway.session_context.get_session_env`, `gateway.approval_actor.{approval_refusal, refusal_text}`, `tools.approval.resolve_gateway_approval_card`, `tools.clarify_gateway.{get_pending, resolve_gateway_clarify, mark_awaiting_text, SKIPPED}`, `agent.secret_scope`, `agent.redact.redact_sensitive_text`, `plugins.plugin_storage.{plugin_data_dir, plugin_db}`, `hermes_cli.gateway_setup_wizard.*` (listed names), `hermes_cli.setup.{prompt, prompt_choice, prompt_yes_no}`, `hermes_cli.cli_output`. Duck-type anything else (e.g. stream event classes by `type(x).__name__`).
- Plugin/platform name: `linear`. Settings namespace `plugins.entries.linear.settings`. Secrets: `LINEAR_CLIENT_SECRET`, `LINEAR_WEBHOOK_SECRET`. Allowlist env: `LINEAR_ALLOWED_USERS`, `LINEAR_ALLOW_ALL_USERS`.
- Webhook path: `/linear/<profile>/webhook`. Listener defaults `127.0.0.1:8650`, read from the default profile only.
- Token: `POST https://api.linear.app/oauth/token`, `grant_type=client_credentials`, `scope=read,write,app:mentionable,app:assignable`, HTTP Basic auth with client id/secret. GraphQL endpoint `https://api.linear.app/graphql`.
- Constants (`hermes_linear/constants.py`, not settings): `REPLAY_WINDOW_S = 60`, `MAX_BODY_BYTES = 1_048_576`, `DEDUP_TTL_S = 86_400`, `DELEGATION_WAIT_S = 5`, `FAST_TOOL_SKIP_S = 2`, `DIGEST_CAP_CHARS = 4_000`, `MAX_PENDING_ITEMS = 200`, `HEARTBEAT_S = 600`, `TYPING_MIN_INTERVAL_S = 60`, `DESCRIPTION_SNIPPET = 800`, `SNAPSHOT_DESCRIPTION = 2_000`, `RESULT_PREVIEW = 500`, `USER_CACHE_TTL_S = 3_600`, `TOKEN_RENEW_MARGIN_S = 86_400`, `WRITE_RETRIES = 3`.
- Never log secrets, tokens or full webhook bodies at INFO; DEBUG may log event `type/action/webhookId` only.
- All Linear text the agent sees is untrusted; never forward it with gateway-control enabled (see Review Focus 1).

## Review Focus

1. **Slash-prefixed Linear text** (a comment `/new` or `/stop`) must not run gateway commands — a ticket comment must never reset or control the session. Every user-originated `MessageEvent` is built with `allow_gateway_control=False`; only the adapter's synthesized stop uses `True`. Test in Task 11 (`test_user_text_never_gateway_control`).
2. **Duplicate input from Linear's double delivery** (a thread reply arrives as `prompted` *and* `Comment create`): the agent must see it once. Test in Task 7 (`test_reply_in_session_thread_dropped_from_context`).
3. **Gateway restart mid-ticket**: after restart the next `prompted` on an existing ticket must land in the same Hermes session (keys derive from store + payload only, never in-memory state). Test in Task 7 (`test_chat_id_stable_across_store_reopen`).
4. **Webhook for a profile whose channel is disabled or whose secret is unset** must 404 without reading the body and never crash the shared listener. Test in Task 5 (`test_profile_without_secret_404`).
5. **Linear API outage / 401 storm while a turn is running**: activity writes fail; the turn must not crash and the token must be renewed at most once per 401 burst. Test in Task 4 (`test_concurrent_401_single_renew`).

---

## File Structure

```
plugin.yaml                     manifest (kind: platform, config_schema, requires_env)
__init__.py                     `from .hermes_linear.plugin import register`
pyproject.toml                  project metadata, entry point, pytest config
hermes_linear/
  __init__.py
  constants.py                  constants above
  models.py                     dataclasses: Settings, ContextItem, decisions (§ Task 2)
  settings.py                   load Settings from ctx config + secrets; allowlist parsing
  store.py                      SQLite schema + data access (spec §5.1)
  linear_client.py              token lifecycle, GraphQL, typed Linear operations
  listener.py                   process-wide aiohttp webhook listener + verification
  access.py                     allowlist decision
  router.py                     pure: event + store → decisions (spec §6, §7.2–7.4)
  digest.py                     pure: context items → "Since your last turn" block (§7.5)
  renderer.py                   pure mapping + ActivityWriter (spec §8)
  adapter.py                    LinearAdapter(BasePlatformAdapter): wires everything
  hooks.py                      pre/post_tool_call hook handlers → renderer
  setup_cli.py                  setup_fn wizard, `hermes linear` CLI
  plugin.py                     register(ctx)
tests/
  conftest.py                   sys.path for repo + Hermes; fixture loader; fake Linear server
  fixtures/probe/*.json         scrubbed RET-43 payloads
  test_*.py                     one file per module
scripts/
  dev-env.sh                    create .venv on Hermes's interpreter
  scrub_fixtures.py             one-off: probe events → tests/fixtures/probe
  smoke.md                      live smoke checklist against the Hermes Test app
docs/hermes-integration-notes.md  findings from Task 1
```

---

### Task 0: Dev environment, skeleton, fixtures — RET-46

**Files:**
- Create: `plugin.yaml`, `__init__.py`, `pyproject.toml`, `hermes_linear/__init__.py`, `hermes_linear/constants.py`, `hermes_linear/plugin.py` (stub), `scripts/dev-env.sh`, `scripts/scrub_fixtures.py`, `tests/conftest.py`, `tests/fixtures/probe/*.json`, `tests/test_skeleton.py`, `.gitignore`

**Interfaces:**
- Produces: `tests/conftest.py::load_fixture(name: str) -> dict` (returns `payload` of a scrubbed probe event); fixture names listed below. `hermes_linear.constants` names from Global Constraints. `register(ctx) -> None` stub in `plugin.py`.

- [ ] **Step 1: `scripts/dev-env.sh`** — resolve `HERMES_PYTHON` (env override; else newest `~/.hermes/installs/*/environments/*/venv/bin/python3` that can `import aiohttp`), create `.venv` with `--system-site-packages`, `pip install pytest pytest-asyncio`, write `.venv/lib/python*/site-packages/hermes.pth` containing `$HERMES_AGENT_DIR` (default `~/.hermes/hermes-agent`). Run it; expected: `.venv/bin/python -c "import gateway.platforms.base, aiohttp, pytest"` exits 0.
- [ ] **Step 2: `plugin.yaml`** — `name: linear`, `label: Linear`, `kind: platform`, `version: 0.1.0`, `description`, `author`, `requires_env` for `LINEAR_CLIENT_SECRET`, `LINEAR_WEBHOOK_SECRET` (password: true), `optional_env` for `LINEAR_ALLOWED_USERS`, `LINEAR_ALLOW_ALL_USERS`, and `config_schema` with exactly the keys/types/defaults of spec §9 (`client_secret`/`webhook_secret` as `type: secret` with `env:` names; `client_id` `required: true`).
- [ ] **Step 3: `scripts/scrub_fixtures.py`** — read `~/.hermes/profiles/neo/cache/scratch/linear-probe/events/*.json`, replace every UUID with a stable fake (`00000000-0000-4000-8000-<counter:012d>`, same input → same output across files), replace emails with `user<n>@example.com`, drop `headers`, write `tests/fixtures/probe/<type>-<action>-<n>.json` keeping `payload` only. Required outputs (names used by later tests): `agent_created_mention`, `agent_prompted_reply`, `agent_created_delegation`, `comment_create_mention_root`, `comment_create_reply`, `comment_create_toplevel_plain`, `issue_update_delegated`, `issue_update_labels`, `issue_update_state`, `issue_update_description`, `issue_create_delegated`, `project_update`. Commit the outputs; the script is kept for reproducibility.
- [ ] **Step 4: Write `tests/test_skeleton.py::test_fixtures_load_and_scrubbed`** — every fixture loads; no string in any fixture matches the original workspace's real UUID/email set (assert no `@retrograde` / no `linear.app/retrograde-capital` substrings).
- [ ] **Step 5: Run** `.venv/bin/pytest -q` → PASS.
- [ ] **Step 6: Run** `hermes plugins validate .` → no errors, no private-import warnings.
- [ ] **Step 7: Commit** `chore: plugin skeleton, dev env, scrubbed probe fixtures`.

### Task 1: Hermes integration probe — RET-63

Answers spec §12 items 1–2 plus the gateway contracts the adapter depends on. Output is a findings doc; no product code. Each question names the default the rest of the plan assumes and the fallback if the probe disproves it.

**Files:**
- Create: `docs/hermes-integration-notes.md`, throwaway `scratch/` scripts (gitignored)

**Interfaces:**
- Produces: `docs/hermes-integration-notes.md` with one section per question, each ending in `Decision: default | fallback`.

- [ ] **Step 1: Answer, by reading `~/.hermes/hermes-agent` source and running throwaway scripts against a dev install (Task 13's symlink may be created early for this):**
  1. *Interim vs final sends.* Does `send()` metadata distinguish interim commentary / tool-progress lines from the final reply? Default: final reply is a `send()` with no progress marker; progress lines go through `format_tool_event` and we return `None`. Fallback: spec §12.2 buffering.
  2. *Tool hooks in gateway turns.* Do `pre_tool_call`/`post_tool_call` fire for gateway-session tools with `get_session_env("HERMES_SESSION_PLATFORM") == "linear"` and `HERMES_SESSION_KEY` / `HERMES_SESSION_MESSAGE_ID` bound? Default: yes. Fallback: actions from `format_tool_event` starts only (no results), documented as a spec deviation.
  3. *`ctx.get_config` per profile* under `gateway.multiplex_profiles` (spec §12.1). Default: per-profile. Fallback: `apply_yaml_config_fn` bridge → `PlatformConfig.extra`.
  4. *Access gate.* With `enforces_own_access_policy` True and an email allowlist, does the gateway's env allowlist (`LINEAR_ALLOWED_USERS` vs `user_id`) still deny? Default: adapter sets `user_id` to the Linear UUID and, for email-only matches, admits through `OwnAccessPolicyMixin` with `_dm_policy = _group_policy = "allowlist"` and `_allow_from` = resolved UUIDs. Record the exact attribute contract.
  5. *Busy session.* A second `handle_message` for a busy shared session is queued (not interrupting) by default. Default: yes, no handler needed.
  6. *Stop.* A synthesized `MessageEvent(text="/stop", allow_gateway_control=True)` on the session's source interrupts the running turn, and the gateway then emits a stop notice through `send()`. Default: yes, and that notice (prefixed with the turn's partial output when the gateway provides it) becomes the §8.5 `response`. Fallback: the adapter posts `"Stopped."` itself once the turn settles (30 s timeout).
  7. *CLI at deferred load.* Does `ctx.register_cli_command` from a deferred `kind: platform` plugin register `hermes linear …`? Default: needs `provides_tools`-style eager entry; if unavailable, ship setup via `setup_fn` (`hermes gateway setup`) and status via a `hermes linear` command registered from an eager `cli.py` if the manifest supports it, else drop the CLI and document `hermes status`.
  8. *Approval prompt contract.* Which `ExecApprovalPrompt` fields and `actions` tuples `_send_exec_approval_prompt` receives; exact `resolve_gateway_approval_card` signature and choice strings (`once`/`session`/`always`/`deny`).
  9. *Plugin load as package.* Relative imports from root `__init__.py` into `hermes_linear/` work for a directory plugin. Default: yes (`spec_from_file_location(..., submodule_search_locations=[plugin_dir])`).
  10. *Fatal errors and expiry.* The public way for an adapter to report a fatal error (base exposes `set_fatal_error_handler` / `has_fatal_error`), and how approval timeouts reach the adapter (clarify uses `retire_clarify_card(clarify_id, notice)`). Record the exact calls.
- [ ] **Step 2: Commit** `docs: Hermes integration notes`. If any answer takes a fallback, add a one-line amendment to the spec's §12 entry in the same commit.

### Task 2: Models and settings — RET-64

**Files:**
- Create: `hermes_linear/models.py`, `hermes_linear/settings.py`, `tests/test_settings.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Settings(client_id: str, client_secret: str, webhook_secret: str, allowed_users: frozenset[str], allow_all_users: bool, auto_start: bool, session_url: str, listener_host: str, listener_port: int)`
  - `parse_allowlist(value: object) -> frozenset[str]` — accepts list, JSON-list string, or comma string; strips; lowercases emails only.
  - `load_settings(get_config: Callable[[str, Any], Any], get_secret: Callable[[str], str | None], env: Mapping[str, str]) -> Settings` — `allowed_users` = union of setting and `LINEAR_ALLOWED_USERS`; `allow_all_users` = setting or env in `{"true","1","yes"}`.
  - `class SettingsError(ValueError)`.
  - `@dataclass(frozen=True) ContextItem(issue_id: str, kind: str, field: str, old: str | None, new: str | None, actor: str, at: str, project: str | None = None)`
  - Decisions (frozen dataclasses): `StartTurn(agent_session_id, activity_id, issue_id, chat_id, thread_id: str | None, user_id, user_name, text, first_turn: bool, ack: str)`, `AddContext(items: tuple[ContextItem, ...])`, `ResolveControl(agent_session_id, request_id, kind, actor_id, answer)`, `Interrupt(agent_session_id)`, `Refuse(agent_session_id, text)`, `SetMode(issue_id, mode, generation)`, `AwaitDelegation(issue_id, deadline_s)`, `OpenSessionOnComment(issue_id, comment_id, user_id, user_name, body)`, `Fatal(reason)`, `Ignore(reason)`.

- [ ] **Step 1: Write failing tests** — `test_parse_allowlist_forms` (`["a@x.com"," B@X.com "]`, `'["u1","*"]'`, `"u1, u2"` → expected sets; `None` → empty); `test_load_settings_defaults` (only required keys set → `auto_start True`, `listener_port 8650`, `listener_host "127.0.0.1"`, `allow_all_users False`); `test_allowlist_union_env`; `test_missing_required_raises` (`client_id` empty → `SettingsError` naming the key).
- [ ] **Step 2: Run** `pytest tests/test_settings.py -q` → FAIL (import error).
- [ ] **Step 3: Implement** `models.py`, `settings.py`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: settings and decision models`.

### Task 3: Store — RET-49

**Files:**
- Create: `hermes_linear/store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `ContextItem` (Task 2).
- Produces: `class Store(conn: sqlite3.Connection)`; `Store.open(path: Path | None = None) -> Store` (None → `plugin_db("linear")`; tests pass `:memory:`). Methods:
  - `ticket(issue_id) -> Ticket | None`; `upsert_ticket(issue_id, *, identifier=None, team_id=None, project_id=None, state_type=None) -> Ticket` (placeholder rows allowed, partial fields never null-out existing values); `set_mode(issue_id, mode: Literal["untouched","mention","delegated"], generation: int | None = None)`; `delegated_in_project(project_id) -> list[str]`.
  - `record_session(agent_session_id, issue_id, root_comment_id, kind, plugin_created=False)`; `session(agent_session_id) -> SessionRow | None`; `session_by_root_comment(comment_id) -> SessionRow | None`; `is_session_root(comment_id) -> bool`.
  - `record_turn(message_id, agent_session_id, chat_id, thread_id: str | None = None)`; `turn(message_id) -> TurnRow | None`; `latest_turn_for_chat(chat_id, thread_id: str | None = None) -> TurnRow | None` (exact `thread_id` match, `None` matches only thread-less turns — mention sessions share `chat_id`).
  - `add_context(items: Sequence[ContextItem])` (enforces `MAX_PENDING_ITEMS` by merging oldest same-field rows, then deleting oldest); `pending_context(issue_id) -> list[ContextItem]`; `consume_context(issue_id, turn_id)`.
  - `put_control(request_id, kind, agent_session_id, execution_generation, choices)`; `control_for_session(agent_session_id) -> ControlRow | None`; `drop_control(request_id)`.
  - `cache_state(state_id, name)`; `state_name(state_id) -> str | None` (filled from every Issue payload's `state`); `cache_label(label_id, name)`; `label_name(label_id) -> str | None` (from every payload's `labels`).
  - `seen_webhook(webhook_id, now: float) -> bool` (inserts; True if already present within `DEDUP_TTL_S`; prunes expired).
  - `cached_user(user_id, now) -> UserRow | None`; `put_user(user_id, email, name, now)`.
  - `Ticket`, `SessionRow`, `TurnRow`, `ControlRow`, `UserRow`: frozen dataclasses mirroring spec §5.1 columns.

- [ ] **Step 1: Write failing tests** — `test_upsert_placeholder_then_fill` (upsert with only id, then with identifier/project → both kept); `test_generation_defaults_to_1_on_first_delegate`; `test_seen_webhook_ttl` (seen at t=0 → True at t=10, False at t=86_401); `test_pending_cap_merges` (250 items for one field → ≤200 rows, newest `new` preserved, earliest `old` preserved); `test_session_root_lookup`; `test_consume_context`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with `CREATE TABLE IF NOT EXISTS` per spec §5.1, WAL as given by `plugin_db`, a `schema_version` pragma (`user_version = 1`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: per-profile state store`.

### Task 4: Linear client — RET-47

**Files:**
- Create: `hermes_linear/linear_client.py`, `tests/fake_linear.py`, `tests/test_linear_client.py`

**Interfaces:**
- Produces: `class LinearError(Exception)` (`.code: str | None`, `.status: int | None`); `class LinearClient(client_id: str, client_secret: Callable[[], str], *, base_url="https://api.linear.app", session: aiohttp.ClientSession | None = None, clock=time.monotonic)`. Async methods:
  - `token() -> str` (cached; renew when `expires_in` − `TOKEN_RENEW_MARGIN_S` elapsed); `gql(query: str, variables: dict | None = None) -> dict` (on 401: renew once, retry once; on 429/`RATELIMITED`: sleep `Retry-After` then retry, up to `WRITE_RETRIES`; GraphQL `errors` → `LinearError`).
  - `viewer_id() -> str` (cached); `user(user_id) -> tuple[email, name]`.
  - `create_activity(agent_session_id: str, content: dict, *, ephemeral: bool = False, signal: str | None = None, signal_metadata: dict | None = None) -> str`.
  - `create_session_on_comment(comment_id: str) -> str`; `create_session_on_issue(issue_id: str) -> str`; `app_sessions_on_issue(issue_id: str) -> list[dict]` (filtered to `appUser.id == viewer_id()`, newest first, non-dismissed).
  - `update_session(agent_session_id: str, *, plan: list[dict] | None = None, external_urls: list[dict] | None = None) -> None`.
  - `issue_snapshot(issue_id: str) -> dict` (`identifier, title, state{name,type}, labels[], assignee, project{name,id}, team{id}, description`).
  - `move_to_first_started(issue_id: str, team_id: str) -> bool` (lowest-`position` `started` state; no-op False if none).
  - `close()`.
- `tests/fake_linear.py`: aiohttp test server implementing `/oauth/token` and `/graphql` with programmable responses and a call log.

- [ ] **Step 1: Write failing tests** against `fake_linear` — `test_token_client_credentials_basic_auth` (asserts form `grant_type=client_credentials`, scopes string exactly `read,write,app:mentionable,app:assignable`, Basic header); `test_token_cached_until_margin`; `test_401_renews_once_and_retries`; `test_concurrent_401_single_renew` (10 concurrent `gql` calls all get 401 once → exactly 1 extra token request; Review Focus 5); `test_429_respects_retry_after`; `test_graphql_errors_raise`; `test_app_sessions_filtered_to_viewer`; `test_move_to_first_started_picks_lowest_position`; `test_create_session_on_comment_returns_id`; `test_create_session_on_issue_returns_id`; `test_create_activity_payload` (`agentSessionId`, `content`, `ephemeral`, `signal`, `signalMetadata` as sent).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; token renewal guarded by an `asyncio.Lock`; GraphQL documents as module constants (`MUTATION_ACTIVITY`, `MUTATION_SESSION_ON_COMMENT`, `MUTATION_SESSION_ON_ISSUE`, `MUTATION_SESSION_UPDATE`, `QUERY_ISSUE_SNAPSHOT`, `QUERY_TEAM_STARTED`, `MUTATION_ISSUE_UPDATE`, `QUERY_VIEWER`, `QUERY_USER`, `QUERY_ISSUE_APP_SESSIONS`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: Linear client with client-credentials tokens`.

### Task 5: Webhook listener — RET-48

**Files:**
- Create: `hermes_linear/listener.py`, `tests/test_listener.py`

**Interfaces:**
- Produces:
  - `verify_signature(secret: bytes, body: bytes, header: str) -> bool` (hex HMAC-SHA256, `hmac.compare_digest`).
  - `@dataclass ProfileEndpoint(profile: str, webhook_secret: Callable[[], bytes | None], client_id: str, deliver: Callable[[dict], None], seen: Callable[[str], bool])`.
  - `class WebhookListener(host: str, port: int, *, clock=time.time)`: `async start()`, `async stop()`, `register(ep: ProfileEndpoint)`, `unregister(profile: str)`, `.app` (aiohttp `Application`, for tests).
  - `get_listener(host: str, port: int) -> WebhookListener` (process singleton; first call fixes host/port; later calls with different values log a warning and return the existing one). Listener starts on first `register`, stops when the last profile unregisters.

- [ ] **Step 1: Write failing tests** (aiohttp `TestClient` on `.app`) — order of operations exactly as spec §7.1: `test_unknown_profile_404_body_unread`; `test_profile_without_secret_404` (Review Focus 4); `test_body_over_1mb_413`; `test_bad_signature_401`; `test_stale_timestamp_401` (`webhookTimestamp` = now − 61 s, ms); `test_duplicate_webhook_200_not_delivered`; `test_client_id_mismatch_401`; `test_valid_event_200_then_delivered` (uses `agent_created_mention` fixture re-signed with a test secret); `test_deliver_exception_does_not_break_listener` (deliver raises → still 200, next request works); `test_two_profiles_isolated` (endpoints `neo` and `raymond` with different secrets: each path accepts only its own signature and delivers only to its own `deliver`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; route `POST /linear/{profile}/webhook`; `deliver` is called after the 200 is prepared via `asyncio.get_running_loop().call_soon(...)`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: shared webhook listener`.

### Task 6: Access policy — RET-55

**Files:**
- Create: `hermes_linear/access.py`, `tests/test_access.py`

**Interfaces:**
- Consumes: `Settings.allowed_users`, `Settings.allow_all_users`.
- Produces: `class AccessPolicy(entries: frozenset[str], allow_all: bool)`; `async allowed(user_id: str | None, email_of: Callable[[str], Awaitable[str | None]]) -> bool`; `REFUSAL_TEXT = "You're not authorized to use this agent. Ask the Hermes owner to add you to the Linear allowlist."`.

- [ ] **Step 1: Write failing tests** — `test_default_deny`; `test_star_allows`; `test_allow_all_flag`; `test_uuid_match_no_email_lookup` (lookup mock not awaited); `test_email_match_case_insensitive`; `test_none_user_denied`; `test_email_lookup_failure_denies`.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat: allowlist access policy`.

### Task 7: Router — agent session events and lifecycle — RET-50

**Files:**
- Create: `hermes_linear/router.py`, `tests/test_router_agent.py`

**Interfaces:**
- Consumes: `Store` (Task 3), decisions (Task 2).
- Produces: `@dataclass RouterContext(store: Store, app_user_id: str, allowed: Callable[[str | None], Awaitable[bool]])` (the adapter passes `AccessPolicy.allowed` bound to the client's email lookup) and `async route(event: dict, ctx: RouterContext) -> list[Decision]` (no I/O besides `allowed` and store calls). Helpers used by the adapter: `ticket_chat_id(issue_id: str, generation: int) -> str` (`f"{issue_id}:{generation}"`), `turn_header(identifier, root_comment_id, author) -> str` (`f"[Linear {identifier} · thread {root_comment_id} · {author}]"`), `message_id(agent_session_id, activity_id) -> str`.

Rules are spec §6.2–6.4 verbatim; the tests below pin them.

- [ ] **Step 1: Write failing tests** (fixtures from Task 0):
  - `test_mention_untouched_starts_thread_session` — `agent_created_mention` → `[SetMode(mention), StartTurn(chat_id=issue_id, thread_id=agent_session_id, first_turn=True, text startswith header and contains promptContext)]`.
  - `test_fresh_delegation_starts_ticket_session` — `agent_created_delegation` → `SetMode(delegated, generation=1)` + `StartTurn(chat_id="<issue>:1", thread_id=None)`.
  - `test_mention_on_delegated_joins_ticket` — store has ticket delegated gen 2 → `StartTurn(chat_id="<issue>:2", thread_id=None)`, `text` contains the `<primary-directive-thread>` and `<user-request>` elements of `promptContext` but not the `<issue>` element (the ticket session already has it).
  - `test_prompted_routes_to_mapped_session` — `agent_prompted_reply` after recording the mention session → `StartTurn` with `text` = header + `agentActivity.content.body`.
  - `test_prompted_stop_signal_interrupts` — same fixture with `agentActivity.signal="stop"` → `[Interrupt]` only.
  - `test_prompted_with_pending_control_resolves` — `put_control` for that session → `[ResolveControl(answer=body)]`, no `StartTurn`.
  - `test_delegate_change_without_created_awaits` — `issue_update_delegated` with no session event → `[SetMode(delegated), AwaitDelegation(issue, 5)]`.
  - `test_undelegate_interrupts_and_untouches` — `delegateId` from app user → other → `[Interrupt(...) for running sessions, SetMode(untouched)]`.
  - `test_redelegate_done_bumps_generation` / `test_redelegate_open_keeps_generation` (`state.type` `completed` vs `started`).
  - `test_plugin_created_session_created_ignored` — session recorded `plugin_created=True` → its `created` → `[Ignore]`; also matched by `comment.id` when the session id is not yet stored.
  - `test_denied_actor_refused` — `allowed` → False → `[Refuse(agent_session_id, REFUSAL_TEXT)]`.
  - `test_chat_id_stable_across_store_reopen` (Review Focus 3) — write store to a temp file, reopen, same `prompted` → same `chat_id`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `route` for `type == "AgentSessionEvent"` and the `delegateId` transitions on `Issue update` (read `delegateId` with `.get` — it is omitted when unset). `ack` strings: `"On it"`; for a ticket whose latest turn is running the adapter replaces it (Task 11).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: router for agent session events and ticket lifecycle`.

### Task 8: Router — data-change events, context, plain comments — RET-53

**Files:**
- Modify: `hermes_linear/router.py`
- Create: `tests/test_router_context.py`

**Interfaces:**
- Produces: `route()` handles `Issue`, `Comment`, `Attachment`, `Project`, `ProjectUpdate`, `OAuthApp`, `AppUserNotification`, `PermissionChange` per spec §7.2–7.4.

- [ ] **Step 1: Write failing tests**:
  - `test_echo_dropped` — any event whose `actor.id` / `data.userId` is the app user → `[Ignore]`.
  - `test_label_change_on_delegated_is_context` — `issue_update_labels` → `AddContext([ContextItem(field="labels", old=None, new="+Feature")])`: diff `updatedFrom.labelIds` against current `labels`, `+name` for added, `-name` for removed (name from `label_name`, else `-(removed label)`), comma-joined.
  - `test_state_change_context_names` — `issue_update_state` → `old="Todo"` when `state_name(updatedFrom.stateId)` is cached, `old="(previous)"` when not; `new="In Progress"`.
  - `test_description_truncated_800`.
  - `test_edit_on_untouched_ignored`.
  - `test_toplevel_human_comment_on_delegated_opens_session` — `comment_create_toplevel_plain` → `[OpenSessionOnComment(...)]`.
  - `test_reply_in_session_thread_dropped_from_context` (Review Focus 2) — `comment_create_reply` where `parentId` is a recorded session root → `[Ignore]`.
  - `test_reply_in_other_thread_is_context`.
  - `test_bot_comment_context_only` — `botActor` set on a top-level comment → `AddContext`, never `OpenSessionOnComment`.
  - `test_denied_actor_context_dropped`.
  - `test_project_update_fans_out` — two delegated tickets in the project → two `ContextItem`s tagged `project="Valkyrie"`.
  - `test_oauth_revoked_fatal`; `test_notifications_ignored`.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat: router for context events and plain comments`.

### Task 9: Digest — RET-54

**Files:**
- Create: `hermes_linear/digest.py`, `tests/test_digest.py`

**Interfaces:**
- Consumes: `ContextItem`.
- Produces: `fold(items: Sequence[ContextItem], cap: int = DIGEST_CAP_CHARS) -> str | None` (None when empty).

- [ ] **Step 1: Write failing tests** — `test_empty_none`; `test_header_and_line_format` (exact output for status + labels + project item, matching the spec §7.5 example layout: header `Since your last turn:`, `- Status: Todo → In Progress (Mike Machado)`, `- [Project Valkyrie] status post: "…" (Val)`); `test_same_field_collapses_earliest_old_latest_new`; `test_cap_summarises_oldest_first` (output ≤ cap, ends with `- +N earlier changes (<fields>)`, newest items intact).
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat: context digest`.

### Task 10: Renderer — RET-51

**Files:**
- Create: `hermes_linear/renderer.py`, `tests/test_renderer.py`

**Interfaces:**
- Consumes: `LinearClient.create_activity`, `LinearClient.update_session`.
- Produces:
  - Pure: `thought(body) -> dict`, `response(body) -> dict`, `error(body) -> dict`, `action(name: str, parameter: str, result: str | None = None) -> dict`, `elicitation(body) -> dict`, `approval_options() -> list[dict]` (`[{"label":"Allow once","value":"once"},{"label":"Allow for this session","value":"session"},{"label":"Deny","value":"deny"}]`), `parse_approval_answer(text: str) -> str | None` (exact value or case-insensitive label only), `plan_from_todos(todos: list[dict]) -> list[dict]` (status map `pending→pending, in_progress→inProgress, completed→completed, cancelled→canceled`), `tool_parameter(tool_name, args) -> str` (≤ 120 chars), `result_preview(result: str) -> str` (`redact_sensitive_text(force=True)`, ≤ `RESULT_PREVIEW`).
  - `class ActivityWriter(client)`: `async post(agent_session_id, content, *, ephemeral=False, signal=None, signal_metadata=None) -> bool` (never raises; returns False after `WRITE_RETRIES` and marks `degraded`); `.degraded: bool`; `.last_error: str | None`.

- [ ] **Step 1: Write failing tests** — content shapes match Linear's validated payloads (spec §8.2): `test_action_shapes`; `test_approval_answer_strict` (`"once"`, `"Allow once"` → `once`; `"sure go ahead"`, `"yes"` → None); `test_plan_mapping`; `test_result_preview_redacts_and_caps` (a fake `sk-…` token is redacted; length ≤ 500); `test_writer_never_raises_and_degrades`.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat: activity renderer`.

### Task 11: Adapter — inbound path and turns — RET-65

**Files:**
- Create: `hermes_linear/adapter.py`, `tests/test_adapter_inbound.py`

**Interfaces:**
- Consumes: everything above; Task 1 notes.
- Produces: `class LinearAdapter(OwnAccessPolicyMixin, BasePlatformAdapter)` with `ALLOW_ALL_ENV_PREFIX = "LINEAR"`, constructed `LinearAdapter(config: PlatformConfig, *, settings: Settings | None = None, client: LinearClient | None = None, store: Store | None = None)` (injection for tests). Methods: `async connect(*, is_reconnect=False) -> bool` (load settings, fetch token + `viewer_id`, open store, `get_listener(...).register(ProfileEndpoint(...))`), `async disconnect()`, `async on_event(event: dict)` (route + execute decisions), `async get_chat_info(chat_id) -> dict`. Profile name from the adapter's owner profile (`set_owner_profile`), default `"default"`.

Decision execution:
- `StartTurn` → post ack `thought` (ephemeral) **before** `handle_message`; if the chat's latest turn is still running, ack text `"Queued — finishing current work first"`. Build source with `build_source(chat_id=..., chat_type="group", user_id=..., user_name=..., thread_id=..., message_id=...)` then `source.shared_session = thread_id is None`. `MessageEvent(text=..., source=..., message_id=message_id(...), allow_gateway_control=False)`. For ticket sessions prepend `fold(store.pending_context(issue))` and `consume_context` only after `handle_message` returns without raising. `first_turn` delegated tickets append the `issue_snapshot` block. Record turn.
- `AddContext` → `store.add_context`.
- `ResolveControl` → approval: `parse_approval_answer`; None → re-post elicitation with options; else `approval_refusal(...)` then `resolve_gateway_approval_card(...)` per Task 1 §8; clarify → `resolve_gateway_clarify(request_id, answer)`.
- `Interrupt` → synthesized `/stop` `MessageEvent` with `allow_gateway_control=True` on the mapped source.
- `Refuse` → `error` activity.
- `AwaitDelegation` → `asyncio` timer; on expiry with no `created` recorded for the issue: reuse `app_sessions_on_issue()[0]` if any else `create_session_on_issue`, record session (`plugin_created=True`), then a `StartTurn` with text `"You have been delegated <identifier>.\n\n" + snapshot`.
- `OpenSessionOnComment` → record a placeholder keyed by `comment_id` **before** calling `create_session_on_comment`, then record the session and `StartTurn` in the ticket session.
- `Fatal` → `self._set_fatal_error(...)`-equivalent per base public API; unregister from listener.
- Auto-start (spec §8.3): on the first `StartTurn` of a delegated generation when `settings.auto_start` and the session `creator` is a human and `state.type ∈ {backlog, unstarted, triage}` → `move_to_first_started`.

- [ ] **Step 1: Write failing tests** with a fake gateway handler (captures `MessageEvent`s), fake client, in-memory store:
  - `test_ack_posted_before_handle_message` (call order).
  - `test_user_text_never_gateway_control` (Review Focus 1) — `prompted` body `"/new"` → captured event has `allow_gateway_control is False`.
  - `test_ticket_session_shared_and_keyed_by_generation`.
  - `test_digest_prepended_and_consumed_only_on_success` (handler raises → items remain).
  - `test_queued_ack_when_turn_running`.
  - `test_stop_synthesizes_control_event`.
  - `test_delegation_wait_opens_session_once` (two `Issue update`s within 5 s → one `create_session_on_issue`).
  - `test_open_session_on_comment_race` (`created` webhook delivered before `create_session_on_comment` returns → ignored, one turn).
  - `test_auto_start_only_for_human_delegation`.
  - `test_refusal_posts_error_no_turn`.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat: adapter inbound path`.

### Task 12: Adapter — outbound path and hooks — RET-66

**Files:**
- Modify: `hermes_linear/adapter.py`
- Create: `hermes_linear/hooks.py`, `tests/test_adapter_outbound.py`

**Interfaces:**
- Produces on `LinearAdapter`: `async send(chat_id, content, reply_to=None, metadata=None) -> SendResult` (resolve the agent session exactly per `docs/hermes-integration-notes.md` Q1 "Send contract": `reply_to` → metadata `reply_to_message_id` → `latest_turn_for_chat(chat_id, thread_id)` → drop+log; turn-error notice → `error`; final → `response`, interim per Task 1 Q1 → `thought`); `async send_typing(chat_id, metadata=None)` (ephemeral `thought "Working…"`, ≤ 1 per `TYPING_MIN_INTERVAL_S`) plus a heartbeat task per running turn every `HEARTBEAT_S`; `format_tool_event(...) -> None` (eat chat-formatted progress); `async _send_exec_approval_prompt(prompt) -> SendResult` (elicitation + `select` with `approval_options()`, `put_control`); `async send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=None) -> SendResult` (elicitation, `select` when choices, `put_control`); `supports_exec_approval_buttons` → True via override. `hooks.py`: `on_pre_tool_call(tool_name, args, task_id, **kw)`, `on_post_tool_call(tool_name, args, result, task_id, duration_ms=0, **kw)` — no-op unless `get_session_env("HERMES_SESSION_PLATFORM") == "linear"`; find the adapter by `HERMES_SESSION_KEY` (adapter registry in `plugin.py`); schedule writes with `loop.call_soon_threadsafe`. Start → ephemeral `action` only if the tool is still running after `FAST_TOOL_SKIP_S`; finish → persistent `action(name, tool_parameter, result_preview)`; `tool_name == "todo"` → `update_session(plan=plan_from_todos(...))`. Adapter lookup: `plugin.py` keeps `ADAPTERS: dict[str, LinearAdapter]` by profile; the hook picks the adapter whose store has `turn(get_session_env("HERMES_SESSION_MESSAGE_ID"))`, else `latest_turn_for_chat(get_session_env("HERMES_SESSION_CHAT_ID"))`. Expiry: `retire_clarify_card(clarify_id, notice)` and the approval-timeout path from Task 1 Q10 post a `thought` ("This request expired.") and `drop_control`. `session_url` set → `update_session(external_urls=[{"label":"Hermes session","url":...}])` once per agent session.

- [ ] **Step 1: Write failing tests** — `test_send_final_is_response_in_asking_session` (two threads, interleaved turns → each response lands on its own agent session); `test_typing_rate_limited`; `test_heartbeat_fires_on_long_turn` (fake clock); `test_approval_prompt_elicitation_select`; `test_clarify_free_text_and_select`; `test_tool_fast_skips_start`; `test_tool_slow_posts_start_and_finish`; `test_hook_ignores_other_platforms`; `test_todo_updates_plan`; `test_external_url_once`; `test_expired_clarify_posts_thought_and_drops_control`; `test_two_adapters_hook_routes_by_turn` (two profiles' adapters registered; hook event for profile B's turn writes only via B's client).
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat: adapter outbound path and tool hooks`.

### Task 13: Registration, setup, CLI, dev install — RET-67

**Files:**
- Modify: `hermes_linear/plugin.py`
- Create: `hermes_linear/setup_cli.py`, `tests/test_plugin_register.py`

**Interfaces:**
- Produces: `register(ctx)` calling `ctx.register_platform(name="linear", label="Linear", adapter_factory=..., check_fn=..., validate_config=..., required_env=["LINEAR_CLIENT_SECRET","LINEAR_WEBHOOK_SECRET"], allowed_users_env="LINEAR_ALLOWED_USERS", allow_all_env="LINEAR_ALLOW_ALL_USERS", setup_fn=interactive_setup, platform_hint=PLATFORM_HINT, emoji="📐", max_message_length=0)`, `ctx.register_hook("pre_tool_call", ...)`, `ctx.register_hook("post_tool_call", ...)`, and the CLI per Task 1 Q7. `PLATFORM_HINT`: "You are working inside Linear. Your final reply is posted as a comment in the thread that asked. Linear issue/user URLs in markdown render as mentions." `interactive_setup()` follows spec §10 (prints categories list and `https://<host>/linear/<profile>/webhook`, the `tailscale funnel --bg --https=8443 --set-path /linear http://127.0.0.1:<port>/linear` command, saves secrets with `save_env_values`, settings with `ctx.set_config`, then token + `viewer` check). `hermes linear status` prints token expiry, app user name, webhook URL, last event time, access mode, degraded/fatal. `hermes linear test` signs a synthetic `AppUserNotification` with the profile secret and POSTs it to the listener, expecting 200.

- [ ] **Step 1: Write failing tests** with a fake `ctx` — `test_register_platform_kwargs`; `test_hooks_registered`; `test_check_fn_false_without_secrets`.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Dev install** — `ln -s ~/git/hermes-linear ~/.hermes/profiles/neo/plugins/linear`, `hermes -p neo plugins enable linear`, configure with the Hermes Test app (`client_id` from `op://hermes/linear-raymond-app/client-id`; secrets into the neo `.env` via `hermes -p neo linear setup`). **Ask Mike to restart the shared gateway** (it drops every bot briefly). Then stop the probe logger, repoint Funnel path `/linear` → `127.0.0.1:8650/linear`, run `hermes -p neo linear test` → 200 and `hermes -p neo linear status` healthy.
- [ ] **Step 6: Commit** `feat: plugin registration, setup wizard and CLI`.

### Task 14: Live smoke, docs, CI — RET-59

**Files:**
- Create: `scripts/smoke.md`, `.github/workflows/ci.yml`
- Modify: `README.md`, spec §12 (record answers)

- [ ] **Step 1: `scripts/smoke.md`** — checklist with expected Linear UI per step: delegate fresh ticket (ack ≤ 10 s, status → In Progress, response in agent thread); reply; second thread via @mention (same Hermes session: agent references thread 1); plain top-level comment (new thread answered); label + status edit then reply (digest appears); risky command → approval pick-list → Allow once; free-text "yes" re-asks; Stop mid-turn; un-delegate → turn stops; re-delegate → memory intact; non-allowlisted user → refusal; a comment `/new` is treated as text.
- [ ] **Step 2: Run the smoke** with Mike on the Hermes Test app; record results (pass/fail per line) in RET-60.
- [ ] **Step 3: CI** — GitHub Actions: Python 3.11 and 3.12, checkout `NousResearch/hermes-agent` at a pinned SHA into `hermes-agent/`, install its requirements, add to `PYTHONPATH`, run `pytest -q` and `python -m hermes_cli.main plugins validate .`.
- [ ] **Step 4: README** — remove the pre-release banner's "target interface" wording for anything that now exists; correct any command/setting that changed during implementation; add a "Development" section (`scripts/dev-env.sh`, `pytest`, dev symlink install).
- [ ] **Step 5: Commit** `docs: smoke checklist, CI, README for 0.1.0`; tag `v0.1.0`.

Milestone exit (spec §13 dogfood: a real profile moves its Linear work onto the plugin, `linear-orion` route retired, one week without a missed 10 s ack) is tracked in RET-60 (`M1 exit · Dogfood`) and is outside this plan.
