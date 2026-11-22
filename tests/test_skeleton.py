import json
import re

import pytest

from conftest import FIXTURE_DIR, fixture_names, load_fixture

REQUIRED_FIXTURES = [
    "agent_created_mention",
    "agent_prompted_reply",
    "agent_created_delegation",
    "comment_create_mention_root",
    "comment_create_reply",
    "comment_create_toplevel_plain",
    "issue_update_delegated",
    "issue_update_labels",
    "issue_update_state",
    "issue_update_description",
    "issue_create_delegated",
    "project_update",
]

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
FAKE_UUID_RE = re.compile(r"00000000-0000-4000-8000-\d{12}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FORBIDDEN = ["@retrograde", "linear.app/retrograde-capital", "retrograde", "machado", "mike"]


def _strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


def test_required_fixtures_present():
    missing = set(REQUIRED_FIXTURES) - set(fixture_names())
    assert not missing, f"missing fixtures: {sorted(missing)}"


@pytest.mark.parametrize("name", REQUIRED_FIXTURES)
def test_fixtures_load_and_scrubbed(name):
    payload = load_fixture(name)
    assert isinstance(payload, dict)
    assert payload.get("type") and payload.get("action")
    assert "headers" not in payload
    for s in _strings(payload):
        low = s.lower()
        for bad in FORBIDDEN:
            assert bad not in low, f"{name}: {bad!r} in {s[:80]!r}"
        for u in UUID_RE.findall(s):
            assert FAKE_UUID_RE.fullmatch(u), f"{name}: unscrubbed UUID {u}"
        for e in EMAIL_RE.findall(s):
            assert e.endswith("@example.com"), f"{name}: unscrubbed email {e}"


def test_fixture_shapes():
    assert load_fixture("agent_created_mention")["agentSession"]["comment"]["body"].startswith(
        "@hermestest this is a top-level comment"
    )
    assert load_fixture("agent_prompted_reply")["action"] == "prompted"
    assert load_fixture("comment_create_reply")["data"]["parentId"]
    assert "delegateId" in load_fixture("issue_update_delegated")["updatedFrom"]
    assert "labelIds" in load_fixture("issue_update_labels")["updatedFrom"]
    assert "stateId" in load_fixture("issue_update_state")["updatedFrom"]
    assert "uploads.linear.app" in load_fixture("issue_update_description")["data"]["description"]
    assert load_fixture("issue_create_delegated")["data"]["delegateId"]
    assert load_fixture("project_update")["type"] == "Project"


def test_uuid_mapping_consistent_across_fixtures():
    # the same entity keeps the same fake id in every fixture
    mention = load_fixture("agent_created_mention")
    root = load_fixture("comment_create_mention_root")
    reply = load_fixture("comment_create_reply")
    assert mention["agentSession"]["commentId"] == root["data"]["id"] == reply["data"]["parentId"]


def test_comment_anchor_scrubbed_consistently():
    # permalinks embed the first 8 hex digits of the comment id; they must
    # follow the fake id rather than leak the real one
    for name in ("comment_create_mention_root", "comment_create_reply"):
        c = load_fixture(name)
        assert c["url"].endswith("#comment-" + c["data"]["id"][-8:]), name


def test_fixture_files_are_payload_only():
    for p in FIXTURE_DIR.glob("*.json"):
        raw = json.loads(p.read_text())
        assert "payload" not in raw and "sig_ok" not in raw


def test_constants():
    from hermes_linear import constants as c

    assert c.REPLAY_WINDOW_S == 60
    assert c.MAX_BODY_BYTES == 1_048_576
    assert c.DEDUP_TTL_S == 86_400
    assert c.DELEGATION_WAIT_S == 5
    assert c.FAST_TOOL_SKIP_S == 2
    assert c.DIGEST_CAP_CHARS == 4_000
    assert c.MAX_PENDING_ITEMS == 200
    assert c.HEARTBEAT_S == 600
    assert c.TYPING_MIN_INTERVAL_S == 60
    assert c.DESCRIPTION_SNIPPET == 800
    assert c.SNAPSHOT_DESCRIPTION == 2_000
    assert c.RESULT_PREVIEW == 500
    assert c.USER_CACHE_TTL_S == 3_600
    assert c.TOKEN_RENEW_MARGIN_S == 86_400
    assert c.WRITE_RETRIES == 3


def test_register_callable():
    from hermes_linear.plugin import register

    assert callable(register)
