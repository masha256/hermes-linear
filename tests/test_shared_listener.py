"""One webhook listener shared by every plugin module copy in a process (RET-181).

Hermes imports the plugin once per profile scope (``hermes_cli/plugins_loader.
_directory_module_name``: bare ``hermes_plugins.<slug>`` for the first scope, a
``__home_<digest>`` suffix for the rest), so module globals are per profile. These tests
load two such copies of this checkout the way the loader does and connect a real adapter
from each, against a real bind on one port.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import importlib
import importlib.util
import json
import logging
import socket
import sys
import time
import types

import aiohttp
import pytest
from aiohttp import web

from conftest import REPO_ROOT
# Registers the "linear" platform and provides the fakes (shared with the inbound tests).
from test_adapter_inbound import DELEGATION, FakeClient, Gateway, make_settings

from hermes_linear import listener as listener_mod

NS = "hermes_plugins"
COPIES = (f"{NS}.lt181", f"{NS}.lt181__home_0123456789ab")


def _load_copy(name: str) -> types.SimpleNamespace:
    """Import the repo root as package ``name`` (as plugins_loader does) and return its modules."""
    if NS not in sys.modules:
        ns = types.ModuleType(NS)
        ns.__path__ = []  # type: ignore[attr-defined]
        sys.modules[NS] = ns
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "__init__.py", submodule_search_locations=[str(REPO_ROOT)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    pkg = f"{name}.hermes_linear"
    return types.SimpleNamespace(
        listener=importlib.import_module(f"{pkg}.listener"),
        adapter=importlib.import_module(f"{pkg}.adapter"),
        store=importlib.import_module(f"{pkg}.store"),
    )


def _evict(prefix: str) -> None:
    for key in [k for k in sys.modules if k == prefix or k.startswith(prefix + ".")]:
        del sys.modules[key]


@pytest.fixture
def copies():
    listener_mod._reset_listener_for_tests()
    loaded = [_load_copy(n) for n in COPIES]
    yield loaded
    listener_mod._reset_listener_for_tests()
    for n in COPIES:
        _evict(n)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Profile:
    """A connected adapter from one module copy, plus its fakes."""

    def __init__(self, mods, name: str, port: int, secret: str) -> None:
        self.name, self.secret = name, secret
        self.order: list = []
        self.store = mods.store.Store.open(":memory:")
        self.adapter = mods.adapter.LinearAdapter(
            mods.adapter.PlatformConfig(enabled=True),
            settings=make_settings(webhook_secret=secret, client_id=f"cid-{name}",
                                   listener_port=port),
            client=FakeClient(self.order), store=self.store)
        self.adapter._write_runtime_status_safe = lambda *a, **kw: None
        self.adapter.set_owner_profile(name)
        self.gw = Gateway(self.order)
        self.gw.adapter = self.adapter
        self.adapter.handle_message = self.gw


async def _post(port: int, prof: Profile, payload: dict) -> int:
    body = json.dumps(payload).encode()
    sig = hmac.new(prof.secret.encode(), body, hashlib.sha256).hexdigest()
    async with aiohttp.ClientSession() as http:
        async with http.post(f"http://127.0.0.1:{port}/linear/{prof.name}/webhook",
                             data=body, headers={"Linear-Signature": sig}) as resp:
            return resp.status


def _delegation(prof: Profile) -> dict:
    return {**copy.deepcopy(DELEGATION), "webhookTimestamp": int(time.time() * 1000),
            "oauthClientId": f"cid-{prof.name}"}


async def _until(pred, tries: int = 50) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.02)


@pytest.fixture
def binds(monkeypatch):
    """Count real TCP binds by any listener copy (TCPSite is aiohttp's, shared by all)."""
    count = {"n": 0}
    real = web.TCPSite.start

    async def start(self):
        count["n"] += 1
        return await real(self)

    monkeypatch.setattr(web.TCPSite, "start", start)
    return count


async def test_two_copies_share_one_bind_and_route_each_profile(copies, binds):
    port = _free_port()
    neo = Profile(copies[0], "neo", port, "secret-neo")
    ray = Profile(copies[1], "raymond", port, "secret-raymond")
    try:
        assert await neo.adapter.connect()
        assert await ray.adapter.connect()  # was EADDRINUSE before RET-181
        assert binds["n"] == 1
        assert copies[0].listener.get_listener("127.0.0.1", port) is \
            copies[1].listener.get_listener("127.0.0.1", port)

        assert await _post(port, ray, _delegation(ray)) == 200
        await _until(lambda: ray.gw.events)
        await ray.adapter.wait_idle()
        assert len(ray.gw.events) == 1 and not neo.gw.events

        assert await _post(port, neo, _delegation(neo)) == 200
        await _until(lambda: neo.gw.events)
        await neo.adapter.wait_idle()
        assert len(neo.gw.events) == 1 and len(ray.gw.events) == 1

        # raymond's secret on neo's route is refused: endpoints stay separate.
        wrong = Profile(copies[1], "neo", port, "secret-raymond")
        assert await _post(port, wrong, _delegation(neo)) == 401
    finally:
        await ray.adapter.disconnect()
        await neo.adapter.disconnect()


async def test_disconnect_one_copy_keeps_the_other_route(copies):
    port = _free_port()
    neo = Profile(copies[0], "neo", port, "secret-neo")
    ray = Profile(copies[1], "raymond", port, "secret-raymond")
    assert await neo.adapter.connect()
    assert await ray.adapter.connect()
    lst = copies[0].listener.get_listener("127.0.0.1", port)

    await neo.adapter.disconnect()  # the copy that created the listener goes first
    await lst.wait_idle()
    assert lst.running
    assert await _post(port, neo, _delegation(neo)) == 404
    assert await _post(port, ray, _delegation(ray)) == 200

    await ray.adapter.disconnect()
    await lst.wait_idle()
    assert not lst.running
    with pytest.raises(aiohttp.ClientConnectionError):
        await _post(port, ray, _delegation(ray))


async def test_reconnect_does_not_take_down_other_profiles(copies):
    """Hermes builds a fresh adapter per reconnect; the old one's late disconnect must
    remove only its own endpoint, never the new one's or another profile's."""
    port = _free_port()
    neo = Profile(copies[0], "neo", port, "secret-neo")
    ray_old = Profile(copies[1], "raymond", port, "secret-raymond")
    assert await neo.adapter.connect()
    assert await ray_old.adapter.connect()
    ray_new = Profile(copies[1], "raymond", port, "secret-raymond")
    assert await ray_new.adapter.connect()
    await ray_old.adapter.disconnect()
    lst = copies[1].listener.get_listener("127.0.0.1", port)
    await lst.wait_idle()
    try:
        assert await _post(port, ray_new, _delegation(ray_new)) == 200
        await _until(lambda: ray_new.gw.events)
        assert ray_new.gw.events and not ray_old.gw.events
        assert await _post(port, neo, _delegation(neo)) == 200
    finally:
        await ray_new.adapter.disconnect()
        await neo.adapter.disconnect()


async def test_protocol_mismatch_refuses_with_clear_error(copies, monkeypatch, caplog):
    port = _free_port()
    neo = Profile(copies[0], "neo", port, "secret-neo")
    ray = Profile(copies[1], "raymond", port, "secret-raymond")
    assert await neo.adapter.connect()
    monkeypatch.setattr(copies[1].listener, "SHARED_PROTOCOL", 999)
    try:
        with caplog.at_level(logging.ERROR):
            assert await ray.adapter.connect() is False
        assert ray.adapter.has_fatal_error and ray.adapter.fatal_error_retryable is False
        msg = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        assert "protocol" in msg and "999" in msg and "1" in msg
        assert COPIES[0] in msg and COPIES[1] in msg
        err = ray.store.plugin_status("listener_error")
        assert err is not None and "protocol" in err[0]
        # neo's route is untouched.
        assert await _post(port, neo, _delegation(neo)) == 200
    finally:
        await ray.adapter.disconnect()
        await neo.adapter.disconnect()


async def test_bind_failure_names_errno_and_is_recorded(copies, caplog):
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        neo = Profile(copies[0], "neo", port, "secret-neo")
        with caplog.at_level(logging.ERROR):
            assert await neo.adapter.connect() is False
    text = caplog.text
    assert "errno" in text.lower()
    err = neo.store.plugin_status("listener_error")
    assert err is not None and f"127.0.0.1:{port}" in err[0] and "errno" in err[0].lower()

    # A later successful connect clears it.
    neo2 = Profile(copies[0], "neo", port, "secret-neo")
    neo2.store = neo.store
    neo2.adapter._store = neo.store
    try:
        assert await neo2.adapter.connect()
        assert neo.store.plugin_status("listener_error") is None
    finally:
        await neo2.adapter.disconnect()


async def test_profiles_on_different_ports_get_separate_listeners(copies, binds):
    p1, p2 = _free_port(), _free_port()
    neo = Profile(copies[0], "neo", p1, "secret-neo")
    ray = Profile(copies[1], "raymond", p2, "secret-raymond")
    try:
        assert await neo.adapter.connect()
        assert await ray.adapter.connect()
        assert binds["n"] == 2
        assert await _post(p2, ray, _delegation(ray)) == 200
        assert await _post(p1, ray, _delegation(ray)) == 404
    finally:
        await ray.adapter.disconnect()
        await neo.adapter.disconnect()
