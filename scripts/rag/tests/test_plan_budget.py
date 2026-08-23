"""Tests for budgeted-refresh safety.

The invariant under test: a refresh that deliberately fetches only part of a
source must never be mistaken for mass upstream deletion.

Before `known_urls` existed, plan.compute() derived removals from
"persisted - collected". That is correct when the handler fetches everything
it enumerates, but it makes a budgeted run catastrophic: fetch 300 of 13,494
techdocs URLs and the plan proposes deleting the other 13,194. The 10%
safety threshold would halt it, so the corpus was never actually at risk --
but the source could never make progress either, which is why the whole
13,494-URL tree sat un-refreshed at a 37-hour full-pass cost.
"""
from __future__ import annotations

import os
import sys
import unittest

RAG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAG_DIR)

from handlers.base import Document  # noqa: E402
from lib import plan as plan_mod  # noqa: E402

try:  # the handler pulls in trafilatura, which lives only in the scraper venv
    from handlers.sphinx_sitemap import SphinxSitemapHandler  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - dev machines without the venv
    SphinxSitemapHandler = None


def doc(url: str, content: str = "body") -> Document:
    return Document(url=url, content=content, title=url)


def persisted(*urls: str, **overrides) -> dict:
    out = {}
    for u in urls:
        out[u] = {"hash": plan_mod.content_hash("body"),
                  "last_fetched": overrides.get(u, "2026-01-01T00:00:00Z")}
    return out


class TestKnownUrls(unittest.TestCase):
    ALL = [f"https://x/{i}.html" for i in range(10)]

    def test_partial_fetch_without_known_urls_proposes_mass_deletion(self):
        """Documents the hazard the parameter exists to remove."""
        state = persisted(*self.ALL)
        p = plan_mod.compute([doc(self.ALL[0])], state)
        self.assertEqual(len(p.removes), 9)

    def test_partial_fetch_with_known_urls_proposes_no_deletion(self):
        state = persisted(*self.ALL)
        p = plan_mod.compute([doc(self.ALL[0])], state,
                             known_urls=set(self.ALL))
        self.assertEqual(p.removes, [])

    def test_known_urls_still_detects_a_genuinely_removed_url(self):
        """Budgeting must not blind the source to real upstream deletions."""
        state = persisted(*self.ALL)
        upstream = set(self.ALL) - {self.ALL[7]}
        p = plan_mod.compute([doc(self.ALL[0])], state, known_urls=upstream)
        self.assertEqual(p.removes, [self.ALL[7]])

    def test_adds_and_updates_come_only_from_fetched_slice(self):
        state = persisted(self.ALL[0])
        p = plan_mod.compute([doc(self.ALL[0], "changed"), doc(self.ALL[1])],
                             state, known_urls=set(self.ALL))
        self.assertEqual([d.url for d in p.updates], [self.ALL[0]])
        self.assertEqual([d.url for d in p.adds], [self.ALL[1]])
        self.assertEqual(p.removes, [])

    def test_additive_only_still_wins_over_known_urls(self):
        state = persisted(*self.ALL)
        p = plan_mod.compute([doc(self.ALL[0])], state, remove_missing=False,
                             known_urls={self.ALL[0]})
        self.assertEqual(p.removes, [])


@unittest.skipIf(SphinxSitemapHandler is None,
                 "trafilatura not installed (run in /opt/vcf-scraper-venv)")
class TestPrioritisation(unittest.TestCase):
    def test_never_fetched_urls_come_first(self):
        urls = ["https://x/a", "https://x/b", "https://x/c"]
        state = {"https://x/a": {"last_fetched": "2020-01-01T00:00:00Z"},
                 "https://x/b": {"last_fetched": "2019-01-01T00:00:00Z"}}
        got = SphinxSitemapHandler._prioritise(urls, state, budget=1)
        self.assertEqual(got, ["https://x/c"])

    def test_then_oldest_last_fetched(self):
        urls = ["https://x/a", "https://x/b"]
        state = {"https://x/a": {"last_fetched": "2026-08-01T00:00:00Z"},
                 "https://x/b": {"last_fetched": "2026-01-01T00:00:00Z"}}
        got = SphinxSitemapHandler._prioritise(urls, state, budget=1)
        self.assertEqual(got, ["https://x/b"])

    def test_budget_larger_than_input_returns_everything(self):
        urls = ["https://x/a", "https://x/b"]
        got = SphinxSitemapHandler._prioritise(urls, {}, budget=99)
        self.assertEqual(sorted(got), sorted(urls))

    def test_cycles_rather_than_starving_a_tail(self):
        """A URL that loses one run rises as others are refreshed."""
        urls = [f"https://x/{i}" for i in range(4)]
        state = {u: {"last_fetched": f"2026-01-0{i+1}T00:00:00Z"}
                 for i, u in enumerate(urls)}
        first = SphinxSitemapHandler._prioritise(urls, state, budget=2)
        self.assertEqual(first, ["https://x/0", "https://x/1"])
        # After refreshing those two, they become the newest.
        for u in first:
            state[u] = {"last_fetched": "2026-12-01T00:00:00Z"}
        second = SphinxSitemapHandler._prioritise(urls, state, budget=2)
        self.assertEqual(second, ["https://x/2", "https://x/3"])


if __name__ == "__main__":
    unittest.main()
