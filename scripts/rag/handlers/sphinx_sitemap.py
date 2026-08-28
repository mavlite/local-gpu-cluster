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

import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

import requests
import trafilatura

from .base import Document, Handler, HandlerContext


SITEMAP_NS_RE = re.compile(r"^\{[^}]+\}")  # strip {namespace} prefix from tags


# Chunks observed in production run 2,700-8,200 characters. The marker must
# repeat more often than the SMALLEST chunk for every chunk to carry one.
#
# 900, not 1200: the interval counts INPUT characters, but each inserted marker
# also lengthens the output, so the spacing seen by the chunker is the interval
# plus roughly one marker (~85 chars). 1200 measured fine at 2,700+ but left
# gaps at 1,500. 900 guarantees coverage down to 2,000-character chunks, which
# is margin against AnythingLLM changing its chunking, at ~7% text overhead.
SOURCE_MARKER_EVERY = 900


# Identify ourselves. requests defaults to "python-requests/x.y.z", which some
# hosts block outright: measured 2026-08-26, www.baeldung.com returned 403 to the
# default and 200 to this string, which accounted for a chunk of the 388
# "permanently dead" URLs in sdg-community. An honest descriptive agent was
# enough -- a browser UA was tested and gave no additional access, so there is no
# reason to impersonate one.
USER_AGENT = "local-gpu-cluster-rag/1.0 (personal documentation mirror)"
HTTP_HEADERS = {"User-Agent": USER_AGENT}


def _interleave_source(text: str, url: str) -> str:
    """Repeat a `Source:` line through the body, not just at the top.

    AnythingLLM chunks a document without preserving per-document metadata, so
    a URL written once in a header reaches only the FIRST chunk. Retrieval that
    surfaces a middle chunk of a long page then hands the model no citable
    link, and it either omits the citation or reconstructs one from a filename.
    Measured before this change: 8/10 chunks carried a URL on typical queries,
    and a 4,165-character answer cited nothing at all.

    Inserting at paragraph boundaries keeps the marker out of the middle of a
    sentence, so it does not corrupt the prose the chunk is embedded on. Costs
    roughly 8% text overhead on a long page, which buys a citable URL in every
    chunk.
    """
    if len(text) <= SOURCE_MARKER_EVERY:
        return text
    sep = "\n\n"
    marker = "[Source: " + url + "]"
    out: list[str] = []
    since = 0
    for para in text.split(sep):
        # A paragraph longer than the interval cannot be covered by inserting
        # only at paragraph boundaries -- a chunk landing wholly inside it gets
        # no marker. Technical docs hit this with long tables and code blocks.
        # Break the oversized paragraph at whitespace instead.
        if len(para) > SOURCE_MARKER_EVERY:
            # Split on ANY whitespace, keeping it, so the original text is
            # reconstructed byte-for-byte. Splitting on " " alone silently fails
            # on newline-separated content: keycloak's server_admin AsciiDoc is a
            # 5,000-character run of "include::...[]" lines with no spaces, which
            # split(" ") returns as ONE token, so the marker landed only after
            # the whole block -- a 5,124-character stretch with no citable URL
            # (measured 2026-08-28).
            tokens = re.split(r"(\s+)", para)
            piece: list[str] = []
            plen = 0
            for tok in tokens:
                piece.append(tok)
                plen += len(tok)
                if plen >= SOURCE_MARKER_EVERY and tok.strip() == "":
                    # Break on the whitespace token itself, never mid-word.
                    out.append("".join(piece))
                    out.append(marker)
                    piece, plen = [], 0
            if piece:
                out.append("".join(piece))
            since = plen
            continue
        out.append(para)
        since += len(para) + len(sep)
        if since >= SOURCE_MARKER_EVERY:
            out.append(marker)
            since = 0
    # Always close with a marker. Without it the tail after the last insertion
    # carries none, and that tail is exactly what becomes the final chunk --
    # which is how the first version of this still left a gap in testing.
    if out and out[-1] != marker:
        out.append(marker)
    return sep.join(out)


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
        searchindex_url: str = config.get("searchindex_url", "")

        urls = self._collect_urls(
            searchindex_url=searchindex_url,
            sitemap_url=sitemap_url,
            base_url=base_url,
            fallback_pages=fallback_pages,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            timeout=context.request_timeout_seconds,
            crawl_delay=context.crawl_delay_seconds,
        )

        if not urls:
            raise RuntimeError(
                f"sphinx_sitemap: collected zero URLs from {sitemap_url} "
                f"and fallback pages. Refusing to overwrite state with empty set."
            )

        # Report the FULL enumerated set before any budgeting, so refresh.py
        # can diff removals against what actually exists upstream rather than
        # against the slice we fetch below.
        context.discovered_urls.update(urls)

        budget = int(config.get("max_urls_per_run") or 0)
        if budget and budget < len(urls):
            urls = self._prioritise(urls, context.prior_state, budget)
            print(f"  budgeted: fetching {len(urls)} of "
                  f"{len(context.discovered_urls)} enumerated URLs this run")

        workers = max(1, int(config.get("parallel_workers") or 1))
        timeout = context.request_timeout_seconds
        delay = context.crawl_delay_seconds
        total = len(urls)

        def fetch_one(url: str):
            """Fetch one URL, then hold the worker slot for the crawl delay.

            Sleeping inside the worker (rather than between yields) is what
            makes N workers approximate N requests per delay window: with
            workers=4 and delay=10 the effective rate is ~2.5s between
            requests, which is the ratio the 2026-05 bulk ingest ran at
            without Cloudflare throttling.
            """
            try:
                doc = self._fetch_and_clean(url, timeout)
            except Exception:
                # Swallow per-URL failures; refresh.py records the shortfall as
                # a plan-level error. One bad page must not abort a run that
                # may be several thousand URLs long.
                doc = None
            time.sleep(delay)
            return doc

        done = 0
        if workers == 1:
            for url in urls:
                doc = fetch_one(url)
                done += 1
                if doc is not None:
                    yield doc
        else:
            print(f"  fetching {total} URLs with {workers} workers "
                  f"(~{delay / workers:.1f}s effective between requests)")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(fetch_one, u) for u in urls]
                for fut in as_completed(futures):
                    try:
                        doc = fut.result()
                    except Exception:
                        doc = None
                    done += 1
                    # Long unattended runs need a pulse; without it a
                    # multi-hour fetch looks indistinguishable from a hang.
                    if done % 100 == 0 or done == total:
                        print(f"    fetched {done}/{total}", flush=True)
                    if doc is not None:
                        yield doc


    @staticmethod
    def _prioritise(urls: list[str], prior_state: dict, budget: int) -> list[str]:
        """Pick which URLs to spend this run's fetch budget on.

        Never-fetched URLs first (a URL absent from state has no content at
        all, so it is strictly more valuable than re-checking one we already
        have), then the least-recently-fetched. last_fetched is an ISO-8601
        UTC string, so lexical sort is chronological.

        Deterministic: a URL that loses the draw this run rises to the front
        as others get refreshed, so the whole set cycles rather than starving
        a tail.
        """
        def key(u: str):
            rec = prior_state.get(u)
            if not rec:
                return (0, "")           # never fetched -> highest priority
            return (1, str(rec.get("last_fetched") or ""))
        return sorted(urls, key=key)[:budget]

    # ─── URL discovery ────────────────────────────────────────────────────
    def _collect_urls(
        self,
        sitemap_url: str,
        base_url: str,
        fallback_pages: list[str],
        include_patterns: list[str],
        exclude_patterns: list[str],
        timeout: int,
        crawl_delay: int = 0,
        searchindex_url: str = "",
    ) -> list[str]:
        # Discovery order: searchindex -> sitemap -> index scraping. Each step is
        # announced, because a SILENT fallback is how truenas-api-v27 spent months
        # looking healthy: its declared sitemap 404s to a 143-byte HTML stub, which
        # parses as zero <loc> entries, so the handler quietly scraped index pages
        # instead and nothing ever said so.
        urls = []
        if searchindex_url:
            urls = self._try_searchindex(searchindex_url, base_url, timeout)
            if urls:
                print(f"  discovery: searchindex ({len(urls)} URLs)")
        if not urls:
            urls = self._try_sitemap(sitemap_url, timeout, crawl_delay)
            if urls:
                print(f"  discovery: sitemap ({len(urls)} URLs)")
        if not urls:
            urls = self._try_fallback_index(base_url, fallback_pages, timeout)
            print(f"  discovery: FALLBACK index scraping ({len(urls)} URLs) — "
                  f"sitemap {sitemap_url} yielded nothing")

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

    def _try_searchindex(
        self, searchindex_url: str, base_url: str, timeout: int
    ) -> list[str]:
        """Enumerate a Sphinx site from its searchindex.js.

        Sphinx ships `Search.setIndex({...})` containing a `docnames` array that
        lists EVERY document in the build -- it is the authoritative inventory,
        and unlike index-page scraping it needs no per-site include patterns and
        picks up new pages automatically.

        Used because api.truenas.com publishes no usable sitemap: the per-version
        URL 404s and the root sitemap.xml lists three URLs (archive, 404error,
        root). searchindex.js lists 1,105 documents; index scraping found 1,098.

        Returns [] on any failure so the caller falls through to sitemap/index.
        """
        try:
            r = requests.get(searchindex_url, timeout=timeout,
                             allow_redirects=True, headers=HTTP_HEADERS)
            r.raise_for_status()
            raw = r.text
        except Exception as exc:
            print(f"  searchindex fetch failed ({exc}); falling through")
            return []
        m = re.search(r"Search\.setIndex\((\{.*\})\)\s*;?\s*$", raw, re.S)
        body = m.group(1) if m else raw
        try:
            names = json.loads(body).get("docnames") or []
        except Exception as exc:
            print(f"  searchindex parse failed ({exc}); falling through")
            return []
        return sorted(f"{base_url}/{n}.html" for n in names)

    def _try_sitemap(
        self, sitemap_url: str, timeout: int, crawl_delay: int = 0
    ) -> list[str]:
        """Fetch a sitemap and return its page URLs.

        Handles BOTH shapes of the sitemaps protocol:
          <urlset>       -> the <loc>s ARE page URLs, return them.
          <sitemapindex> -> the <loc>s are CHILD SITEMAPS; fetch each and
                            return the union of their page URLs.

        Large doc sites (techdocs.broadcom.com ships 17 sub-sitemaps) use the
        index form. Without this, the handler would "succeed" and return a
        handful of sitemap URLs instead of pages — which then fail extraction
        and look like an empty source.
        """
        locs, is_index = self._fetch_sitemap_locs(sitemap_url, timeout)
        if not is_index:
            return locs

        urls: list[str] = []
        for child in locs:
            if crawl_delay:
                time.sleep(crawl_delay)
            child_locs, child_is_index = self._fetch_sitemap_locs(child, timeout)
            # One level of nesting only. Nested indexes are vanishingly rare
            # and recursing risks an unbounded fetch loop on a malformed feed.
            if child_is_index:
                continue
            urls.extend(child_locs)
        return urls

    def _fetch_sitemap_locs(
        self, sitemap_url: str, timeout: int
    ) -> tuple[list[str], bool]:
        """Return (locs, is_sitemap_index) for one sitemap document."""
        try:
            r = requests.get(sitemap_url, timeout=timeout, allow_redirects=True,
                             headers=HTTP_HEADERS)
            r.raise_for_status()
        except requests.RequestException:
            return [], False
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            return [], False
        is_index = SITEMAP_NS_RE.sub("", root.tag) == "sitemapindex"
        # Sitemap XML uses xmlns; strip namespace prefixes to query <loc>.
        locs: list[str] = []
        for elem in root.iter():
            tag = SITEMAP_NS_RE.sub("", elem.tag)
            if tag == "loc" and elem.text:
                locs.append(elem.text.strip())
        return locs, is_index

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
                r = requests.get(page_url, timeout=timeout, headers=HTTP_HEADERS)
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
            r = requests.get(url, timeout=timeout, headers=HTTP_HEADERS)
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
            content=header + _interleave_source(text, url),
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
