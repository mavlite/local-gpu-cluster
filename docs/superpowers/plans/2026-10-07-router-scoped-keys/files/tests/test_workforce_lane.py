"""Reserved user lane (workforce design rev 2, §5.4).

Scoped (workforce) requests may hold at most `chat capacity - RESERVED` chat slots, so the owner always
has a slot. In 1-slot mode the lane is closed and scoped requests are refused at once.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import access_keys as ak  # noqa: E402
from stream_admission import AdmissionGate, hold_slot  # noqa: E402
from workforce_lane import LaneClosed, ReservedLane, gate_for  # noqa: E402

SCOPED = ak.Principal("wf", "scoped", frozenset({"m"}), None)


def run(coro):
    return asyncio.run(coro)


def test_lane_capacity_follows_chat_capacity_minus_reserve():
    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        assert lane.capacity == 2
        await chat.set_capacity(1)
        assert lane.capacity == 0
    run(go())


def test_owner_gets_the_plain_chat_gate_and_scoped_gets_the_lane():
    chat = AdmissionGate(3)
    lane = ReservedLane(chat, reserve=1)
    assert gate_for(ak.OWNER, chat, lane) is chat
    assert gate_for(SCOPED, chat, lane) is not chat


def test_workforce_can_never_take_the_last_slot():
    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        g = gate_for(SCOPED, chat, lane)
        await g.acquire()
        await g.acquire()                        # 2 workforce = capacity - 1
        third = asyncio.ensure_future(g.acquire())
        await asyncio.sleep(0.05)
        assert not third.done()                  # a 3rd workforce request waits...
        await asyncio.wait_for(chat.acquire(), 0.5)   # ...while the owner gets the last slot now
        assert chat.in_use == 3
        await g.release()                        # a workforce slot frees -> the waiter proceeds
        await asyncio.wait_for(third, 1.0)
        assert lane.in_use == 2
        third.cancel()
    run(go())


def test_closed_lane_refuses_immediately():
    async def go():
        chat = AdmissionGate(1)
        g = gate_for(SCOPED, chat, ReservedLane(chat, reserve=1))
        with pytest.raises(LaneClosed):
            await asyncio.wait_for(g.acquire(), 0.5)
        assert chat.in_use == 0
    run(go())


def test_cancel_while_waiting_for_chat_releases_the_lane():
    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        await chat.acquire(); await chat.acquire(); await chat.acquire()   # owner fills all 3
        g = gate_for(SCOPED, chat, lane)
        t = asyncio.ensure_future(g.acquire())
        await asyncio.sleep(0.05)
        assert lane.in_use == 1                  # lane taken, waiting on the chat gate
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert lane.in_use == 0 and chat.in_use == 3
    run(go())


def test_capacity_increase_wakes_lane_waiters():
    async def go():
        chat = AdmissionGate(2)
        lane = ReservedLane(chat, reserve=1)
        g = gate_for(SCOPED, chat, lane)
        await g.acquire()                        # lane capacity 1, now full
        waiter = asyncio.ensure_future(g.acquire())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        await chat.set_capacity(3)               # redteam / 3-slot mode
        await asyncio.wait_for(waiter, 2.0)
        assert lane.in_use == 2
    run(go())


def test_works_inside_hold_slot_for_streams():
    async def body():
        yield b"a"
        yield b"b"

    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        out = [c async for c in hold_slot(gate_for(SCOPED, chat, lane), body(), keepalive_s=0.05)]
        assert out == [b"a", b"b"] and lane.in_use == 0 and chat.in_use == 0
    run(go())


def test_client_disconnect_mid_stream_releases_lane_and_chat_slot():
    # A crashed sandbox agent must not pin the reserve: when the response body is closed early
    # (Starlette closes it on disconnect via ClosingStreamingResponse), both slots come back.
    async def body():
        yield b"first"
        await asyncio.sleep(10)
        yield b"never"

    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        stream = hold_slot(gate_for(SCOPED, chat, lane), body(), keepalive_s=0.05)
        assert await stream.__anext__() == b"first"
        assert lane.in_use == 1 and chat.in_use == 1
        await stream.aclose()                     # what Starlette does on client disconnect
        assert lane.in_use == 0 and chat.in_use == 0
    run(go())
