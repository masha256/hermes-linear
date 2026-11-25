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
    "asked. Linear issue/user URLs in markdown render as mentions. If the ticket says 'manage "
    "with hermes-linear', or the repo has `.hermes-linear.yaml`, load "
    "`linear:manage-superpowers` first."
)
MARKER_SENTENCE = ("If the ticket says 'manage with hermes-linear', or the repo has "
                   "`.hermes-linear.yaml`, load `linear:manage-superpowers` first.")
SKILL_DESCRIPTION = "Use when a repo has .hermes-linear.yaml or ticket says so."
SKILL_MD = REPO_ROOT / "hermes_linear" / "skills" / "manage-superpowers" / "SKILL.md"
TOOL_NAMES = ["linear_issue_get", "linear_issue_create", "linear_issue_update", "linear_comment",
              "linear_sync_spec", "linear_sync_plan", "linear_task_state", "linear_query",
              "linear_search", "linear_attach", "linear_session_link", "linear_relation",
              "linear_lookup"]


class FakeCtx:
    def __init__(self, config: dict | None = None, profile: str = "neo") -> None:
        self.config = dict(config or {})
        self.profile_name = profile
        self.platforms: list[dict] = []
        self.hooks: list[tuple[str, object]] = []
        self.cli: list[dict] = []
        self.set_calls: list[tuple[str, object]] = []
        self.tools: list[dict] = []
        self.skills: list[dict] = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.set_calls.append((key, value))
        self.config[key] = value

    def register_platform(self, **kw):
        self.platforms.append(kw)

    def register_hook(self, name, cb, **kw):
        self.hooks.append((name, cb))

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        self.tools.append({"name": name, "toolset": toolset, "schema": schema, "handler": handler,
                           "is_async": is_async, "description": description, "emoji": emoji,
                           "override": override})

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append({"name": name, "path": Path(path), "description": description})

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
    # Tool progress renders as Linear `action` activities (hooks.py); the
    # gateway's text progress lines would otherwise post as `response`s.
    assert kw["display_overrides"] == {"tool_progress": "off"}
    for fn in ("adapter_factory", "check_fn", "validate_config", "setup_fn"):
        assert callable(kw[fn]), fn
    # The real registry entry accepts every kwarg we pass (unknown keys raise TypeError).
    entry = PlatformEntry(source="plugin", plugin_name="linear", **kw)
    assert entry.name == "linear"


def test_register_platform_dashboard_card(ctx):
    """Channels-page card metadata (web dashboard + Desktop): RET-114."""
    kw = ctx.platforms[0]
    assert kw["dashboard_env_vars"] == ("LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET",
                                        "LINEAR_ALLOWED_USERS")
    assert kw["docs_url"] == "https://github.com/masha256/hermes-linear#web-dashboard"
    desc = kw["dashboard_description"]
    # client_id is a plugin setting, not an env var: the card must say where it goes.
    assert "client_id" in desc and "plugins.entries.linear.settings" in desc
    assert "hermes linear setup" in desc
    # The card's "configured" verdict stays on the two required secrets.
    assert "dashboard_required_env" not in kw


def test_dashboard_card_through_real_catalog_builder(ctx):
    """The card Hermes builds from our PlatformEntry. Known coupling: calls Hermes's private
    ``hermes_cli.web_server_messaging._build_catalog_entry`` (the one function the Channels
    page catalog runs per platform); update here if Hermes renames it."""
    builder = pytest.importorskip("hermes_cli.web_server_messaging")._build_catalog_entry
    entry = PlatformEntry(source="plugin", plugin_name="linear", **ctx.platforms[0])
    card = builder("linear", entry)
    assert card["name"] == "Linear"
    assert card["description"] == plugin.DASHBOARD_DESCRIPTION
    assert card["docs_url"] == plugin.DOCS_URL
    assert card["env_vars"][:3] == plugin.DASHBOARD_ENV_VARS
    # Hermes hides *_ALLOW_ALL_USERS from setup forms; the card must not offer it.
    assert "LINEAR_ALLOW_ALL_USERS" not in card["env_vars"]
    assert card["required_env"] == ("LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET")


def test_readme_has_web_dashboard_section():
    """``DOCS_URL`` points at this README anchor."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "\n## Web dashboard\n" in text


def test_linear_tool_progress_defaults_off_real_resolver(ctx):
    from gateway.display_config import resolve_display_setting

    platform_registry.register(PlatformEntry(source="plugin", plugin_name="linear",
                                             **ctx.platforms[0]))
    assert resolve_display_setting({}, "linear", "tool_progress") == "off"


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


def test_plugin_registers_transform_hook(ctx, monkeypatch):
    """Registered, and Hermes's real dispatch returns the hook's rewritten result."""
    from types import SimpleNamespace

    from hermes_cli.plugins import PluginManager

    from hermes_linear import adapter as adapter_mod
    from hermes_linear import questions

    assert ("transform_tool_result", hooks.on_transform_tool_result) in ctx.hooks
    store = Store.open(None)
    store.put_control("req-1", "approval", "sess-1", 1, [], session_key="sk", command="ls -la",
                      turn_mid="mid-1")
    monkeypatch.setattr(adapter_mod, "ADAPTERS", {"neo": SimpleNamespace(_store=store)})
    env = {"HERMES_SESSION_PLATFORM": "linear", "HERMES_SESSION_KEY": "sk",
           "HERMES_SESSION_MESSAGE_ID": "mid-1"}
    monkeypatch.setattr(hooks, "get_session_env", lambda name, default="": env.get(name, default))
    pm = PluginManager()
    # Known coupling: writes Hermes's private PluginManager._hooks (no public way to load
    # a plugin's callbacks without discovery); update here if Hermes renames it.
    for name, cb in ctx.hooks:
        pm._hooks.setdefault(name, []).append(cb)
    out = pm.invoke_hook("transform_tool_result", tool_name="terminal", args={"command": "ls -la"},
                         result="BLOCKED: Command timed out without user response.",
                         task_id="t", tool_call_id="c", session_id="s", duration_ms=3,
                         status="error", error_type=None, error_message=None)
    assert out == [questions.APPROVAL_PENDING_TEXT]


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


# ------------------------------------------------------------------ agent tools and skill (RET-81)


def test_tools_registered_in_hermes_linear_toolset(ctx):
    from hermes_linear import agent_tools

    assert [t["name"] for t in ctx.tools] == TOOL_NAMES
    for t in ctx.tools:
        assert t["toolset"] == "hermes_linear", t["name"]
        assert t["schema"] is agent_tools.TOOL_SCHEMAS[t["name"]]
        assert t["schema"]["name"] == t["name"] and t["schema"]["parameters"]["properties"]
        assert t["handler"] is plugin.tool_handler(t["name"])
        assert t["description"] == t["schema"]["description"]
        assert t["emoji"] == "📐"
        assert t["is_async"] is False and t["override"] is False


def test_registered_handlers_return_json_strings_real_registry(monkeypatch):
    # Hermes's registry only accepts str (or the multimodal envelope) from a tool
    # handler; a bare dict becomes a "tool_result_contract" error at dispatch.
    import json as _json

    from tools.registry import ToolRegistry

    from hermes_linear import adapter as adapter_mod
    from hermes_linear import agent_tools

    monkeypatch.setattr(adapter_mod, "ADAPTERS", {})
    for name in agent_tools.TOOL_SCHEMAS:
        handler = plugin.tool_handler(name)
        raw = handler({"id": "RET-1"}, session_id="s")
        assert isinstance(raw, str), name
        normalized = ToolRegistry._normalize_handler_result(name, raw)
        assert "tool_result_contract" not in normalized, name
        assert _json.loads(raw)["ok"] is False


@pytest.mark.parametrize("first", ["hermes_linear.tools_lookup", "hermes_linear.tools_relation"])
def test_import_order_fresh_interpreter(first):
    """A tool module imported first in a fresh interpreter still yields the full roster,
    every slot filled, and ``agent_tools`` reaches the sibling modules' helpers."""
    import subprocess
    import sys

    code = (f"import {first}; from hermes_linear import agent_tools as a; "
            "print(','.join(a.TOOL_SCHEMAS), ','.join(a.TOOL_HANDLERS), "
            "all(a.TOOL_SCHEMAS.values()), all(callable(h) for h in a.TOOL_HANDLERS.values()), "
            "all(getattr(a, n) is a.TOOL_HANDLERS[n] for n in a.TOOL_HANDLERS), "
            "callable(a.tools_relation.check_relation))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    roster = ",".join(TOOL_NAMES)
    assert proc.stdout.split() == [roster, roster, "True", "True", "True", "True"]


def test_manifest_declares_every_tool():
    import yaml

    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())
    assert manifest["provides_tools"] == TOOL_NAMES
    assert manifest["provides_hooks"] == ["pre_tool_call", "post_tool_call",
                                         "transform_tool_result"]


def test_skill_registered(ctx):
    assert len(ctx.skills) == 1
    skill = ctx.skills[0]
    assert skill["name"] == "manage-superpowers"
    assert skill["path"] == Path(plugin.__file__).parent / "skills" / "manage-superpowers" / "SKILL.md"
    assert skill["path"].is_file() and skill["path"].resolve() == SKILL_MD.resolve()
    assert skill["description"] == SKILL_DESCRIPTION


def test_platform_hint_mentions_marker():
    assert MARKER_SENTENCE in plugin.PLATFORM_HINT
    assert plugin.PLATFORM_HINT.startswith("You are working inside Linear.")


def _frontmatter(text: str) -> dict:
    import yaml

    assert text.startswith("---\n")
    head, _, _ = text[4:].partition("\n---\n")
    return yaml.safe_load(head)


def test_skill_frontmatter_valid():
    meta = _frontmatter(SKILL_MD.read_text())
    assert meta["name"] == "manage-superpowers"
    assert meta["description"] == SKILL_DESCRIPTION
    assert len(meta["description"]) <= 60
    for key in ("requires_tools", "requires_toolsets", "fallback_for_tools",
                "fallback_for_toolsets"):
        assert key not in meta, key
        assert key not in (meta.get("metadata") or {}).get("hermes", {}), key


def test_skill_body_is_a_short_overlay_naming_every_tool():
    text = SKILL_MD.read_text()
    assert len(text.splitlines()) <= 150
    assert "manage with hermes-linear" in text
    for name in TOOL_NAMES:
        assert f"`{name}`" in text, name
    assert "tool_describe" in text and "tool_call" in text
    for stage in ("brainstorming", "writing-plans", "subagent-driven-development"):
        assert f"superpowers:{stage}" in text, stage


def test_skill_has_a_fallback_when_superpowers_is_missing():
    """RET-130: superpowers is optional. When a stage skill isn't found the overlay must say
    what to do (tell the thread, skip the spec/plan/SDD stages, work with the tools) instead
    of leaving the agent to improvise."""
    text = SKILL_MD.read_text()
    _, marker, rest = text.partition("**Without superpowers**")
    assert marker, "no **Without superpowers** paragraph"
    para = rest.split("\n\n", 1)[0]
    assert "not found" in para
    assert "Setup" in para and "Per-stage" in para
    assert "Questions and approvals" in para
    assert text.index("**Without superpowers**") < text.index("## Calling the tools")


def test_skill_finish_links_pr():
    text = SKILL_MD.read_text()
    _, _, rest = text.partition("**Finish.**")
    finish = rest.split("\n## ", 1)[0]
    assert finish, "no **Finish.** paragraph"
    assert "`linear_session_link`" in finish
    assert "`linear_attach`" in finish


def test_skill_ships_as_package_data():
    meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    data = meta["tool"]["setuptools"]["package-data"]["hermes_linear"]
    assert "skills/*/SKILL.md" in data


# ------------------------------------------------------------------ fakes for the CLI


class FakeClient:
    def __init__(self, *, fail: Exception | None = None, expires_in: float = 30 * 86_400,
                 teams: list[dict] | None = None, labels: dict[str, list[dict]] | None = None,
                 labels_fail: Exception | None = None):
        self.fail = fail
        self.expires_in = expires_in
        self.token_expires_at = None
        self.closed = False
        self._teams = teams if teams is not None else [
            {"id": "t1", "key": "ENG", "name": "Engineering"}]
        self.labels = labels if labels is not None else {
            "t1": [{"id": "l1", "name": "Blocked"}]}
        self.labels_fail = labels_fail

    async def teams(self):  # root `teams` connection: the workspace teams the app can see
        if self.labels_fail:
            raise self.labels_fail
        return list(self._teams)

    async def team_labels(self, team_id):
        if self.closed:
            raise AssertionError("team_labels after close")
        return list(self.labels.get(team_id, []))

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


BLOCKED_WARN = ('Team {key} has no "Blocked" label — create it in Linear (Settings → Labels) '
                'so tasks can be marked blocked.')


def test_setup_warns_missing_blocked_label():
    client = FakeClient(
        teams=[{"id": "t1", "key": "ENG", "name": "Engineering"},
               {"id": "t2", "key": "OPS", "name": "Operations"},
               {"id": "t3", "key": "RET", "name": "Retail"}],
        labels={"t1": [{"id": "l1", "name": "Bug"}],
                "t2": [{"id": "l2", "name": "blocked"}],  # case-insensitive match
                "t3": [{"id": "l3", "name": "Bug"}, {"id": "w1", "name": "BLOCKED"}]})
    h = Harness(answers=["", CLIENT_ID, UUID_A], hidden=[CLIENT_SECRET, WEBHOOK_SECRET],
                client=client)
    assert run_setup(h) == 0
    assert BLOCKED_WARN.format(key="ENG") in h.lines
    assert BLOCKED_WARN.format(key="OPS") not in h.text
    assert BLOCKED_WARN.format(key="RET") not in h.text
    assert client.closed


def test_setup_no_blocked_warning_when_every_team_has_it():
    h = Harness(answers=["", CLIENT_ID, UUID_A], hidden=[CLIENT_SECRET, WEBHOOK_SECRET])
    assert run_setup(h) == 0
    assert "Blocked" not in h.text  # neither a missing-label nor an error/skip line


def test_setup_reports_when_no_teams_visible():
    h = Harness(answers=["", CLIENT_ID, UUID_A], hidden=[CLIENT_SECRET, WEBHOOK_SECRET],
                client=FakeClient(teams=[]))
    assert run_setup(h) == 0
    assert "No Linear teams visible to the app; skipped the Blocked label check." in h.lines
    assert [ln for ln in h.lines if "Blocked" in ln] == [
        "No Linear teams visible to the app; skipped the Blocked label check."]


def test_setup_blocked_label_check_error_is_advisory():
    client = FakeClient(labels_fail=LinearError("rate limited", code="RATELIMITED"))
    h = Harness(answers=["", CLIENT_ID, UUID_A], hidden=[CLIENT_SECRET, WEBHOOK_SECRET],
                client=client)
    assert run_setup(h) == 0
    warn = [ln for ln in h.lines if "Blocked" in ln]
    assert len(warn) == 1 and warn[0].startswith("⚠") and "rate limited" in warn[0]
    assert "Hermes Test" in h.text and client.closed


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


def test_test_event_delivery_id_carries_test_prefix():
    # The listener dedups (and records) on the Linear-Delivery header, so the
    # probe must send one with the test prefix or `last Linear event` counts it.
    body, headers = setup_cli.signed_test_event(WEBHOOK_SECRET, CLIENT_ID, now=1_800_000_000.0)
    delivery = headers["Linear-Delivery"]
    assert delivery.startswith(setup_cli.TEST_WEBHOOK_PREFIX)
    assert delivery == json.loads(body)["webhookId"]


# ------------------------------------------------------------------ status


def test_plugin_revision_reads_git_checkout(tmp_path, monkeypatch):
    import subprocess

    # tmp_path may sit inside another repo (e.g. a profile dir under git).
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    assert setup_cli.plugin_revision(str(tmp_path)) is None  # not a checkout
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    (tmp_path / "f").write_text("x")
    run("add", "f")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first commit")
    rev = setup_cli.plugin_revision(str(tmp_path))
    assert rev.endswith(" first commit (main)") and len(rev.split()[0]) >= 7
    run("switch", "-q", "--detach", "HEAD")
    assert setup_cli.plugin_revision(str(tmp_path)).endswith("(detached)")


def test_store_last_event_at():
    store = Store.open(":memory:")
    assert store.last_event_at() is None
    store.seen_webhook("w1", 100.0)
    store.seen_webhook("w2", 250.0)
    assert store.last_event_at() == 250.0


def test_last_real_event_ignores_test_probes():
    store = Store.open(":memory:")
    assert store.last_real_event_at() is None
    store.seen_webhook(f"{setup_cli.TEST_WEBHOOK_PREFIX}probe-only", 50.0)
    assert store.last_real_event_at() is None
    store.seen_webhook("w1", 100.0)
    store.seen_webhook(f"{setup_cli.TEST_WEBHOOK_PREFIX}abc", 300.0)
    assert store.last_real_event_at() == 100.0
    assert store.last_event_at() == 300.0


def test_status_prints_last_linear_event():
    now = time.time()
    store = Store.open(":memory:")
    store.seen_webhook("real-1", now - 7200)
    store.seen_webhook(f"{setup_cli.TEST_WEBHOOK_PREFIX}x", now - 60)
    h = _status_harness()
    h.env.open_store = lambda: store
    h.env.now = lambda: now
    assert setup_cli.run_status(h.env) == 0
    assert f"last event:    {setup_cli._fmt_ts(now - 60, now)}" in h.lines
    assert f"last Linear event: {setup_cli._fmt_ts(now - 7200, now)}" in h.lines

    probes_only = Store.open(":memory:")
    probes_only.seen_webhook(f"{setup_cli.TEST_WEBHOOK_PREFIX}y", now - 60)
    h2 = _status_harness()
    h2.env.open_store = lambda: probes_only
    setup_cli.run_status(h2.env)
    assert "last Linear event: never" in h2.lines


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


# ------------------------------------------------------------------ platform pinning


def test_register_pins_platform_member_after_registration(monkeypatch):
    """register() resolves Platform("linear") right after register_platform, in the plugin's own
    scope, so the enum member is cached process-wide before the gateway's (launch-scope) session
    store parses ``agent:<profile>:linear:…`` routing entries. Without it those entries are
    dropped at startup ("'linear' is not a valid Platform") and tickets lose their session."""
    order: list[str] = []

    class OrderCtx(FakeCtx):
        def register_platform(self, **kw):
            order.append("register_platform")
            super().register_platform(**kw)

    def spy_platform(value):
        order.append(f"Platform({value})")
        return value

    monkeypatch.setattr(plugin, "Platform", spy_platform)
    monkeypatch.setattr(plugin, "get_secret", secrets_of({}))
    plugin.register(OrderCtx({"client_id": CLIENT_ID}))
    assert order[:2] == ["register_platform", "Platform(linear)"]


def test_register_survives_platform_pin_failure(monkeypatch):
    def boom(value):
        raise ValueError(f"{value!r} is not a valid Platform")

    monkeypatch.setattr(plugin, "Platform", boom)
    monkeypatch.setattr(plugin, "get_secret", secrets_of({}))
    c = FakeCtx({"client_id": CLIENT_ID})
    plugin.register(c)  # must not raise
    assert len(c.platforms) == 1 and c.hooks  # rest of registration still happened


def test_platform_member_cached_across_scopes_real_hermes(monkeypatch):
    """The Hermes behaviour the pin relies on (real gateway.config / platform_registry): a
    plugin platform registered only in a profile scope resolves there, and once resolved the
    member is cached so a launch-scope lookup succeeds too. Unpinned, the launch scope fails."""
    from gateway.config import Platform as RealPlatform
    from gateway.platform_registry import PlatformEntry, platform_registry

    def entry(name):
        return PlatformEntry(name=name, label=name, adapter_factory=lambda c: None,
                             check_fn=lambda: True, source="plugin", plugin_name=name)

    scope = {"key": "launch"}
    monkeypatch.setattr(type(platform_registry), "current_scope_key",
                        staticmethod(lambda: scope["key"]))
    for name in ("zz-pinned-probe", "zz-unpinned-probe"):
        platform_registry.register(entry(name), scope="profile-neo")

    scope["key"] = "profile-neo"
    RealPlatform("zz-pinned-probe")            # what _pin_platform_member does at register time
    scope["key"] = "launch"                    # session store parses routing entries here
    assert RealPlatform("zz-pinned-probe").value == "zz-pinned-probe"
    with pytest.raises(ValueError):
        RealPlatform("zz-unpinned-probe")
