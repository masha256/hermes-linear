"""Tests for hermes_linear.access (allowlist policy)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from hermes_linear.access import REFUSAL_TEXT, AccessPolicy

UUID = "3f2a9c1e-7b4d-4e8a-9c0f-1a2b3c4d5e6f"
OTHER_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_refusal_text_verbatim():
    assert REFUSAL_TEXT == (
        "You're not authorized to use this agent. "
        "Ask the Hermes owner to add you to the Linear allowlist."
    )


async def test_default_deny():
    lookup = AsyncMock(return_value="someone@example.com")
    policy = AccessPolicy(frozenset(), False)
    assert await policy.allowed(UUID, lookup) is False
    lookup.assert_not_awaited()


async def test_star_allows():
    lookup = AsyncMock(return_value=None)
    policy = AccessPolicy(frozenset({"*"}), False)
    assert await policy.allowed(UUID, lookup) is True
    lookup.assert_not_awaited()


async def test_allow_all_flag():
    lookup = AsyncMock(return_value=None)
    policy = AccessPolicy(frozenset(), True)
    assert await policy.allowed(UUID, lookup) is True
    lookup.assert_not_awaited()


async def test_uuid_match_no_email_lookup():
    lookup = AsyncMock(return_value="a@example.com")
    policy = AccessPolicy(frozenset({UUID, "a@example.com"}), False)
    assert await policy.allowed(UUID, lookup) is True
    lookup.assert_not_awaited()


async def test_uuid_match_case_insensitive():
    lookup = AsyncMock(return_value=None)
    policy = AccessPolicy(frozenset({UUID.upper()}), False)
    assert await policy.allowed(UUID, lookup) is True
    lookup.assert_not_awaited()


async def test_uuid_mismatch_without_email_entries_skips_lookup():
    lookup = AsyncMock(return_value="a@example.com")
    policy = AccessPolicy(frozenset({OTHER_UUID}), False)
    assert await policy.allowed(UUID, lookup) is False
    lookup.assert_not_awaited()


async def test_email_match_case_insensitive():
    lookup = AsyncMock(return_value="Alice@Example.COM")
    policy = AccessPolicy(frozenset({"ALICE@example.com"}), False)
    assert await policy.allowed(UUID, lookup) is True
    lookup.assert_awaited_once_with(UUID)


async def test_email_mismatch_denied():
    lookup = AsyncMock(return_value="bob@example.com")
    policy = AccessPolicy(frozenset({"alice@example.com"}), False)
    assert await policy.allowed(UUID, lookup) is False


async def test_none_user_denied():
    lookup = AsyncMock(return_value="a@example.com")
    for policy in (
        AccessPolicy(frozenset({"a@example.com"}), False),
        AccessPolicy(frozenset({"*"}), False),
        AccessPolicy(frozenset(), True),
    ):
        assert await policy.allowed(None, lookup) is False
    lookup.assert_not_awaited()


async def test_email_lookup_failure_denies(caplog):
    policy = AccessPolicy(frozenset({"a@example.com"}), False)
    failing = AsyncMock(side_effect=RuntimeError("boom"))
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.access"):
        assert await policy.allowed(UUID, failing) is False
    assert "a@example.com" not in caplog.text
    missing = AsyncMock(return_value=None)
    assert await policy.allowed(UUID, missing) is False


async def test_denial_log_omits_email(caplog):
    policy = AccessPolicy(frozenset({"a@example.com"}), False)
    lookup = AsyncMock(return_value="secret.person@example.com")
    with caplog.at_level(logging.DEBUG, logger="hermes_linear.access"):
        assert await policy.allowed(UUID, lookup) is False
    assert "secret.person" not in caplog.text


def test_entry_properties():
    policy = AccessPolicy(
        frozenset({UUID.upper(), " Alice@Example.com ", "*", ""}), False
    )
    assert policy.uuid_entries == frozenset({UUID})
    assert policy.email_entries == frozenset({"alice@example.com"})
    assert policy.open is True
    assert isinstance(policy.uuid_entries, frozenset)
    assert isinstance(policy.email_entries, frozenset)


def test_open_property():
    assert AccessPolicy(frozenset({UUID}), False).open is False
    assert AccessPolicy(frozenset({UUID}), True).open is True
    assert AccessPolicy(frozenset(), False).open is False


def test_properties_read_only():
    policy = AccessPolicy(frozenset({UUID}), False)
    with pytest.raises(AttributeError):
        policy.open = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        policy.uuid_entries = frozenset()  # type: ignore[misc]
