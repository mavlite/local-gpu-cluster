"""Reference solution for T3 (never copied into a worker's workspace)."""
import time


class TokenBucket:
    def __init__(self, rate, capacity, clock=time.monotonic):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.rate, self.capacity, self.clock = float(rate), capacity, clock
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self):
        now = self.clock()
        self._tokens = min(float(self.capacity), self._tokens + (now - self._last) * self.rate)
        self._last = now

    def _check(self, n):
        if n <= 0 or n > self.capacity:
            raise ValueError("n must be in 1..capacity")

    def allow(self, n=1):
        self._check(n)
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    @property
    def tokens(self):
        self._refill()
        return self._tokens

    def wait_time(self, n=1):
        self._check(n)
        self._refill()
        return 0.0 if self._tokens >= n else (n - self._tokens) / self.rate
