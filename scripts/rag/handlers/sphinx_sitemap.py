"""sphinx_sitemap handler.

Fetches a Sphinx-rendered docs site's sitemap.xml, filters URLs against
include/exclude patterns, falls back to scraping per-section index pages
if the sitemap is unavailable, then uses trafilatura to extract clean
text per page.

Mirrors the two-step pipeline in
scripts/tools/build-truenas-api-urls.sh + recover-long-urls.sh, but in
one Python module that yields Document objects directly.
"""
from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterator

import requests
import trafilatura

from .base import Document, Handler, HandlerContext


SITEMAP_NS_RE = re.compile(r"^\{[^}]+\}")  # strip {namespace} prefix from tags


class SphinxSitemapHandler(Handler):
    name = "sphinx_sitemap"

    def collect(
        self,
        config: dict[str, Any],
        context: HandlerContext,
    ) -> Iterator[Document]:
        sitemap_url: str = config["sitemap_url"]
        base_url: str = config["base_url"].rstrip("/")
        fallback_pages: list[str] = config.get("fallback_index_pages", [])
        include_patterns: list[str] = config.get("include_patterns", [])
        exclude_patterns: list[str] = config.get("exclude_patterns", [])

        urls = self._collect_urls(
            sitemap_url=sitemap_url,
            base_url=base_url,
            fallback_pages=fallback_pages,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            timeout=context.request_timeout_seconds,
        )

        if not urls:
            raise RuntimeError(
                f"sphinx_sitemap: collected zero URLs from {sitemap_url} "
                f"and fallback pages. Refusing to overwrite state with empty set."
            )

        for url in urls:
            try:
                doc = self._fetch_and_clean(url, context.request_timeout_seconds)
            except Exception as e:
                # Yield nothing for this URL; refresh.py records it as a
                # plan-level error and decides what to do.
                continue
            if doc is None:
                continue
            yield doc
            time.sleep(context.crawl_delay_seconds)

    # ─── URL discovery ────────────────────────────────────────────────────
    def _collect_urls(
        self,
        sitemap_url: str,
        base_url: str,
        fallback_pages: list[str],
        include_patterns: list[str],
        exclude_patterns: list[str],
        timeout: int,
    ) -> list[str]:
        urls = self._try_sitemap(sitemap_url, timeout)
        if not urls:
            urls = self._try_fallback_index(base_url, fallback_pages, timeout)

        include_re = (
            re.compile("|".join(include_patterns)) if include_patterns else None
        )
        exclude_re = (
            re.compile("|".join(exclude_patterns)) if exclude_patterns else None
        )

        out = []
        for u in urls:
            if include_re and not include_re.search(u):
                continue
            if exclude_re and exclude_re.search(u):
                continue
            out.append(u)
        return sorted(set(out))

    def _try_sitemap(self, sitemap_url: str, timeout: int) -> list[str]:
        try:
            r = requests.get(sitemap_url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
        except requests.RequestException:
            return []
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            return []
        # Sitemap XML uses xmlns; strip namespace prefixes to query <loc>.
        urls: list[str] = []
        for elem in root.iter():
            tag = SITEMAP_NS_RE.sub("", elem.tag)
            if tag == "loc" and elem.text:
                urls.append(elem.text.strip())
        return urls

    def _try_fallback_index(
        self, base_url: str, pages: list[str], timeout: int
    ) -> list[str]:
        """Scrape per-section index pages for href= entries pointing at
        sibling .html pages. Less reliable than sitemap but Sphinx always
        ships these landing pages."""
        href_re = re.compile(r'href="([^"]+\.html)(?:#[^"]*)?"')
        urls: list[str] = []
        for page in pages:
            page_url = f"{base_url}/{page.lstrip('/')}"
            try:
                r = requests.get(page_url, timeout=timeout)
                r.raise_for_status()
            except requests.RequestException:
                continue
            for href in href_re.findall(r.text):
                if href.startswith("http"):
                    urls.append(href)
                elif href.startswith("/"):
                    urls.append(urllib.parse.urljoin(base_url + "/", href))
                else:
                    urls.append(f"{base_url}/{href}")
        return urls

    # ─── page fetch + clean ───────────────────────────────────────────────
    def _fetch_and_clean(self, url: str, timeout: int) -> Document | None:
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"fetch failed: {e}") from e

        text = trafilatura.extract(
            r.text,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            no_fallback=False,
        )
        if not text or len(text.strip()) < 50:
            # Tiny extractions are usually navigation-only pages; skip.
            return None

        title = self._extract_title(r.text) or url.rsplit("/", 1)[-1]
        meta = self._extract_metadata(r.text)

        # Header preserves URL + date inside the chunk text so they survive
        # AnythingLLM's metadata-stripping at chunk write.
        header = f"Source: {url}\nURL: {url}\n"
        if meta.get("published"):
            header += f"Published: {meta['published']}\n"
        header += "\n"

        return Document(
            url=url,
            content=header + text,
            title=title,
            metadata=meta,
        )

    def _extract_title(self, html: str) -> str | None:
        m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
        return m.group(1).strip() if m else None

    def _extract_metadata(self, html: str) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        for name, attr_name in [
            ("published", r'name="citation_publication_date"'),
            ("published", r'name="article:published_time"'),
            ("modified", r'name="article:modified_time"'),
        ]:
            m = re.search(attr_name + r'\s+content="([^"]+)"', html)
            if m and name not in meta:
                meta[name] = m.group(1)
        return meta
