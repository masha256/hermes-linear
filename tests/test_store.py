"""Tests for the per-profile SQLite store."""

from __future__ import annotations

import threading

import pytest

from hermes_linear.constants import DEDUP_TTL_S, MAX_PENDING_ITEMS, USER_CACHE_TTL_S
from hermes_linear.models import ContextItem
from hermes_linear import store as store_mod
from hermes_linear.store import ApprovalRequest, Store


@pytest.fixture
def store() -> Store:
    s = Store.open(":memory:")
    yield s
    s.close()


def _item(issue="I1", kind="Issue", field="state", old="a", new="b", at="t0", actor="u"):
    return ContextItem(issue_id=issue, kind=kind, field=field, old=old, new=new, actor=actor, at=at)


def test_user_version_set(store):
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_upsert_placeholder_then_fill(store):
    t = store.upsert_ticket("I1")
    assert t.issue_id == "I1" and t.identifier is None and t.mode == "untouched"
    store.upsert_ticket("I1", identifier="RET-1", project_id="P1")
    store.upsert_ticket("I1", team_id="T1")  # partial must not null-out
    t = store.ticket("I1")
    assert t.identifier == "RET-1"
    assert t.project_id == "P1"
    assert t.team_id == "T1"
    assert store.ticket("missing") is None


def test_generation_defaults_to_1_on_first_delegate(store):
    store.upsert_ticket("I1")
    store.set_mode("I1", "delegated")
    t = store.ticket("I1")
    assert t.mode == "delegated" and t.generation == 1
    store.set_mode("I1", "mention")
    assert store.ticket("I1").generation == 1
    store.set_mode("I1", "delegated", generation=3)
    assert store.ticket("I1").generation == 3
    store.set_mode("I1", "delegated")
    assert store.ticket("I1").generation == 3


def test_set_mode_creates_placeholder(store):
    store.set_mode("I9", "mention")
    t = store.ticket("I9")
    assert t.mode == "mention"


def test_set_mode_rejects_unknown(store):
    with pytest.raises(ValueError):
        store.set_mode("I1", "bogus")  # type: ignore[arg-type]


def test_delegated_in_project(store):
    store.upsert_ticket("I1", project_id="P")
    store.upsert_ticket("I2", project_id="P")
    store.upsert_ticket("I3", project_id="Q")
    store.set_mode("I1", "delegated")
    store.set_mode("I3", "delegated")
    assert store.delegated_in_project("P") == ["I1"]


def test_clear_project(store):
    store.upsert_ticket("I1", project_id="P")
    store.set_mode("I1", "delegated")
    store.clear_project("I1")
    assert store.ticket("I1").project_id is None
    assert store.delegated_in_project("P") == []
    store.clear_project("missing")  # no-op
    assert store.ticket("missing") is None


def test_seen_webhook_ttl(store):
    assert store.seen_webhook("w1", 0) is False
    assert store.seen_webhook("w1", 10) is True
    assert store.seen_webhook("w1", DEDUP_TTL_S + 1) is False  # expired → fresh


def test_pending_cap_merges(store):
    items = [_item(old=f"o{i}", new=f"n{i}", at=f"t{i:04d}") for i in range(250)]
    store.add_context(items)
    pending = store.pending_context("I1")
    assert len(pending) <= MAX_PENDING_ITEMS
    assert pending[-1].new == "n249"
    assert any(p.old == "o0" for p in pending)


def test_pending_cap_deletes_oldest_when_no_merge(store):
    items = [_item(field=f"f{i}", new=f"n{i}", at=f"t{i:04d}") for i in range(250)]
    store.add_context(items)
    pending = store.pending_context("I1")
    assert len(pending) == MAX_PENDING_ITEMS
    assert pending[0].field == "f50"
    assert pending[-1].field == "f249"


def test_pending_cap_per_ticket(store):
    store.add_context([_item(issue="I2")])
    store.add_context([_item(field=f"f{i}") for i in range(250)])
    assert len(store.pending_context("I2")) == 1


def test_context_roundtrip_preserves_project(store):
    it = ContextItem("I1", "Project", "name", None, "X", "u", "t", project="P1")
    store.add_context([it])
    assert store.pending_context("I1") == [it]


def test_consume_context(store):
    store.add_context([_item(), _item(issue="I2")])
    store.consume_context("I1", "turn-1")
    assert store.pending_context("I1") == []
    assert len(store.pending_context("I2")) == 1
    store.add_context([_item(new="c")])
    assert [p.new for p in store.pending_context("I1")] == ["c"]


def test_pending_context_batch_empty(store):
    assert store.pending_context_batch("I1") == ([], None)


def test_pending_context_batch_matches_pending(store):
    store.add_context([_item(new="x"), _item(field="title", new="y"), _item(issue="I2")])
    items, watermark = store.pending_context_batch("I1")
    assert items == store.pending_context("I1")
    assert watermark is not None


def test_consume_up_to_watermark_keeps_later_items(store):
    store.add_context([_item(new="x"), _item(field="title", new="y")])
    rendered, watermark = store.pending_context_batch("I1")
    assert [p.new for p in rendered] == ["x", "y"]
    store.add_context([_item(field="labels", new="late")])  # arrives before admission
    store.consume_context("I1", "turn-1", up_to_id=watermark)
    assert [p.new for p in store.pending_context("I1")] == ["late"]


def test_consume_without_watermark_consumes_all(store):
    store.add_context([_item(new="x")])
    _, watermark = store.pending_context_batch("I1")
    store.add_context([_item(field="title", new="late")])
    store.consume_context("I1", "turn-1")
    assert store.pending_context("I1") == []
    assert watermark is not None


def test_merge_across_watermark_keeps_merged_row_pending(store):
    # Rendered batch: one "state" change plus filler of distinct fields.
    filler = [_item(field=f"f{i}", new=f"v{i}") for i in range(MAX_PENDING_ITEMS - 1)]
    store.add_context([_item(old="o0", new="n0"), *filler])
    rendered, watermark = store.pending_context_batch("I1")
    assert len(rendered) == MAX_PENDING_ITEMS
    # A later, unrendered "state" change pushes over the cap -> merge of the
    # "state" group. The merged row keeps the group's MAX id (> watermark).
    store.add_context([_item(old="n0", new="n1")])
    store.consume_context("I1", "turn-1", up_to_id=watermark)
    pending = store.pending_context("I1")
    assert [(p.field, p.old, p.new) for p in pending] == [("state", "o0", "n1")]


def test_merge_entirely_below_watermark_is_consumed(store):
    filler = [_item(field=f"f{i}", new=f"v{i}") for i in range(MAX_PENDING_ITEMS - 1)]
    store.add_context([_item(old="o0", new="n0"), *filler, _item(old="n0", new="n1")])
    rendered, watermark = store.pending_context_batch("I1")
    assert len(rendered) == MAX_PENDING_ITEMS  # merged already, all rendered
    store.consume_context("I1", "turn-1", up_to_id=watermark)
    assert store.pending_context("I1") == []


def test_session_root_lookup(store):
    store.record_session("S1", "I1", "C1", "delegation")
    store.record_session("S2", "I1", None, "plugin", plugin_created=True)
    assert store.is_session_root("C1") is True
    assert store.is_session_root("C2") is False
    s = store.session_by_root_comment("C1")
    assert s.agent_session_id == "S1" and s.kind == "delegation" and s.plugin_created is False
    assert store.session("S2").plugin_created is True
    assert store.session("nope") is None
    assert store.session_by_root_comment("C2") is None


def test_turns_thread_exact_match(store):
    store.record_turn("S1:a1", "S1", "chat")
    store.record_turn("S2:a1", "S2", "chat", thread_id="th2")
    store.record_turn("S1:a2", "S1", "chat")
    assert store.turn("S2:a1").thread_id == "th2"
    assert store.turn("x") is None
    assert store.latest_turn_for_chat("chat").message_id == "S1:a2"
    assert store.latest_turn_for_chat("chat", "th2").message_id == "S2:a1"
    assert store.latest_turn_for_chat("chat", "th3") is None
    assert store.latest_turn_for_chat("other") is None


def test_controls(store):
    store.put_control("R1", "approval", "S1", 2, ["approve", "deny"])
    c = store.control_for_session("S1")
    assert c.request_id == "R1" and c.execution_generation == 2
    assert list(c.choices) == ["approve", "deny"]
    store.drop_control("R1")
    assert store.control_for_session("S1") is None


def test_state_and_label_cache(store):
    assert store.state_name("s") is None
    store.cache_state("s", "Todo")
    store.cache_state("s", "In Progress")
    assert store.state_name("s") == "In Progress"
    store.cache_label("l", "bug")
    assert store.label_name("l") == "bug"
    assert store.label_name("x") is None


def test_user_cache_ttl(store):
    assert store.cached_user("u", 0) is None
    store.put_user("u", "a@b", "A", 100)
    u = store.cached_user("u", 100 + USER_CACHE_TTL_S - 1)
    assert u.email == "a@b" and u.name == "A"
    assert store.cached_user("u", 100 + USER_CACHE_TTL_S + 1) is None


def test_reopen_file_persists(tmp_path):
    p = tmp_path / "d.db"
    s = Store.open(p)
    s.record_turn("S:a", "S", "chat")
    s.close()
    s2 = Store.open(p)
    assert s2.turn("S:a").agent_session_id == "S"
    s2.close()


def test_concurrent_threads_smoke(tmp_path):
    s = Store.open(tmp_path / "c.db")
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(50):
                s.add_context([_item(field=f"f{n}-{i}")])
                s.record_turn(f"S{n}:{i}", f"S{n}", "chat")
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert len(s.pending_context("I1")) == MAX_PENDING_ITEMS
    s.close()


def test_sessions_for_issue(store):
    store.record_session("s1", "I1", "c1", "delegation")
    store.record_session("s2", "I1", "c2", "mention")
    store.record_session("s3", "I2", "c3", "mention")
    assert [s.agent_session_id for s in store.sessions_for_issue("I1")] == ["s1", "s2"]
    assert store.sessions_for_issue("none") == []


def test_control_session_key_roundtrip(store):
    store.put_control("R9", "approval", "S9", 1, ["once", "deny"], session_key="agent:main:linear:dm:i:1")
    assert store.control_for_session("S9").session_key == "agent:main:linear:dm:i:1"
    store.put_control("R8", "clarify", "S8", 1, [])
    assert store.control_for_session("S8").session_key is None


def test_control_session_key_migrates_old_db(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pending_controls (request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "agent_session_id TEXT NOT NULL, execution_generation INTEGER NOT NULL, "
        "choices TEXT NOT NULL, created_at REAL NOT NULL)")
    conn.commit()
    conn.close()
    s = Store.open(path)
    s.put_control("R1", "approval", "S1", 1, [], session_key="sk")
    assert s.control_for_session("S1").session_key == "sk"
    s.close()


# --------------------------------------------- pending questions (RET-94 T2)


def test_migrates_old_pending_controls(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pending_controls (request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "agent_session_id TEXT NOT NULL, execution_generation INTEGER NOT NULL, "
        "choices TEXT NOT NULL, created_at REAL NOT NULL, session_key TEXT)")
    conn.execute(
        "INSERT INTO pending_controls VALUES ('R1', 'clarify', 'S1', 1, '[]', 1.0, 'sk')")
    conn.commit()
    conn.close()
    s = Store.open(path)
    c = s.control("R1")
    assert c is not None
    assert c.state == "live"
    assert c.question is None and c.command is None
    assert c.ticket_id is None and c.turn_mid is None
    assert c.authority is None
    assert c.session_key == "sk"
    s.close()


def test_put_control_new_fields_roundtrip(store):
    store.put_control("R1", "clarify", "S1", 1, ["a", "b"], session_key="K",
                      question="Which?", turn_mid="m1")
    store.put_control("R2", "approval", "S1", 1, ["once"], command="make deploy")
    c1, c2 = store.control("R1"), store.control("R2")
    assert (c1.state, c1.question, c1.command, c1.turn_mid) == ("live", "Which?", None, "m1")
    assert (c2.state, c2.question, c2.command, c2.turn_mid) == ("live", None, "make deploy", None)


def test_put_control_authority_roundtrip(store):
    """Final review 1: the approval's asking-time authority is kept as JSON, survives the
    switch to pending, and is absent (None) when not captured."""
    auth = {"request_id": "R1", "requester_id": "u1", "requester_name": "Alice",
            "approvers": ["u2"], "chat_type": "dm"}
    store.put_control("R1", "approval", "S1", 1, ["once"], command="ls", authority=auth)
    store.put_control("R2", "approval", "S1", 1, ["once"], command="ls")
    assert store.control("R1").authority == auth
    assert store.control("R2").authority is None
    assert store.mark_control_pending("R1", "ISSUE") is True
    assert store.control("R1").authority == auth


def test_mark_pending_and_queries(store):
    store.put_control("R1", "clarify", "S1", 1, [], question="q1")
    store.put_control("R2", "approval", "S2", 1, ["once"], command="ls")
    assert store.mark_control_pending("R1", "ISSUE") is True
    c = store.control("R1")
    assert c.state == "pending" and c.ticket_id == "ISSUE"
    assert [r.request_id for r in store.live_controls()] == ["R2"]
    assert store.mark_control_pending("gone", None) is False
    # put_control always (re)writes a live row
    store.put_control("R1", "clarify", "S1", 1, [], question="q1")
    assert store.control("R1").state == "live"


def test_controls_for_issue_and_key(store):
    store.record_session("S1", "I1", None, "delegation")
    store.record_session("S2", "I1", None, "mention")
    store.record_session("S3", "I2", None, "mention")
    store.put_control("R1", "clarify", "S1", 1, [], session_key="K1")
    store.put_control("R2", "approval", "S2", 1, [], session_key="K1")
    store.put_control("R3", "clarify", "S3", 1, [], session_key="K2")
    store.put_control("R4", "clarify", "S-unknown", 1, [], session_key="K3")
    store.conn.execute("UPDATE pending_controls SET created_at = 10 WHERE request_id = 'R1'")
    store.conn.execute("UPDATE pending_controls SET created_at = 20 WHERE request_id = 'R2'")
    store.conn.commit()
    assert sorted(r.request_id for r in store.controls_for_issue("I1")) == ["R1", "R2"]
    assert [r.request_id for r in store.controls_for_issue("I2")] == ["R3"]
    assert store.controls_for_issue("nope") == []
    assert [r.request_id for r in store.controls_for_session_key("K1")] == ["R2", "R1"]
    assert [r.request_id for r in store.controls_for_session_key("K3")] == ["R4"]
    assert store.controls_for_session_key("nope") == []


def test_open_tasks_set_and_clear(store):
    store.set_open_task("I1", "A", True)
    store.set_open_task("I1", "B", True)
    store.set_open_task("I1", "B", True)  # idempotent open
    store.set_open_task("I2", "C", True)
    assert sorted(store.open_tasks("I1")) == ["A", "B"]
    store.set_open_task("I1", "A", False)
    store.set_open_task("I1", "A", False)  # idempotent clear
    assert store.open_tasks("I1") == ["B"]
    assert store.open_tasks("I2") == ["C"]
    assert store.open_tasks("none") == []


def test_grant_is_one_use(store):
    store.put_grant("K", "c", "once", "u1", "m1")
    assert store.take_grant("K", "c") == ("once", "u1")
    assert store.take_grant("K", "c") is None
    store.put_grant("K", "c", "once", "u1", "m1")
    store.put_grant("K", "c", "always", "u2", "m1")  # replaces
    store.put_grant("K", "d", "once", "u1", "m2")
    store.put_grant("K2", "c", "once", "u1", "m1")
    store.drop_grants_for_turn("m1")
    assert store.take_grant("K", "c") is None
    assert store.take_grant("K2", "c") is None
    assert store.take_grant("K", "d") == ("once", "u1")


def test_grant_not_offered_stays(store):
    store.put_grant("K", "c", "once", "u1", "m1")
    assert store.take_grant("K", "c", offered=["deny"]) is None
    assert store.take_grant("K", "c", offered=["once", "deny"]) == ("once", "u1")
    assert store.take_grant("K", "c") is None


def test_drop_all_grants(store):
    store.put_grant("K", "c", "once", "u1", "m1")
    store.put_grant("K2", "d", "always", "u2", "m2")
    store.drop_all_grants()
    assert store.take_grant("K", "c") is None and store.take_grant("K2", "d") is None
    store.drop_all_grants()  # idempotent on an empty table


# ------------------------------------------------------------- approval requests (RET-124)


def test_request_put_arm_take(store):
    store.put_request("K", "d1", "linear_comment", ["u-1", "u-2"], "msg")
    r = store.request("K", "d1")
    assert isinstance(r, ApprovalRequest)
    assert (r.session_key, r.digest, r.tool, r.targets, r.message, r.armed) == (
        "K", "d1", "linear_comment", ["u-1", "u-2"], "msg", False)
    assert store.request("K", "other") is None and store.request("K2", "d1") is None
    assert store.take_armed_request("K", "d1") is None  # not armed yet
    armed = store.arm_request("K", "d1")
    assert armed is not None and armed.armed is True and armed.targets == ["u-1", "u-2"]
    taken = store.take_armed_request("K", "d1")
    assert taken == armed
    assert store.take_armed_request("K", "d1") is None
    assert store.request("K", "d1") is None  # deleted by the take
    assert store.arm_request("K", "d1") is None  # nothing left to arm


def test_request_disarm_blocks_take(store):
    store.put_request("K", "d1", "linear_comment", ["u-1"], "msg")
    store.arm_request("K", "d1")
    store.disarm_request("K", "d1")
    assert store.take_armed_request("K", "d1") is None
    r = store.request("K", "d1")
    assert r is not None and r.armed is False  # the request itself stays
    store.disarm_request("K", "missing")  # no-op


def test_request_put_replaces_and_disarms(store):
    store.put_request("K", "d1", "linear_comment", ["u-1"], "first")
    store.arm_request("K", "d1")
    store.put_request("K", "d1", "linear_issue_update", ["u-2"], "second")
    r = store.request("K", "d1")
    assert (r.tool, r.targets, r.message, r.armed) == (
        "linear_issue_update", ["u-2"], "second", False)
    assert store.take_armed_request("K", "d1") is None


def test_request_ttl_prune(store, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(store_mod.time, "time", lambda: now)
    store.put_request("K", "old", "linear_comment", ["u-1"], "m")
    now += 25 * 3600
    assert store.request("K", "old") is not None  # reads never prune
    store.put_request("K", "new", "linear_comment", ["u-1"], "m")
    assert store.request("K", "old") is None
    assert store.request("K", "new") is not None
    assert store_mod.REQUEST_TTL_S == 86_400


def test_drop_all_requests(store):
    store.put_request("K", "d1", "linear_comment", ["u-1"], "m")
    store.put_request("K2", "d2", "linear_comment", [], "m")
    store.drop_all_requests()
    assert store.request("K", "d1") is None and store.request("K2", "d2") is None
    store.drop_all_requests()  # idempotent on an empty table


# ------------------------------------------------------------- created issues (RET-110)


def test_created_issues_keyed_by_owner_ticket(store):
    assert store.created_by("T1") == set()
    store.record_created("T1", "i1")
    store.record_created("T1", "i2")
    store.record_created("T1", "i1")  # idempotent
    store.record_created("T2", "i3")
    assert store.created_by("T1") == {"i1", "i2"}
    assert store.created_by("T2") == {"i3"}


def test_created_issues_survive_reopen(tmp_path):
    path = tmp_path / "db.sqlite"
    s = Store.open(path)
    s.record_created("T1", "i1")
    s.close()
    s = Store.open(path)
    try:
        assert s.created_by("T1") == {"i1"}
    finally:
        s.close()
