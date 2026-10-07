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
from workforce_lane import (LaneClosed, ReservedLane, admitted, gate_for,  # noqa: E402
                            refuse_on_lane_closed)

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


def test_stream_waiting_in_the_lane_ends_with_closing_frames_if_the_lane_closes():
    # Security review L2: LaneClosed raised while waiting ended the stream as an empty 200 (no error
    # frame, no [DONE]) -- a harness could record that as a blank completion.
    async def body():
        yield b"never"

    async def go():
        chat = AdmissionGate(2)
        lane = ReservedLane(chat, reserve=1)
        holder = gate_for(SCOPED, chat, lane)
        await holder.acquire()                   # the lane (capacity 1) is full
        stream = refuse_on_lane_closed(hold_slot(gate_for(SCOPED, chat, lane), body(), keepalive_s=0.05),
                                       b"CLOSED")
        out = []

        async def consume():
            async for c in stream:
                out.append(c)

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.1)
        await chat.set_capacity(1)               # lane capacity -> 0 while the request waits
        await asyncio.wait_for(task, 3.0)
        assert out[-1] == b"CLOSED" and b"never" not in out
        await holder.release()
        assert lane.in_use == 0 and chat.in_use == 0
    run(go())


def test_closing_the_guarded_stream_mid_body_releases_both_slots():
    async def body():
        yield b"first"
        await asyncio.sleep(10)
        yield b"never"

    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        stream = refuse_on_lane_closed(hold_slot(gate_for(SCOPED, chat, lane), body(), keepalive_s=0.05),
                                       b"CLOSED")
        assert await stream.__anext__() == b"first"
        assert lane.in_use == 1 and chat.in_use == 1
        await stream.aclose()                     # what Starlette does on client disconnect
        assert lane.in_use == 0 and chat.in_use == 0
    run(go())


def test_admitted_raises_the_callers_error_when_the_lane_is_closed_and_releases_on_exit():
    class Refused(Exception):
        pass

    async def go():
        one = AdmissionGate(1)
        with pytest.raises(Refused):
            async with admitted(gate_for(SCOPED, one, ReservedLane(one, reserve=1)), Refused):
                pass
        assert one.in_use == 0
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        async with admitted(gate_for(SCOPED, chat, lane), Refused):
            assert lane.in_use == 1 and chat.in_use == 1
        assert lane.in_use == 0 and chat.in_use == 0
        async with admitted(chat, Refused):      # the owner's plain chat gate works the same way
            assert chat.in_use == 1
        assert chat.in_use == 0
    run(go())


def test_a_request_admitted_after_a_shrink_never_takes_the_owners_last_slot():
    # Final review: a scoped request already past the lane but waiting on the chat gate, when the
    # layout shrank 3 -> 1, was admitted to the only slot ahead of the owner -- breaking "workforce
    # holds at most total_slots - reserve". It must be refused instead.
    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        await chat.acquire()
        await chat.acquire()                      # the owner holds 2 slots
        a = gate_for(SCOPED, chat, lane)
        await a.acquire()                         # scoped A holds the third
        b = asyncio.ensure_future(gate_for(SCOPED, chat, lane).acquire())
        await asyncio.sleep(0.05)
        assert lane.in_use == 2 and not b.done()  # B is past the lane, waiting on the chat gate
        await chat.set_capacity(1)
        await chat.release()
        await chat.release()
        await a.release()                         # everything drains; B would now get the only slot
        with pytest.raises(LaneClosed):
            await asyncio.wait_for(b, 2.0)
        assert lane.in_use == 0 and chat.in_use == 0
        await asyncio.wait_for(chat.acquire(), 0.5)   # the owner gets the slot at once
    run(go())
