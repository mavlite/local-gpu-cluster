"""TTL cache for Tavily search responses.

The reporting page issues the SAME set of searches for every customer — the
query strings carry no customer-specific placeholders, only the LLM prompt
does. Without a cache, an N-customer week bills N identical search sets.

Kept stdlib-only and separate from router-app.py on purpose: that module
imports FastAPI/slowapi/prometheus, which are not installed on every dev
machine, so logic living there cannot be unit-tested locally.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

DEFAULT_TTL_S = 21600          # 6h — one report-writing sitting
DEFAULT_MAX_ENTRIES = 128
DEFAULT_MAX_VALUE_BYTES = 1_048_576


def cache_key(body: dict) -> str:
    """Stable SHA-256 over a Tavily request body.

    Canonical JSON (sorted keys, no whitespace) so that field order in the
    incoming request cannot split one search across two cache entries. Every
    field that changes what Tavily bills or returns — query, search_depth,
    max_results, time_range, include_raw_content — is part of the key, so a
    timeline or depth change can never serve a stale hit.
    """
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def should_cache(status_code: int) -> bool:
    """Only successful responses are cacheable.

    Caching an error would pin a transient failure in place for a full TTL —
    exactly the wrong behaviour for Tavily's 432 (plan quota exhausted), which
    otherwise clears the moment the plan resets.
    """
    return status_code == 200


class TTLCache:
    """Small LRU + TTL cache. Not thread-safe by design.

    The router runs a single uvicorn worker with an async event loop, so
    get/put never interleave mid-operation. If the worker count ever rises
    above one, this needs a lock (or a shared store) — the entries are
    per-process.
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES):
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._max_value_bytes = max_value_bytes
        self._entries: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0, "skipped_large": 0}

    def get(self, key: str, now: float):
        entry = self._entries.get(key)
        if entry is None:
            self.stats["misses"] += 1
            return None
        stored_at, value = entry
        if now - stored_at >= self._ttl_s:
            del self._entries[key]
            self.stats["misses"] += 1
            return None
        self._entries.move_to_end(key)   # keep hot entries alive under eviction
        self.stats["hits"] += 1
        return value

    def put(self, key: str, value, now: float) -> bool:
        """Store a value. Returns False if it was rejected for size."""
        try:
            size = len(json.dumps(value, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return False
        if size > self._max_value_bytes:
            self.stats["skipped_large"] += 1
            return False
        self._entries[key] = (now, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.stats["evictions"] += 1
        return True

    def __len__(self) -> int:
        return len(self._entries)
