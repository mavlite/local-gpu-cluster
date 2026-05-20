"""url_list_hashed handler — STUB (Phase 2).

Plan: read a hand-curated list of URLs (file path or inline list in
sources.yaml), fetch each, content-hash, and yield only changed/new
URLs as Documents. Use this for one-off authoritative pages that don't
fit any other pattern (single architecture-overview page on a vendor
site, a specific RFC, etc.).

Config will accept:
  url_list_file   path to a file with one URL per line, OR
  urls            inline list of URL strings
"""
from __future__ import annotations

from typing import Any, Iterator

from .base import Document, Handler, HandlerContext


class UrlListHashedHandler(Handler):
    name = "url_list_hashed"

    def collect(
        self, config: dict[str, Any], context: HandlerContext
    ) -> Iterator[Document]:
        raise NotImplementedError(
            "url_list_hashed handler is a Phase 2 stub. Implementation is a "
            "trimmed-down sphinx_sitemap.py — no sitemap step, just the "
            "per-URL fetch + trafilatura + hash compare."
        )
        yield  # unreachable; marks this as a generator function
