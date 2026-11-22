"""Plugin registration, setup wizard and ``hermes linear`` CLI (Task 13).

Everything is faked: ``ctx`` is a recorder, prompts/prints/HTTP/Linear are injected, and no
real profile is read or written (``HERMES_HOME`` points at a throwaway dir).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import importlib
import importlib.metadata
import json
import os
import tempfile
import time
import tomllib
from pathlib import Path

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hl-plugin-test-home-")

import pytest  # noqa: E402

from conftest import REPO_ROOT  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

from hermes_linear import hooks, plugin, setup_cli  # noqa: E402
from hermes_linear.adapter import LinearAdapter  # noqa: E402
from hermes_linear.linear_client import LinearError  # noqa: E402
from hermes_linear.listener import ProfileEndpoint, WebhookListener  # noqa: E402
from hermes_linear.store import Store  # noqa: E402

CLIENT_ID = "00000000000000000000000000000001"
CLIENT_SECRET = "cs-SUPER-SECRET-value-1234"
WEBHOOK_SECRET = "wh-SUPER-SECRET-value-5678"
ACCESS_TOKEN = "lin_oauth_TOKEN-should-never-print"
UUID_A = "00000000-0000-4000-8000-000000000001"

EXPECTED_HINT = (
    "You are working inside Linear. Your final reply is posted as a comment in the thread that "
    "asked. Linear issue/user URLs in markdown render as mentions."
)


class FakeCtx:
    def __init__(self, config: dict | None = None, profile: str = "neo") -> None:
        self.config = dict(config or {})
        self.profile_name = profile
        self.platforms: list[dict] = []
        self.hooks: list[tuple[str, object]] = []
        self.cli: list[dict] = []
        self.set_calls: list[tuple[str, object]] = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.set_calls.append((key, value))
        self.config[key] = value

    def register_platform(self, **kw):
        self.platforms.append(kw)

    def register_hook(self, name, cb, **kw):
        self.hooks.append((name, cb))

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli.append({"name": name, "help": help, "setup_fn": setup_fn,
                         "handler_fn": handler_fn, "description": description})


def secrets_of(mapping):
    return lambda name, default=None: mapping.get(name, default)


@pytest.fixture
def ctx(monkeypatch):
    c = FakeCtx({"client_id": CLIENT_ID})
    monkeypatch.setattr(plugin, "get_secret", secrets_of({
        "LINEAR_CLIENT_SECRET": CLIENT_SECRET, "LINEAR_WEBHOOK_SECRET": WEBHOOK_SECRET}))
    plugin.register(c)
    return c


# ------------------------------------------------------------------ register


def test_register_platform_kwargs(ctx):
    assert len(ctx.platforms) == 1
    kw = ctx.platforms[0]
    assert kw["name"] == "linear"
    assert kw["label"] == "Linear"
    assert kw["required_env"] == ["LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET"]
    assert kw["allowed_users_env"] == "LINEAR_ALLOWED_USERS"
    assert kw["allow_all_env"] == "LINEAR_ALLOW_ALL_USERS"
    assert kw["platform_hint"] == EXPECTED_HINT == plugin.PLATFORM_HINT
    assert kw["emoji"] == "📐"
    assert kw["max_message_length"] == 0
    for fn in ("adapter_factory", "check_fn", "validate_config", "setup_fn"):
        assert callable(kw[fn]), fn
    # The real registry entry accepts every kwarg we pass (unknown keys raise TypeError).
    entry = PlatformEntry(source="plugin", plugin_name="linear", **kw)
    assert entry.name == "linear"


def test_adapter_factory_builds_profile_adapter(ctx):
    # Register what the plugin produced, as PluginContext.register_platform does.
    platform_registry.register(PlatformEntry(source="plugin", plugin_name="linear",
                                             **ctx.platforms[0]))
    adapter = ctx.platforms[0]["adapter_factory"](PlatformConfig(enabled=True))
    assert isinstance(adapter, LinearAdapter)
    assert adapter._get_config == ctx.get_config


def test_hooks_registered(ctx):
    assert ("pre_tool_call", hooks.on_pre_tool_call) in ctx.hooks
    assert ("post_tool_call", hooks.on_post_tool_call) in ctx.hooks


@pytest.mark.parametrize("present", [
    {}, {"LINEAR_CLIENT_SECRET": "x"}, {"LINEAR_WEBHOOK_SECRET": "y"},
    {"LINEAR_CLIENT_SECRET": "", "LINEAR_WEBHOOK_SECRET": "y"},
])
def test_check_fn_false_without_secrets(ctx, monkeypatch, present):
    check = ctx.platforms[0]["check_fn"]
    monkeypatch.setattr(plugin, "get_secret", secrets_of(present))
    assert check() is False


def test_check_fn_true_with_both_secrets_and_never_raises(ctx, monkeypatch):
    check = ctx.platforms[0]["check_fn"]
    assert check() is True

    def boom(name, default=None):
        raise RuntimeError("unscoped")

    monkeypatch.setattr(plugin, "get_secret", boom)
    assert check() is False


def test_validate_config_reports_errors(monkeypatch):
    c = FakeCtx({"listener_port": "nope"})
    monkeypatch.setattr(plugin, "get_secret", secrets_of({}))
    plugin.register(c)
    errors = plugin.config_errors(c.get_config, plugin.get_secret)
    joined = "\n".join(errors)
    assert "client_id" in joined
    assert "LINEAR_CLIENT_SECRET" in joined
    assert "LINEAR_WEBHOOK_SECRET" in joined
    assert len(errors) >= 3
    # The gateway contract is boolean: a truthy list would read as "valid".
    assert c.platforms[0]["validate_config"](PlatformConfig()) is False

    monkeypatch.setattr(plugin, "get_secret", secrets_of({
        "LINEAR_CLIENT_SECRET": "a", "LINEAR_WEBHOOK_SECRET": "b"}))
    c.config.update(client_id=CLIENT_ID)
    errors = plugin.config_errors(c.get_config, plugin.get_secret)
    assert errors and "listener_port" in errors[0]
    c.config["listener_port"] = 8650
    assert plugin.config_errors(c.get_config, plugin.get_secret) == []
    assert c.platforms[0]["validate_config"](PlatformConfig()) is True


def test_cli_registered(ctx):
    assert [e["name"] for e in ctx.cli] == ["linear"]
    entry = ctx.cli[0]
    parser = argparse.ArgumentParser(prog="hermes linear")
    entry["setup_fn"](parser)
    for sub in ("setup", "status", "test"):
        args = parser.parse_args([sub])
        assert args.linear_command == sub
    assert callable(entry["handler_fn"])


# ------------------------------------------------------------------ fakes for the CLI


class FakeClient:
    def __init__(self, *, fail: Exception | None = None, expires_in: float = 30 * 86_400):
        self.fail = fail
        self.expires_in = expires_in
        self.token_expires_at = None
        self.closed = False

    async def token(self):
        if self.fail:
            raise self.fail
        self.token_expires_at = time.time() + self.expires_in
        return ACCESS_TOKEN

    async def viewer(self):
        return {"id": "app-user-1", "name": "Hermes Test", "displayName": "hermestest"}

    async def close(self):
        self.closed = True


class Harness:
    def __init__(self, *, config=None, secrets=None, answers=None, hidden=None, client=None,
                 host="box.tail1234.ts.net:8443", store=None, adapters=None, post=None):
        self.config = dict(config or {})
        self.secrets = dict(secrets or {})
        self.answers = list(answers or [])
        self.hidden = list(hidden or [])
        self.lines: list[str] = []
        self.saved: list[dict] = []
        self.set_calls: list[tuple] = []
        self.prompts: list[str] = []
        self.client = client or FakeClient()
        self.clients_made: list[tuple] = []
        self.env = setup_cli.CliEnv(
            profile="neo",
            get_config=lambda k, d=None: self.config.get(k, d),
            set_config=self._set,
            get_secret=lambda k, d=None: self.secrets.get(k, d),
            save_env_values=self._save,
            ask=self._ask,
            ask_secret=self._ask_secret,
            out=self.lines.append,
            make_client=self._make_client,
            detect_host=lambda: host,
            post=post or self._no_post,
            open_store=lambda: store,
            adapters=adapters if adapters is not None else {},
        )

    def _set(self, k, v):
        self.set_calls.append((k, v))
        self.config[k] = v

    def _save(self, **values):
        self.saved.append(values)
        self.secrets.update(values)

    def _ask(self, question, default=""):
        self.prompts.append(question)
        ans = self.answers.pop(0) if self.answers else ""
        return ans or (default or "")

    def _ask_secret(self, question):
        self.prompts.append(question)
        return self.hidden.pop(0) if self.hidden else ""

    def _make_client(self, client_id, client_secret):
        self.clients_made.append((client_id, client_secret))
        return self.client

    async def _no_post(self, url, body, headers):
        raise AssertionError("unexpected POST")

    @property
    def text(self):
        return "\n".join(self.lines)


def run_setup(h):
    return setup_cli.interactive_setup(env=h.env)


# ------------------------------------------------------------------ setup


def test_setup_saves_secrets_via_save_env_values_not_config():
    h = Harness(answers=["", CLIENT_ID, f"{UUID_A}, alice@example.com"],
                hidden=[CLIENT_SECRET, WEBHOOK_SECRET])
    assert run_setup(h) == 0
    saved = {k: v for d in h.saved for k, v in d.items()}
    assert saved["LINEAR_CLIENT_SECRET"] == CLIENT_SECRET
    assert saved["LINEAR_WEBHOOK_SECRET"] == WEBHOOK_SECRET
    assert saved["LINEAR_ALLOWED_USERS"] == f"{UUID_A},alice@example.com"
    assert ("client_id", CLIENT_ID) in h.set_calls
    for key, value in h.set_calls:
        assert CLIENT_SECRET not in str(value) and WEBHOOK_SECRET not in str(value), key
    # secrets were asked through the hidden prompt and are never echoed
    assert CLIENT_SECRET not in h.text and WEBHOOK_SECRET not in h.text
    assert ACCESS_TOKEN not in h.text
    assert h.clients_made == [(CLIENT_ID, CLIENT_SECRET)]
    assert "Hermes Test" in h.text and h.client.closed


def test_setup_prints_webhook_url_and_funnel_command():
    h = Harness(config={"listener_port": 8650}, answers=["", CLIENT_ID, UUID_A],
                hidden=[CLIENT_SECRET, WEBHOOK_SECRET])
    assert run_setup(h) == 0
    assert "https://box.tail1234.ts.net:8443/linear/neo/webhook" in h.text
    assert ("tailscale funnel --bg --https=8443 --set-path /linear "
            "http://127.0.0.1:8650/linear") in h.text
    for cat in setup_cli.WEBHOOK_CATEGORIES:
        assert cat in h.text
    for cat in ("Agent session events", "Issues", "Comments", "Attachments", "Projects",
                "Project updates", "OAuth app"):
        assert cat in setup_cli.WEBHOOK_CATEGORIES
    assert ("public_host", "box.tail1234.ts.net:8443") in h.set_calls


def test_tailscale_dns_name_carries_funnel_port(monkeypatch):
    class Proc:
        stdout = json.dumps({"Self": {"DNSName": "uranus.tailnet.ts.net."}})

    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/usr/bin/tailscale")
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda *a, **k: Proc())
    assert setup_cli.tailscale_dns_name() == "uranus.tailnet.ts.net:8443"
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: None)
    assert setup_cli.tailscale_dns_name() is None


def test_setup_host_prompt_when_tailscale_unavailable():
    h = Harness(host=None, answers=["hooks.example.com", CLIENT_ID, UUID_A],
                hidden=[CLIENT_SECRET, WEBHOOK_SECRET])
    assert run_setup(h) == 0
    assert "https://hooks.example.com/linear/neo/webhook" in h.text


def test_setup_keeps_existing_secrets_on_blank_input():
    h = Harness(config={"client_id": CLIENT_ID},
                secrets={"LINEAR_CLIENT_SECRET": CLIENT_SECRET,
                         "LINEAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
                         "LINEAR_ALLOWED_USERS": UUID_A},
                answers=["", "", ""], hidden=["", ""])
    assert run_setup(h) == 0
    saved = {k for d in h.saved for k in d}
    assert "LINEAR_CLIENT_SECRET" not in saved and "LINEAR_WEBHOOK_SECRET" not in saved
    assert h.clients_made == [(CLIENT_ID, CLIENT_SECRET)]


def test_setup_token_failure_nonzero_exit():
    client = FakeClient(fail=LinearError("token request failed (HTTP 401)", code="invalid_client",
                                         status=401))
    h = Harness(answers=["", CLIENT_ID, UUID_A], hidden=[CLIENT_SECRET, WEBHOOK_SECRET],
                client=client)
    assert run_setup(h) != 0
    assert "failed" in h.text.lower()
    assert CLIENT_SECRET not in h.text


def test_setup_missing_client_id_nonzero_exit():
    h = Harness(answers=["", ""], hidden=[CLIENT_SECRET, WEBHOOK_SECRET])
    assert run_setup(h) != 0
    assert h.saved == [] and h.clients_made == []


# ------------------------------------------------------------------ test command


async def test_test_command_signs_and_posts():
    delivered: list[dict] = []
    listener = WebhookListener("127.0.0.1", 0, autostart=False)
    listener.register(ProfileEndpoint(
        profile="neo", webhook_secret=lambda: WEBHOOK_SECRET.encode(), client_id=CLIENT_ID,
        deliver=delivered.append, seen=lambda wid: False))
    await listener.start()
    try:
        port = listener.bound_port
        h = Harness(config={"client_id": CLIENT_ID, "listener_port": port},
                    secrets={"LINEAR_CLIENT_SECRET": CLIENT_SECRET,
                             "LINEAR_WEBHOOK_SECRET": WEBHOOK_SECRET},
                    post=setup_cli.aiohttp_post)
        assert await setup_cli.run_test(h.env) == 0
        await asyncio.sleep(0.05)
        assert len(delivered) == 1
        ev = delivered[0]
        assert ev["type"] == "AppUserNotification" and ev["action"] == "test"
        assert ev["webhookId"].startswith("hermes-linear-test-")
        assert f"http://127.0.0.1:{port}/linear/neo/webhook" in h.text
        assert "200" in h.text
        assert WEBHOOK_SECRET not in h.text

        # wrong secret -> listener 401 -> non-zero exit
        h.secrets["LINEAR_WEBHOOK_SECRET"] = "not-the-secret"
        assert await setup_cli.run_test(h.env) == 1
        assert "401" in h.text
    finally:
        await listener.stop()


def test_test_event_signature_matches_listener_scheme():
    body, headers = setup_cli.signed_test_event(WEBHOOK_SECRET, CLIENT_ID, now=1_800_000_000.0)
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert headers["Linear-Signature"] == expected
    payload = json.loads(body)
    assert payload["webhookTimestamp"] == 1_800_000_000_000
    assert payload["oauthClientId"] == CLIENT_ID


# ------------------------------------------------------------------ status


def test_store_last_event_at():
    store = Store.open(":memory:")
    assert store.last_event_at() is None
    store.seen_webhook("w1", 100.0)
    store.seen_webhook("w2", 250.0)
    assert store.last_event_at() == 250.0


class FakeStatusAdapter:
    degraded = True
    ack_degraded = False
    has_fatal_error = True
    fatal_error_code = "linear_auth"
    fatal_error_message = "Linear token or viewer request failed"


def _status_harness(**kw):
    store = Store.open(":memory:")
    store.seen_webhook("w1", time.time() - 120)
    return Harness(
        config={"client_id": CLIENT_ID, "public_host": "box.tail1234.ts.net:8443",
                "allowed_users": ["someone-handle"]},
        secrets={"LINEAR_CLIENT_SECRET": CLIENT_SECRET,
                 "LINEAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
                 "LINEAR_ALLOWED_USERS": f"{UUID_A},alice@example.com"},
        store=store, **kw)


def test_status_never_prints_secrets():
    h = _status_harness()
    rc = setup_cli.run_status(h.env)
    text = h.text
    assert rc == 0
    for secret in (CLIENT_SECRET, WEBHOOK_SECRET, ACCESS_TOKEN):
        assert secret not in text
    assert "https://box.tail1234.ts.net:8443/linear/neo/webhook" in text
    assert "127.0.0.1:8650" in text
    assert "Hermes Test" in text
    assert "expires" in text.lower()
    assert "last event:    never" not in text and "last event:" in text
    assert "allowlist" in text.lower() and "3 entr" in text
    assert "someone-handle" in text and "never match" in text
    assert "first admission" in text
    assert "n/a (gateway process)" in text


def test_status_reports_running_adapter_state():
    h = _status_harness(adapters={"neo": FakeStatusAdapter()})
    setup_cli.run_status(h.env)
    assert "degraded" in h.text.lower() and "linear_auth" in h.text
    assert "n/a (gateway process)" not in h.text


def test_status_token_failure_nonzero_and_allow_all():
    client = FakeClient(fail=LinearError("token request failed (HTTP 401)", status=401))
    h = _status_harness(client=client)
    h.secrets["LINEAR_ALLOW_ALL_USERS"] = "true"
    assert setup_cli.run_status(h.env) == 1
    assert "allow-all" in h.text.lower()
    assert CLIENT_SECRET not in h.text


def test_status_unconfigured_nonzero():
    h = Harness()
    assert setup_cli.run_status(h.env) == 1
    assert "client_id" in h.text


# ------------------------------------------------------------------ packaging


def test_entry_point_resolves():
    meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    target = meta["project"]["entry-points"]["hermes_agent.plugins"]["linear"]
    assert target == "hermes_linear.plugin"
    assert "build-system" in meta
    mod = importlib.import_module(target)
    assert callable(mod.register)


def test_entry_point_via_metadata_when_installed():
    eps = [ep for ep in importlib.metadata.entry_points(group="hermes_agent.plugins")
           if ep.name == "linear"]
    if not eps:
        pytest.skip("hermes-linear is not pip-installed in this environment")
    assert callable(eps[0].load().register)
