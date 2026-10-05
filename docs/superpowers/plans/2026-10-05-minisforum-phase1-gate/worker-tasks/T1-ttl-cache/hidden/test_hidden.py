import pytest

from ttl_cache import TTLCache


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(maxsize=3, ttl=10):
    c = Clock()
    return TTLCache(maxsize, ttl, clock=c), c


def test_rejects_bad_arguments():
    with pytest.raises(ValueError):
        TTLCache(0, 10)
    with pytest.raises(ValueError):
        TTLCache(1, 0)


def test_get_set_and_default():
    cache, _ = make()
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert cache.get("missing", "d") == "d"


def test_expiry_is_inclusive_at_ttl():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.t = 9.999
    assert cache.get("a") == 1
    clock.t = 10.0
    assert cache.get("a") is None


def test_set_existing_key_resets_time():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.t = 8
    cache.set("a", 2)
    clock.t = 15
    assert cache.get("a") == 2


def test_lru_eviction_counts_get_as_use():
    cache, _ = make(maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")                 # a is now most recent
    cache.set("c", 3)              # evicts b
    assert cache.get("b") is None
    assert cache.get("a") == 1 and cache.get("c") == 3


def test_miss_and_contains_are_not_uses():
    cache, _ = make(maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert "a" in cache
    cache.get("zzz")
    cache.set("c", 3)              # a is still least recent -> evicted
    assert "a" not in cache
    assert "b" in cache


def test_expired_entries_purged_before_lru_eviction():
    cache, clock = make(maxsize=2, ttl=10)
    cache.set("old", 1)
    clock.t = 5
    cache.set("fresh", 2)
    clock.t = 11                   # old expired, fresh not
    cache.get("fresh")
    cache.set("new", 3)            # purge removes old; no LRU eviction needed
    assert cache.get("fresh") == 2 and cache.get("new") == 3


def test_len_excludes_expired():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.t = 5
    cache.set("b", 2)
    clock.t = 12
    assert len(cache) == 1


def test_contains_false_for_expired():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.t = 10
    assert "a" not in cache
