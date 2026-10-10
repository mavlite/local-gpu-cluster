from token_bucket import TokenBucket


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_starts_full_and_refills():
    c = Clock()
    b = TokenBucket(2.0, 4, clock=c)
    assert b.allow(4) is True
    assert b.allow() is False
    c.t = 1.0
    assert b.allow(2) is True
