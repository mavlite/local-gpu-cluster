"""Reserved user lane (workforce design rev 2, §5.4).

Scoped (workforce) requests pass through a second gate capped at `chat capacity - reserve`, then the
normal chat gate. Workforce traffic therefore never holds the last `reserve` chat slots, and the owner
-- who uses only the chat gate -- always finds one free. When the lane's capacity is 0 (1-slot mode)
a scoped request is refused immediately with LaneClosed rather than queued.

`gate_for` returns an object with the same acquire()/release() contract hold_slot() and
`async with` expect, so the chat handler swaps `chat_sem` for `gate_for(principal, ...)`.
"""
import asyncio

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
    waiting for the chat gate gives the lane slot back."""

    def __init__(self, lane: ReservedLane, chat_gate):
        self._lane, self._chat = lane, chat_gate

    async def acquire(self) -> bool:
        await self._lane.acquire()
        try:
            await self._chat.acquire()
        except BaseException:
            await asyncio.shield(self._lane.release())
            raise
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
