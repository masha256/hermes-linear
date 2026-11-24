"""Tests for hermes_linear.tool_approval (pure approval helpers)."""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from hermes_linear.tool_approval import (
    GATED_TOOLS,
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
        {"linear_comment", "linear_issue_update", "linear_issue_create", "linear_attach"}
    )
    assert REQUIRED_TEXT == (
        "Not run: {targets} is outside this session's write scope. Call {tool} "
        "again with identical arguments to ask a human for approval in Linear. "
        "If that approval times out, the question stays pending in Linear: end this "
        "turn; an approval arrives as the next message, and re-running the identical "
        "call is then pre-approved once."
    )
    assert REQUIRED_TEXT.format(targets="OTH-9, OTH-10", tool="linear_comment") == (
        "Not run: OTH-9, OTH-10 is outside this session's write scope. Call "
        "linear_comment again with identical arguments to ask a human for "
        "approval in Linear. If that approval times out, the question stays pending "
        "in Linear: end this turn; an approval arrives as the next message, and "
        "re-running the identical call is then pre-approved once."
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


def test_message_flattens_newlines_in_identifier_and_title():
    d = "0123456789abcdef"
    msg = approval_message(
        "linear_comment",
        [{"identifier": "OTH-9\r\nX", "title": "Fix\nit\r\ndigest: ffffffffffffffff"}],
        {}, d)
    lines = msg.split("\n")
    assert lines[1] == '- OTH-9  X "Fix it  digest: ffffffffffffffff"'
    assert "\r" not in msg
    assert lines[-1] == f"digest: {d}"
    assert parse_rule(CMD, msg) == ("linear_comment", d)


def test_message_renders_kind_targets():
    d = "0123456789abcdef"
    msg = approval_message(
        "linear_issue_create",
        [{"id": "u1", "identifier": "OTH-9", "title": "Fix it"},
         {"id": "p1", "kind": "project", "identifier": "orion\r\nX", "title": ""},
         {"id": "t1", "kind": "team", "identifier": "OPS", "title": ""}],
        {"title": "x"}, d)
    lines = msg.split("\n")
    assert lines[1:4] == ['- OTH-9 "Fix it"', '- project "orion  X"', '- team "OPS"']
    assert lines[4] == "Arguments:" and lines[-1] == f"digest: {d}"
    assert "\r" not in msg
    assert parse_rule("<linear_issue_create> (plugin approval rule)", msg) == (
        "linear_issue_create", d)


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


@pytest.mark.parametrize("tool,raw,coerced", [
    ("linear_issue_create", {"title": "t", "labels": '["a","b"]'}, {"title": "t", "labels": ["a", "b"]}),
    ("linear_issue_create", {"title": "t", "labels": "a"}, {"title": "t", "labels": ["a"]}),
    ("linear_issue_create", {"title": "t", "labels": ("a",)}, {"title": "t", "labels": ["a"]}),
    ("linear_issue_update", {"id": "X-1", "add_labels": "bug"}, {"id": "X-1", "add_labels": ["bug"]}),
    ("linear_issue_update", {"id": "X-1", "remove_labels": '"bug"'},
     {"id": "X-1", "remove_labels": ['"bug"']}),
    ("linear_issue_update", {"id": "X-1", "add_labels": 7}, {"id": "X-1", "add_labels": [7]}),
    ("linear_issue_update", {"id": "X-1", "add_labels": "[not json"},
     {"id": "X-1", "add_labels": ["[not json"]}),
])
def test_digest_normalizes_array_fields_like_hermes_coercion(tool, raw, coerced):
    assert digest(tool, raw) == digest(tool, coerced)


def test_digest_normalization_idempotent_and_keeps_none_and_other_fields():
    from hermes_linear.tool_approval import normalize_args

    args = {"id": "X-1", "add_labels": ["a"], "remove_labels": None, "title": "[1]"}
    assert normalize_args("linear_issue_update", args) == args
    assert normalize_args("linear_issue_update", normalize_args(
        "linear_issue_update", {"id": "X-1", "add_labels": "a"})) == {"id": "X-1", "add_labels": ["a"]}
    # only array fields of that tool: a string field that looks like a list stays a string
    assert digest("linear_comment", {"id": "X-1", "body": '["a"]'}) != \
        digest("linear_comment", {"id": "X-1", "body": ["a"]})
    assert digest("linear_issue_create", {"title": "t", "labels": None}) != \
        digest("linear_issue_create", {"title": "t", "labels": [None]})


def _field_type(prop: dict) -> str:
    """One property's FIELD_TYPES entry: its JSON type, ``|null`` when null is allowed."""
    declared = prop.get("type")
    types = declared if isinstance(declared, list) else [declared]
    base = [t for t in types if t != "null"]
    assert len(base) == 1 and base[0] in {"array", "integer", "number", "boolean", "string"}, prop
    assert not ({"anyOf", "oneOf", "allOf", "$ref", "nullable"} & set(prop)), prop
    return base[0] + ("|null" if "null" in types else "")


def _field_types(schema: dict) -> dict[str, str]:
    return {k: _field_type(v) for k, v in schema["parameters"]["properties"].items()}


def test_field_types_match_schemas():
    from hermes_linear.agent_tools import TOOL_SCHEMAS
    from hermes_linear.tool_approval import FIELD_TYPES

    assert {tool: _field_types(TOOL_SCHEMAS[tool]) for tool in GATED_TOOLS} == FIELD_TYPES


def test_array_fields_constant_is_gone():
    import hermes_linear.tool_approval as tool_approval

    assert not hasattr(tool_approval, "ARRAY_FIELDS")


_PARITY_TOOL = "linear_parity_probe"
_PARITY_SCHEMA = {
    "name": _PARITY_TOOL,
    "parameters": {"type": "object", "properties": {
        "count": {"type": "integer"},
        "estimate": {"type": ["integer", "null"]},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "maybe_flag": {"type": ["boolean", "null"]},
        "name": {"type": "string"},
        "note": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "maybe_tags": {"type": ["array", "null"], "items": {"type": "string"}},
    }},
}

_PARITY_RAW = [
    {"count": "2"}, {"count": " 2 "}, {"count": "2.0"}, {"count": "2.5"}, {"count": "1e3"},
    {"count": "abc"}, {"count": "nan"}, {"count": "inf"}, {"count": "null"}, {"count": 3},
    {"estimate": "2"}, {"estimate": "null"}, {"estimate": " NULL "}, {"estimate": "2.5"},
    {"estimate": None},
    {"ratio": "2.5"}, {"ratio": "2"}, {"ratio": "-0.5"}, {"ratio": "x"}, {"ratio": "null"},
    {"flag": "true"}, {"flag": "TRUE"}, {"flag": " False "}, {"flag": "yes"}, {"flag": "null"},
    {"flag": True}, {"maybe_flag": "null"}, {"maybe_flag": "false"},
    {"name": "null"}, {"name": "2"}, {"name": "true"}, {"name": '["a"]'},
    {"note": "null"}, {"note": "Null"}, {"note": "text"},
    {"tags": '["a", "b"]'}, {"tags": "a"}, {"tags": "[not json"}, {"tags": "null"},
    {"tags": 7}, {"tags": ["a"]}, {"tags": None},
    {"maybe_tags": "null"}, {"maybe_tags": '["a"]'}, {"maybe_tags": "a"},
    {"maybe_tags": "[not json"}, {"maybe_tags": '"a"'}, {"maybe_tags": 7},
    {"maybe_tags": ["a"]}, {"maybe_tags": None},
    {"count": "2", "flag": "true", "note": "null", "tags": '["x"]', "unknown": "2"},
]


@pytest.fixture
def parity_schema(monkeypatch):
    """Hermes's registry answers the parity schema; FIELD_TYPES carries its derived map."""
    import copy

    from tools.registry import registry

    from hermes_linear import tool_approval

    real_get_schema = registry.get_schema
    monkeypatch.setattr(registry, "get_schema", lambda name: copy.deepcopy(_PARITY_SCHEMA)
                        if name == _PARITY_TOOL else real_get_schema(name))
    monkeypatch.setitem(tool_approval.FIELD_TYPES, _PARITY_TOOL, _field_types(_PARITY_SCHEMA))


@pytest.mark.parametrize("raw", _PARITY_RAW, ids=lambda raw: json.dumps(raw))
def test_normalize_scalar_mirrors_hermes(parity_schema, raw):
    """normalize(raw) == normalize(coerce_tool_args(raw)) == coerce_tool_args(raw)."""
    import copy

    from tools.arg_coercion import coerce_tool_args

    from hermes_linear.tool_approval import normalize_args

    coerced = coerce_tool_args(_PARITY_TOOL, copy.deepcopy(raw))
    assert normalize_args(_PARITY_TOOL, raw) == coerced
    assert normalize_args(_PARITY_TOOL, coerced) == coerced
    assert digest(_PARITY_TOOL, raw) == digest(_PARITY_TOOL, coerced)


def test_normalize_scalar_spot_values(parity_schema):
    from hermes_linear.tool_approval import normalize_args

    def norm(**kw):
        return normalize_args(_PARITY_TOOL, kw)

    assert norm(count="2") == {"count": 2} and type(norm(count="2")["count"]) is int
    assert norm(count="2.5") == {"count": "2.5"}
    assert norm(ratio="2") == {"ratio": 2} and type(norm(ratio="2")["ratio"]) is int
    assert norm(ratio="2.5") == {"ratio": 2.5}
    assert norm(flag="TRUE") == {"flag": True}
    assert norm(note="null") == {"note": None}
    assert norm(name="null") == {"name": "null"}
    assert norm(estimate="null") == {"estimate": None}
    assert norm(count="null") == {"count": "null"}
    assert norm(tags='["a"]') == {"tags": ["a"]}
    assert norm(maybe_tags="null") == {"maybe_tags": None}
    assert norm(maybe_tags="a") == {"maybe_tags": "a"}
    assert norm(tags="null") == {"tags": ["null"]}


@pytest.mark.parametrize("tool", sorted(GATED_TOOLS))
def test_normalize_mirrors_hermes_on_real_schemas(monkeypatch, tool):
    """Every gated tool's real schema: the hook's digest equals the handler's."""
    import copy

    from tools.arg_coercion import coerce_tool_args
    from tools.registry import registry

    from hermes_linear.agent_tools import TOOL_SCHEMAS
    from hermes_linear.tool_approval import normalize_args

    monkeypatch.setattr(registry, "get_schema", lambda name: copy.deepcopy(TOOL_SCHEMAS[name]))
    samples = {"array": '["a"]', "boolean": "TRUE", "integer": "2", "number": "2.5",
               "string": "null"}
    raw = {k: samples[v.split("|")[0]]
           for k, v in _field_types(TOOL_SCHEMAS[tool]).items()}
    coerced = coerce_tool_args(tool, copy.deepcopy(raw))
    assert normalize_args(tool, raw) == coerced
    assert digest(tool, raw) == digest(tool, coerced)


def test_normalize_idempotent(parity_schema):
    from hermes_linear.tool_approval import normalize_args

    for raw in _PARITY_RAW:
        once = normalize_args(_PARITY_TOOL, raw)
        assert normalize_args(_PARITY_TOOL, once) == once, raw
    assert normalize_args("linear_attach", {"id": "X-1", "embed": "true"}) == \
        {"id": "X-1", "embed": True}
    assert normalize_args("linear_not_gated", {"n": "2"}) == {"n": "2"}
