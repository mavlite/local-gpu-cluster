import secrets
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (sorts by ASCII)
_lock = threading.Lock()
_last = (0, 0)


def _b32(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def new_id() -> str:
    """ULID-style id: 10 chars of ms time + 16 chars random; strictly increasing in-process."""
    global _last
    with _lock:
        ms = int(time.time() * 1000)
        last_ms, last_rand = _last
        if ms <= last_ms:
            ms, rnd = last_ms, last_rand + 1  # monotonic within the same ms
        else:
            rnd = secrets.randbits(79)  # headroom so +1 never overflows 80 bits
        _last = (ms, rnd)
        return _b32(ms, 10) + _b32(rnd, 16)
