"""Hidden checks for w2-07: the 6 h default, quota errors never cached, keys split on every field."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tavily_cache as tc  # noqa: E402


def test_the_default_ttl_is_six_hours():
    c = tc.TTLCache()
    c.put("k", {"r": 1}, now=0.0)
    assert c.get("k", now=6 * 3600 - 60) == {"r": 1}
    assert c.get("k", now=6 * 3600 + 60) is None


def test_a_quota_error_is_never_cacheable():
    assert not tc.should_cache(432) and not tc.should_cache(429) and tc.should_cache(200)


def test_each_billable_field_splits_the_key():
    base = {"query": "vcf 9.1 release notes", "search_depth": "basic", "max_results": 5}
    keys = {tc.cache_key(base), tc.cache_key(dict(base, max_results=6)),
            tc.cache_key(dict(base, search_depth="advanced")), tc.cache_key(dict(base, time_range="week"))}
    assert len(keys) == 4
    reordered = {"max_results": 5, "query": "vcf 9.1 release notes", "search_depth": "basic"}
    assert tc.cache_key(reordered) == tc.cache_key(base)
