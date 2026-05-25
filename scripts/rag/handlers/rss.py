"""rss handler — fetch an RSS/Atom feed, trafilatura-extract each entry's
permalink URL, yield Documents keyed by URL.

Best for sources where new posts arrive individually rather than as a
sitemap update (vendor blogs, news feeds). RSS feeds typically expose
only the most recent N entries (often 10-50), not the full history — so
this handler is designed to be ADDITIVE: combined with the source-level
`removal_policy: additive_only` field (see refresh.py), it adds new
entries without flagging historical entries that fell out of the feed's
window as removals.

Without `additive_only`, the diff layer would plan to delete every
historical entry on each refresh (the feed is treated as the universe
of URLs), which is almost always wrong for RSS-backed sources. The
handler raises if asked to run without that flag set — see refresh.py.

Config keys (under `config:` in sources.yaml):
  rss_url              The RSS or Atom feed URL.
  url_domain_filter    Optional regex; entries whose permalink doesn't
                       match are skipped. Useful for feeds that
                       syndicate off-domain.
  max_entries          Optional cap (default: 50). Most feeds expose at
                       most ~50 entries anyway; this is a defensive cap.

Source-level keys (top level in sources.yaml, NOT under `config:`):
  removal_policy: additive_only
                       Required for RSS sources. Tells refresh.py to
                       skip the remove-stale-from-state pass.

Dependencies: feedparser, trafilatura, requests (see requirements.txt).
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterator

import feedparser
import requests
import trafilatura

from .base import Document, Handler, HandlerContext


class RssHandler(Handler):
    name = "rss"

    def collect(
        self,
        config: dict[str, Any],
        context: HandlerContext,
    ) -> Iterator[Document]:
        rss_url: str = config["rss_url"]
        url_domain_filter: str | None = config.get("url_domain_filter")
        max_entries: int = int(config.get("max_entries", 50))

        domain_re = re.compile(url_domain_filter) if url_domain_filter else None

        # feedparser handles RSS 2.0 / Atom / RDF transparently.
        feed = feedparser.parse(rss_url)
        entries = list(feed.entries[:max_entries])
        # feedparser sets bozo=1 on malformed XML but often still returns
        # entries; only fail hard if there are literally zero.
        if not entries:
            raise RuntimeError(
                f"rss: feedparser returned zero entries from {rss_url} "
                f"(bozo={getattr(feed, 'bozo', 0)}, "
                f"status={getattr(feed, 'status', None)}). "
                f"Refusing to proceed with empty collection — would otherwise "
                f"trigger spurious 'all entries removed' if removal_policy "
                f"were ever misconfigured."
            )

        for entry in entries:
            permalink = entry.get("link") or entry.get("id")
            if not isinstance(permalink, str) or not permalink.strip():
                continue
            if domain_re and not domain_re.search(permalink):
                continue

            content, fetched_from_url = self._extract_content(
                entry, permalink, context.request_timeout_seconds,
            )
            if not content or not content.strip():
                continue

            title = entry.get("title") or "(no title)"
            published = entry.get("published") or entry.get("updated") or ""
            summary = entry.get("summary") or ""

            # Provenance header survives AnythingLLM's metadata stripping at
            # chunk write time. Same convention as sphinx_sitemap / github_repo.
            header = f"Source: {permalink}\nURL: {permalink}\nTitle: {title}\n"
            if published:
                header += f"Published: {published}\n"
            header += "\n"

            yield Document(
                url=permalink,
                content=header + content,
                title=title,
                metadata={
                    "rss_feed": rss_url,
                    "published": published,
                    "title": title,
                    "summary": (summary[:500] if summary else None),
                    "fetched_from_url": fetched_from_url,
                },
            )
            # Polite crawl delay only if we actually did an HTTP fetch (when
            # the feed gave us full content already, no need to throttle).
            if fetched_from_url:
                time.sleep(context.crawl_delay_seconds)

    # ─── content extraction ───────────────────────────────────────────────
    def _extract_content(
        self, entry: Any, url: str, timeout: int,
    ) -> tuple[str | None, bool]:
        """Return (content, fetched_from_url).

        Strategy:
          1. If the feed entry has substantial content (>1 KB) in
             content:encoded / atom:content, use it directly — many vendor
             blogs syndicate full text and we save an HTTP round-trip.
          2. Otherwise fetch the permalink and trafilatura-extract.
          3. On fetch failure, fall back to whatever feed content existed
             (even if short) rather than dropping the entry entirely.

        The boolean tells the caller whether we did an HTTP fetch — used to
        decide whether to apply the crawl delay.
        """
        feed_content = self._feed_content(entry)

        # Heuristic: feeds with >1 KB of content typically include full text.
        # Sub-1 KB is almost always just a summary blurb.
        if feed_content and len(feed_content) > 1000:
            return feed_content, False

        try:
            r = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "local-gpu-cluster-rag/1.0 (rss handler)"},
                allow_redirects=True,
            )
            r.raise_for_status()
        except requests.RequestException:
            # Network blip or 4xx — fall back to whatever feed content we have,
            # even if short. Better a stub than a missing entry.
            return feed_content, False

        text = trafilatura.extract(
            r.text,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            no_fallback=False,
        )
        if text and len(text.strip()) >= 50:
            return text, True
        return feed_content, True  # last resort, fall back to feed snippet

    def _feed_content(self, entry: Any) -> str | None:
        """Pull the longest content blob from a feedparser entry.

        atom:content arrives as a list of {value, type, ...}. RSS
        content:encoded also surfaces under entry.content. Pick the longest
        candidate, which is almost always the one with actual article body
        (vs short alternative-format snippets)."""
        if "content" in entry:
            contents = entry["content"]
            if isinstance(contents, list) and contents:
                vals: list[str] = []
                for c in contents:
                    v = c.get("value") if isinstance(c, dict) else c
                    if isinstance(v, str):
                        vals.append(v)
                if vals:
                    return max(vals, key=len)
        # Atom <summary> or RSS <description> — usually a short snippet,
        # but better than nothing.
        for key in ("summary", "description"):
            v = entry.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return None
