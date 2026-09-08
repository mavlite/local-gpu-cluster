"""Unit tests for the router's Tavily response cache.

Deliberately importable with stdlib only — router-app.py itself pulls in
FastAPI/slowapi/prometheus, which are not installed on every dev machine.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tavily_cache as tc


BODY = {"query": "VMSA-2026 VMware Security Advisory",
        "search_depth": "basic", "max_results": 8,
        "time_range": "week", "include_raw_content": "text"}


class TestCacheKey(unittest.TestCase):
    def test_key_is_stable_across_field_order(self):
        a = dict(BODY)
        b = {k: BODY[k] for k in reversed(list(BODY))}
        self.assertEqual(tc.cache_key(a), tc.cache_key(b))

    def test_key_is_deterministic_across_calls(self):
        self.assertEqual(tc.cache_key(BODY), tc.cache_key(dict(BODY)))

    def test_every_billable_field_changes_the_key(self):
        """A different search must never serve another search's cached body."""
        base = tc.cache_key(BODY)
        for field, other in [("query", "something else"),
                             ("search_depth", "advanced"),
                             ("max_results", 4),
                             ("time_range", "month"),
                             ("include_raw_content", None)]:
            variant = dict(BODY, **{field: other})
            self.assertNotEqual(base, tc.cache_key(variant),
                                f"{field} did not affect the cache key")

    def test_missing_field_differs_from_present_field(self):
        without = {k: v for k, v in BODY.items() if k != "time_range"}
        self.assertNotEqual(tc.cache_key(BODY), tc.cache_key(without))


class TestShouldCache(unittest.TestCase):
    def test_only_200_is_cacheable(self):
        self.assertTrue(tc.should_cache(200))

    def test_errors_are_never_cached(self):
        # The 432 quota error is the reason this matters: caching it would
        # have pinned the outage in place for a full TTL.
        for status in (400, 401, 403, 429, 432, 500, 502, 503):
            self.assertFalse(tc.should_cache(status), f"{status} was cacheable")


class TestTTLCache(unittest.TestCase):
    def test_put_then_get_roundtrip(self):
        c = tc.TTLCache(ttl_s=100)
        c.put("k", {"results": [1]}, now=0.0)
        self.assertEqual(c.get("k", now=1.0), {"results": [1]})

    def test_miss_returns_none(self):
        self.assertIsNone(tc.TTLCache().get("nope", now=0.0))

    def test_entry_expires_after_ttl(self):
        c = tc.TTLCache(ttl_s=100)
        c.put("k", "v", now=0.0)
        self.assertEqual(c.get("k", now=99.0), "v")
        self.assertIsNone(c.get("k", now=101.0))

    def test_expired_entry_is_dropped_not_just_hidden(self):
        c = tc.TTLCache(ttl_s=10)
        c.put("k", "v", now=0.0)
        c.get("k", now=50.0)
        self.assertEqual(len(c), 0)

    def test_oldest_evicted_beyond_max_entries(self):
        c = tc.TTLCache(ttl_s=1000, max_entries=2)
        c.put("a", 1, now=0.0)
        c.put("b", 2, now=1.0)
        c.put("c", 3, now=2.0)
        self.assertEqual(len(c), 2)
        self.assertIsNone(c.get("a", now=3.0))
        self.assertEqual(c.get("c", now=3.0), 3)

    def test_read_refreshes_recency_so_hot_entry_survives(self):
        c = tc.TTLCache(ttl_s=1000, max_entries=2)
        c.put("a", 1, now=0.0)
        c.put("b", 2, now=1.0)
        c.get("a", now=2.0)          # 'a' is now the most recently used
        c.put("c", 3, now=3.0)       # evicts 'b', not 'a'
        self.assertEqual(c.get("a", now=4.0), 1)
        self.assertIsNone(c.get("b", now=4.0))

    def test_oversized_value_is_not_cached(self):
        c = tc.TTLCache(ttl_s=1000, max_value_bytes=50)
        c.put("k", {"big": "x" * 500}, now=0.0)
        self.assertIsNone(c.get("k", now=1.0))
        self.assertEqual(len(c), 0)

    def test_overwrite_updates_value_and_expiry(self):
        c = tc.TTLCache(ttl_s=10)
        c.put("k", "old", now=0.0)
        c.put("k", "new", now=5.0)
        self.assertEqual(c.get("k", now=14.0), "new")
        self.assertEqual(len(c), 1)

    def test_stats_track_hits_and_misses(self):
        c = tc.TTLCache(ttl_s=100)
        c.put("k", "v", now=0.0)
        c.get("k", now=1.0)
        c.get("absent", now=1.0)
        self.assertEqual(c.stats["hits"], 1)
        self.assertEqual(c.stats["misses"], 1)


if __name__ == "__main__":
    unittest.main()
