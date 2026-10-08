## Router: identical Tavily searches billed again for every customer report

The weekly reporting page issues the same set of Tavily searches for every customer. The query strings
carry no customer-specific placeholders; only the LLM prompt does. An N-customer week therefore bills
N identical search sets: 42 credits for 3 customers, where 14 would do.

**Wanted:**
- Search responses are cached in-process for 6 hours.
- The key is a stable hash of the request body, so field order doesn't matter, but a change to any
  field (query, depth, result count, time range, raw content) never serves a stale hit.
- Only HTTP 200 responses are cached. An error, above all Tavily's 432 quota error, must never be
  pinned for the TTL.
- The cache is bounded in entries and value size, evicting least recently used.
- Clients can force a re-fetch.

Keep the logic stdlib-only and out of `router-app.py`, so it can be tested without the router's
dependencies. `scripts/files/tests/test_tavily_cache.py` shows the expected interface.
