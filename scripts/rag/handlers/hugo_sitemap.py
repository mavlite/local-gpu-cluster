"""hugo_sitemap handler — STUB (Phase 2).

Plan: like sphinx_sitemap, but tuned for Hugo's sitemap shape and able
to honor HTTP Last-Modified / If-Modified-Since for cheap freshness
checks (Hugo emits these headers reliably; Sphinx often doesn't).

Useful for: truenas.com/docs (rendered), docs.opnsense.org (rendered),
any Hugo-generated vendor site where we want the rendered content
rather than the upstream Markdown.
"""
from __future__ import annotations

from typing import Any, Iterator

from .base import Document, Handler, HandlerContext


class HugoSitemapHandler(Handler):
    name = "hugo_sitemap"

    def collect(
        self, config: dict[str, Any], context: HandlerContext
    ) -> Iterator[Document]:
        raise NotImplementedError(
            "hugo_sitemap handler is a Phase 2 stub. See "
            "scripts/rag/handlers/sphinx_sitemap.py for the general pattern; "
            "this handler will add Last-Modified caching on top."
        )
        yield  # unreachable; marks this as a generator function
