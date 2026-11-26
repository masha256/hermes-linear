"""Plugin entry point: platform, tool hooks and the ``hermes linear`` CLI (spec §10).

API contracts (``docs/hermes-integration-notes.md``; Hermes ``hermes_cli/plugins_context.py``,
``gateway/platform_registry.py``):

* ``register_platform`` forwards extra kwargs to ``PlatformEntry`` (TypeError on an unknown
  field). ``check_fn() -> bool`` is a passive probe; ``validate_config(PlatformConfig) -> bool``
  is read as truthy = valid by ``create_adapter`` and as the "configured?" verdict by
  ``GatewayConfig.get_connected_platforms``. So ``validate_config`` returns a bool and the
  human-readable list lives in ``config_errors``; ``setup_fn`` takes no arguments.
* Settings/secrets resolve from the calling context's profile (Q3); nothing here does network.
"""

from __future__ import annotations

import json

import logging
from pathlib import Path
from typing import Any, Callable

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret

from . import agent_tools, hooks
from .adapter import PLATFORM, LinearAdapter, set_config_getter
from .settings import (
    ALLOW_ALL_USERS_ENV, ALLOWED_USERS_ENV, CLIENT_SECRET_KEY, WEBHOOK_SECRET_KEY, SettingsError,
    load_scoped_settings,
)

log = logging.getLogger(__name__)

PLATFORM_HINT = (
    "You are working inside Linear. Your final reply is posted as a comment in the thread that "
    "asked. Linear issue/user URLs in markdown render as mentions. Load "
    "`linear:manage-superpowers` first when the ticket description starts with \"Build with "
    "hermes\" (any case, optionally followed by \".\" or \":\") or has that phrase alone on a "
    "line, or when the repo has `.hermes-linear.yaml`. The older \"Manage with hermes-linear\" "
    "counts the same way. A ticket that only mentions Hermes inside a sentence (\"can we build "
    "with Hermes instead?\") is not the trigger."
)
EMOJI = "📐"
#: Plugin toolset for the agent tools. Not ``linear``: that name hides the Linear MCP server's
#: tools (integration notes Q1); not ``hermes-*``: those are platform bundles.
TOOLSET = "hermes_linear"
SKILL_NAME = "manage-superpowers"
#: The skill index clips at 60 chars (notes Q4); the exact trigger rule (start of the
#: description or a line of its own, the old phrase as an alias) lives in PLATFORM_HINT.
SKILL_DESCRIPTION = 'Use when ticket says "build with hermes". Superpowers flow.'
SKILL_PATH = Path(__file__).parent / "skills" / SKILL_NAME / "SKILL.md"
REQUIRED_ENV = [CLIENT_SECRET_KEY, WEBHOOK_SECRET_KEY]
INSTALL_HINT = "run `hermes linear setup` to store the Linear client and webhook secrets"

# Channels-page card (web dashboard + Desktop; Hermes hermes_cli/web_server_messaging.py).
# The card edits env vars only; client_id is a plugin setting, so the description says where
# it goes. ``dashboard_required_env`` stays unset: "configured" = REQUIRED_ENV.
DASHBOARD_DESCRIPTION = (
    "One Hermes session per delegated Linear ticket. The card holds the secrets and the "
    "allowlist; client_id goes in plugins.entries.linear.settings (Config page, raw YAML) "
    "or run `hermes linear setup`."
)
DOCS_URL = "https://github.com/masha256/hermes-linear#web-dashboard"
#: No ALLOW_ALL_USERS_ENV: Hermes hides every ``*_ALLOW_ALL_USERS`` from setup forms on purpose
#: (hermes_cli/setup_hidden_env.py: enabling it is a security decision).
DASHBOARD_ENV_VARS = (*REQUIRED_ENV, ALLOWED_USERS_ENV)

#: Secret reader (module-level so tests can swap it). Profile-scope aware.
get_secret: Callable[..., Any] = get_scoped_secret


def _secret(name: str) -> str:
    value = get_secret(name, None)
    return "" if value is None else str(value).strip()


def config_errors(get_config: Callable[[str, Any], Any],
                  get_secret_fn: Callable[..., Any] | None = None) -> list[str]:
    """Human-readable configuration problems for the calling profile (no network)."""
    read = get_secret_fn or get_secret
    errors: list[str] = []
    try:
        if not str(get_config("client_id", None) or "").strip():
            errors.append("missing required setting 'client_id' (run `hermes linear setup`)")
        for key in REQUIRED_ENV:
            if not str(read(key, None) or "").strip():
                errors.append(f"missing required secret '{key}' (run `hermes linear setup`)")
        if errors:
            return errors
        load_scoped_settings(get_config, lambda k: read(k, None))
    except SettingsError as exc:
        errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001 - a probe must never raise into the registry
        errors.append(f"cannot read Linear settings: {type(exc).__name__}")
    return errors


def _pin_platform_member() -> None:
    """Resolve ``Platform("linear")`` now, while our registration is visible.

    Plugin platforms are dynamic enum members created on first lookup, and only when the
    platform is registered *in the looking-up scope*. Our registration lives in this
    profile's plugin scope, but the gateway's session store parses every profile's routing
    entries in the launch scope; there ``Platform("linear")`` fails and the ticket sessions
    are dropped at startup. Once created here, the member is cached process-wide, so later
    lookups in any scope succeed. Never fatal.
    """
    try:
        Platform(PLATFORM)
    except Exception as exc:  # noqa: BLE001 - registration must not fail on this
        log.warning("linear: could not pin Platform(%r): %s; ticket sessions may not survive "
                    "a gateway restart", PLATFORM, type(exc).__name__)


_JSON_HANDLERS: dict = {}


def tool_handler(name: str):
    """The registered handler for tool ``name``: the agent_tools handler with its
    dict result serialized to a JSON string. Hermes's tool registry only accepts
    a ``str`` (or the multimodal envelope) from a handler; a bare dict becomes a
    ``tool_result_contract`` error. One wrapper per name, so it is stable."""
    if name not in _JSON_HANDLERS:
        inner = agent_tools.TOOL_HANDLERS[name]

        def handler(args=None, **kw):
            return json.dumps(inner(args, **kw), ensure_ascii=False, default=str)

        handler.__name__ = f"{name}_json"
        _JSON_HANDLERS[name] = handler
    return _JSON_HANDLERS[name]


def register(ctx) -> None:
    """Register the Linear platform, agent tools, skill, tool hooks and ``hermes linear``."""
    from . import setup_cli  # deferred: keeps CLI helpers out of the adapter import path

    get_config = ctx.get_config
    set_config_getter(get_config)

    def adapter_factory(config: Any) -> LinearAdapter:
        return LinearAdapter(config, get_config=get_config)

    def check_fn() -> bool:
        try:
            return all(_secret(k) for k in REQUIRED_ENV)
        except Exception:  # noqa: BLE001 - passive probe; never raise
            return False

    def validate_config(config: Any) -> bool:
        errors = config_errors(get_config)
        for err in errors:
            log.warning("linear: %s", err)
        return not errors

    def setup_fn() -> None:
        setup_cli.interactive_setup(ctx)

    ctx.register_platform(
        name="linear",
        label="Linear",
        adapter_factory=adapter_factory,
        check_fn=check_fn,
        validate_config=validate_config,
        required_env=list(REQUIRED_ENV),
        install_hint=INSTALL_HINT,
        dashboard_description=DASHBOARD_DESCRIPTION,
        docs_url=DOCS_URL,
        dashboard_env_vars=DASHBOARD_ENV_VARS,
        allowed_users_env=ALLOWED_USERS_ENV,
        allow_all_env=ALLOW_ALL_USERS_ENV,
        setup_fn=setup_fn,
        platform_hint=PLATFORM_HINT,
        emoji=EMOJI,
        max_message_length=0,
        # Tool calls render as Linear `action` activities via hooks.py. Gateway
        # text progress lines would post as extra `response`s (which end the
        # turn in Linear's UI). An operator's global display.tool_progress still
        # wins; README says to set display.platforms.linear.tool_progress: off.
        display_overrides={"tool_progress": "off"},
    )
    _pin_platform_member()
    for name, schema in agent_tools.TOOL_SCHEMAS.items():
        ctx.register_tool(name, toolset=TOOLSET, schema=schema,
                          handler=tool_handler(name),
                          description=schema["description"], emoji=EMOJI)
    ctx.register_skill(SKILL_NAME, SKILL_PATH, description=SKILL_DESCRIPTION)
    ctx.register_hook("pre_tool_call", hooks.on_pre_tool_call)
    ctx.register_hook("post_tool_call", hooks.on_post_tool_call)
    ctx.register_hook("transform_tool_result", hooks.on_transform_tool_result)
    ctx.register_cli_command(
        "linear",
        "Linear channel: setup, status, test",
        setup_cli.setup_parser,
        setup_cli.make_handler(ctx),
        description="Configure and check the Linear channel for this profile.",
    )
