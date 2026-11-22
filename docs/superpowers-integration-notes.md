# Superpowers integration notes (Task 0 probe, RET-88)

Answers to the host-integration questions that Task 1 (tool runtime) and Task 6 (skill/tool
registration) build on. Source: `~/.hermes/hermes-agent` at `3b42157858`, read-only; paths below are
relative to it. Spec context: `docs/superpowers/specs/2026-11-21-hermes-linear-superpowers-design.md`
§4, §5.

**Probe setup.** A copy of this worktree's plugin went into a throwaway `HERMES_HOME`
(`~/.hermes/profiles/neo/cache/scratch/t0probe/home`, plugin enabled only there). The copy's
`register()` also registered a dummy tool `linear_probe` and a skill `manage-superpowers`. The
scripts are in `scratch/` (gitignored): `t0_probe.py`, `t0_collision_control.py` and `t0_neo_cfg.py`
(reads the neo `config.yaml` read-only). They ran in-process with the worktree `.venv`. No real profile
was changed, and the gateway was not touched. Answers that need a live gateway are marked
**Live-verify**.

## Q1. Toolset enablement

**Question.** Is a plugin tool registered with `toolset="linear"` offered to a gateway session on the
`linear` platform without extra config?

**Answer.** Yes, and on every other platform too. But in the neo profile the name `linear` collides with
the Linear MCP server, and the collision hides every MCP Linear tool. Use a different toolset name.

- Plugin toolsets are on by default on every platform. `_get_platform_tools` ORs in
  `_enabled_plugin_toolsets` (`hermes_cli/tools_config.py:620-621`). A plugin toolset is enabled when
  either holds (`tools_config.py:521-528`):
  - it is on the saved list;
  - it is not in `_DEFAULT_OFF_TOOLSETS` (`tools_config.py:111`, only spotify/a2a/… are listed) and
    not in `known_plugin_toolsets.<platform>`.

  `known_plugin_toolsets` is written only when a user saves through `hermes tools`
  (`tools_config.py:803-805`). The only platform restriction is discord (`hermes_cli/toolset_scope.py:7`).
- Using the platform name as the toolset name also puts the tools in the implicit
  `hermes-<platform>` bundle (`toolsets.py:335-352`). That is not needed: the plugin-toolset path above
  already enables them.
- **The collision.** An MCP server named `linear` registers its tools under `mcp-linear` and a bare alias
  `linear → mcp-linear` (`tools/mcp_tool_registration.py:163,365`). `_merge_mcp_servers` adds the server
  name `linear` to the enabled set (`tools_config.py:734-754`). `get_toolset("linear")` then resolves a
  plugin toolset of that name before it consults the alias (`toolsets.py:297-305`), so the MCP tools are
  never selected. The neo profile has exactly this server (`mcp_servers.linear`,
  `https://mcp.linear.app/mcp`).
- **With neo's defaults the tools are always deferred behind Tool Search.** The comment at
  `model_tools.py:545-547` speaks of a size threshold, but the gate is `should_activate`
  (`tools/tool_search.py:204-213`): unless `enabled == "off"`, it activates whenever *any* deferrable
  tool exists. The default is `tools.tool_search.enabled: "auto"` (`hermes_cli/config_defaults.py:1970-1978`,
  "auto" is an alias of "on"), and neo's `config.yaml` has no `tool_search` override. Plugin tools are
  deferrable (`tools/tool_search.py:150-162`: not core, not a direct-surface toolset). So
  `get_tool_definitions` replaces `linear_*` with the `tool_search`/`tool_describe`/`tool_call` bridge
  (`model_tools.py:549-559`). The probe agreed: `linear_probe` was in the catalog listing, not in the
  direct schemas.
  - A direct call to a deferred name is rejected as an unknown tool; deferred tools are reachable only
    through the `tool_call` bridge (`agent/prompt_builder.py:615-624`).
  - `tool_call` is unwrapped to the real tool name before the pre/post hooks, middleware and dispatch
    run (`model_tools.py:928-947`). So `hooks.py` and the handler still see `linear_*`, never the
    bridge name.
- Probe (`t0_probe.py`):
  - With `toolset="linear"`, `linear_probe` is selected on linear, telegram and cli.
  - With an MCP `linear` server and its alias added, `mcp_linear_list_issues` is **not** selected.
  - Control without the plugin toolset (`t0_collision_control.py`): it **is** selected.
  - With `PROBE_TOOLSET=hermes_linear`, both are selected.
  - `t0_neo_cfg.py` reads neo's real config: the tool is enabled on linear, slack, cli and telegram,
    with no config change.

**Decision: default (no config) with a renamed toolset.**
- Task 6 registers every tool with `toolset="hermes_linear"`, not `"linear"`. Do not use a `hermes-`
  prefix: those names are treated as platform bundles (`model_tools.py:306`,
  `tools_config.py:718`).
- The README does not need a config key. It should note two things:
  - the tools are on by default on every platform, and writes are refused outside Linear sessions
    (spec §4.3);
  - a user who later saves a toolset list with `hermes tools` controls them under `hermes_linear` in
    `platform_toolsets.<platform>`.
- **Task 6: the skill must tell the agent to reach the tools through the bridge.** It says to call
  `tool_describe` with the exact tool name to load the schema, then
  `tool_call(name="linear_issue_get", arguments={...})`, and so on for each tool. It also says plainly
  that calling `linear_issue_get(...)` directly fails as an unknown tool. The skill lists all seven exact
  names (Q5).
- Fallback, if a toolset named after the platform is ever needed: document
  `platform_toolsets.<platform>: [..., hermes_linear]`.

## Q2. Session env inside a sync tool handler

**Question.** Does `get_session_env("HERMES_SESSION_KEY")` / `("HERMES_SESSION_PLATFORM")` resolve
inside a sync tool handler run by `registry.dispatch`?

**Answer.** Yes, on both the sequential and the concurrent tool paths. The handler also receives
`session_id` and `task_id` as keyword arguments.

- The turn binds the session contextvars on the gateway loop, inside the turn task:
  `gateway/run_turn_prepare.py:754` calls `_set_session_env`, which calls `set_session_vars`
  (`gateway/run.py:3933-3960`). `get_session_env` reads the contextvar first
  (`gateway/session_context.py:230-235`).
- `run_sync` goes to the executor through `_run_in_executor_with_context`, which uses
  `copy_context().run` (`gateway/session_runtime_workers.py:136`, `gateway/run.py:3981-3985`). That
  carries the vars to the turn thread.
- Both tool paths run the call on a `DaemonThreadPoolExecutor` worker wrapped by
  `propagate_context_to_thread`, which snapshots the context (`tools/thread_context.py:38-45`):
  - **sequential** path: a single-worker executor per call (`agent/tool_executor.py:944-945`);
  - **concurrent** path: the batch pool (`agent/tool_executor.py:1418`).

  The middleware chain that ends in `handle_function_call` (`agent/agent_runtime_helpers.py:2544`) runs
  inside that worker.
- The `pre_tool_call` hook and the handler run in the same `handle_function_call` frame
  (`model_tools.py:966,981`, dispatch at `:863`). That is why m1's hooks already see these values.
- Context kwargs are matched to the handler's signature (`tools/registry.py:1077-1080`). The probe
  handler received `session_id`, `task_id`, `conversation_id` and `user_task`.
- Delegated subagents copy the context too (`tools/delegate_tool_dispatch.py:176`,
  `tools/delegate_tool_child_run.py:944`), so their tool calls see the parent's Linear session.
- Probe: on all three cases (`seq` = `handle_function_call` called directly on the turn thread, `par` =
  pool plus `propagate_context_to_thread`, `dispatch` = `registry.dispatch`), the handler read
  `HERMES_SESSION_KEY=agent:main:linear:dm:as1`, `HERMES_SESSION_PLATFORM=linear` and
  `HERMES_SESSION_MESSAGE_ID=as1:act1`. Note: the `seq` case did **not** go through the real sequential
  middleware (single-worker executor). That path is the same `propagate_context_to_thread` wrapper the
  `par` case exercised, so it is covered by `par`, not by `seq`.

**Decision: default.**
- Task 1 resolves the session ticket from `get_session_env("HERMES_SESSION_PLATFORM") == "linear"` and
  `get_session_env("HERMES_SESSION_KEY")`, exactly as `hooks.py` does, and maps the key to the
  agent-session/issue through the adapter's store.
- Task 1 also accepts `**kwargs` (or `session_id=None`) and logs it. This is not a fallback: it is a
  cross-check that costs nothing.
- If the platform is not `linear` or the key is empty, writes are refused with
  `{"ok": false, "error": "no_session_ticket"}` (spec §4.3).
- **Live-verify** in a real Linear turn (Task 7/acceptance).

## Q3. Threading

**Question.** On which thread and event loop do sync tool handlers run? Is
`asyncio.run_coroutine_threadsafe(coro, adapter.event_loop)` with a timed wait safe from there?

**Answer.** A sync handler for a gateway turn runs on a worker thread that has **no running event
loop**: a `DaemonThreadPoolExecutor` worker spawned from the turn's executor thread (`run_sync`). On the
agent-turn path it is never the gateway loop thread. Scheduling onto `adapter.event_loop` with
`run_coroutine_threadsafe(...).result(timeout)` is safe there. One host path can break that:
`PluginContext.dispatch_tool` (`hermes_cli/plugins_context.py:483-492`) calls `registry.dispatch`
synchronously on whatever thread calls it. Another plugin calling it from gateway-loop code would run
our sync handler on the loop thread.

- Turn path: `GatewayRunner._run_agent_start_turn_worker` → `start_turn_worker` →
  `asyncio.ensure_future(runner._run_in_executor_with_context(run_sync))`
  (`gateway/run_turn.py:1320-1383`, `gateway/session_runtime_workers.py:134-136`). From that thread the
  agent loop runs each call on a worker: the sequential path uses a single-worker
  `DaemonThreadPoolExecutor` (`agent/tool_executor.py:944`), and the concurrent path uses the batch pool
  (`agent/tool_executor.py:1418`). The middleware → `handle_function_call` step
  (`agent/agent_runtime_helpers.py:2544`) runs inside that worker.
- Nothing in `gateway/` calls `registry.dispatch` or `handle_function_call` directly; the grep finds no
  callers. The one generic path that could is `PluginContext.dispatch_tool` (above). `tui_gateway/methods_connectors.py:162` is the separate TUI backend and only dispatches
  connector tools.
- `is_async=True` handlers would run in a *new* loop: `_run_async` uses a worker loop, or a fresh thread
  if a loop is running (`model_tools.py:95-147`). Because `LinearClient` and its aiohttp session belong
  to the gateway loop, async handlers are the wrong choice.
- The codex app-server runtime exposes only a fixed allowlist of Hermes tools over MCP, in a separate
  process (`agent/transports/hermes_tools_mcp_server.py:56-66`). Our tools are not offered there, and
  `ADAPTERS` would be empty in that process.
- Probe: the handler ran on `turn-exec_0` / `ThreadPoolExecutor-0_0` with `get_running_loop()` raising
  `RuntimeError`. `run_coroutine_threadsafe(coro, gateway_loop).result(timeout=5)` returned, and the
  coroutine ran on `GATEWAY-LOOP`.

**Decision: sync handlers (`is_async=False`) and `run_coroutine_threadsafe` onto `adapter.event_loop`
with a bounded wait.** Task 1's runtime bridge must:

1. Look up the adapter for the session's profile in `adapter.ADAPTERS`. Return
   `{"ok": false, "error": "not_connected"}` when the entry is missing, `event_loop` is `None`, or the
   loop is closed or not running.
2. **Mandatory** deadlock guard. The agent-turn paths never run a handler on the loop thread, but
   `PluginContext.dispatch_tool` (`hermes_cli/plugins_context.py:483-492`) can. If
   `asyncio.get_running_loop()` succeeds and is `adapter.event_loop`, return
   `{"ok": false, "error": "wrong_thread"}` without blocking. Never call `.result()` on the loop
   thread.
3. Call `fut.result(timeout=T)` with `T` around 60 s. The bound that matters is the per-call sequential
   deadline `timeouts.tools.sequential_call`. It falls back to the concurrent batch deadline, 420 s by
   default (`agent/tool_executor.py:139,839-846`). 60 s stays well under it and under the gateway
   inactivity watchdog. On `TimeoutError`, call `fut.cancel()` and return
   `{"ok": false, "error": "timeout"}`.
4. Map every other exception to a sanitized `{"ok": false, "error": "<code>"}`. Never raise
   (spec §4.3).

## Q4. Skills

**Question.** Does `register_skill(name, path, description)` expose `linear:manage-superpowers` to
`skill_view`, and does its index line appear in `<available_skills>` for the neo profile?

**Answer.** Yes to both, while the plugin is enabled in the profile.

- `register_skill` qualifies the name as `<manifest.name>:<name>`, which gives `linear:` here
  (`hermes_cli/plugins_content.py:32-66`). The name must match `[a-zA-Z0-9_-]+` and contain no `:`.
  `path` must exist. Nothing is copied into the skills tree.
- The index rows come from `_plugin_skill_prompt_rows` (`agent/prompt_builder.py:1189-1217`, used at
  `:1376`), via `list_plugin_skill_metadata` (`hermes_cli/plugins.py:669-676`, category `plugin`). They
  go through the same platform, environment and conditions gates as on-disk skills. The row text is the
  `description` argument, clipped at a word boundary to `SKILL_PROMPT_DESC_LIMIT = 60` characters
  (`agent/skill_utils.py:846,857-865`); the frontmatter description is used only if that argument is
  empty.
- `skill_view("linear:manage-superpowers")` finds the file through `find_plugin_skill`
  (`hermes_cli/plugins.py:659`) and prefixes `[Bundle context: This skill is part of the 'linear'
  plugin.]`.
- The plugin is user-installed, so it is loaded eagerly. Only *bundled* platforms are deferred
  (`hermes_cli/plugins_discovery.py:312-319`). Its `register()` runs in every process of a profile with
  `plugins.enabled: [linear]`, and neo has that. The skill therefore shows in CLI/TUI sessions as well
  as Linear sessions.
- Probe: `build_skills_system_prompt()` contained
  `  - linear:manage-superpowers: Use when a repo has .hermes-linear.yaml. Probe skill.`, and
  `skill_view` returned `success: true` with the body.

**Decision: default.** Task 6 calls
`ctx.register_skill("manage-superpowers", <plugin dir>/skills/manage-superpowers/SKILL.md, description=...)`
inside `register()`.
- The `description` must fit in 60 characters. The spec's text (§5) is about 110 characters and would
  be clipped to "Use when a Linear ticket says 'manage with…".
- Proposed text (58 characters): `Use when a repo has .hermes-linear.yaml or ticket says so.` Put the
  full trigger sentence in the SKILL.md frontmatter and body.
- Ship SKILL.md inside the package directory, so that a symlinked or copied plugin dir resolves it.
- **Do not gate the skill on tools or toolsets.** The skill index is built with
  `available_tools=agent.valid_tool_names` (`agent/system_prompt.py:319`). That set is computed from the
  tool surface *after* Tool Search deferral (`agent/agent_init.py:1019-1021`), so it holds the bridge
  names, not `linear_*` (Q1). `_skill_should_show` hides a skill when any `requires_tools` or
  `requires_toolsets` entry is missing (`agent/prompt_builder.py:1168-1186`). Frontmatter naming
  `linear_*` or `hermes_linear` there would therefore hide the skill from `<available_skills>`.
  SKILL.md must carry no `requires_tools`, `requires_toolsets`, `fallback_for_tools` or
  `fallback_for_toolsets` keys. Scope it through its description and body.
- **Live-verify** the index line in a neo Linear session after enablement.

## Q5. Manifest

**Question.** Which manifest keys must declare the tools for `hermes plugins validate` to pass?

**Answer.**
- **Tools:** `provides_tools`, listing every registered tool name. `validate` compares the declared
  `provides_tools`, `provides_hooks` and `provides_middleware` with what `register()` recorded, and fails
  on any registered name that is not declared (`hermes_cli/plugin_validate.py:435-458`).
- **Skills:** there is no manifest key. `provides_skills` is not a known field
  (`hermes_cli/plugins_manifest.py:33-39`), and `validate` does not check skills.
- `provides_tools` (field at `plugins_manifest.py:369`) has no loading side effect for us. The
  `tools.py` / `register_tools(ctx)` path applies only to deferred *bundled* platforms
  (`hermes_cli/plugins_loader.py:426-446`), so registering in `register()` is correct.
- Probe, on the scratch copy, run with `hermes plugins validate`:
  - without `provides_tools`: `✗ declared tools — undeclared tools registered (not in provides_tools): linear_probe`, then `Validation failed.`;
  - with `provides_tools: [linear_probe]`: `✓ declared tools — matches registrations`, then
    `Validation passed.`

  The skill registration produced no finding either way.

**Decision: default.** Task 6 adds `provides_tools:` to `plugin.yaml`, listing all seven names in
spec §4 and keeping `provides_hooks`:
- `linear_issue_get`
- `linear_issue_create`
- `linear_issue_update`
- `linear_comment`
- `linear_sync_spec`
- `linear_sync_plan`
- `linear_task_state`

Task 6 adds no manifest key for the skill. Its test runs `hermes plugins validate .` and expects a
pass, which also checks for collisions with built-in tool names (`plugin_validate.py:477-500`).
