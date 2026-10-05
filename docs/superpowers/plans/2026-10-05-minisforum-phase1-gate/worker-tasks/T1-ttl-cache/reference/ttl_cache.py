"""Reference solution for T1 (never copied into a worker's workspace)."""
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, maxsize, ttl, clock=time.monotonic):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        self.maxsize, self.ttl, self.clock = maxsize, ttl, clock
        self._d = OrderedDict()                     # key -> (value, set_time); end = most recent

    def _expired(self, set_time):
        return self.clock() - set_time >= self.ttl

    def _purge(self):
        for k in [k for k, (_, t) in self._d.items() if self._expired(t)]:
            del self._d[k]

    def set(self, key, value):
        if key in self._d:
            self._d[key] = (value, self.clock())
            self._d.move_to_end(key)
            return
        if len(self._d) >= self.maxsize:
            self._purge()
            if len(self._d) >= self.maxsize:
                self._d.popitem(last=False)
        self._d[key] = (value, self.clock())

    def get(self, key, default=None):
        item = self._d.get(key)
        if item is None:
            return default
        value, t = item
        if self._expired(t):
            del self._d[key]
            return default
        self._d.move_to_end(key)
        return value

    def __len__(self):
        self._purge()
        return len(self._d)

    def __contains__(self, key):
        item = self._d.get(key)
        return item is not None and not self._expired(item[1])
