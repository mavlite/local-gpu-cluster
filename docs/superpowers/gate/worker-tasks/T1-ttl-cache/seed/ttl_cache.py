"""TTL cache with LRU eviction. See TASK.md for the full contract."""
import time


class TTLCache:
    def __init__(self, maxsize, ttl, clock=time.monotonic):
        raise NotImplementedError

    def set(self, key, value):
        raise NotImplementedError

    def get(self, key, default=None):
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError

    def __contains__(self, key):
        raise NotImplementedError
