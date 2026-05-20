"""rss handler — STUB (Phase 2).

Plan: poll an RSS/Atom feed, diff entries by guid+pubDate against
state, trafilatura-extract each post URL, yield as Documents. Use this
for community blogs (homenetworkguy, ServeTheHome, klarasystems blog)
where new posts appear individually rather than as a sitemap update.

Config will accept:
  rss_url                     feed URL
  include_only_modified_since_last_run  bool — skip entries with
                                              pubDate < last_success
  url_domain_filter           optional regex to drop off-domain links
                              (some feeds syndicate other sites)
"""
from __future__ import annotations

from typing import Any, Iterator

from .base import Document, Handler, HandlerContext


class RssHandler(Handler):
    name = "rss"

    def collect(
        self, config: dict[str, Any], context: HandlerContext
    ) -> Iterator[Document]:
        raise NotImplementedError(
            "rss handler is a Phase 2 stub. Implementation will use the "
            "feedparser library (add to requirements.txt) plus trafilatura "
            "for per-entry content extraction."
        )
        yield  # unreachable; marks this as a generator function
