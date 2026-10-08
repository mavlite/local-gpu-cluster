"""Token-bucket rate limiter. See TASK.md for the full contract."""
import time


class TokenBucket:
    def __init__(self, rate, capacity, clock=time.monotonic):
        raise NotImplementedError

    def allow(self, n=1):
        raise NotImplementedError

    @property
    def tokens(self):
        raise NotImplementedError

    def wait_time(self, n=1):
        raise NotImplementedError
