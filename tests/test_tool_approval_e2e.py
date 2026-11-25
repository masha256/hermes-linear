"""Out-of-scope write approval end to end through Hermes's real approval machinery
(RET-119 spec §2, §5 "Hook: end to end"; Task 7, RET-129).

What is real: Hermes's ``hermes_cli.plugins._dispatch_pre_tool_call_hooks`` (directive
parsing and ``_resolve_block_from_details``), ``tools.approval.request_tool_approval`` /
``_run_approval_gate`` / the gateway wait, ``register_gateway_notify`` and
``register_gateway_settle``; the plugin's hooks, its registered tool handler
(``plugin.tool_handler``), the adapter's ``send_exec_approval`` -> ``Outbound.approval_prompt``
(select, control row, grant lookup), and the inbound answer path (live answer and
``answer_pending``).

What stands in for Hermes:
- ``hermes_cli.lifecycle.invoke_hook`` is patched to call ``hooks.on_pre_tool_call`` only
  (no plugin manager); Hermes's own directive parsing runs on its result.
- ``Hermes.call`` mirrors the agent loop's dispatch (``agent/tool_executor.py``
  ``_dispatch_authorized_once``): pre-hooks, then either the blocked result
  ``json.dumps({"error": block})`` plus ``post_tool_call(status="blocked")``
  (``_blocked_tool_result``), or the handler plus ``post_tool_call``.
- ``Hermes.notify`` mirrors ``GatewayRunner``'s approval notify: it schedules the adapter's
  ``send_exec_approval`` on the gateway loop and, like ``register_timeout_notice``, registers a
  settle hook that edits the card (``edit_message``) when the wait times out.
- Linear is two fakes: ``FakeWorkspace`` serves the tool's issue reads and comment writes,
  ``OutClient`` the agent-session activities. The tool context is pinned to the session.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from fake_workspace import FakeWorkspace
from test_adapter_inbound import D_ISSUE, D_SID, DELEGATION, HUMAN, complete, deliver, prompted
from test_adapter_outbound import D_CHAT, env, settle  # noqa: F401  (fixture)

from hermes_linear import agent_tools, hooks, plugin, tool_approval
from hermes_linear.router import message_id

SK = "agent:main:linear:issue:ret129-e2e"
SELF = "uuid-self"
OTHER = "uuid-other"
TOOL = "linear_comment"
ARGS = {"id": "OTH-9", "body": "hello from RET-129"}
RULE = f"<{TOOL}> (plugin approval rule)"
WAIT_S = 20.0


def _comments(ws, issue=OTHER):
    return [c["body"] for c in ws.comments.values() if c["issue"] == issue]


def _elicitations(client, since=0):
    return [c for _, c, *_ in client.activities[since:] if c["type"] == "elicitation"]


async def _until(pred, what, timeout=WAIT_S):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


class Hermes:
    """The Hermes side of one Linear turn: agent-loop dispatch and the gateway notify."""

    def __init__(self, a, loop, mid, *, platform="linear", sk=SK, chat=D_CHAT):
        self.a, self.loop, self.mid = a, loop, mid
        self.platform, self.sk, self.chat = platform, sk, chat
        self.notified: list[dict] = []
        self.sent: list = []

    # --- gateway notify (GatewayRunner._approval_notify_sync, register_timeout_notice)
    def notify(self, data: dict) -> None:
        from tools.approval import register_gateway_settle

        self.notified.append(data)
        rid = data["request_id"]
        fut = asyncio.run_coroutine_threadsafe(self.a.send_exec_approval(
            chat_id=D_CHAT, command=data.get("command", ""), session_key=SK,
            description=data.get("description"), metadata={"thread_id": None},
            allow_permanent=data.get("allow_permanent", True),
            allow_session=data.get("allow_session", True),
            smart_denied=data.get("smart_denied", False), request_id=rid), self.loop)
        res = fut.result(timeout=15)
        self.sent.append(res)

        def on_settle(reason: str) -> None:
            if reason == "timeout":
                asyncio.run_coroutine_threadsafe(
                    self.a.edit_message(D_CHAT, res.message_id, "timed out"), self.loop)

        if res.success and res.message_id:
            register_gateway_settle(SK, rid, on_settle)  # False once a grant resolved it

    # --- agent loop (tool_executor._dispatch_authorized_once, on a tool worker thread)
    def call(self, tool: str, args: dict, cid: str, coerced: dict | None = None) -> dict:
        from gateway.session_context import clear_session_vars, set_session_vars
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        from tools.approval_context import reset_current_session_key, set_current_session_key

        tokens = set_session_vars(platform=self.platform, session_key=self.sk,
                                  chat_id=self.chat, chat_type="dm", user_id=HUMAN,
                                  message_id=self.mid)
        token = set_current_session_key(self.sk)
        try:
            block, modified = _dispatch_pre_tool_call_hooks(
                tool, args, task_id="task-1", session_id="sess-1", tool_call_id=cid)
            assert modified is None  # no modify directive: the handler gets the hook's args
            if block is not None:
                result = json.dumps({"error": block}, ensure_ascii=False)
                hooks.on_post_tool_call(tool, args, result, "task-1", tool_call_id=cid,
                                        status="blocked")
            else:
                # handle_function_call's coerce_tool_args runs after the loop's hook
                handler_args = dict(args if coerced is None else coerced)
                result = plugin.tool_handler(tool)(handler_args, task_id="task-1",
                                                   session_id="sess-1")
                hooks.on_post_tool_call(tool, args, result, "task-1", tool_call_id=cid)
            return json.loads(result)
        finally:
            reset_current_session_key(token)
            clear_session_vars(tokens)

    async def run(self, tool: str = TOOL, args: dict | None = None, cid: str = "c1",
                  coerced: dict | None = None) -> dict:
        args = dict(ARGS if args is None else args)
        return await asyncio.wait_for(
            asyncio.to_thread(self.call, tool, args, cid, coerced), WAIT_S)


def _lifecycle(hook_name, **kw):
    """``hermes_cli.lifecycle.invoke_hook`` with only this plugin's pre_tool_call hook."""
    return [hooks.on_pre_tool_call(**kw)] if hook_name == "pre_tool_call" else []


@pytest.fixture
async def e2e(env, monkeypatch):  # noqa: F811
    from tools import approval

    a, client, gw, store, order = env
    ws = FakeWorkspace()
    ws.add_issue("RET-1", title="Session issue", uuid=SELF)
    ws.add_issue("OTH-9", title="Unrelated", uuid=OTHER, project=None)
    await deliver(a, DELEGATION)
    ctx = agent_tools.ToolContext(adapter=a, client=ws, session_issue_id=SELF, session_key=SK,
                                  is_linear=True)
    monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: ctx)
    h = Hermes(a, asyncio.get_running_loop(), gw.events[0].message_id)
    config = {"mode": "manual", "timeout": 30, "approvers": []}
    with ExitStack() as stack:
        stack.enter_context(patch("hermes_cli.lifecycle.invoke_hook", _lifecycle))
        stack.enter_context(patch.object(approval, "_YOLO_MODE_FROZEN", False))
        stack.enter_context(patch("tools.approval_context._get_approval_mode",
                                  return_value="manual"))
        stack.enter_context(patch("tools.approval_context._get_approval_config",
                                  return_value=config))
        approval.register_gateway_notify(SK, h.notify)
        try:
            yield h, a, client, gw, store, ws, config
        finally:
            approval.unregister_gateway_notify(SK)  # wakes a worker still blocked


async def _asked(h, client, store, args=ARGS):
    """First call (APPROVAL_REQUIRED, request stored), then start the identical re-call;
    returns its task once the Linear select is posted and the control row is live."""
    first = await h.run(args=args, cid="c-first")
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED"
    assert "again with identical arguments" in first["message"]
    d = tool_approval.digest(TOOL, args)
    assert store.request(SK, d) is not None
    n, n_acts = len(h.notified), len(client.activities)
    task = asyncio.create_task(h.run(args=args, cid="c-again"))
    # notify returns once send_exec_approval finished (row stored, select posted)
    await _until(lambda: len(h.sent) == n + 1, "the approval select")
    assert len(h.notified) == n + 1 and h.sent[-1].success
    data = h.notified[-1]
    assert data["command"] == RULE and data["description"].endswith(f"digest: {d}")
    assert data["pattern_key"] == f"plugin_rule:{tool_approval.rule_key(TOOL, d)}"
    row = store.control_for_session(D_SID)
    assert row.state == "live" and row.command == tool_approval.grant_command(RULE, d)
    assert list(row.choices) == ["once", "deny"]
    selects = [act for act in client.activities[n_acts:] if act[1]["type"] == "elicitation"]
    assert len(selects) == 1 and [o["value"] for o in selects[0][4]["options"]] == ["once", "deny"]
    return task, d


async def test_e2e_approve_writes(e2e):
    h, a, client, gw, store, ws, config = e2e
    task, d = await _asked(h, client, store)
    assert _comments(ws) == []
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    out = await task
    assert out["ok"] is True and out["issue"] == "OTH-9", out
    assert _comments(ws) == [ARGS["body"]]
    assert store.request(SK, d) is None  # consumed by the approved pass
    assert store.control_for_session(D_SID) is None
    assert len(gw.events) == 1  # answered live: no answer turn


async def test_e2e_deny_never_writes(e2e):
    h, a, client, gw, store, ws, config = e2e
    task, d = await _asked(h, client, store)
    await deliver(a, prompted(D_SID, D_ISSUE, "Deny", "act-ans"))
    out = await task
    assert set(out) == {"error"} and out["error"].startswith("BLOCKED:"), out
    assert "denied" in out["error"]
    assert _comments(ws) == [] and ws.writes() == []
    req = store.request(SK, d)
    assert req is not None and req.armed is False  # post_tool_call disarmed the block
    assert store.take_armed_request(SK, d) is None
    # A hook-less identical call (e.g. ctx.dispatch_tool) still cannot write.
    raw = json.loads(await asyncio.to_thread(plugin.tool_handler(TOOL), dict(ARGS),
                                             session_id="sess-1"))
    assert raw["error"] == "APPROVAL_REQUIRED" and _comments(ws) == []


async def test_e2e_deferred_rerun_uses_grant(e2e):
    """Review Focus 3: the select times out (pending); the later answer grants exactly one
    identical re-run in the answer turn, with no new elicitation."""
    h, a, client, gw, store, ws, config = e2e
    config["timeout"] = 2
    task, d = await _asked(h, client, store)
    blocked = await task
    assert set(blocked) == {"error"} and blocked["error"].startswith("BLOCKED:"), blocked
    assert "timed out" in blocked["error"]
    await settle(a)
    row = store.control_for_session(D_SID)
    assert row is not None and row.state == "pending"
    rid = row.request_id
    assert store.request(SK, d).armed is False
    assert _comments(ws) == []
    await complete(a, gw.events[0])  # the asking turn ends
    await settle(a)

    # The human answers later: answer_pending starts the answer turn and stores the grant.
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    await settle(a)
    ev = gw.events[-1]
    assert ev.message_id == message_id(D_SID, "act-ans")
    assert ev.text.endswith(
        f"Answer to approval for `{TOOL}` call {d}: approved (once). "
        "Re-run the same call with identical arguments.")
    assert store.control(rid) is None

    # The answer turn re-runs the identical call: the hook asks Hermes, the prompt resolves
    # through the stored grant (_use_grant), nothing is posted, and the comment is written.
    h.mid = ev.message_id
    n_notified, n_acts = len(h.notified), len(client.activities)
    out = await h.run(cid="c-rerun")
    assert out["ok"] is True and out["issue"] == "OTH-9", out
    assert _comments(ws) == [ARGS["body"]]
    assert len(h.notified) == n_notified + 1  # Hermes asked once more...
    assert _elicitations(client, n_acts) == []  # ...and the grant answered it: no new select
    assert store.control_for_session(D_SID) is None
    assert store.take_grant(SK, tool_approval.grant_command(RULE, d)) is None  # spent
    assert store.request(SK, d) is None

    # The same call with another body is a new digest: it asks again with a new select.
    other = {**ARGS, "body": "a different body"}
    task2, d2 = await _asked(h, client, store, args=other)
    assert d2 != d
    assert len(_elicitations(client, n_acts)) == 1
    out2 = await task2  # times out again (2 s): nothing written
    assert out2["error"].startswith("BLOCKED:")
    assert _comments(ws) == [ARGS["body"]]


async def test_e2e_yolo_bypasses(e2e):
    from tools import approval

    h, a, client, gw, store, ws, config = e2e
    first = await h.run(cid="c-first")
    assert first["error"] == "APPROVAL_REQUIRED"
    n_acts = len(client.activities)
    with patch.object(approval, "_YOLO_MODE_FROZEN", True):
        out = await h.run(cid="c-again")
    assert out["ok"] is True, out
    assert _comments(ws) == [ARGS["body"]]
    assert h.notified == [] and _elicitations(client, n_acts) == []
    assert store.control_for_session(D_SID) is None
    assert store.request(SK, tool_approval.digest(TOOL, ARGS)) is None


async def test_e2e_in_scope_never_asks(e2e):
    h, a, client, gw, store, ws, config = e2e
    out = await h.run(args={"id": "RET-1", "body": "own ticket"})
    assert out["ok"] is True and _comments(ws, SELF) == ["own ticket"]
    assert h.notified == []
    assert store.conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


async def test_e2e_coerced_array_arg_digests_agree(e2e):
    """R10: the hook sees ``add_labels: "bug"``, the handler the coerced ``["bug"]``."""
    h, a, client, gw, store, ws, config = e2e
    tool, raw, coerced = "linear_issue_update", {"id": "OTH-9", "add_labels": "bug"}, \
        {"id": "OTH-9", "add_labels": ["bug"]}
    first = await h.run(tool, raw, cid="c-first", coerced=coerced)
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED", first
    d = tool_approval.digest(tool, raw)
    assert d == tool_approval.digest(tool, coerced) and store.request(SK, d) is not None
    task = asyncio.create_task(h.run(tool, raw, cid="c-again", coerced=coerced))
    await _until(lambda: len(h.sent) == 1, "the approval select")
    assert h.notified[-1]["description"].endswith(f"digest: {d}")
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    out = await task
    assert out["ok"] is True, out
    assert ws.issues[OTHER]["labels"] == ["lb-bug"]
    assert store.request(SK, d) is None


async def test_e2e_coerced_estimate_one_prompt(e2e, monkeypatch):
    """RET-121 Review Focus 1: the hook digests the raw ``estimate: "2"``, the handler the
    output of Hermes's real ``coerce_tool_args`` for the plugin's registered schema
    (``estimate: 2``). One approval prompt, and the approved re-call writes."""
    import copy

    from tools.arg_coercion import coerce_tool_args
    from tools.registry import registry

    h, a, client, gw, store, ws, config = e2e
    tool = "linear_issue_update"
    real_get_schema = registry.get_schema
    monkeypatch.setattr(registry, "get_schema", lambda name: copy.deepcopy(
        agent_tools.TOOL_SCHEMAS[name]) if name == tool else real_get_schema(name))
    raw = {"id": "OTH-9", "estimate": "2", "due_date": "null"}
    coerced = coerce_tool_args(tool, copy.deepcopy(raw))
    assert coerced == {"id": "OTH-9", "estimate": 2, "due_date": None}  # what Hermes hands on
    first = await h.run(tool, raw, cid="c-first", coerced=coerced)
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED", first
    d = tool_approval.digest(tool, raw)
    assert d == tool_approval.digest(tool, coerced) and store.request(SK, d) is not None
    task = asyncio.create_task(h.run(tool, raw, cid="c-again", coerced=coerced))
    await _until(lambda: len(h.sent) == 1, "the approval select")
    assert h.notified[-1]["description"].endswith(f"digest: {d}")
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    out = await task
    assert out["ok"] is True, out
    assert len(h.notified) == 1  # one prompt: the approved re-call did not ask again
    assert ws.issues[OTHER]["fields"] == {"estimate": 2, "dueDate": None}
    assert store.request(SK, d) is None


async def test_e2e_project_target(e2e):
    """RET-121 §2.4: another project is an approval target; after "Allow once" the
    identical re-call creates the issue in that project only, and a third identical call
    asks again."""
    h, a, client, gw, store, ws, config = e2e
    ws.add_project("proj-orion", "orion")
    tool, args = "linear_issue_create", {"title": "Over there", "project": "orion"}
    first = await h.run(tool, args, cid="c-first")
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED", first
    assert "orion" in first["message"] and ws.writes() == []
    d = tool_approval.digest(tool, args)
    assert store.request(SK, d).targets == ["proj-orion"]
    task = asyncio.create_task(h.run(tool, args, cid="c-again"))
    await _until(lambda: len(h.sent) == 1, "the approval select")
    data = h.notified[-1]
    assert data["command"] == f"<{tool}> (plugin approval rule)"
    assert '\n- project "orion"\n' in data["description"]
    assert data["description"].endswith(f"digest: {d}")
    assert ws.writes() == []
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    out = await task
    assert out["ok"] is True, out
    [(_, inp)] = ws.writes()
    assert inp["projectId"] == "proj-orion" and inp["teamId"] == ws.issues[SELF]["team"]
    assert ws.issues[out["issue"]["id"]]["project"] == {"id": "proj-orion", "name": "orion"}
    assert store.request(SK, d) is None
    again = await h.run(tool, args, cid="c-third")
    assert again["error"] == "APPROVAL_REQUIRED" and len(ws.writes()) == 1


async def test_out_of_scope_attach_end_to_end(e2e, tmp_path, monkeypatch):
    """RET-134: an out-of-scope ``linear_attach`` of a file goes through the same approval
    path. The first call uploads nothing; the identical re-call asks in Linear, and after
    "Allow once" the upload and the attach run exactly once. The file is a real tmp file
    judged by the real ``validate_media_delivery_path`` (HOME moved under ``tmp_path``)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_MEDIA_DELIVERY_STRICT", raising=False)
    h, a, client, gw, store, ws, config = e2e
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG")
    tool, args = "linear_attach", {"id": "OTH-9", "path": str(f)}
    first = await h.run(tool, args, cid="c-first")
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED", first
    assert ws.writes() == []
    d = tool_approval.digest(tool, args)
    assert store.request(SK, d) is not None
    task = asyncio.create_task(h.run(tool, args, cid="c-again"))
    await _until(lambda: len(h.sent) == 1, "the approval select")
    data = h.notified[-1]
    assert data["command"] == f"<{tool}> (plugin approval rule)"
    assert data["description"].endswith(f"digest: {d}")
    assert ws.writes() == []  # still nothing while the human is asked
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    out = await task
    assert out["ok"] is True and out["issue"] == "OTH-9", out
    assert [c[0] for c in ws.writes()] == ["file_upload", "attachment_create"]
    [att] = ws.attachments.values()
    assert att["issue"] == OTHER and att["url"] == out["asset_url"] and att["title"] == "shot.png"
    assert store.request(SK, d) is None


async def test_e2e_relation_out_of_scope(e2e):
    """RET-141: ``linear_relation`` with an out-of-scope ``other`` asks once; after "Allow
    once" the identical re-call creates the relation exactly once."""
    h, a, client, gw, store, ws, config = e2e
    tool, args = "linear_relation", {"id": "RET-1", "type": "blocks", "other": "OTH-9"}
    first = await h.run(tool, args, cid="c-first")
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED", first
    assert ws.writes() == []
    d = tool_approval.digest(tool, args)
    assert store.request(SK, d).targets == [OTHER]
    task = asyncio.create_task(h.run(tool, args, cid="c-again"))
    await _until(lambda: len(h.sent) == 1, "the approval select")
    data = h.notified[-1]
    assert data["command"] == f"<{tool}> (plugin approval rule)"
    assert '\n- OTH-9 "Unrelated"\n' in data["description"]
    assert data["description"].endswith(f"digest: {d}")
    assert ws.writes() == []
    await deliver(a, prompted(D_SID, D_ISSUE, "Allow once", "act-ans"))
    out = await task
    assert out == {"ok": True, "relation_id": out["relation_id"], "issue": "RET-1",
                   "other": "OTH-9", "type": "blocks"}, out
    assert ws.writes() == [("relation_create", (SELF, OTHER, "blocks"))]
    assert len(h.notified) == 1 and store.request(SK, d) is None


# ------------------------------------------- outside a Linear session (RET-148, Task 1)

SLACK_SK = "agent:main:slack:dm:C0E2E:1700000000.0001"
SLACK_CHAT = "C0E2E"
OUT_ARGS = {"id": "RET-1", "body": "hello from Slack"}  # the Linear session's own ticket
#: The origin footer (RET-148 §4): set_session_vars binds profile "" -> "default".
SLACK_FOOTER = "\n\n---\n_Filed by default via slack_"


class SlackHermes(Hermes):
    """The Hermes side of one Slack turn. ``notify`` stands in for the gateway rendering
    Slack's approval buttons and a human clicking one: it answers the request it was called
    for with Hermes's public ``resolve_gateway_approval`` (the typed ``/approve`` path)."""

    def __init__(self, a, loop, choice: str):
        super().__init__(a, loop, "slack-m1", platform="slack", sk=SLACK_SK, chat=SLACK_CHAT)
        self.choice = choice

    def notify(self, data: dict) -> None:
        from tools.approval import resolve_gateway_approval

        self.notified.append(data)
        self.sent.append(resolve_gateway_approval(SLACK_SK, self.choice,
                                                  request_id=data["request_id"]))


@pytest.fixture
async def e2e_outside(env, monkeypatch):  # noqa: F811
    """Like ``e2e``, for a call from a Slack session (``HERMES_SESSION_PLATFORM="slack"``).

    What stands in for Hermes, besides ``e2e``'s seams (``invoke_hook`` patched to this
    plugin's ``pre_tool_call`` only; ``Hermes.call`` as the agent loop's dispatch): the
    Slack adapter's button card and the human's click (``SlackHermes.notify``), and the tool
    context, pinned to the outside shape ``resolve_context`` gives off Linear (no session
    issue, the Slack session key bound, ``outside=True``). The approval wait, directive
    parsing and the plugin's hooks, handler and store are real.
    """
    from tools import approval

    a, client, gw, store, order = env
    ws = FakeWorkspace()
    ws.add_issue("RET-1", title="Session issue", uuid=SELF)
    ctx = agent_tools.ToolContext(adapter=a, client=ws, session_issue_id=None,
                                  session_key=SLACK_SK, is_linear=False, outside=True)
    monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: ctx)
    config = {"mode": "manual", "timeout": 30, "approvers": []}

    def make(choice: str) -> SlackHermes:
        h = SlackHermes(a, asyncio.get_running_loop(), choice)
        approval.register_gateway_notify(SLACK_SK, h.notify)
        return h

    with ExitStack() as stack:
        stack.enter_context(patch("hermes_cli.lifecycle.invoke_hook", _lifecycle))
        stack.enter_context(patch.object(approval, "_YOLO_MODE_FROZEN", False))
        stack.enter_context(patch("tools.approval_context._get_approval_mode",
                                  return_value="manual"))
        stack.enter_context(patch("tools.approval_context._get_approval_config",
                                  return_value=config))
        try:
            yield make, client, store, ws
        finally:
            approval.unregister_gateway_notify(SLACK_SK)


async def _outside_first(h, store):
    first = await h.run(args=OUT_ARGS, cid="c-first")
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED", first
    assert "outside a Linear session" in first["message"]
    assert "ask in this chat" in first["message"]
    d = tool_approval.digest(TOOL, OUT_ARGS)
    req = store.request(SLACK_SK, d)
    assert req is not None and req.targets == [SELF]
    return d


async def test_e2e_outside_comment_approve_writes(e2e_outside):
    make, client, store, ws = e2e_outside
    h = make("once")
    n_acts = len(client.activities)
    d = await _outside_first(h, store)
    assert _comments(ws, SELF) == []
    out = await h.run(args=OUT_ARGS, cid="c-again")
    assert out["ok"] is True and out["issue"] == "RET-1", out
    # exactly one, with the origin footer from the tool thread's session (Task 2)
    assert _comments(ws, SELF) == [OUT_ARGS["body"] + SLACK_FOOTER]
    assert len(h.notified) == 1 and h.sent == [1]
    data = h.notified[0]
    assert data["command"] == RULE and data["description"].endswith(f"digest: {d}")
    assert data["pattern_key"] == (
        f"plugin_rule:{tool_approval.rule_key(TOOL, d, outside=True)}")
    assert data["description"].splitlines()[0] == (
        f"Approve `{TOOL}` from outside a Linear session?")
    assert store.request(SLACK_SK, d) is None  # consumed
    assert len(client.activities) == n_acts  # nothing posted to a Linear agent session


async def test_e2e_outside_comment_deny_never_writes(e2e_outside):
    make, client, store, ws = e2e_outside
    h = make("deny")
    d = await _outside_first(h, store)
    out = await h.run(args=OUT_ARGS, cid="c-again")
    assert set(out) == {"error"} and out["error"].startswith("BLOCKED:"), out
    assert len(h.notified) == 1
    assert _comments(ws, SELF) == [] and ws.writes() == []
    req = store.request(SLACK_SK, d)
    assert req is not None and req.armed is False  # post_tool_call disarmed the block
    raw = json.loads(await asyncio.to_thread(plugin.tool_handler(TOOL), dict(OUT_ARGS),
                                             session_id="sess-1"))
    assert raw["error"] == "APPROVAL_REQUIRED" and ws.writes() == []


async def test_e2e_outside_create_approve_writes_once(e2e_outside):
    """RET-148 Task 2: an outside create (no parent; the destination is the only target)
    through the real hook and gate. The hook sees the model's raw ``labels: "bug"``, the
    handler the ``coerce_tool_args`` output ``["bug"]``; both digest the same, so the
    approved re-call writes exactly once, with the footer bound in the tool thread."""
    import dataclasses

    from fake_workspace import TEAM

    make, client, store, ws = e2e_outside
    a = agent_tools.resolve_context().adapter  # the pinned outside context's adapter
    a._settings = dataclasses.replace(a._settings, default_team="RET")
    h = make("once")
    tool = "linear_issue_create"
    raw, coerced = {"title": "From Slack", "labels": "bug", "estimate": "2"}, \
        {"title": "From Slack", "labels": ["bug"], "estimate": 2}
    first = await h.run(tool, raw, cid="c-first", coerced=coerced)
    assert first["ok"] is False and first["error"] == "APPROVAL_REQUIRED", first
    assert "outside a Linear session" in first["message"] and "RET" in first["message"]
    d = tool_approval.digest(tool, raw)
    assert d == tool_approval.digest(tool, coerced)
    assert store.request(SLACK_SK, d).targets == [f"destination:{TEAM}/-"]
    assert ws.writes() == []
    out = await h.run(tool, raw, cid="c-again", coerced=coerced)
    assert out["ok"] is True, out
    (create,) = [w for w in ws.writes() if w[0] == "issue_create"]
    assert ws.writes() == [create]
    stored = ws.issues[out["issue"]["id"]]
    assert stored["team"] == TEAM and stored["labels"] == ["lb-bug"]
    assert stored["fields"] == {"estimate": 2}  # a RET-121 field, coerced as Hermes does
    assert stored["description"] == SLACK_FOOTER.lstrip("\n")
    assert len(h.notified) == 1
    desc = h.notified[0]["description"]
    assert desc.splitlines()[0] == f"Approve `{tool}` from outside a Linear session?"
    assert '- destination "RET (no project)"' in desc and desc.endswith(f"digest: {d}")
    assert store.request(SLACK_SK, d) is None


# ------------------------- permanent grants never cross the Linear boundary (final review)


@pytest.fixture
def grants():
    """Hermes's real allowlist state, emptied for one test. Stand-in: the config write
    (``save_permanent_allowlist``) is a no-op so no ``command_allowlist`` lands on disk;
    ``_persist_choice`` / ``approve_permanent`` / ``is_approved`` are real."""
    from tools import approval

    with ExitStack() as stack:
        stack.enter_context(patch.object(approval, "_permanent_approved", set()))
        stack.enter_context(patch.object(approval, "_session_approved", {}))
        stack.enter_context(patch.object(approval, "save_permanent_allowlist",
                                         lambda patterns: None))
        yield approval


def _outside_ctx(a, ws):
    return agent_tools.ToolContext(adapter=a, client=ws, session_issue_id=None,
                                   session_key=SLACK_SK, is_linear=False, outside=True)


async def test_e2e_permanent_outside_grant_still_asks_in_linear(e2e, grants, monkeypatch):
    """A Slack "always" for a call persists a permanent rule through Hermes's own
    ``_persist_choice``. It pre-approves the next identical Slack call, but the identical
    call from a Linear session still asks in the ticket thread with once/deny."""
    approval = grants
    h, a, client, gw, store, ws, config = e2e
    linear_ctx = agent_tools.resolve_context()
    monkeypatch.setattr(agent_tools, "resolve_context",
                        lambda session_id=None: _outside_ctx(a, ws))
    slack = SlackHermes(a, asyncio.get_running_loop(), "always")
    approval.register_gateway_notify(SLACK_SK, slack.notify)
    d = tool_approval.digest(TOOL, ARGS)
    try:
        assert (await slack.run(cid="s1"))["error"] == "APPROVAL_REQUIRED"
        assert (await slack.run(cid="s2"))["ok"] is True
        key = slack.notified[0]["pattern_key"]
        assert key in approval._permanent_approved  # persisted by Hermes's 'always' path
        # the permanent rule covers the next identical outside call: no new prompt
        assert (await slack.run(cid="s3"))["error"] == "APPROVAL_REQUIRED"
        assert (await slack.run(cid="s4"))["ok"] is True
        assert len(slack.notified) == 1
    finally:
        approval.unregister_gateway_notify(SLACK_SK)
    written = _comments(ws)
    assert len(written) == 2

    monkeypatch.setattr(agent_tools, "resolve_context", lambda session_id=None: linear_ctx)
    task, d_linear = await _asked(h, client, store)  # asks in Linear with once/deny
    assert d_linear == d
    await deliver(a, prompted(D_SID, D_ISSUE, "Deny", "act-ans"))
    out = await task
    assert set(out) == {"error"} and out["error"].startswith("BLOCKED:"), out
    assert _comments(ws) == written  # nothing more written
    assert key == f"plugin_rule:{tool_approval.rule_key(TOOL, d, outside=True)}"
    assert h.notified[-1]["pattern_key"] == f"plugin_rule:{tool_approval.rule_key(TOOL, d)}"


async def test_e2e_permanent_inside_rule_does_not_approve_outside(e2e_outside, grants):
    """The reverse: a permanent rule on the in-Linear key leaves an outside call asking."""
    approval = grants
    make, client, store, ws = e2e_outside
    d = tool_approval.digest(TOOL, OUT_ARGS)
    approval.approve_permanent(f"plugin_rule:{tool_approval.rule_key(TOOL, d)}")
    h = make("deny")
    await _outside_first(h, store)
    out = await h.run(args=OUT_ARGS, cid="c-again")
    assert set(out) == {"error"} and out["error"].startswith("BLOCKED:"), out
    assert len(h.notified) == 1
    assert h.notified[0]["pattern_key"] == (
        f"plugin_rule:{tool_approval.rule_key(TOOL, d, outside=True)}")
    assert _comments(ws, SELF) == [] and ws.writes() == []
