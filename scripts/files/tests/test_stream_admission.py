"""hold_slot: a streaming response must hold its admission slot for its whole life.

The router returned StreamingResponse from inside `async with chat_sem:`, so the
slot was released before the first byte streamed and CHAT_CONCURRENCY limited
only non-streamed requests. These tests pin the replacement's contract.

No pytest-asyncio dependency: each test drives its own event loop.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from stream_admission import AdmissionGate, hold_slot, refresh_capacity_once  # noqa: E402

PING = b": ping\n\n"


def run(coro):
    # Every test is bounded: a regression that deadlocks the semaphore (e.g. a
    # slot never released) must FAIL in a second, not hang the suite.
    async def bounded():
        return await asyncio.wait_for(coro, timeout=3)
    return asyncio.run(bounded())


async def body(chunks, gate=None, log=None, fail_after=None):
    """An upstream stream. Optionally parks on `gate` after the first chunk."""
    try:
        for i, c in enumerate(chunks):
            if fail_after is not None and i == fail_after:
                raise RuntimeError("upstream blew up")
            yield c
            if gate is not None and i == 0:
                await gate.wait()
    finally:
        if log is not None:
            log.append("inner-closed")


def test_slot_held_for_the_whole_stream_and_released_after():
    async def t():
        sem = asyncio.Semaphore(1)
        seen_locked = []
        async for chunk in hold_slot(sem, body([b"a", b"b", b"c"]), keepalive_s=5):
            seen_locked.append(sem.locked())
        assert seen_locked == [True, True, True]      # held while streaming
        assert not sem.locked()                        # released at the end
    run(t())


def test_second_stream_waits_with_pings_and_never_overlaps():
    async def t():
        sem = asyncio.Semaphore(1)
        gate = asyncio.Event()
        first = hold_slot(sem, body([b"1a", b"1b"], gate=gate), keepalive_s=0.01)
        assert await first.__anext__() == b"1a"        # first holds the slot, parked

        second = hold_slot(sem, body([b"2a"]), keepalive_s=0.01)
        got = await asyncio.wait_for(second.__anext__(), 1)
        assert got == PING                             # queued: keepalive, not data
        assert sem.locked()

        gate.set()
        assert [c async for c in first] == [b"1b"]     # first finishes, frees slot
        rest = [c async for c in second]
        assert rest[-1] == b"2a" and all(c == PING for c in rest[:-1])
        assert not sem.locked()
    run(t())


def test_client_disconnect_mid_stream_releases_and_closes_upstream():
    async def t():
        sem = asyncio.Semaphore(1)
        log = []
        agen = hold_slot(sem, body([b"a", b"b"], gate=asyncio.Event(), log=log), keepalive_s=5)
        assert await agen.__anext__() == b"a"
        assert sem.locked()
        await agen.aclose()                            # what Starlette does on disconnect
        assert not sem.locked()
        assert log == ["inner-closed"]
    run(t())


def test_disconnect_while_queued_does_not_leak_or_steal_a_permit():
    async def t():
        sem = asyncio.Semaphore(1)
        gate = asyncio.Event()
        holder = hold_slot(sem, body([b"h", b"h2"], gate=gate), keepalive_s=5)
        await holder.__anext__()

        queued = hold_slot(sem, body([b"q"]), keepalive_s=0.01)
        assert await queued.__anext__() == PING
        await queued.aclose()                          # gave up while waiting

        gate.set()
        _ = [c async for c in holder]
        assert not sem.locked()
        assert sem._value == 1                         # exactly one permit, none stolen/leaked
    run(t())


def test_upstream_exception_still_releases_the_slot():
    async def t():
        sem = asyncio.Semaphore(1)
        agen = hold_slot(sem, body([b"a", b"b"], fail_after=1), keepalive_s=5)
        assert await agen.__anext__() == b"a"
        try:
            await agen.__anext__()
            raise AssertionError("expected the upstream error to propagate")
        except RuntimeError:
            pass
        assert not sem.locked()
    run(t())


def test_never_iterated_wrapper_acquires_nothing():
    async def t():
        sem = asyncio.Semaphore(1)
        agen = hold_slot(sem, body([b"a"]), keepalive_s=5)
        del agen                                       # response object dropped unstarted
        await asyncio.sleep(0)
        assert not sem.locked() and sem._value == 1
    run(t())


def test_concurrency_two_admits_exactly_two():
    async def t():
        sem = asyncio.Semaphore(2)
        gate = asyncio.Event()
        a = hold_slot(sem, body([b"a", b"a2"], gate=gate), keepalive_s=0.01)
        b = hold_slot(sem, body([b"b", b"b2"], gate=gate), keepalive_s=0.01)
        c = hold_slot(sem, body([b"c"]), keepalive_s=0.01)
        assert await a.__anext__() == b"a"
        assert await b.__anext__() == b"b"
        assert await c.__anext__() == PING             # third waits
        gate.set()
        _ = [x async for x in a]
        _ = [x async for x in b]
        assert [x async for x in c][-1] == b"c"
        assert sem._value == 2
    run(t())


# ----------------------------------------------------------- AdmissionGate ---
# chat_sem must follow the chat server's slot count: 1 slot normally, 3 in
# redteam mode. A fixed CHAT_CONCURRENCY=1 would serialize redteam's parallel
# agents once streams are actually admitted.

def test_gate_works_with_hold_slot_like_a_semaphore():
    async def t():
        g = AdmissionGate(1)
        seen = []
        async for _ in hold_slot(g, body([b"a", b"b"]), keepalive_s=5):
            seen.append(g.in_use)
        assert seen == [1, 1] and g.in_use == 0
    run(t())


def test_gate_async_with_for_non_streamed_requests():
    async def t():
        g = AdmissionGate(1)
        async with g:
            assert g.in_use == 1
        assert g.in_use == 0
    run(t())


def test_raising_capacity_admits_waiters_without_a_restart():
    async def t():
        g = AdmissionGate(1)
        gate = asyncio.Event()
        streams = [hold_slot(g, body([b"s%d" % i, b"e"], gate=gate), keepalive_s=0.01) for i in range(3)]
        assert await streams[0].__anext__() == b"s0"
        assert await streams[1].__anext__() == PING            # capacity 1: waits
        await g.set_capacity(3)                                # redteam mode: 3 slots
        assert await streams[1].__anext__() == b"s1"
        assert await streams[2].__anext__() == b"s2"
        assert g.in_use == 3
        gate.set()
        for s in streams:
            _ = [c async for c in s]
        assert g.in_use == 0
    run(t())


def test_lowering_capacity_never_evicts_running_streams():
    async def t():
        g = AdmissionGate(3)
        gate = asyncio.Event()
        a = hold_slot(g, body([b"a", b"a2"], gate=gate), keepalive_s=0.01)
        b = hold_slot(g, body([b"b", b"b2"], gate=gate), keepalive_s=0.01)
        await a.__anext__(); await b.__anext__()
        await g.set_capacity(1)                                # back to normal mode
        c = hold_slot(g, body([b"c"]), keepalive_s=0.01)
        assert await c.__anext__() == PING                     # 2 running >= 1: waits
        gate.set()
        _ = [x async for x in a]
        assert await c.__anext__() == PING                     # 1 still running >= 1
        _ = [x async for x in b]
        assert [x async for x in c][-1] == b"c"
        assert g.in_use == 0
    run(t())


def test_capacity_never_below_one():
    async def t():
        g = AdmissionGate(0)
        assert g.capacity == 1
        await g.set_capacity(0)
        assert g.capacity == 1
    run(t())


def test_refresh_follows_slot_count_and_keeps_last_value_on_failure():
    async def t():
        g = AdmissionGate(1)
        async def three():
            return 3
        async def broken():
            raise OSError("chat server restarting")
        async def nonsense():
            return None
        assert await refresh_capacity_once(g, three) == 3 and g.capacity == 3
        assert await refresh_capacity_once(g, broken) is None and g.capacity == 3
        assert await refresh_capacity_once(g, nonsense) is None and g.capacity == 3
    run(t())


# ------------------------------------------- the real Starlette disconnect path ---
# Starlette 1.6's StreamingResponse.stream_response is a bare `async for` over the
# body with NO aclose(). A disconnect that lands while the response is inside
# send() leaves our generator suspended at a yield: its finally -- the slot
# release -- would run only when the garbage collector got round to it. With one
# chat slot, one dead client stalls all chat. These tests drive real responses
# through ASGI for both disconnect styles (listener task < 2.4, OSError >= 2.4).
import pytest  # noqa: E402
from starlette.responses import StreamingResponse  # noqa: E402

from stream_admission import ClosingStreamingResponse  # noqa: E402


async def _disconnect_mid_send(response_cls, spec_version):
    g = AdmissionGate(1)
    resp = response_cls(hold_slot(g, body([b"a", b"b", b"c"]), keepalive_s=5),
                        media_type="text/event-stream")
    first_body = asyncio.Event()

    async def send(msg):
        if msg["type"] == "http.response.body" and msg.get("body"):
            first_body.set()
            if spec_version == "2.4":
                raise OSError("client went away")     # >= 2.4: disconnect surfaces in send
            await asyncio.sleep(10)                    # < 2.4: stuck in send when cancelled

    async def receive():
        await first_body.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": spec_version}}
    try:
        await asyncio.wait_for(resp(scope, receive, send), 2)
    except Exception:
        pass
    await asyncio.sleep(0)
    return g.in_use, resp                              # keep resp alive: no GC rescue


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
def test_harness_reaches_the_path_where_plain_starlette_leaks(spec_version):
    # Guards against a vacuous pass below. If Starlette ever closes the body
    # itself, this fails -- and ClosingStreamingResponse can be retired.
    async def t():
        in_use, _ = await _disconnect_mid_send(StreamingResponse, spec_version)
        assert in_use == 1, "plain StreamingResponse no longer leaks; Starlette closes bodies now"
    run(t())


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
def test_closing_response_releases_the_slot_on_disconnect(spec_version):
    async def t():
        in_use, _ = await _disconnect_mid_send(ClosingStreamingResponse, spec_version)
        assert in_use == 0
    run(t())


def test_closing_response_releases_after_a_normal_finish():
    async def t():
        g = AdmissionGate(1)
        resp = ClosingStreamingResponse(hold_slot(g, body([b"a", b"b"]), keepalive_s=5))
        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            await asyncio.sleep(10)
            return {"type": "http.disconnect"}

        await resp({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
        assert [m.get("body") for m in sent if m["type"] == "http.response.body" and m.get("body")] == [b"a", b"b"]
        assert g.in_use == 0
    run(t())


# hold_slot's core contract, against the type chat_sem actually is in production.
@pytest.mark.parametrize("make", [lambda: asyncio.Semaphore(1), lambda: AdmissionGate(1)])
def test_hold_slot_contract_on_both_gate_types(make):
    async def t():
        g = make()
        busy = (lambda: g.locked())
        gate = asyncio.Event()
        first = hold_slot(g, body([b"1a", b"1b"], gate=gate), keepalive_s=0.01)
        assert await first.__anext__() == b"1a" and busy()
        second = hold_slot(g, body([b"2a"]), keepalive_s=0.01)
        assert await second.__anext__() == PING
        await second.aclose()                          # gives up while queued
        gate.set()
        _ = [c async for c in first]
        assert not busy()
        third = hold_slot(g, body([b"3"]), keepalive_s=0.01)
        assert [c async for c in third] == [b"3"]      # the slot really came back
    run(t())
