"""Hidden checks for w2-05: a shrink to 2 slots refuses the waiting request; the lane recovers."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_workforce_lane import SCOPED, AdmissionGate, LaneClosed, ReservedLane, gate_for, run  # noqa: E402


def test_a_shrink_to_two_refuses_the_waiting_request_and_the_lane_recovers():
    async def go():
        chat = AdmissionGate(3)
        lane = ReservedLane(chat, reserve=1)
        await chat.acquire()                      # the owner holds 1 slot
        a = gate_for(SCOPED, chat, lane)
        await a.acquire()                         # scoped A: lane 1/2, chat 2/3
        c = gate_for(SCOPED, chat, lane)
        await c.acquire()                         # scoped C: lane 2/2, chat 3/3
        await c.release()
        await chat.acquire()                      # the owner takes the freed slot: chat 3/3, lane 1/2
        b = asyncio.ensure_future(gate_for(SCOPED, chat, lane).acquire())
        await asyncio.sleep(0.05)
        assert lane.in_use == 2 and not b.done()  # B is past the lane, waiting on the chat gate
        await chat.set_capacity(2)                # the lane share drops to 1, held by A
        await chat.release()
        await chat.release()                      # the owner frees both: B would now get a chat slot
        with pytest.raises(LaneClosed):
            await asyncio.wait_for(b, 2.0)
        assert lane.in_use == 1                   # only A holds the lane
        await a.release()
        await chat.set_capacity(3)
        d = gate_for(SCOPED, chat, lane)
        await asyncio.wait_for(d.acquire(), 1.0)  # after growing back, a scoped request is admitted
        await d.release()
    run(go())
