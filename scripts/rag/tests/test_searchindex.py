"""Sphinx searchindex.js is the authoritative document inventory.

api.truenas.com publishes no usable sitemap: the per-version URL 404s to a
143-byte HTML stub that parses as zero <loc> entries, and the root sitemap lists
three URLs (archive, 404error, root). The handler fell back to scraping index
pages and kept reporting healthy -- a silent 8-document shortfall that included
one page (api_methods_nfs.get_nfs4_clients.html) linked from no index at all.

searchindex.js listed 1,105 documents against the 1,098 scraping reached.
"""
from __future__ import annotations

import json
import os
import sys
import types
import unittest

RAG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAG_DIR)

for _m in ("trafilatura",):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import handlers.sphinx_sitemap as sm  # noqa: E402

BASE = "https://api.truenas.com/v27.0"


class FakeResp:
    def __init__(self, text, ok=True):
        self.text = text
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP error")


class TestSearchIndex(unittest.TestCase):
    def setUp(self):
        # Other tests in this suite stub `requests` as a bare ModuleType, so the
        # attribute may not exist to save. Record whether it did.
        self._had = hasattr(sm.requests, "get")
        self._real_get = getattr(sm.requests, "get", None)

    def tearDown(self):
        if self._had:
            sm.requests.get = self._real_get
        else:
            try:
                del sm.requests.get
            except AttributeError:
                pass

    def _serve(self, text, ok=True):
        sm.requests.get = lambda *a, **k: FakeResp(text, ok)

    def test_parses_sphinx_wrapper(self):
        payload = json.dumps({"docnames": ["api_methods", "rbac", "jobs"]})
        self._serve("Search.setIndex(%s)" % payload)
        urls = sm.SphinxSitemapHandler()._try_searchindex("u", BASE, 10)
        self.assertEqual(urls, [
            BASE + "/api_methods.html",
            BASE + "/jobs.html",
            BASE + "/rbac.html",
        ])

    def test_accepts_bare_json_without_wrapper(self):
        self._serve(json.dumps({"docnames": ["a"]}))
        self.assertEqual(sm.SphinxSitemapHandler()._try_searchindex("u", BASE, 10),
                         [BASE + "/a.html"])

    def test_trailing_semicolon_and_whitespace(self):
        payload = json.dumps({"docnames": ["a"]})
        self._serve("Search.setIndex(%s);\n" % payload)
        self.assertEqual(sm.SphinxSitemapHandler()._try_searchindex("u", BASE, 10),
                         [BASE + "/a.html"])

    def test_failures_return_empty_so_caller_falls_through(self):
        """A broken searchindex must not abort the run -- sitemap and index
        scraping are still behind it in the ladder."""
        for text, ok in (("not json at all", True), ("", True), ("{}", False)):
            self._serve(text, ok)
            self.assertEqual(
                sm.SphinxSitemapHandler()._try_searchindex("u", BASE, 10), [])

    def test_missing_docnames_key_is_empty_not_crash(self):
        self._serve(json.dumps({"terms": {}}))
        self.assertEqual(sm.SphinxSitemapHandler()._try_searchindex("u", BASE, 10),
                         [])


if __name__ == "__main__":
    unittest.main()
