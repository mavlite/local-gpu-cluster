# T1 — TTL cache with LRU eviction

Implement `TTLCache` in `ttl_cache.py`. Every rule below is tested; nothing else is.

- `TTLCache(maxsize, ttl, clock=time.monotonic)`.
  - `maxsize < 1` raises `ValueError`.
  - `ttl <= 0` raises `ValueError`.
  - `clock` is a zero-argument callable that returns seconds as a float.
- An entry **expires** when `clock() - time_it_was_last_set >= ttl`.
- `set(key, value)`:
  - Stores the value.
  - Setting an existing key replaces its value, resets its set time, and makes it most
    recently used.
  - Inserting a **new** key when the cache already holds `maxsize` entries:
    1. first remove every expired entry;
    2. then, if it is still full, evict the **least recently used** entry.
- `get(key, default=None)`:
  - Returns the value if the key is present and not expired, and counts as a use (makes it most
    recently used).
  - A missing or expired key returns `default`. An expired key found by `get` is removed. A miss is
    not a use.
- `len(cache)` is the number of entries that are **not expired**. Expired entries are removed when
  `len` is called.
- `key in cache` is `True` only for a present, non-expired key. It is **not** a use.
