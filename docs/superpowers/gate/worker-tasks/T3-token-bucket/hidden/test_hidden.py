import pytest

from token_bucket import TokenBucket


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(rate=2.0, capacity=5):
    c = Clock()
    return TokenBucket(rate, capacity, clock=c), c


def test_rejects_bad_arguments():
    with pytest.raises(ValueError):
        TokenBucket(0, 5)
    with pytest.raises(ValueError):
        TokenBucket(1, 0)
    b, _ = make()
    for bad in (0, -1, 6):
        with pytest.raises(ValueError):
            b.allow(bad)
        with pytest.raises(ValueError):
            b.wait_time(bad)


def test_starts_full_and_drains():
    b, _ = make(capacity=3)
    assert b.tokens == 3.0
    assert b.allow() and b.allow() and b.allow()
    assert not b.allow()


def test_failed_allow_consumes_nothing():
    b, _ = make(capacity=5)
    assert b.allow(4)
    assert not b.allow(2)
    assert b.tokens == pytest.approx(1.0)


def test_fractional_refill_and_cap():
    b, c = make(rate=2.0, capacity=5)
    assert b.allow(5)
    c.t += 0.75                      # +1.5 tokens
    assert b.tokens == pytest.approx(1.5)
    assert b.allow(1)
    c.t += 100                       # capped at capacity
    assert b.tokens == pytest.approx(5.0)


def test_wait_time():
    b, c = make(rate=2.0, capacity=5)
    assert b.wait_time(5) == 0.0
    b.allow(5)
    assert b.wait_time(3) == pytest.approx(1.5)
    c.t += 1.5
    assert b.wait_time(3) == 0.0
    assert b.tokens == pytest.approx(3.0)        # wait_time consumed nothing
