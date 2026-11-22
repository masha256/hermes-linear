"""``hermes linear {setup,status,test}`` and the gateway-setup wizard (spec §10).

All I/O goes through ``CliEnv`` so tests inject prompts, output, HTTP, Linear and the store;
``default_env(ctx)`` wires the real ones. Secrets are read with a hidden prompt, stored with
``save_env_values`` (profile ``.env``), never passed to ``set_config`` and never printed.
Nothing here runs ``tailscale funnel``; the command is printed for the operator.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .constants import TEST_WEBHOOK_PREFIX
from .settings import (
    ALLOWED_USERS_ENV, CLIENT_SECRET_KEY, DEFAULT_LISTENER_HOST,
    DEFAULT_LISTENER_PORT, WEBHOOK_SECRET_KEY, SettingsError, load_scoped_settings,
)

#: Webhook categories to tick on the Linear OAuth app.
WEBHOOK_CATEGORIES = (
    "Agent session events",
    "Issues",
    "Comments",
    "Attachments",
    "Projects",
    "Project updates",
    "OAuth app",
)
FUNNEL_HTTPS_PORT = 8443
SCOPES = "read, write, app:mentionable, app:assignable"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass
class CliEnv:
    profile: str
    get_config: Callable[..., Any]
    set_config: Callable[[str, Any], None]
    get_secret: Callable[..., Any]
    save_env_values: Callable[..., None]
    ask: Callable[..., str]  # (question, default="") -> visible answer
    ask_secret: Callable[[str], str]  # hidden input; "" = keep current
    out: Callable[[str], None]
    make_client: Callable[[str, str], Any]  # (client_id, client_secret) -> LinearClient-like
    detect_host: Callable[[], str | None]
    post: Callable[[str, bytes, dict], Awaitable[int]]
    open_store: Callable[[], Any]
    adapters: dict = field(default_factory=dict)
    now: Callable[[], float] = time.time


# ------------------------------------------------------------------ helpers


def webhook_url(host: str, profile: str) -> str:
    return f"https://{host}/linear/{profile}/webhook"


def funnel_command(port: int) -> str:
    return (f"tailscale funnel --bg --https={FUNNEL_HTTPS_PORT} --set-path /linear "
            f"http://127.0.0.1:{port}/linear")


def tailscale_dns_name(timeout_s: float = 3.0) -> str | None:
    """``Self.DNSName`` from ``tailscale status --json`` (read-only), else None.

    Returned as ``<name>:8443``: the printed Funnel command serves on ``--https=8443``, and
    port 443 on a Tailscale name is tailnet-only, so the public URL must carry the port.
    """
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                              timeout=timeout_s, check=False)
        name = json.loads(proc.stdout or "{}").get("Self", {}).get("DNSName") or ""
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None
    name = name.rstrip(".")
    return f"{name}:{FUNNEL_HTTPS_PORT}" if name else None


def _run(coro) -> Any:
    """Run ``coro`` to completion from sync code, even when a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["e"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _err_text(exc: BaseException) -> str:
    """Safe one-line error: our own LinearError text, else only the type (may hold secrets)."""
    if type(exc).__name__ == "LinearError":
        code = getattr(exc, "code", None)
        return f"{exc}" + (f" [code={code}]" if code else "")
    return type(exc).__name__


def _listener(env: CliEnv) -> tuple[str, int]:
    host = str(env.get_config("listener_host", DEFAULT_LISTENER_HOST) or DEFAULT_LISTENER_HOST)
    try:
        port = int(env.get_config("listener_port", DEFAULT_LISTENER_PORT) or DEFAULT_LISTENER_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_LISTENER_PORT
    return host.strip() or DEFAULT_LISTENER_HOST, port


def _secret(env: CliEnv, key: str) -> str:
    return str(env.get_secret(key, None) or "").strip()


async def _check_app(client) -> dict:
    try:
        await client.token()
        return await client.viewer()
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()


BLOCKED_LABEL = "Blocked"


async def _missing_blocked(client) -> list[str] | None:
    """Keys of the workspace teams (visible to the app) with no ``Blocked`` label (team or
    workspace label, any case); ``None`` when no team is visible."""
    teams = await client.teams()
    if not teams:
        return None
    missing: list[str] = []
    for team in teams:
        labels = await client.team_labels(team["id"])
        if not any((lb.get("name") or "").strip().casefold() == BLOCKED_LABEL.casefold()
                   for lb in labels):
            missing.append(str(team.get("key") or team.get("name") or team["id"]))
    return missing


async def _check_setup(client) -> tuple[dict, list[str] | None | BaseException]:
    """Token + viewer (raises on failure), then the advisory label check (error returned)."""
    try:
        await client.token()
        viewer = await client.viewer()
        try:
            missing: list[str] | None | BaseException = await _missing_blocked(client)
        except Exception as exc:  # noqa: BLE001 - advisory only
            missing = exc
        return viewer, missing
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()


def _viewer_text(viewer: Any) -> str:
    viewer = viewer or {}
    name = viewer.get("name") or "?"
    handle = viewer.get("displayName")
    return f"{name} (@{handle})" if handle else str(name)


# ------------------------------------------------------------------ setup


def interactive_setup(ctx: Any = None, *, env: CliEnv | None = None) -> int:
    """Walk through the Linear app, collect settings/secrets, then check token + viewer."""
    env = env or default_env(ctx)
    out = env.out
    out("")
    out("📐 Linear channel setup (profile: %s)" % env.profile)
    out("")
    out("1. In Linear: Settings → API → Applications → create an OAuth application")
    out("   (any HTTPS callback URL works if one is asked for). Enable client credentials;")
    out(f"   the plugin requests scopes: {SCOPES}.")

    detected = env.detect_host()
    current_host = str(env.get_config("public_host", "") or "").strip()
    host = env.ask("Public HTTPS host[:port] for the webhook (Tailscale Funnel name)",
                   current_host or detected or "").strip()
    host = host.removeprefix("https://").rstrip("/")
    if not host:
        out("✗ A public host is required for the webhook URL.")
        return 1
    _, port = _listener(env)
    out("")
    out("2. Enable webhooks on the app with this URL:")
    out(f"   {webhook_url(host, env.profile)}")
    out("   and tick these categories:")
    for cat in WEBHOOK_CATEGORIES:
        out(f"     • {cat}")
    out("")
    out("3. Expose the listener through Tailscale Funnel (run this yourself; setup never does):")
    out(f"   {funnel_command(port)}")
    out("")

    current_cid = str(env.get_config("client_id", "") or "").strip()
    client_id = env.ask("Linear client ID", current_cid).strip()
    if not client_id:
        out("✗ client_id is required.")
        return 1

    existing_cs = _secret(env, CLIENT_SECRET_KEY)
    existing_ws = _secret(env, WEBHOOK_SECRET_KEY)
    keep = " (hidden; Enter keeps the current value)"
    client_secret = env.ask_secret("Linear client secret" + (keep if existing_cs else " (hidden)"))
    client_secret = client_secret.strip() or existing_cs
    webhook_secret = env.ask_secret(
        "Linear webhook signing secret" + (keep if existing_ws else " (hidden)"))
    webhook_secret = webhook_secret.strip() or existing_ws
    if not client_secret or not webhook_secret:
        out("✗ Both the client secret and the webhook signing secret are required.")
        return 1

    current_users = _secret(env, ALLOWED_USERS_ENV)
    users = env.ask("Allowed Linear users (comma-separated UUIDs or emails, * = everyone)",
                    current_users)
    users = ",".join(u.strip() for u in users.split(",") if u.strip())

    # Writes: non-secrets to plugin settings, secrets to the profile .env.
    if host != current_host:
        env.set_config("public_host", host)
    if client_id != current_cid:
        env.set_config("client_id", client_id)
    to_save: dict[str, str] = {}
    if client_secret != existing_cs:
        to_save[CLIENT_SECRET_KEY] = client_secret
    if webhook_secret != existing_ws:
        to_save[WEBHOOK_SECRET_KEY] = webhook_secret
    if users and users != current_users:
        to_save[ALLOWED_USERS_ENV] = users
    if to_save:
        env.save_env_values(**to_save)
        out("✓ Saved " + ", ".join(sorted(to_save)) + " to the profile .env")
    if not users:
        out(f"⚠ {ALLOWED_USERS_ENV} is empty: nobody can drive the agent until it is set.")
    out("  Leave `unauthorized_dm_behavior` unset for Linear (pairing text would be posted).")

    out("")
    out("Checking the app token and viewer…")
    try:
        viewer, missing = _run(_check_setup(env.make_client(client_id, client_secret)))
    except Exception as exc:  # noqa: BLE001
        out(f"✗ Linear check failed: {_err_text(exc)}")
        out("  Check the client ID/secret. If the app was never installed in this workspace,")
        out("  a workspace admin must approve it once (actor=app install) before client")
        out("  credentials work.")
        return 1
    out(f"✓ App user: {_viewer_text(viewer)}")
    if isinstance(missing, BaseException):
        out(f"⚠ Could not check team labels for \"{BLOCKED_LABEL}\": {_err_text(missing)}")
    elif missing is None:
        out(f"No Linear teams visible to the app; skipped the {BLOCKED_LABEL} label check.")
    else:
        for key in missing:
            out(f"Team {key} has no \"{BLOCKED_LABEL}\" label — create it in Linear "
                "(Settings → Labels) so tasks can be marked blocked.")
    out("Enable the platform for this profile and restart the gateway, then run")
    out(f"`hermes -p {env.profile} linear test` and `hermes -p {env.profile} linear status`.")
    return 0


# ------------------------------------------------------------------ status


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _fmt_ts(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    iso = _iso(ts)
    ago = max(0, int(now - ts))
    return f"{iso} ({ago // 3600}h{(ago % 3600) // 60:02d}m ago)"


def _access_lines(settings) -> list[str]:
    entries = sorted(settings.allowed_users)
    if settings.allow_all_users or "*" in entries:
        return ["access:        allow-all (every workspace member)"]
    lines = [f"access:        allowlist, {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"]
    bad = [e for e in entries if "@" not in e and not _UUID_RE.match(e)]
    if bad:
        lines.append("  ⚠ never match (not a UUID or email): " + ", ".join(bad))
    if any("@" in e for e in entries):
        lines.append("  note: email entries resolve to user ids on first admission")
    if not entries:
        lines.append("  ⚠ empty: nobody can drive the agent")
    return lines


def run_status(env: CliEnv) -> int:
    out = env.out
    try:
        settings = load_scoped_settings(env.get_config, lambda k: env.get_secret(k, None))
    except SettingsError as exc:
        out(f"linear ({env.profile}): not configured: {exc}")
        return 1
    rc = 0
    host = str(env.get_config("public_host", "") or "").strip() or env.detect_host() or "<host>"
    out(f"linear ({env.profile})")
    out(f"webhook URL:   {webhook_url(host, env.profile)}")
    out(f"listener:      {settings.listener_host}:{settings.listener_port}")
    client = env.make_client(settings.client_id, settings.client_secret)
    try:
        viewer = _run(_check_app(client))
        exp = getattr(client, "token_expires_at", None)
        if exp:
            left = int(exp - env.now())
            out(f"token:         ok, expires {_iso(exp)} "
                f"(in {left // 86_400}d{(left % 86_400) // 3600}h)")
        else:
            out("token:         ok (expiry unknown)")
        out(f"app user:      {_viewer_text(viewer)}")
    except Exception as exc:  # noqa: BLE001
        out(f"token:         FAILED: {_err_text(exc)}")
        out("app user:      unknown")
        rc = 1
    last = last_real = None
    try:
        store = env.open_store()
        if store is not None:
            last = store.last_event_at()
            last_real = store.last_real_event_at()
    except Exception as exc:  # noqa: BLE001
        out(f"last event:    unavailable ({type(exc).__name__})")
    else:
        out(f"last event:    {_fmt_ts(last, env.now())}")
        out(f"last Linear event: {_fmt_ts(last_real, env.now())}")
    for line in _access_lines(settings):
        out(line)
    adapter = env.adapters.get(env.profile)
    if adapter is None:
        out("adapter:       n/a (gateway process)")
    else:
        state = "degraded" if getattr(adapter, "degraded", False) else "ok"
        if getattr(adapter, "ack_degraded", False):
            state += ", acks degraded"
        out(f"adapter:       {state}")
        if getattr(adapter, "has_fatal_error", False):
            out(f"fatal:         {adapter.fatal_error_code}: {adapter.fatal_error_message}")
    return rc


# ------------------------------------------------------------------ test


def signed_test_event(webhook_secret: str, client_id: str, *, now: float | None = None
                      ) -> tuple[bytes, dict]:
    """A side-effect-free ``AppUserNotification`` (router: log only), signed like Linear."""
    ts = time.time() if now is None else now
    payload = {
        "type": "AppUserNotification",
        "action": "test",
        "webhookTimestamp": int(ts * 1000),
        "webhookId": f"{TEST_WEBHOOK_PREFIX}{uuid.uuid4()}",
        "oauthClientId": client_id,
        "createdAt": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "data": {"source": "hermes linear test"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "Linear-Signature": sig}


async def aiohttp_post(url: str, body: bytes, headers: dict) -> int:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post(url, data=body, headers=headers) as resp:
            await resp.read()
            return resp.status


_HINTS = {
    404: "profile not registered on the listener (platform not enabled, secret unset, or "
         "gateway not restarted)",
    401: "signature/timestamp rejected (webhook secret differs from the running gateway's, or "
         "clock skew)",
    413: "payload too large",
}


async def run_test(env: CliEnv) -> int:
    out = env.out
    try:
        settings = load_scoped_settings(env.get_config, lambda k: env.get_secret(k, None))
    except SettingsError as exc:
        out(f"linear ({env.profile}): not configured: {exc}")
        return 1
    host = settings.listener_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    url = f"http://{host}:{settings.listener_port}/linear/{env.profile}/webhook"
    body, headers = signed_test_event(settings.webhook_secret, settings.client_id, now=env.now())
    try:
        status = await env.post(url, body, headers)
    except Exception as exc:  # noqa: BLE001
        out(f"POST {url} → failed: {type(exc).__name__} (is the gateway running?)")
        return 1
    hint = _HINTS.get(status)
    out(f"POST {url} → {status}" + (f" ({hint})" if hint else ""))
    return 0 if status == 200 else 1


# ------------------------------------------------------------------ CLI wiring


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="linear_command", metavar="{setup,status,test}")
    sub.add_parser("setup", help="Configure the Linear app, secrets and allowlist")
    sub.add_parser("status", help="Token, app user, webhook URL, last event, access mode")
    sub.add_parser("test", help="POST a locally signed synthetic event to the listener")


def make_handler(ctx: Any) -> Callable[[Any], int]:
    def handler(args) -> int:
        cmd = getattr(args, "linear_command", None)
        if cmd == "setup":
            return interactive_setup(ctx)
        if cmd == "status":
            return run_status(default_env(ctx))
        if cmd == "test":
            return asyncio.run(run_test(default_env(ctx)))
        print("usage: hermes linear {setup,status,test}")
        return 2

    return handler


def default_env(ctx: Any) -> CliEnv:
    from hermes_cli.gateway_setup_wizard import save_env_values
    from hermes_cli.setup import prompt

    from . import plugin
    from .adapter import ADAPTERS
    from .linear_client import LinearClient

    def make_client(client_id: str, client_secret: str) -> LinearClient:
        return LinearClient(client_id, lambda: client_secret)

    def open_store():
        from .store import Store

        return Store.open(None)

    return CliEnv(
        profile=str(getattr(ctx, "profile_name", "") or "default"),
        get_config=ctx.get_config,
        set_config=ctx.set_config,
        get_secret=lambda k, d=None: plugin.get_secret(k, d),
        save_env_values=save_env_values,
        ask=lambda q, d="": prompt(q, d or None),
        ask_secret=lambda q: prompt(q, password=True),
        out=print,
        make_client=make_client,
        detect_host=tailscale_dns_name,
        post=aiohttp_post,
        open_store=open_store,
        adapters=ADAPTERS,
    )


__all__ = [
    "BLOCKED_LABEL", "CliEnv", "FUNNEL_HTTPS_PORT", "TEST_WEBHOOK_PREFIX", "WEBHOOK_CATEGORIES", "aiohttp_post", "default_env",
    "funnel_command", "interactive_setup", "make_handler", "run_status", "run_test",
    "setup_parser", "signed_test_event", "tailscale_dns_name", "webhook_url",
]
