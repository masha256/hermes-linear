"""Tests for hermes_linear.tool_approval (pure approval helpers)."""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from hermes_linear.tool_approval import (
    GATED_TOOLS,
    PENDING_TEXT,
    REQUIRED_TEXT,
    approval_message,
    digest,
    grant_command,
    parse_rule,
    rule_key,
    split_grant_command,
)

CMD = "<linear_comment> (plugin approval rule)"


def test_constants_verbatim():
    assert GATED_TOOLS == frozenset(
        {"linear_comment", "linear_issue_update", "linear_issue_create"}
    )
    assert REQUIRED_TEXT == (
        "Not run: {targets} is outside this session's write scope. Call {tool} "
        "again with identical arguments to ask a human for approval in Linear."
    )
    assert PENDING_TEXT == (
        "Not run: approval is pending in Linear. End this turn. If it is approved, "
        "the next message says so and re-running this same call with identical "
        "arguments is pre-approved once."
    )
    assert REQUIRED_TEXT.format(targets="OTH-9, OTH-10", tool="linear_comment") == (
        "Not run: OTH-9, OTH-10 is outside this session's write scope. Call "
        "linear_comment again with identical arguments to ask a human for "
        "approval in Linear."
    )


def test_digest_stable_across_key_order():
    a = {"issue": "OTH-9", "body": "hi", "nested": {"x": 1, "y": 2}}
    b = {"nested": {"y": 2, "x": 1}, "body": "hi", "issue": "OTH-9"}
    assert digest("linear_comment", a) == digest("linear_comment", b)


def test_digest_matches_global_constraint_formula():
    args = {"issue": "OTH-9", "body": "héllo ✓"}
    payload = json.dumps(
        {"tool": "linear_comment", "args": args},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert digest("linear_comment", args) == hashlib.sha256(payload).hexdigest()[:16]


def test_digest_non_dict_args_digest_as_empty():
    assert digest("linear_comment", None) == digest("linear_comment", {})
    assert digest("linear_comment", "x") == digest("linear_comment", {})


BASE = {"issue": "OTH-9", "body": "hello", "title": "T"}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("linear_comment", {**BASE, "body": "hello!"}),
        ("linear_comment", {**BASE, "title": "T2"}),
        ("linear_comment", {**BASE, "issue": "OTH-10"}),
        ("linear_issue_update", BASE),
    ],
)
def test_digest_changes_with_body_title_target(tool, args):
    assert digest(tool, args) != digest("linear_comment", BASE)


def test_digest_is_16_hex():
    assert re.fullmatch(r"[0-9a-f]{16}", digest("linear_comment", BASE))


def test_rule_key():
    assert rule_key("linear_comment", "0123456789abcdef") == (
        "linear_comment:0123456789abcdef"
    )


def test_message_cuts_long_strings_and_ends_with_digest():
    long_body = "a" * 2000
    args = {"issue": "OTH-9", "body": long_body, "meta": {"note": "b" * 1600}}
    d = digest("linear_comment", args)
    msg = approval_message(
        "linear_comment",
        [{"identifier": "OTH-9", "title": "Fix it"}, {"identifier": "OTH-10", "title": "Other"}],
        args,
        d,
    )
    lines = msg.split("\n")
    assert lines[0] == "Approve `linear_comment` outside this session's tickets?"
    assert lines[1] == '- OTH-9 "Fix it"'
    assert lines[2] == '- OTH-10 "Other"'
    assert lines[3] == "Arguments:"
    assert lines[-1] == f"digest: {d}"
    preview = "\n".join(lines[4:-1])
    parsed = json.loads(preview)
    assert parsed["body"] == "a" * 1500 + "… [cut, 2000 chars]"
    assert parsed["meta"]["note"] == "b" * 1500 + "… [cut, 1600 chars]"
    assert parsed["issue"] == "OTH-9"
    assert long_body not in msg
    # indent=1, sort_keys
    assert preview.startswith('{\n "body"')
    # Round-trips through parse_rule.
    assert parse_rule(CMD, msg) == ("linear_comment", d)


def test_message_tolerates_non_json_values():
    args = {"when": object()}
    msg = approval_message("linear_comment", [], args, "0123456789abcdef")
    assert msg.split("\n")[-1] == "digest: 0123456789abcdef"


def test_parse_rule_matches_only_linear_tools_with_digest():
    d = "0123456789abcdef"
    good = f"Approve `linear_comment` ...\nArguments:\n{{}}\ndigest: {d}\n\n"
    assert parse_rule(CMD, good) == ("linear_comment", d)
    assert parse_rule("<terminal> (plugin approval rule)", good) is None
    assert parse_rule("linear_comment (plugin approval rule)", good) is None
    assert parse_rule(CMD, "Approve `linear_comment`\nArguments:\n{}") is None
    assert parse_rule(CMD, f"digest: {d}\nArguments:\n{{}}") is None
    assert parse_rule(CMD, None) is None
    assert parse_rule(CMD, "digest: 0123") is None


def test_grant_command_round_trip():
    d = "0123456789abcdef"
    granted = grant_command(CMD, d)
    assert granted == f"{CMD}#{d}"
    assert split_grant_command(granted) == ("linear_comment", d)
    assert split_grant_command(CMD) is None
    assert split_grant_command(None) is None
    assert split_grant_command(f"<terminal> (plugin approval rule)#{d}") == ("terminal", d)
    assert split_grant_command(f"{CMD}#xyz") is None
