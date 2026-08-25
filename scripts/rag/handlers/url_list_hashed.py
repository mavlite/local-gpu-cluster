"""url_list_hashed handler — fetch a curated URL list, extract, hash-compare.

For content that has no sitemap and no feed: community blogs, vendor pages
scattered across hosts, one-off authoritative articles. Everything the
sphinx_sitemap handler does except discovery — you supply the URLs.

Why it exists: 5,763 of the 10,480 documents in the sdg-documentation
workspace (55%) had no owning source. They came from ad-hoc ingests predating
refresh.py — zenarmor 798, servethehome 668, truenas.com 431, 45drives 232,
phasetwo 174, homenetworkguy 120 and a long tail. They were being served to
users while nothing ever re-validated them, which is the same failure the VCF
corpus had.

Those hosts span many domains with no common sitemap, so sphinx_sitemap cannot
reach them. A curated URL list can.

Config:
  url_list_file      path to a file with one URL per line ('#' comments ok)
  urls               inline list of URL strings (either, or both)
  parallel_workers   fetch concurrency (default 1)
  max_urls_per_run   fetch budget; the full list is still reported as
                     discovered, so removals stay exact. See
                     plan.compute(known_urls=...)

REMOVAL SEMANTICS ARE DIFFERENT HERE. For a sitemap source, absence from the
sitemap means the page is gone. For a curated list, absence means *nobody
added it to the list* — so a shrinking list would delete documents that are
still perfectly good. Sources using this handler should normally set
`removal_policy: additive_only`; the handler warns if they do not.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

from .base import Document, Handler, HandlerContext
# Reuse the extraction path verbatim rather than reimplementing it: the header
# format, the tiny-extraction skip, and the per-chunk Source interleaving all
# have to stay identical or citations behave differently per source.
from .sphinx_sitemap import SphinxSitemapHandler


class UrlListHashedHandler(Handler):
    name = "url_list_hashed"

    def collect(
        self, config: dict[str, Any], context: HandlerContext
    ) -> Iterator[Document]:
        urls = self._load_urls(config)
        if not urls:
            raise RuntimeError(
                "url_list_hashed: no URLs from url_list_file or urls. "
                "Refusing to overwrite state with an empty set."
            )

        # Report everything before budgeting so removals diff against the whole
        # curated list, not the slice fetched this run.
        context.discovered_urls.update(urls)

        budget = int(config.get("max_urls_per_run") or 0)
        if budget and budget < len(urls):
            urls = SphinxSitemapHandler._prioritise(
                urls, context.prior_state, budget)
            print(f"  budgeted: fetching {len(urls)} of "
                  f"{len(context.discovered_urls)} listed URLs this run")

        fetcher = SphinxSitemapHandler()
        workers = max(1, int(config.get("parallel_workers") or 1))
        timeout = context.request_timeout_seconds
        delay = context.crawl_delay_seconds
        total = len(urls)

        def fetch_one(url: str):
            try:
                doc = fetcher._fetch_and_clean(url, timeout)
            except Exception:
                # One dead blog post must not abort a run of several thousand.
                # refresh.py records the shortfall at plan level.
                doc = None
            time.sleep(delay)
            return doc

        done = 0
        if workers == 1:
            for url in urls:
                doc = fetch_one(url)
                done += 1
                if done % 100 == 0 or done == total:
                    print(f"    fetched {done}/{total}", flush=True)
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
                    if done % 100 == 0 or done == total:
                        print(f"    fetched {done}/{total}", flush=True)
                    if doc is not None:
                        yield doc

    @staticmethod
    def _load_urls(config: dict[str, Any]) -> list[str]:
        out: list[str] = []
        path = config.get("url_list_file")
        if path:
            p = Path(path)
            if not p.is_file():
                raise RuntimeError(f"url_list_hashed: no such url_list_file: {p}")
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
        out.extend(config.get("urls") or [])
        # Deduplicate, preserving order so prioritisation stays deterministic.
        return list(dict.fromkeys(out))
