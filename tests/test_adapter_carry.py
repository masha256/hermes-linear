"""Task 11 review carry items fixed in Task 12: disconnect vs. release, the delegated-session
deadline, and the drain fallback deadline."""

from __future__ import annotations

import asyncio
import copy
import logging
import time

from test_adapter_inbound import (  # noqa: F401  (also registers the linear platform)
    D_ISSUE, D_SID, DELEGATION, HUMAN, ISSUE_DELEGATED, deliver, prompted,
)
from test_adapter_outbound import env, new_adapter  # noqa: F401  (fixture)
from gateway.platforms.event import ProcessingOutcome

from hermes_linear import sessions_open as sessions_open_mod
from hermes_linear import turns as turns_mod




async def test_disconnect_does_not_release_queued_turn():
    a, client, gw, store, order = new_adapter()
    assert await a.connect()
    client.activity_delay[D_SID] = 5.0  # first ack hangs: the dispatch waits for it
    a.deliver(copy.deepcopy(DELEGATION))
    a.deliver(prompted(D_SID, D_ISSUE, "more", "act-2"))
    await asyncio.sleep(0.05)
    assert gw.events == [] and a.turns.queues

    async def slow_close():  # a real aiohttp close yields to the loop
        await asyncio.sleep(0.05)

    client.close = slow_close
    await a.disconnect()
    await asyncio.sleep(0.05)
    assert gw.events == []
    assert not [t for t in a._tasks if not t.done()]
    assert a.turns.inflight == {} and a.turns.queues == {}


async def test_delegated_session_open_shares_deadline(monkeypatch):
    a, client, gw, store, order = new_adapter()
    assert await a.connect()
    try:
        monkeypatch.setattr(sessions_open_mod, "ACK_TIMEOUT_S", 0.3)
        client.app_delay = client.create_delay = 0.2  # each < 0.3, together > 0.3
        issue = ISSUE_DELEGATED["data"]["id"]
        store.set_mode(issue, "delegated", 1)
        a.opener.delegation_timers[issue] = None
        t0 = time.monotonic()
        await a.opener.open_delegated_session(issue, HUMAN, "Test User")
        assert time.monotonic() - t0 < 0.36
        assert store.session("sess-issue") is None
        assert gw.events == []
    finally:
        await a.disconnect()


async def test_drain_deadline_releases(env, monkeypatch, caplog):
    a, client, gw, store, order = env
    monkeypatch.setattr(turns_mod, "DRAIN_DEADLINE_S", 0.1)
    await deliver(a, DELEGATION)
    await deliver(a, prompted(D_SID, D_ISSUE, "more", "act-2"))
    stuck = asyncio.Event()

    async def base_task_that_never_ends():
        await a.on_processing_complete(gw.events[0], ProcessingOutcome.SUCCESS)
        await stuck.wait()

    task = asyncio.create_task(base_task_that_never_ends())
    with caplog.at_level(logging.WARNING):
        await asyncio.sleep(0.05)
        assert len(gw.events) == 1  # still draining
        await asyncio.sleep(0.15)
        await asyncio.sleep(0.05)
    assert len(gw.events) == 2
    assert "draining" in caplog.text
    stuck.set()
    await task
    await a.wait_idle()
    assert len(gw.events) == 2
