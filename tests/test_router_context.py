"""Router tests: data-change events, context collection, plain comments (spec §7.2–7.4)."""

from __future__ import annotations

import copy

import pytest

from conftest import load_fixture
from hermes_linear.constants import DESCRIPTION_SNIPPET
from hermes_linear.models import (
    AddContext,
    AwaitDelegation,
    ContextItem,
    Fatal,
    Ignore,
    OpenSessionOnComment,
    SetMode,
)
from hermes_linear.router import RouterContext, route
from hermes_linear.store import Store

APP = "00000000-0000-4000-8000-000000000001"
HUMAN = "00000000-0000-4000-8000-000000000006"
HANDLE = "hermestest"

LABELS = load_fixture("issue_update_labels")
STATE = load_fixture("issue_update_state")
DESCRIPTION = load_fixture("issue_update_description")
ISSUE_CREATE = load_fixture("issue_create_delegated")
TOPLEVEL = load_fixture("comment_create_toplevel_plain")
REPLY = load_fixture("comment_create_reply")
MENTION_ROOT = load_fixture("comment_create_mention_root")
PROJECT = load_fixture("project_update")

C_ISSUE = TOPLEVEL["data"]["issueId"]  # RET-61 (comments, labels)
S_ISSUE = STATE["data"]["id"]  # RET-62 (state, description)
PROJECT_ID = PROJECT["data"]["id"]


async def _yes(_uid):
    return True


async def _no(_uid):
    return False


@pytest.fixture
def store():
    s = Store.open(":memory:")
    yield s
    s.close()


@pytest.fixture
def ctx(store):
    return RouterContext(store=store, app_user_id=APP, allowed=_yes, app_user_handle=HANDLE)


def _delegate(store, issue_id, project_id=None):
    store.upsert_ticket(issue_id, project_id=project_id)
    store.set_mode(issue_id, "delegated", 1)


def _items(out):
    assert len(out) == 1 and isinstance(out[0], AddContext), out
    return out[0].items


def _human_project_event():
    ev = copy.deepcopy(PROJECT)  # the probe edit was made by the app user itself
    ev["actor"] = copy.deepcopy(LABELS["actor"])
    return ev


# ------------------------------------------------------------------- echo


async def test_echo_dropped(ctx, store):
    _delegate(store, C_ISSUE)
    ev = copy.deepcopy(LABELS)
    ev["actor"]["id"] = APP
    assert await route(ev, ctx) == [Ignore("echo")]
    ev = copy.deepcopy(TOPLEVEL)
    ev["data"]["userId"] = APP
    assert await route(ev, ctx) == [Ignore("echo")]
    ev = copy.deepcopy(REPLY)
    ev["data"]["user"]["id"] = APP
    assert await route(ev, ctx) == [Ignore("echo")]
    # the probe's project edit is itself an echo
    assert await route(copy.deepcopy(PROJECT), ctx) == [Ignore("echo")]
    assert store.pending_context(C_ISSUE) == []


# ------------------------------------------------------------------ Issue


async def test_label_change_on_delegated_is_context(ctx, store):
    _delegate(store, C_ISSUE)
    items = _items(await route(copy.deepcopy(LABELS), ctx))
    assert items == (
        ContextItem(
            issue_id=C_ISSUE, kind="issue", field="labels", old=None, new="+Feature",
            actor="Test User", at=LABELS["createdAt"],
        ),
    )


async def test_label_removed_names(ctx, store):
    _delegate(store, C_ISSUE)
    store.cache_label("L-old", "Bug")
    ev = copy.deepcopy(LABELS)
    ev["updatedFrom"]["labelIds"] = ["L-old", "L-gone"]
    (item,) = _items(await route(ev, ctx))
    assert item.new == "+Feature, -Bug, -(removed label)"


async def test_state_change_context_names(ctx, store):
    _delegate(store, S_ISSUE)
    (item,) = _items(await route(copy.deepcopy(STATE), ctx))
    assert (item.field, item.old, item.new) == ("state", "(previous)", "In Progress")
    store.cache_state(STATE["updatedFrom"]["stateId"], "Todo")
    (item,) = _items(await route(copy.deepcopy(STATE), ctx))
    assert (item.field, item.old, item.new) == ("state", "Todo", "In Progress")


async def test_description_truncated_800(ctx, store):
    _delegate(store, S_ISSUE)
    ev = copy.deepcopy(DESCRIPTION)
    ev["data"]["description"] = "x" * 5000
    (item,) = _items(await route(ev, ctx))
    assert item.field == "description" and item.old is None
    assert len(item.new) <= DESCRIPTION_SNIPPET and item.new.startswith("x" * 100)


async def test_title_priority_assignee_generic(ctx, store):
    _delegate(store, S_ISSUE)
    ev = copy.deepcopy(STATE)
    ev["updatedFrom"] = {"updatedAt": "t", "title": "Old title", "priority": 3,
                         "assigneeId": None, "sortOrder": 1, "subscriberIds": []}
    ev["data"]["priority"] = 1
    ev["data"]["priorityLabel"] = "Urgent"
    items = _items(await route(ev, ctx))
    got = {i.field: (i.old, i.new) for i in items}
    assert got["title"] == ("Old title", "Second test ticket for neo")
    assert got["priority"][1] == "Urgent"
    assert got["assignee"][1] == "Test User"
    assert set(got) == {"title", "priority", "assignee"}


async def test_edit_on_untouched_ignored(ctx, store):
    out = await route(copy.deepcopy(LABELS), ctx)
    assert len(out) == 1 and isinstance(out[0], Ignore)
    assert store.pending_context(C_ISSUE) == []
    # names are still cached for later
    assert store.label_name(LABELS["data"]["labels"][0]["id"]) == "Feature"


async def test_issue_create_already_delegated_caches_only(ctx, store):
    out = await route(copy.deepcopy(ISSUE_CREATE), ctx)
    assert not any(isinstance(d, (AddContext, AwaitDelegation, SetMode)) for d in out)
    t = store.ticket(ISSUE_CREATE["data"]["id"])
    assert t.project_id == ISSUE_CREATE["data"]["projectId"]
    assert store.state_name(ISSUE_CREATE["data"]["stateId"]) == "Todo"


async def test_issue_moved_out_of_project_clears_project(ctx, store):
    _delegate(store, S_ISSUE, PROJECT_ID)
    ev = copy.deepcopy(STATE)
    ev["updatedFrom"] = {"updatedAt": "t", "projectId": PROJECT_ID}
    ev["data"]["projectId"] = None
    ev["data"].pop("project")
    await route(ev, ctx)
    assert store.ticket(S_ISSUE).project_id is None
    assert store.delegated_in_project(PROJECT_ID) == []


async def test_denied_actor_context_dropped(store):
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no, app_user_handle=HANDLE)
    _delegate(store, C_ISSUE)
    out = await route(copy.deepcopy(LABELS), ctx)
    assert len(out) == 1 and isinstance(out[0], Ignore)
    out = await route(copy.deepcopy(TOPLEVEL), ctx)
    assert len(out) == 1 and isinstance(out[0], Ignore)
    store.record_session("s", C_ISSUE, "other-root", "delegation")
    out = await route(copy.deepcopy(REPLY), ctx)
    assert len(out) == 1 and isinstance(out[0], Ignore)


async def test_bot_issue_edit_is_context_without_allowlist(store):
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no)
    _delegate(store, C_ISSUE)
    ev = copy.deepcopy(LABELS)
    ev["actor"] = {"id": "integration-1", "name": "GitHub", "type": "integration"}
    (item,) = _items(await route(ev, ctx))
    assert item.actor == "GitHub"


# ---------------------------------------------------------------- Comment


async def test_toplevel_human_comment_on_delegated_opens_session(ctx, store):
    _delegate(store, C_ISSUE)
    out = await route(copy.deepcopy(TOPLEVEL), ctx)
    d = TOPLEVEL["data"]
    assert out == [
        OpenSessionOnComment(
            issue_id=C_ISSUE, comment_id=d["id"], user_id=HUMAN, user_name="Test User",
            body="top-level comment with no mention",
        )
    ]


async def test_toplevel_comment_on_untouched_ignored(ctx, store):
    out = await route(copy.deepcopy(TOPLEVEL), ctx)
    assert len(out) == 1 and isinstance(out[0], Ignore)


async def test_mention_comment_ignored(ctx, store):
    _delegate(store, C_ISSUE)
    assert await route(copy.deepcopy(MENTION_ROOT), ctx) == [Ignore("mention comment")]
    for body in (f"hey @HermesTest please", f"cc {APP}",
                 "see https://linear.app/example-workspace/profiles/hermestest"):
        ev = copy.deepcopy(TOPLEVEL)
        ev["data"]["body"] = body
        assert await route(ev, ctx) == [Ignore("mention comment")], body
    # word-bounded: a longer handle is not a mention
    ev = copy.deepcopy(TOPLEVEL)
    ev["data"]["body"] = "ask @hermestester instead"
    assert isinstance((await route(ev, ctx))[0], OpenSessionOnComment)


async def test_router_context_handle_optional(store):
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_yes)
    assert ctx.app_user_handle is None


async def test_reply_in_session_thread_dropped_from_context(ctx, store):
    """Review Focus 2: the reply already arrived as `prompted`."""
    _delegate(store, C_ISSUE)
    store.record_session("sess-1", C_ISSUE, REPLY["data"]["parentId"], "mention")
    assert await route(copy.deepcopy(REPLY), ctx) == [Ignore("session thread")]
    assert store.pending_context(C_ISSUE) == []


async def test_reply_in_other_thread_is_context(ctx, store):
    _delegate(store, C_ISSUE)
    store.record_session("sess-1", C_ISSUE, "some-other-root", "delegation")
    (item,) = _items(await route(copy.deepcopy(REPLY), ctx))
    assert (item.kind, item.field, item.new) == ("comment", "reply", "second reply in thread")
    assert item.actor == "Test User" and item.at == REPLY["createdAt"]


async def test_reply_body_truncated(ctx, store):
    _delegate(store, C_ISSUE)
    ev = copy.deepcopy(REPLY)
    ev["data"]["body"] = "y" * 3000
    (item,) = _items(await route(ev, ctx))
    assert len(item.new) <= DESCRIPTION_SNIPPET


async def test_bot_comment_context_only(ctx, store):
    _delegate(store, C_ISSUE)
    ev = copy.deepcopy(TOPLEVEL)
    ev["data"]["botActor"] = {"id": "bot-1", "name": "GitHub", "type": "github"}
    out = await route(ev, ctx)
    assert not any(isinstance(d, OpenSessionOnComment) for d in out)
    (item,) = _items(out)
    assert (item.kind, item.new) == ("comment", "top-level comment with no mention")


async def test_comment_update_and_remove_ignored(ctx, store):
    _delegate(store, C_ISSUE)
    for action in ("update", "remove"):
        ev = copy.deepcopy(TOPLEVEL)
        ev["action"] = action
        out = await route(ev, ctx)
        assert len(out) == 1 and isinstance(out[0], Ignore)


# ------------------------------------------------------------- Attachment


async def test_attachment_is_context(ctx, store):
    _delegate(store, C_ISSUE)
    ev = {
        "type": "Attachment", "action": "create", "createdAt": "2026-11-21T21:00:00Z",
        "actor": copy.deepcopy(LABELS["actor"]),
        "data": {"id": "A1", "issueId": C_ISSUE, "title": "PR #12",
                 "url": "https://github.com/x/y/pull/12", "updatedAt": "t"},
    }
    (item,) = _items(await route(ev, ctx))
    assert (item.kind, item.field, item.new) == (
        "attachment", "link", "PR #12 https://github.com/x/y/pull/12")


# ---------------------------------------------------------------- Project


async def test_project_update_fans_out(ctx, store):
    _delegate(store, "I-a", PROJECT_ID)
    _delegate(store, "I-b", PROJECT_ID)
    store.upsert_ticket("I-c", project_id=PROJECT_ID)  # not delegated
    items = _items(await route(_human_project_event(), ctx))
    assert sorted(i.issue_id for i in items) == ["I-a", "I-b"]
    assert all(i.project == "Valkyrie" and i.kind == "project" for i in items)
    assert all(i.field == "description" for i in items)
    assert items[0].new.startswith("Hermes Linear channel plugin")


async def test_project_update_without_delegated_tickets_ignored(ctx, store):
    out = await route(_human_project_event(), ctx)
    assert len(out) == 1 and isinstance(out[0], Ignore)


async def test_project_status_post_fans_out(ctx, store):
    _delegate(store, "I-a", PROJECT_ID)
    _delegate(store, "I-b", PROJECT_ID)
    ev = {
        "type": "ProjectUpdate", "action": "create", "createdAt": "2026-11-21T22:00:00Z",
        "actor": copy.deepcopy(LABELS["actor"]),
        "data": {"id": "PU1", "body": "z" * 2000, "health": "onTrack",
                 "projectId": PROJECT_ID,
                 "project": {"id": PROJECT_ID, "name": "Valkyrie"},
                 "userId": HUMAN, "user": {"id": HUMAN, "name": "Test User"}},
    }
    items = _items(await route(ev, ctx))
    assert sorted(i.issue_id for i in items) == ["I-a", "I-b"]
    assert all(i.field == "status post" and i.project == "Valkyrie" for i in items)
    assert all(len(i.new) <= DESCRIPTION_SNIPPET for i in items)


# -------------------------------------------------- review fixes (round 1)


def _no_actor_toplevel():
    ev = copy.deepcopy(TOPLEVEL)
    ev.pop("actor", None)
    return ev


async def test_null_actor_comment_is_human_and_checked(store):
    _delegate(store, C_ISSUE)
    seen = []

    async def allow(uid):
        seen.append(uid)
        return True

    ctx = RouterContext(store=store, app_user_id=APP, allowed=allow, app_user_handle=HANDLE)
    out = await route(_no_actor_toplevel(), ctx)
    assert len(out) == 1 and isinstance(out[0], OpenSessionOnComment)
    assert out[0].user_id == HUMAN and seen == [HUMAN]
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no, app_user_handle=HANDLE)
    assert await route(_no_actor_toplevel(), ctx) == [Ignore("denied actor")]
    assert store.pending_context(C_ISSUE) == []


async def test_external_user_context_dropped_when_not_allowed(store):
    async def only_human(uid):
        return uid == HUMAN

    ctx = RouterContext(store=store, app_user_id=APP, allowed=only_human, app_user_handle=HANDLE)
    _delegate(store, C_ISSUE)
    ev = copy.deepcopy(LABELS)
    ev["actor"] = {"id": "ext-1", "name": "Slack person", "type": "externalUser"}
    assert await route(ev, ctx) == [Ignore("denied actor")]
    ev = copy.deepcopy(TOPLEVEL)
    ev["actor"] = {"id": "ext-1", "name": "Slack person", "type": "externalUser"}
    ev["data"]["userId"] = None
    ev["data"].pop("user", None)
    assert await route(ev, ctx) == [Ignore("denied actor")]


async def test_explicit_bot_context_exempt(store):
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no, app_user_handle=HANDLE)
    _delegate(store, C_ISSUE)
    for actor_type in ("integration", "oauthClient"):
        ev = copy.deepcopy(LABELS)
        ev["actor"] = {"id": "bot-1", "name": "GitHub", "type": actor_type}
        (item,) = _items(await route(ev, ctx))
        assert item.field == "labels"
        # a top-level comment by an explicit bot is context, never a session
        ev = copy.deepcopy(TOPLEVEL)
        ev["actor"] = {"id": "bot-1", "name": "GitHub", "type": actor_type}
        out = await route(ev, ctx)
        assert not any(isinstance(d, OpenSessionOnComment) for d in out)
        (item,) = _items(out)
        assert item.kind == "comment"
    # botActor alone (actor absent) is an explicit bot signal too
    ev = copy.deepcopy(LABELS)
    ev.pop("actor")
    ev["botActor"] = {"id": "b", "name": "Automation", "type": "workflow"}
    assert isinstance((await route(ev, ctx))[0], AddContext)


_INTEGRATION = {"id": "bot-1", "name": "GitHub", "type": "integration"}


async def test_issue_created_by_bot_human_edit_still_checked(store):
    # Issue ``data.botActor`` is "the bot that created the issue", not who
    # triggered this update: a human editing it must still pass the allowlist.
    _delegate(store, C_ISSUE)
    ev = copy.deepcopy(LABELS)
    assert ev["actor"]["type"] == "user"
    ev["data"]["botActor"] = dict(_INTEGRATION)
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no, app_user_handle=HANDLE)
    assert await route(copy.deepcopy(ev), ctx) == [Ignore("denied actor")]
    assert store.pending_context(C_ISSUE) == []
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_yes, app_user_handle=HANDLE)
    (item,) = _items(await route(copy.deepcopy(ev), ctx))
    assert item.actor == LABELS["actor"]["name"] and item.actor != "GitHub"


def _attachment_event():
    return {
        "type": "Attachment", "action": "create", "createdAt": "2026-11-21T21:00:00Z",
        "actor": copy.deepcopy(LABELS["actor"]),
        "data": {"id": "A1", "issueId": C_ISSUE, "title": "PR #12",
                 "url": "https://github.com/x/y/pull/12", "updatedAt": "t"},
    }


def _project_update_event():
    return {
        "type": "ProjectUpdate", "action": "create", "createdAt": "2026-11-21T22:00:00Z",
        "actor": copy.deepcopy(LABELS["actor"]),
        "data": {"id": "PU1", "body": "status", "projectId": PROJECT_ID,
                 "project": {"id": PROJECT_ID, "name": "Valkyrie"},
                 "userId": HUMAN, "user": {"id": HUMAN, "name": "Test User"}},
    }


@pytest.mark.parametrize("make", [_attachment_event, _human_project_event, _project_update_event],
                         ids=["Attachment", "Project", "ProjectUpdate"])
async def test_entity_bot_actor_not_exempt_for_non_comment(store, make):
    _delegate(store, C_ISSUE, PROJECT_ID)
    ev = make()
    ev["data"]["botActor"] = dict(_INTEGRATION)
    ctx = RouterContext(store=store, app_user_id=APP, allowed=_no, app_user_handle=HANDLE)
    assert await route(ev, ctx) == [Ignore("denied actor")]
    # event-level botActor is still an explicit bot signal
    ev = make()
    ev["botActor"] = dict(_INTEGRATION)
    assert isinstance((await route(ev, ctx))[0], AddContext)


async def test_echo_issue_update_still_refreshes_cache(ctx, store):
    _delegate(store, S_ISSUE)
    ev = copy.deepcopy(STATE)
    ev["actor"]["id"] = APP
    ev["data"]["state"] = {"id": "st-done", "name": "Done", "type": "completed"}
    ev["data"]["projectId"] = PROJECT_ID
    assert await route(ev, ctx) == [Ignore("echo")]
    t = store.ticket(S_ISSUE)
    assert t.state_type == "completed" and t.project_id == PROJECT_ID
    assert store.state_name("st-done") == "Done"
    assert store.pending_context(S_ISSUE) == []


async def test_raw_id_fields_not_forwarded(ctx, store):
    _delegate(store, S_ISSUE, PROJECT_ID)
    ev = copy.deepcopy(STATE)
    ev["updatedFrom"] = {"updatedAt": "t", "cycleId": "c-old", "teamId": "t-old",
                         "parentId": None, "lastUpdateId": "u-old"}
    ev["data"].update({"cycleId": "c-new", "teamId": "t-new", "parentId": "p-1"})
    assert await route(ev, ctx) == []
    # a status post also emits a Project update (lastUpdateId + health): not context
    ev = _human_project_event()
    ev["updatedFrom"] = {"updatedAt": "t", "lastUpdateId": "u-old", "health": "atRisk"}
    ev["data"].update({"lastUpdateId": "u-new", "health": "onTrack"})
    out = await route(ev, ctx)
    assert out == [Ignore("no meaningful project change")]
    # health on its own is still a real change
    ev["updatedFrom"] = {"updatedAt": "t", "health": "atRisk"}
    (item,) = _items(await route(ev, ctx))
    assert (item.field, item.old, item.new) == ("health", "atRisk", "onTrack")


# ------------------------------------------------------------ misc types


async def test_oauth_revoked_fatal(ctx):
    out = await route({"type": "OAuthApp", "action": "revoked", "data": {}}, ctx)
    assert out == [Fatal("oauth app revoked")]


async def test_notifications_ignored(ctx):
    for t in ("AppUserNotification", "PermissionChange"):
        out = await route({"type": t, "action": "create", "data": {}}, ctx)
        assert len(out) == 1 and isinstance(out[0], Ignore)
