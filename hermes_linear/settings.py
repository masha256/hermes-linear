"""User settings for the Linear channel.

``load_settings`` is a pure function of its injected callables: in
production ``get_config`` is the plugin context's ``ctx.get_config`` and
``get_secret`` is ``agent.secret_scope.get_secret``; both resolve the
active profile from the *calling* context, so call this from the adapter's
``__init__``/``connect()`` only (never from the shared listener or worker
threads). No Hermes imports and no direct ``os.environ`` reads here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_LISTENER_HOST = "127.0.0.1"
DEFAULT_LISTENER_PORT = 8650

CLIENT_SECRET_KEY = "LINEAR_CLIENT_SECRET"
WEBHOOK_SECRET_KEY = "LINEAR_WEBHOOK_SECRET"
ALLOWED_USERS_ENV = "LINEAR_ALLOWED_USERS"
ALLOW_ALL_USERS_ENV = "LINEAR_ALLOW_ALL_USERS"

OUT_OF_SCOPE_MODES = ("approve", "deny", "allow")
OUTSIDE_MODES = OUT_OF_SCOPE_MODES  # outside_session_writes (RET-148)

_TRUE = frozenset({"true", "1", "yes"})
_FALSE = frozenset({"false", "0", "no", "off", ""})


class SettingsError(ValueError):
    """Missing or invalid Linear channel setting."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    allowed_users: frozenset[str]
    allow_all_users: bool
    auto_start: bool
    session_url: str
    listener_host: str
    listener_port: int
    out_of_scope_writes: str = "approve"
    outside_session_writes: str = "approve"
    default_team: str = ""
    default_project: str = ""


def _norm_entry(entry: object) -> str:
    text = str(entry).strip()
    # Emails are case-insensitive; Linear user UUIDs are kept verbatim.
    return text.lower() if "@" in text else text


def parse_allowlist(value: object) -> frozenset[str]:
    """Parse an allowlist given as a list, a JSON-list string or a comma string.

    Entries are stripped, blanks dropped, and emails (entries containing
    ``@``) lowercased. ``None`` yields an empty set.
    """
    if value is None:
        return frozenset()
    items: Iterable[object]
    if isinstance(value, str):
        text = value.strip()
        items = text.split(",")
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        items = [value]
    return frozenset(e for e in (_norm_entry(i) for i in items if i is not None) if e)


def _as_bool(value: object, key: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise SettingsError(f"invalid boolean for setting '{key}': {value!r}")


def _as_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def load_settings(
    get_config: Callable[[str, Any], Any],
    get_secret: Callable[[str], str | None],
    env: Mapping[str, str],
) -> Settings:
    """Build ``Settings`` from plugin config, profile secrets and env.

    Raises ``SettingsError`` naming the key when ``client_id``,
    ``LINEAR_CLIENT_SECRET`` or ``LINEAR_WEBHOOK_SECRET`` is missing/empty,
    or when a value cannot be coerced.
    """
    client_id = _as_str(get_config("client_id", None))
    if not client_id:
        raise SettingsError("missing required setting 'client_id'")
    client_secret = _as_str(get_secret(CLIENT_SECRET_KEY))
    if not client_secret:
        raise SettingsError(f"missing required secret '{CLIENT_SECRET_KEY}'")
    webhook_secret = _as_str(get_secret(WEBHOOK_SECRET_KEY))
    if not webhook_secret:
        raise SettingsError(f"missing required secret '{WEBHOOK_SECRET_KEY}'")

    allowed_users = parse_allowlist(get_config("allowed_users", None)) | parse_allowlist(
        env.get(ALLOWED_USERS_ENV)
    )
    allow_all_users = _as_bool(
        get_config("allow_all_users", False), "allow_all_users", False
    ) or (_as_str(env.get(ALLOW_ALL_USERS_ENV)).lower() in _TRUE)

    raw_port = get_config("listener_port", DEFAULT_LISTENER_PORT)
    try:
        if isinstance(raw_port, bool):
            raise ValueError
        listener_port = int(DEFAULT_LISTENER_PORT if raw_port in (None, "") else raw_port)
    except (TypeError, ValueError):
        raise SettingsError(f"invalid integer for setting 'listener_port': {raw_port!r}") from None
    if not 0 < listener_port < 65536:
        raise SettingsError(f"setting 'listener_port' out of range: {listener_port}")

    raw_mode = get_config("out_of_scope_writes", "approve")
    out_of_scope_writes = _as_str(raw_mode).lower()
    if out_of_scope_writes not in OUT_OF_SCOPE_MODES:
        raise SettingsError(
            f"invalid value for setting 'out_of_scope_writes': {raw_mode!r} "
            f"(expected one of {', '.join(OUT_OF_SCOPE_MODES)})"
        )
    raw_outside = get_config("outside_session_writes", "approve")
    outside_session_writes = _as_str(raw_outside).lower()
    if outside_session_writes not in OUTSIDE_MODES:
        raise SettingsError(
            f"invalid value for setting 'outside_session_writes': {raw_outside!r} "
            f"(expected one of {', '.join(OUTSIDE_MODES)})"
        )

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        webhook_secret=webhook_secret,
        allowed_users=allowed_users,
        allow_all_users=allow_all_users,
        auto_start=_as_bool(get_config("auto_start", True), "auto_start", True),
        session_url=_as_str(get_config("session_url", "")),
        listener_host=_as_str(get_config("listener_host", DEFAULT_LISTENER_HOST))
        or DEFAULT_LISTENER_HOST,
        listener_port=listener_port,
        out_of_scope_writes=out_of_scope_writes,
        outside_session_writes=outside_session_writes,
        default_team=_as_str(get_config("default_team", "")),
        default_project=_as_str(get_config("default_project", "")),
    )


def load_scoped_settings(
    get_config: Callable[[str, Any], Any], get_secret: Callable[[str], str | None]
) -> Settings:
    """``load_settings`` with the allowlist env vars read through ``get_secret`` (Q3)."""
    env = {k: v for k in (ALLOWED_USERS_ENV, ALLOW_ALL_USERS_ENV) if (v := get_secret(k))}
    return load_settings(get_config, get_secret, env)


GLOBAL_ALLOWLIST_WARNING = (
    "linear: GATEWAY_ALLOWED_USERS is set but %s is not; the gateway will admit only "
    "Linear UUIDs listed in GATEWAY_ALLOWED_USERS (email/settings entries are denied). "
    "Set %s to enable the Linear allowlist." % (ALLOWED_USERS_ENV, ALLOWED_USERS_ENV)
)


def only_global_allowlist(get_secret: Callable[[str], str | None]) -> bool:
    """True when ``GATEWAY_ALLOWED_USERS`` is set but the Linear allowlist env is not (Q4)."""
    return bool(get_secret("GATEWAY_ALLOWED_USERS")) and not get_secret(ALLOWED_USERS_ENV)


__all__ = [
    "GLOBAL_ALLOWLIST_WARNING",
    "OUT_OF_SCOPE_MODES",
    "Settings",
    "SettingsError",
    "load_scoped_settings",
    "load_settings",
    "only_global_allowlist",
    "parse_allowlist",
]
