"""Hold an admission slot for the WHOLE life of a streaming response.

Why this exists: the router used to do

    async with chat_sem:
        ...
        return StreamingResponse(gen())

which releases the semaphore the moment `return` leaves the block -- before the
generator has produced a single byte. Streamed chat (nearly all agent traffic)
therefore ran with no admission control at all; CHAT_CONCURRENCY only ever
limited non-streamed requests.

hold_slot wraps the response body instead, so the slot is acquired when the
body starts and released when it ends, however it ends: finished, upstream
error, or client disconnect (Starlette closes the generator). While a request
waits for a slot it receives SSE keepalive comments, so queued clients do not
hit their own read timeouts.
"""
import asyncio
import inspect
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional

import anyio
from starlette.responses import StreamingResponse

PING = b": ping\n\n"
log = logging.getLogger("stream_admission")


class ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse that ALWAYS closes its body iterator.

    Starlette 1.6's stream_response is a bare `async for` with no aclose(). When
    a client disconnects while the response is inside send() -- cancelled by the
    disconnect listener (ASGI < 2.4) or by send raising OSError (>= 2.4) -- the
    body generator is left suspended at a yield, and hold_slot's `finally`
    (which releases the chat slot) would only run whenever the garbage
    collector finalised it. With one chat slot, one dead client could stall
    every chat. The close is shielded because it runs inside the very
    cancellation that triggered it, and the release itself awaits.
    """

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                with anyio.CancelScope(shield=True):
                    await aclose()


class AdmissionGate:
    """A counting gate whose capacity can change while the router runs.

    chat_sem has to follow the chat server's slot count -- 1 normally, 3 in
    redteam mode -- and an asyncio.Semaphore cannot be resized. Lowering the
    capacity never evicts anything already admitted; new arrivals simply wait
    until in_use drops below the new capacity. Capacity is never below 1.

    acquire()/release() mirror asyncio.Semaphore (release is a coroutine here),
    and `async with gate:` works for non-streamed requests.
    """

    def __init__(self, capacity: int):
        self._cap = max(1, int(capacity))
        self._in_use = 0
        self._cond = asyncio.Condition()

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def in_use(self) -> int:
        return self._in_use

    def locked(self) -> bool:
        return self._in_use >= self._cap

    async def acquire(self) -> bool:
        async with self._cond:
            await self._cond.wait_for(lambda: self._in_use < self._cap)
            self._in_use += 1
        return True

    async def release(self) -> None:
        async with self._cond:
            self._in_use -= 1
            # notify_all, not notify: a waiter cancelled just as it is woken
            # must not be able to swallow the only wake-up. Waiters re-check
            # the predicate, so extra wake-ups cost nothing at this scale.
            self._cond.notify_all()

    async def set_capacity(self, capacity: int) -> None:
        async with self._cond:
            self._cap = max(1, int(capacity))
            self._cond.notify_all()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        await self.release()
        return False


async def refresh_capacity_once(
    gate: AdmissionGate, fetch_slots: Callable[[], Awaitable[Optional[int]]]
) -> Optional[int]:
    """Set the gate's capacity from the chat server's slot count.

    Returns the new capacity, or None when the count could not be read -- in
    which case the gate keeps its last value (a restarting chat server must not
    collapse admission to 1 or open it wide).
    """
    try:
        slots = await fetch_slots()
    except Exception as e:  # network errors, bad JSON, server restarting
        log.info("chat slot refresh failed, keeping capacity %s: %s", gate.capacity, e)
        return None
    if not isinstance(slots, int) or slots < 1:
        return None
    if slots != gate.capacity:
        log.info("chat admission capacity %s -> %s (server slots)", gate.capacity, slots)
        await gate.set_capacity(slots)
    return slots


async def hold_slot(
    sem,                      # asyncio.Semaphore or AdmissionGate
    body: AsyncIterator[bytes],
    keepalive_s: float,
    ping: bytes = PING,
) -> AsyncIterator[bytes]:
    # A single acquire task, waited on with a timeout, rather than
    # wait_for(sem.acquire()) in a loop: cancelling an acquire that has just
    # been granted can leak the permit. Here the outcome is read from the task
    # itself in `finally`, so a grant that races a disconnect is still returned.
    acquire = asyncio.ensure_future(sem.acquire())
    acquired = False
    try:
        while not acquire.done():
            done, _ = await asyncio.wait({acquire}, timeout=keepalive_s)
            if not done:
                yield ping
        acquire.result()
        acquired = True
        async for chunk in body:
            yield chunk
    finally:
        if not acquired:
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                acquired = True          # granted in the same instant we gave up
            else:
                acquire.cancel()
        if acquired:
            r = sem.release()                # sync for Semaphore, a coroutine for AdmissionGate
            if inspect.isawaitable(r):
                await r
        aclose = getattr(body, "aclose", None)
        if aclose is not None:
            await aclose()
