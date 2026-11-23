"""Tests for hermes_linear.settings and hermes_linear.models."""

from __future__ import annotations

import dataclasses

import pytest

from hermes_linear.models import (
    AddContext,
    AwaitDelegation,
    ContextItem,
    Decision,
    Fatal,
    Ignore,
    Interrupt,
    Refuse,
    Reply,
    ResolveControl,
    SetMode,
    StartTurn,
)
from hermes_linear.settings import (
    Settings,
    SettingsError,
    load_settings,
    parse_allowlist,
)

REQUIRED_CONFIG = {"client_id": "cid-123"}
REQUIRED_SECRETS = {
    "LINEAR_CLIENT_SECRET": "csecret",
    "LINEAR_WEBHOOK_SECRET": "wsecret",
}


def _getters(config=None, secrets=None):
    cfg = dict(REQUIRED_CONFIG if config is None else config)
    sec = dict(REQUIRED_SECRETS if secrets is None else secrets)
    return (lambda key, default=None: cfg.get(key, default)), (lambda key: sec.get(key))


# ---------------------------------------------------------------- parse_allowlist


def test_parse_allowlist_forms():
    assert parse_allowlist(["a@x.com", " B@X.com "]) == frozenset({"a@x.com", "b@x.com"})
    assert parse_allowlist('["u1","*"]') == frozenset({"u1", "*"})
    assert parse_allowlist("u1, u2") == frozenset({"u1", "u2"})
    assert parse_allowlist(None) == frozenset()


def test_parse_allowlist_keeps_uuid_case_and_drops_blanks():
    assert parse_allowlist("ABC-Uuid, ,Val@Example.COM,") == frozenset(
        {"ABC-Uuid", "val@example.com"}
    )
    assert parse_allowlist("") == frozenset()
    assert parse_allowlist([]) == frozenset()
    assert parse_allowlist(("u1", "u1")) == frozenset({"u1"})


# ---------------------------------------------------------------- load_settings


def test_load_settings_defaults():
    get_config, get_secret = _getters()
    s = load_settings(get_config, get_secret, {})
    assert isinstance(s, Settings)
    assert s.client_id == "cid-123"
    assert s.client_secret == "csecret"
    assert s.webhook_secret == "wsecret"
    assert s.allowed_users == frozenset()
    assert s.allow_all_users is False
    assert s.auto_start is True
    assert s.session_url == ""
    assert s.listener_host == "127.0.0.1"
    assert s.listener_port == 8650


def test_out_of_scope_writes_default_approve():
    get_config, get_secret = _getters()
    assert load_settings(get_config, get_secret, {}).out_of_scope_writes == "approve"


def test_out_of_scope_writes_case_insensitive():
    get_config, get_secret = _getters({**REQUIRED_CONFIG, "out_of_scope_writes": " Deny "})
    assert load_settings(get_config, get_secret, {}).out_of_scope_writes == "deny"


def test_out_of_scope_writes_invalid_raises():
    get_config, get_secret = _getters({**REQUIRED_CONFIG, "out_of_scope_writes": "ask"})
    with pytest.raises(SettingsError, match="'out_of_scope_writes'"):
        load_settings(get_config, get_secret, {})


def test_load_settings_explicit_values():
    get_config, get_secret = _getters(
        config={
            "client_id": " cid ",
            "auto_start": "false",
            "allow_all_users": True,
            "session_url": "https://h.example/s/{session_id}",
            "listener_host": "0.0.0.0",
            "listener_port": "9000",
        }
    )
    s = load_settings(get_config, get_secret, {})
    assert s.client_id == "cid"
    assert s.auto_start is False
    assert s.allow_all_users is True
    assert s.session_url == "https://h.example/s/{session_id}"
    assert s.listener_host == "0.0.0.0"
    assert s.listener_port == 9000


def test_allowlist_union_env():
    get_config, get_secret = _getters(
        config={**REQUIRED_CONFIG, "allowed_users": ["A@X.com", "u1"]}
    )
    s = load_settings(get_config, get_secret, {"LINEAR_ALLOWED_USERS": "u2, B@x.COM"})
    assert s.allowed_users == frozenset({"a@x.com", "u1", "u2", "b@x.com"})


@pytest.mark.parametrize("value", ["true", "1", "yes", " TRUE "])
def test_allow_all_users_from_env(value):
    get_config, get_secret = _getters()
    s = load_settings(get_config, get_secret, {"LINEAR_ALLOW_ALL_USERS": value})
    assert s.allow_all_users is True


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_allow_all_users_env_falsey(value):
    get_config, get_secret = _getters()
    s = load_settings(get_config, get_secret, {"LINEAR_ALLOW_ALL_USERS": value})
    assert s.allow_all_users is False


def test_missing_required_raises():
    get_config, get_secret = _getters(config={"client_id": ""})
    with pytest.raises(SettingsError, match="client_id"):
        load_settings(get_config, get_secret, {})


@pytest.mark.parametrize("key", ["LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET"])
def test_missing_secret_raises(key):
    secrets = {k: v for k, v in REQUIRED_SECRETS.items() if k != key}
    get_config, get_secret = _getters(secrets=secrets)
    with pytest.raises(SettingsError, match=key):
        load_settings(get_config, get_secret, {})


def test_invalid_port_raises():
    get_config, get_secret = _getters(config={**REQUIRED_CONFIG, "listener_port": "abc"})
    with pytest.raises(SettingsError, match="listener_port"):
        load_settings(get_config, get_secret, {})


def test_settings_error_is_value_error_and_settings_frozen():
    assert issubclass(SettingsError, ValueError)
    get_config, get_secret = _getters()
    s = load_settings(get_config, get_secret, {})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.client_id = "x"  # type: ignore[misc]


def test_secrets_not_in_repr():
    get_config, get_secret = _getters()
    s = load_settings(get_config, get_secret, {})
    assert "csecret" not in repr(s)
    assert "wsecret" not in repr(s)


# ---------------------------------------------------------------- models


def test_decision_models_frozen_with_fields():
    item = ContextItem(
        issue_id="i1", kind="issue", field="state", old="Todo", new="Done",
        actor="Val", at="2026-11-21T00:00:00Z",
    )
    assert item.project is None
    decisions = [
        StartTurn(
            agent_session_id="s1", activity_id="a1", issue_id="i1", chat_id="c1",
            thread_id=None, user_id="u1", user_name="Val", text="hi",
            first_turn=True, ack="On it",
        ),
        AddContext(items=(item,)),
        ResolveControl(
            agent_session_id="s1", request_id="r1", kind="approval",
            actor_id="u1", answer="yes",
        ),
        Interrupt(agent_session_id="s1"),
        Refuse(agent_session_id="s1", text="no"),
        Reply(agent_session_id="s1", issue_id="i1", text="task"),
        SetMode(issue_id="i1", mode="delegated", generation=2),
        AwaitDelegation(issue_id="i1", deadline_s=5),
        Fatal(reason="boom"),
        Ignore(reason="dup"),
    ]
    for d in decisions:
        assert dataclasses.is_dataclass(d)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(d, dataclasses.fields(d)[0].name, "changed")
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.new = "x"  # type: ignore[misc]

    assert [f.name for f in dataclasses.fields(StartTurn)] == [
        "agent_session_id", "activity_id", "issue_id", "chat_id", "thread_id",
        "user_id", "user_name", "text", "first_turn", "ack",
    ]
    assert [f.name for f in dataclasses.fields(ContextItem)] == [
        "issue_id", "kind", "field", "old", "new", "actor", "at", "project",
    ]
    # Decision alias covers every decision type.
    args = set(getattr(Decision, "__args__", ()))
    assert args == {type(d) for d in decisions}
