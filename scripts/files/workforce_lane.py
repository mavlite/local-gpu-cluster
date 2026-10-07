"""Reserved user lane (workforce design rev 2, §5.4).

Scoped (workforce) requests pass through a second gate capped at `chat capacity - reserve`, then the
normal chat gate. Workforce traffic therefore never holds the last `reserve` chat slots, and the owner
-- who uses only the chat gate -- always finds one free. When the lane's capacity is 0 (1-slot mode)
a scoped request is refused immediately with LaneClosed rather than queued.

`gate_for` returns an object with the same acquire()/release() contract hold_slot() and
`async with` expect, so the chat handler swaps `chat_sem` for `gate_for(principal, ...)`.

The lane can also close while a request is already waiting (the chat layout shrank). `admitted`
and `refuse_on_lane_closed` turn that into a proper refusal instead of an HTTP 500 or an empty
stream.
"""
import asyncio
import contextlib
import inspect

POLL_S = 0.5      # lane waiters re-check capacity this often (chat capacity changes elsewhere)


class LaneClosed(Exception):
    """No workforce capacity in the current chat layout."""


class ReservedLane:
    def __init__(self, chat_gate, reserve: int = 1):
        self._chat = chat_gate
        self._reserve = max(0, int(reserve))
        self._in_use = 0
        self._cond = asyncio.Condition()

    @property
    def capacity(self) -> int:
        return max(0, self._chat.capacity - self._reserve)

    @property
    def in_use(self) -> int:
        return self._in_use

    async def acquire(self) -> bool:
        async with self._cond:
            while self._in_use >= self.capacity:
                if self.capacity == 0:
                    raise LaneClosed()
                try:
                    await asyncio.wait_for(self._cond.wait(), POLL_S)
                except asyncio.TimeoutError:
                    pass
            self._in_use += 1
            return True

    async def release(self) -> None:
        async with self._cond:
            self._in_use -= 1
            self._cond.notify_all()


class _LaneThenChat:
    """Acquire the lane, then the chat gate; release in reverse. Cancellation-safe: a cancel while
    waiting for the chat gate gives the lane slot back. A request whose chat slot arrives after the
    layout shrank below its lane share is refused (LaneClosed), never admitted over the reserve."""

    def __init__(self, lane: ReservedLane, chat_gate):
        self._lane, self._chat = lane, chat_gate

    async def acquire(self) -> bool:
        await self._lane.acquire()
        try:
            await self._chat.acquire()
        except BaseException:
            await asyncio.shield(self._lane.release())
            raise
        if self._lane.in_use > self._lane.capacity:
            # The layout shrank while we waited on the chat gate: taking this slot would leave the
            # owner without one. Give both back and refuse, as a new request would be refused.
            await asyncio.shield(self.release())
            raise LaneClosed()
        return True

    async def release(self) -> None:
        try:
            await self._chat.release()
        finally:
            await self._lane.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        await self.release()
        return False


def gate_for(principal, chat_gate, lane: ReservedLane):
    """The admission gate a request from `principal` must hold."""
    return chat_gate if principal.is_owner else _LaneThenChat(lane, chat_gate)


@contextlib.asynccontextmanager
async def admitted(gate, refused):
    """Hold `gate` for the block. LaneClosed while waiting raises `refused()` instead (an HTTP 503)."""
    try:
        await gate.acquire()
    except LaneClosed:
        raise refused() from None
    try:
        yield
    finally:
        r = gate.release()                   # sync or async, as in hold_slot
        if inspect.isawaitable(r):
            await r


async def refuse_on_lane_closed(stream, closing: bytes):
    """Pass `stream` through. If admission fails with LaneClosed, end with `closing` (an error frame
    and [DONE]) rather than an empty body. Always closes `stream` -- `async for` does not -- so a
    client disconnect still releases its slots."""
    try:
        async for chunk in stream:
            yield chunk
    except LaneClosed:
        yield closing
    finally:
        await stream.aclose()
