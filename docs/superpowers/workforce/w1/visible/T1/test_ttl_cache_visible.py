from ttl_cache import TTLCache


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_set_then_get():
    cache = TTLCache(2, 10, clock=Clock())
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert cache.get("b", "default") == "default"


def test_entry_expires_after_ttl():
    clock = Clock()
    cache = TTLCache(2, 10, clock=clock)
    cache.set("a", 1)
    clock.t = 10.0
    assert cache.get("a") is None
