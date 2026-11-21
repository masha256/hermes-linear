# Hermes integration notes (Task 1 probe)

Findings for the gateway contracts the Linear adapter depends on. Source: `~/.hermes/hermes-agent`
at `3b42157858`; paths below are relative to it. "Probe" lines name the throwaway scripts in
`scratch/` (gitignored) that exercised the answer in-process with the worktree `.venv`. Nothing was
enabled in a real profile and the running gateway was not touched; answers that need a live
gateway are marked **Live-verify in T13**.

## Q1. Interim vs final sends

**Question.** Does `send()` metadata distinguish interim commentary / tool-progress lines from the
final reply?

**Answer.** Yes. Every mid-turn send carries `metadata["_interim_send"] = True`; the final reply
does not.

- `gateway/run.py:480-487` `_interim_metadata()`: "every mid-turn send MUST carry this marker".
  Used for heartbeats, stuck warnings, approval text prompts, background-review notes, approval
  timeout notices (`gateway/run_turn.py:1414,2121`, `gateway/run_turn_runner.py:420,827`,
  `gateway/run_turn_runner_approval_settle.py:73`).
- Commentary (interim assistant text between tool calls) goes through the stream consumer's
  `_send_commentary`, which sets `_interim_send` (`gateway/stream_consumer_fallback.py:346-364`).
  For a non-editing adapter a consumer is created for commentary even with text streaming off
  (`gateway/run_turn_runner.py:271-304`). Its `reply_to` is `None` for `linear` (only
  buzz/slack/mattermost/feishu get one, line 361); the triggering message id rides in
  `metadata["reply_to_message_id"]` (`gateway/stream_consumer.py:261-272`).
- Full-command preface before a truncated approval card is `_interim_send`
  (`gateway/platforms/base_exec_approval.py:200`).
- The final reply is `_send_final_text` with `_mark_notify_metadata(thread_meta)`: no
  `_interim_send`, `notify: True`, `reply_to` = the turn's inbound `message_id`
  (`gateway/platforms/base.py:4490`, `153-157`). Inline command replies (`/stop`) use the same
  shape (`base.py:3488-3490`).
- Tool progress never reaches `send()` when `format_tool_event` returns `None`
  (`gateway/stream_dispatch.py:71-81`).
- Unmarked sends that are not the turn's final reply: the turn-error notice
  (`base.py:4329`, no `notify`), slash-command acks (`gateway/run_busy.py:1091`) and busy acks.
  The adapter never triggers the last two: user text is sent with `allow_gateway_control=False` and
  Q5 keeps the gateway from seeing busy input.
- Probe `probe_commentary.py`: commentary send metadata =
  `{"thread_id": null, "reply_to_message_id": "as1:act1", "_interim_send": true}`, `reply_to=None`.
  Probe `probe_adapter.py`: final send `reply_to="as1:act2"`, metadata `{"notify": true}`.

- Streaming previews are **unmarked**. When text streaming is on, the consumer's first preview is
  `adapter.send(..., metadata={…, "reply_to_message_id", "expect_edits": True})` with no
  `_interim_send` (`gateway/stream_consumer_transport.py:460-462`, `gateway/stream_consumer.py:261-272`),
  and later deltas arrive through `edit_message`. Whether the consumer streams deltas depends on
  `SUPPORTS_MESSAGE_EDITING`. The attribute is not defined on the base class, so it defaults to
  `True` through `getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)`
  (`gateway/run_turn_runner.py:276-282`). The adapter overrides `edit_message` (Q10), so with
  `streaming.enabled` an adapter that keeps the default would get partial text as an unmarked
  `send()`, and the rule below would post it as the `response`.

Adapter contract:

- `SUPPORTS_MESSAGE_EDITING = False` as a class attribute, the same as `signal`, `wecom`, `qqbot`,
  `weixin`, `bluebubbles` and `photon`. Effects of the flag:
  - `consumer_stream_deltas` is False whatever `streaming.enabled` says
    (`run_turn_runner.py:279-282,303`), so no delta previews.
  - A commentary-only consumer still works. It is built with `on_missing_cursor="fallback"`, which
    for a non-editing adapter means an empty cursor instead of the `RuntimeError` skip
    (`run_turn_runner.py:288`, `gateway/run_turn.py:593-597`).
  - The approval-timeout edit (Q10) still reaches the adapter. `_edit_card` looks up
    `getattr(adapter, "edit_message", None)` and never reads the flag
    (`gateway/run_turn_runner_approval_settle.py:99-110`).
  - Plugin-message edits are refused (`gateway/plugin_messages.py:58-62`), which the adapter does not
    need. Those three call sites are the only readers outside platform plugins.
- Rule: `_interim_send` → `thought`; otherwise → `response`. Resolve the Linear session from
  `reply_to`, falling back to `metadata["reply_to_message_id"]`. A send that resolves to no session
  (no `reply_to`, no `reply_to_message_id`, e.g. Q4's pairing/decline text) is dropped and logged.
- Residual: proxy mode (`GATEWAY_PROXY_URL` / `gateway.proxy_url`) builds its consumer with
  `on_missing_cursor="fallback"` when streaming is on, whatever the flag says
  (`run_turn.py:629-660,721-723`). It then sends one `expect_edits: True` preview, its edits fail,
  and the tail arrives as a fallback send (`stream_consumer_transport.py:525-605`). Linear does not
  support that combination. The adapter treats a send whose metadata has `expect_edits` as a
  `thought` and logs a warning.
- The adapter task adds `test_supports_message_editing_false`, asserting
  `LinearAdapter.SUPPORTS_MESSAGE_EDITING is False`. It also adds a classification test: a send with
  `expect_edits` → `thought`; an unmarked send with `notify` → `response`.

Caveat: when commentary text matches the final answer, the gateway's final-send dedup may skip the
final send (`gateway/run_turn_runner.py:286-288`). The adapter needs a turn-end fallback (see Q5:
`on_processing_complete`). If no `response` was posted for the turn, promote the last `thought` to
the `response`. **Live-verify in T13.**

Decision: default

## Q2. Tool hooks in gateway turns

**Question.** Do `pre_tool_call`/`post_tool_call` fire for gateway-session tools with
`get_session_env("HERMES_SESSION_PLATFORM") == "linear"` and `HERMES_SESSION_KEY` /
`HERMES_SESSION_MESSAGE_ID` bound?

**Answer.** Yes.

- The gateway binds session ContextVars per turn: `gateway/run.py:3933-3966` `_set_session_env`
  (`platform=source.platform.value`, `session_key=context.session_key`,
  `message_id=source.message_id`). **`HERMES_SESSION_MESSAGE_ID` is `source.message_id`, not
  `event.message_id`**, so the adapter must pass `message_id=` to `self.build_source(...)`
  (`gateway/platforms/base.py:4701-4721`).
- Hooks fire from `model_tools.handle_function_call` (pre via `_pre_dispatch_guards` →
  `hermes_cli/plugins.py:1116-1135`; post at `model_tools.py:697-718`). Agent-loop tools are covered
  by `agent/tool_executor.py:666` (pre) and `agent/inline_tool_executors.py:49` (post).
- Tool workers propagate context (`agent/tool_executor.py:944`, `propagate_context_to_thread`), and
  hook callbacks run on a timeout worker created with `contextvars.copy_context()`
  (`hermes_cli/plugins_dispatch.py:388-412`). `pre_tool_call` fails closed on timeout
  (`plugins_dispatch.py:60`), and the default timeout is 30 s (`hermes_cli/plugins.py:236-261`).
  Hooks must only enqueue and never do network I/O inline.
- Kwargs: pre = `tool_name, args, task_id, session_id, tool_call_id, turn_id, api_request_id,
  conversation_id, middleware_trace, telemetry_schema_version`; post adds `result, duration_ms,
  status, error_type, error_message`. Use `tool_call_id` to match a post to its pre.
- Probe `probe_hooks.py` loaded a plugin copy whose `register()` adds both hooks through the real
  `PluginManager`. It bound vars with `set_session_vars(platform="linear", …)` and dispatched a real
  `read_file` on a `DaemonThreadPoolExecutor` + `propagate_context_to_thread`. Both hooks saw
  `platform=linear`, `key=agent:main:linear:dm:ISSUE-1:1`, `msg=sess-1:act-1`. Post had
  `status=ok`, `duration_ms=86`, `result` was a `str`.

Decision: default

## Q3. `ctx.get_config` per profile under `gateway.multiplex_profiles`

**Question.** Does `ctx.get_config()` inside a multiplexed secondary profile's adapter return that
profile's settings?

**Answer.** Yes, but only while it is called inside that profile's runtime scope. The value follows
the **calling context**, not the `ctx` object.

- `hermes_cli/plugins_context.py:73-83`: `get_config(key, default=None)` reads
  `plugins.entries.<plugin_id>.settings.<key>` from `load_config_readonly()`, which resolves
  `get_hermes_home()`. That uses the ContextVar override `set_hermes_home_override`
  (`hermes_constants.py:32-50`).
- Secondary adapters are constructed and connected inside `_profile_runtime_scope(profile_home)`
  (`gateway/run_adapters.py:1322-1324` create, `1349-1350` connect; scope =
  `gateway/run.py:1789-1809`, which also sets the profile secret scope).
- Probe `probe_config.py` used two throwaway homes with different `client_id`s. Results:
  no override → `client-default`; override=second → `client-second`; an asyncio task created under
  the override → `client-second`; a plain `threading.Thread` → `client-default` (context is not
  inherited).

Constraint: snapshot all settings and secrets in the adapter's `__init__`/`connect()`. Never call
`ctx.get_config` or `get_secret` from the shared webhook listener or worker threads. The listener
runs in whichever profile's context started it.

Decision: default

## Q4. Access gate

**Question.** With `enforces_own_access_policy` True and an email allowlist, does the gateway's env
allowlist (`LINEAR_ALLOWED_USERS` vs `user_id`) still deny?

**Answer.** Yes. When `LINEAR_ALLOWED_USERS` is non-empty, the gateway never consults the
own-policy path and matches `source.user_id` literally.

- `gateway/authz_mixin.py:785-799`: the own-policy branch (`enforces_own_access_policy` →
  `_own_policy_authorizes`) runs **only when no env allowlist is set**.
- With an env allowlist: `allowed_ids = env set ∪ adapter.resolved_allowlist_user_ids()`, then
  `"*" in allowed_ids or user_id in allowed_ids` (`authz_mixin.py:811-818`, resolver read at
  `598-614`). This is the hook Discord uses for username entries
  (`plugins/platforms/discord/adapter.py:5010-5016`).
- Without an env allowlist, the own policy is trusted only when it is `"allowlist"`. For
  `chat_type="dm"` it re-checks `adapter._is_dm_allowed(user_id)`. For group types it
  **rubber-stamps True** (`authz_mixin.py:542-562`). `LINEAR_ALLOW_ALL_USERS` is honoured first,
  via `allow_all_env` (`authz_mixin.py:774`).
- Probe `probe_authz.py` drove `GatewayAuthorizationMixin._is_user_authorized` with a stub runner,
  a stub `OwnAccessPolicyMixin` adapter, and a registered `linear` entry with
  `allowed_users_env="LINEAR_ALLOWED_USERS"`:

  | env | adapter | chat_type | listed uuid | email-resolved uuid | stranger |
  |---|---|---|---|---|---|
  | `uuid,alice@example.com` | `resolved_allowlist_user_ids()` = {email uuid} | dm/group | True | True | False |
  | `uuid,alice@example.com` | no resolved ids | dm/group | True | **False** | False |
  | unset | own policy allowlist | dm | True | True | False |
  | unset | own policy allowlist | group | True | True | **True** |
  | `LINEAR_ALLOW_ALL_USERS=true` | — | any | True | True | True |

Contract the adapter must implement:

- `register_platform(..., allowed_users_env="LINEAR_ALLOWED_USERS", allow_all_env="LINEAR_ALLOW_ALL_USERS")`.
- `class LinearAdapter(OwnAccessPolicyMixin, BasePlatformAdapter)` with
  `ALLOW_ALL_ENV_PREFIX = "LINEAR"`. In `__init__`/`connect`: `self._dm_policy = self._group_policy
  = "allowlist"`, `self._allow_from: set[str]` = every admitted UUID (UUID entries from env and
  settings, plus emails resolved to UUIDs), `self._group_allow_from = set()`.
- Override `_entry_matches(entries, target)` to return `"*" in entries or target in entries`, and
  add `"*"` to `_allow_from` when settings say `allow_all_users: true` or list `*`.
- `def resolved_allowlist_user_ids(self) -> set[str]` returns UUIDs from settings entries,
  email-resolved UUIDs, and `"*"` under settings allow-all. Without this, a non-empty
  `LINEAR_ALLOWED_USERS` denies everyone listed only by email or only in settings.
- Every `SessionSource` uses `chat_type="dm"` (`self.build_source(chat_id=…, chat_type="dm",
  user_id=<Linear UUID>, user_name=…, thread_id=…, message_id=…)`). Then the gateway re-checks
  `_is_dm_allowed` instead of rubber-stamping. Keys stay user-independent.
  `build_session_key` (`gateway/session.py:700-746`) gives `<ns>:linear:dm:<chat_id>[:<thread_id>]`
  for a `dm` with a `chat_id`. The participant is added only when `chat_id` is empty (line 726), and
  `<ns>` is `agent:main` for the default profile or `agent:<profile>` otherwise (`session.py:679-688`).
  With the spec §5 sources:
  - delegated (`chat_id=<issueId>:<generation>`, no thread) → `agent:<ns>:linear:dm:<issueId>:<gen>`
  - mention (`chat_id=<issueId>`, `thread_id=<agentSessionId>`) →
    `agent:<ns>:linear:dm:<issueId>:<agentSessionId>`

  Probe output: `agent:main:linear:dm:ISSUE-UUID:1` and
  `agent:main:linear:dm:ISSUE-UUID:agentSession-9`. Delegated and mention keys could collide only if
  an agentSessionId equalled a generation integer, and agentSessionIds are UUIDs. `shared_session` is moot for
  `dm`, and `build_source` cannot set it anyway. `SessionSource` is not in the public API
  (`hermes_cli/data/plugin_api.json`).
- `GATEWAY_ALLOWED_USERS` is an env allowlist too. If it is set, the own-policy branch is skipped
  even while `LINEAR_ALLOWED_USERS` is empty (`authz_mixin.py:788-798`). The adapter's resolved ids
  are merged only `if platform_allowlist`, so they are not consulted either
  (`authz_mixin.py:811-818`). Email-only and settings-only Linear users are then denied, and only
  literal UUIDs in `GATEWAY_ALLOWED_USERS` pass. This fails closed. The adapter logs a warning at
  `connect()` when `GATEWAY_ALLOWED_USERS` is set and `LINEAR_ALLOWED_USERS` is not. Setup docs tell
  operators who use the global list to also set `LINEAR_ALLOWED_USERS` (one listed UUID is
  enough to enable the resolver union).
- Side effect of `chat_type="dm"`: a denied sender is "pairable", so `_get_unauthorized_dm_behavior`
  runs (`gateway/run_inbound.py:234-244`). With `dm_policy="allowlist"` the default is `ignore`
  (`authz_mixin.py:853-854`). An explicit per-platform `unauthorized_dm_behavior`, or any global
  value other than `pair`, wins first (`authz_mixin.py:836-844`). `decline` would then post the
  decline text, and `pair` a pairing code, through `adapter.send(chat_id, text)` with no `reply_to`
  and no metadata (`run_inbound.py:80-131`). Those sends resolve to no Linear session and are dropped
  (Q1 rule).

Decision: fallback — admit email/settings-only entries through `resolved_allowlist_user_ids()` (consulted when `LINEAR_ALLOWED_USERS` is set) plus `OwnAccessPolicyMixin` allowlist (consulted when it is not), with `chat_type="dm"` on every source

## Q5. Busy session

**Question.** Is a second `handle_message` for a busy shared session queued (not interrupting) by
default?

**Answer.** No. The gateway default is **interrupt**.

- `gateway/run.py:3332-3333` `_busy_input_mode = _busy_text_mode = "interrupt"`.
  `gateway/run_runtime_init.py:16-17` then loads the process-wide modes from
  `HERMES_GATEWAY_BUSY_INPUT_MODE` (env wins over `display.busy_input_mode`;
  `gateway/run_config_loaders.py:310-314`), plus the legacy `HERMES_GATEWAY_BUSY_TEXT_MODE` /
  `display.busy_text_mode` (`run_config_loaders.py:316-327`). Per-profile `display.busy_input_mode`
  overrides that (`run_config_loaders.py:329-341,399`). None of these is per platform, and the adapter
  cannot rely on any of them. Probe `probe_busy_mode.py`: `_busy_modes_from_config({})` →
  `('interrupt','interrupt')`.
- With the shared runtime (always built: `gateway/run_runtime.py:198`), a non-command busy input
  skips the adapter's busy handler. `base.py:3982-3985` `_shared_runtime_owns_input` →
  `dispatch_busy_burst` → `dispatch_shared_busy` calls the runner's message handler
  (`gateway/session_ingress.py:501-520`). The runner then interrupts or redirects the running turn
  (`gateway/run_inbound.py:800-860`, `_hm_busy_interrupt` 749-772) and sends a busy ack. An
  installed `set_busy_session_handler` (`base.py:2292`) is therefore **not** a reliable queueing
  lever, and the runner overwrites it at wiring time anyway (`gateway/run_adapters.py:1385`).
- Probe `probe_adapter.py` with no runner: the base layer alone queues (`pending queued = True`).
  That is the runner-less path, not production.

Adapter rule: keep a per-session-key FIFO in the adapter. Call `handle_message` for a key only
when the adapter has no turn of its own in flight for it. Release the next item from
`on_processing_complete(event, outcome)` (`base.py:3409`). It fires with SUCCESS/FAILURE
(`base.py:4535`) and CANCELLED/FAILURE (`base.py:4553-4557`). Call `super()` to keep the base
reaction behaviour. Probe: hook fired `('complete','first','cancelled')` after `/stop` and
`('complete','go','success')` after a normal turn. Whether `_message_handler` returns only after
the full turn under the shared runtime (so the hook marks the real end) is **Live-verify in T13**.

Decision: fallback — adapter-side per-session FIFO gated on `on_processing_complete`; no busy-session handler

## Q6. Stop

**Question.** Does a synthesized `MessageEvent(text="/stop", allow_gateway_control=True)` on the
session's source interrupt the running turn, and does the gateway then emit a stop notice through
`send()`?

**Answer.** Yes for both. The notice text varies by case and never carries partial output.

- `/stop` is a command, so it bypasses the shared-runtime path (`base.py:3956-3958` requires
  `not event.get_command()`). `should_bypass_active_session` → `_dispatch_active_session_command`
  (`base.py:3996-4000`, `3892-3913`) runs the runner handler inline, sends its reply via
  `_dispatch_inline_reply` (`base.py:3477-3492`), and then cancels the session task (pending
  input kept: `discard_pending=False`).
- The runner's stop handler interrupts and clears the run (`gateway/run_busy.py:1181-1189`,
  `gateway/slash_commands.py:430-496`). The plugin hook `agent_loop_stopped(session_key, platform,
  reason, …)` also fires (`gateway/run_agent_cache.py:564-578`). The reply is one of these
  (`locales/en.yaml:463-467`):
  - `EphemeralReply(t("gateway.stop.stopped"))` "⚡ Stopped. You can continue this session." This
    covers a running agent, a chat-scope/sibling run, or background delegations
    (`slash_commands.py:455,478,485`).
  - `EphemeralReply(t("gateway.stop.stopped_pending"))` when the agent was still a pending sentinel
    (`slash_commands.py:449-452`).
  - `EphemeralReply(t("gateway.stop.force_stopped_pending"))` from the runner's busy early intercept
    on a pending sentinel (`gateway/run_inbound.py:825-828`).
  - Plain `t("gateway.stop.no_active")` "No active task to stop." when nothing ran
    (`slash_commands.py:496`).

  The adapter must not match on text, only on `reply_to`.
- `EphemeralReply` deletion: `_unwrap_ephemeral` keeps a TTL > 0 only when the adapter overrides
  `delete_message` (`base.py:3463-3475`). `_dispatch_inline_reply` then schedules
  `_schedule_ephemeral_delete` on the sent `message_id` (`base.py:3491-3492`). Contract: the adapter
  **does not override `delete_message`** (base default `base.py:2652`), so the TTL is forced to 0 and
  nothing tries to delete the Linear activity. The adapter task adds a test asserting
  `LinearAdapter.delete_message is BasePlatformAdapter.delete_message`.
- The notice arrives as `send(chat_id, text, reply_to=<stop event message_id>,
  metadata={…thread meta, "notify": True})`, with no `_interim_send`. The cancelled turn's own reply
  is never sent. Probe `probe_adapter.py`: handler saw `['first', '/stop']`; one send
  `"⚡ Stopped. You can continue this session."`, `reply_to="as1:stop"`, `{"notify": true}`; the
  first turn completed `cancelled` and sent nothing.
- The runner authorizes `/stop` like any input. The source's `user_id` must pass Q4. Slash access
  gating is off unless an operator sets `allow_admin_from` (`gateway/slash_access.py:1-10`).

Adapter rule: synthesize `/stop` only while the adapter knows a turn is in flight (else post
`Stopped.` itself). Give it a unique `message_id` and recognize the notice by
`reply_to == that id`. Post the §8.5 `response` from it: the gateway text is dropped and
"Done so far" comes from the adapter's own record of rendered actions/thoughts, because the gateway
supplies none. Keep the 30 s timeout as a safety net. The runner path (vs this runner-less probe)
is **Live-verify in T13**.

Decision: default

## Q7. CLI at deferred load

**Question.** Does `ctx.register_cli_command` from a deferred `kind: platform` plugin register
`hermes linear …`?

**Answer.** Yes, and no eager entry is needed. Only **bundled** platform plugins are deferred
(`hermes_cli/plugins_discovery.py:312-319`). A user-installed `kind: platform` plugin listed in
`plugins.enabled` loads eagerly (`plugins_discovery.py:320-324` → `hermes_cli/plugins.py:545-557`).
Even deferred ones are materialized by name before argparse reads `_cli_commands`
(`hermes_cli/main.py:2691-2714`, `3146-3172`). `register_cli_command(name, help, setup_fn,
handler_fn=None, description="")` is at `hermes_cli/plugins_context.py:442-453`.

Probe `probe_load.py copy` used a throwaway HERMES_HOME with `plugins.enabled: [linear]` and a
plugin copy whose `register()` calls `register_cli_command("linear", …)`. Result:
`source=user kind=platform deferred=False enabled=True`, `cli_commands: ['linear']`, and both tool
hooks registered.

Decision: default

## Q8. Approval prompt contract

**Question.** Which `ExecApprovalPrompt` fields and `actions` tuples does
`_send_exec_approval_prompt` receive? What are the exact `resolve_gateway_approval_card` signature
and choice strings?

**Answer.**

- Overriding `_send_exec_approval_prompt` is the opt-in (`supports_exec_approval_buttons`,
  `base.py:2765-2769`). The runner then calls `adapter.send_exec_approval(chat_id=<status chat>,
  command=<redacted>, session_key=<turn session key>, description=…, metadata=<thread meta>,
  request_id=…, allow_permanent, allow_session, smart_denied)` with a 15 s send budget
  (`gateway/run_turn_runner.py:749-762`). The base builds the prompt (`base.py:2771-2792`).
- `ExecApprovalPrompt` (`gateway/platforms/base.py:1570-1588`, public module):
  `chat_id: str, session_key: str, text: str, actions: list[tuple[label, choice, style]],
  command: str, description: str, smart_denied: bool, metadata: Optional[dict],
  request_id: Optional[str]`, plus property `choices`.
- `actions` (`base.py:2752-2763`): `once` always; `session` if `allow_session and not
  smart_denied`; `always` only alongside `session` when `allow_permanent`; `deny` always.
  Styles: `once`→`primary`, `deny`→`danger`, else `""`. Labels are localized
  (`base_exec_approval.py:53-60`). A command over `_EA_CMD_BUDGET` (3000, `base.py:2694`) is first
  sent whole as an `_interim_send`. If that fails, the card is **Deny-only**
  (`base_exec_approval.py:164-176`, `base.py:2785-2789`). Probe `probe_adapter.py`:
  `[('Allow Once','once','primary'), ('Allow Session','session',''), ('Always Allow','always',''),
  ('Deny','deny','danger')]`; `allow_permanent=False` → `['once','session','deny']`;
  `smart_denied` → `['once','deny']`.
- Resolve: `tools.approval.resolve_gateway_approval_card(session_key: str, choice: str,
  request_id: Optional[str], *, actor_id=None, admin_ids=(), extra=None) -> int`
  (`tools/approval.py:197-221`). It returns the number resolved: 0 means no `request_id`, already
  settled, a non-deny choice on a Deny-only card, or an actor the requester/approvers rule refuses.
  Choice strings: `"once" | "session" | "always" | "deny"`.
- Pre-check: `gateway.approval_actor.approval_refusal(session_key, actor_id, *, admin_ids=(),
  request_id=None, extra=None) -> Optional[str]` returns `None` when allowed, else the requester's
  display name. Pass it to `refusal_text(requester) -> str` (`gateway/approval_actor.py:78-93`).

Contract: render exactly `prompt.choices ∩ {"once","session","deny"}`, in that order. Spec §8.4
omits `always`, and Deny-only or `smart_denied` prompts must stay narrowed. Store `request_id` and
`session_key` in `pending_controls`, then call
`resolve_gateway_approval_card(prompt.session_key, choice, prompt.request_id, actor_id=<Linear UUID>)`.
Return `SendResult(success=True, message_id=f"approval:{request_id}")`. That id comes back on
expiry (Q10).

Decision: default

## Q9. Plugin load as package

**Question.** Do relative imports from the root `__init__.py` into `hermes_linear/` work for a
directory plugin?

**Answer.** Yes. `hermes_cli/plugins_loader.py:814-848` imports the directory as
`hermes_plugins.<slug>` via `spec_from_file_location(module_name, <dir>/__init__.py,
submodule_search_locations=[plugin_dir])` and sets `__path__`/`__package__`, so
`from .hermes_linear.plugin import register` resolves. Probe `probe_load.py real` (worktree
symlinked into a throwaway HERMES_HOME) gave `enabled=True error=None`, with modules
`hermes_plugins.linear`, `hermes_plugins.linear.hermes_linear`, and
`hermes_plugins.linear.hermes_linear.plugin` (+ `.constants` in the copy run).

Decision: default

## Q10. Fatal errors and expiry

**Question.** What is the public way for an adapter to report a fatal error, and how do approval
timeouts reach the adapter?

**Answer.**

- Fatal errors (pattern used by `plugins/platforms/irc/adapter.py:158-160,272-273`, `line`, `teams`):
  - At connect: `self._set_fatal_error(code, message, retryable=bool)` then `return False`
    (`base.py:2087-2091`).
  - At runtime: `self._set_fatal_error(...)` then `await self._notify_fatal_error()`
    (`base.py:2109-2121`). That calls the runner handler set through `set_fatal_error_handler`
    (`base.py:2040`, wired at `gateway/run_adapters.py:1384`).
  - `retryable=True` queues a background reconnect. `retryable=False` leaves the platform `fatal`
    (`gateway/run_adapters.py:232-243`, `367-395`).
  - `self._mark_connected()` on success clears it (`base.py:2058-2064`).
  - Read back via `has_fatal_error` / `fatal_error_code` / `fatal_error_message` /
    `fatal_error_retryable` (`base.py:2025-2028`).
  - `self._…` calls are not flagged by the private-access lint, which excludes `self`/`cls`
    receivers (`hermes_cli/plugin_validate_private_imports.py:181-189`).
  - Caveat: a *retryable* fatal that fails to enter the reconnect queue can make the runner stop the
    gateway (`run_adapters.py:336-361`). Prefer `retryable=False` for config errors (bad secret).
- Approval timeout:
  - After a delivered card, the runner registers a settle hook (`gateway/run_turn_runner.py:768-770`
    → `gateway/run_turn_runner_approval_settle.py:26-62`). On `"timeout"` it calls
    `adapter.edit_message(chat_id, card_message_id, body)` with the card's `SendResult.message_id`.
  - If that returns not-success or raises, it sends the notice as
    `adapter.send(chat_id, notice, metadata={…, "_interim_send": True})` (`approval_settle.py:63-111`).
  - The notice text is `"⌛ Approval timed out after {window} — the command was NOT run. …"`
    (`locales/en.yaml:623`). Answered, interrupted, and notify-failed outcomes post nothing.
- Clarify end without an answer: `async retire_clarify_card(self, clarify_id: str, notice: str)`,
  looked up on the adapter **class** (`gateway/run_turn_runner.py:697-704`; notice
  `"⏳ This prompt expired — please send a new request."`, `en.yaml:678`). It is also called with a
  `gateway.clarify.answered` notice when the gateway's own text intercept answered
  (`gateway/run_inbound.py:410-416,450-456`). That path never runs for Linear (user text has
  `allow_gateway_control=False`), but the method must ignore a `clarify_id` already resolved in
  `pending_controls`.

Contract:

- `edit_message(chat_id, message_id, content, *, finalize=False) -> SendResult`: on an
  `approval:<request_id>` id, post the adapter's **own** expiry `thought` (fixed text, e.g. "Approval
  timed out; the command was not run."), clear the `pending_controls` row, and return
  `SendResult(success=True, message_id=message_id)`. It never posts or echoes `content`. That body is
  `_timed_out_card_body`, which is the localized notice plus the flagged reason (capped at 500) and
  the redacted command in a code fence (`gateway/run_turn_runner_approval_settle.py:83-96`,
  `locales/en.yaml:624`). Echoing it would re-publish the command into the ticket. Any other id
  returns `SendResult(success=False, error="Not supported")`. This override coexists with
  `SUPPORTS_MESSAGE_EDITING = False` (Q1): `_edit_card` calls it without reading the flag
  (`approval_settle.py:99-110`).
- `async def retire_clarify_card(self, clarify_id, notice)`: if the row is still pending, post the
  expiry `thought` and clear it.

Decision: default
