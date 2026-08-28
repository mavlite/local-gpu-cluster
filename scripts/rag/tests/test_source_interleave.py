"""Every chunk of an ingested page must carry a citable URL.

AnythingLLM chunks a document without preserving per-document metadata, so a
URL written once in a header reaches only the FIRST chunk. Retrieval that
surfaces a middle chunk of a long page then hands the model no link, and it
either omits the citation or reconstructs one from a filename.

Measured before the fix: 8/10 chunks carried a URL on typical queries, and a
4,165-character answer cited nothing at all.

The invariant under test: for any realistic page shape, and any chunk size in
the range AnythingLLM actually produces (2,700-8,200 characters observed in
production), every chunk contains a Source marker.

Two shapes broke earlier attempts and are pinned explicitly:
  - the TAIL after the final inserted marker became the last chunk, uncovered
  - a single paragraph LONGER than the marker interval (long tables, code
    blocks) cannot be covered by inserting at paragraph boundaries alone
"""
from __future__ import annotations

import os
import re
import sys
import types
import unittest

RAG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAG_DIR)

# The handler imports trafilatura/requests at module scope; neither is needed
# for the pure text function under test.
for _m in ("trafilatura", "requests"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from handlers.sphinx_sitemap import (  # noqa: E402
    SOURCE_MARKER_EVERY, _interleave_source,
)

URL = "https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/x.html"
# Production chunks measured 2,700-8,200 characters. 2,000 is included as
# margin against AnythingLLM changing its chunking; below that the marker
# overhead needed to guarantee coverage stops being worth it.
CHUNK_SIZES = (2000, 2700, 4000, 8200)


def page(n_paras: int, para_chars: int) -> str:
    """A page of n paragraphs, each roughly para_chars long, word-separated."""
    words = " ".join(["w" * 7] * max(para_chars // 8, 1))
    return "\n\n".join([words] * n_paras)


class TestSourceInterleave(unittest.TestCase):
    def assert_every_chunk_cited(self, text: str, label: str):
        out = _interleave_source(text, URL)
        for size in CHUNK_SIZES:
            chunks = [out[i:i + size] for i in range(0, len(out), size)]
            missing = [i for i, c in enumerate(chunks) if "Source:" not in c]
            self.assertEqual(
                missing, [],
                f"{label}: chunk size {size} left chunks {missing} of "
                f"{len(chunks)} with no citable URL")

    def test_short_page_is_untouched(self):
        """A page below the interval already fits in one chunk with the header."""
        short = "a short navigation page"
        self.assertEqual(_interleave_source(short, URL), short)

    def test_typical_pages(self):
        for n, p in ((20, 400), (50, 200), (6, 1500), (100, 80), (40, 50)):
            self.assert_every_chunk_cited(page(n, p), f"{n}x{p}")

    def test_tail_after_last_marker_is_covered(self):
        """Regression: the trailing segment became the final chunk, uncovered."""
        self.assert_every_chunk_cited(page(20, 400), "tail")

    def test_paragraph_longer_than_interval(self):
        """Regression: long tables/code blocks cannot split on paragraphs."""
        for n, p in ((3, 3000), (2, 6000), (1, 9000)):
            self.assertGreater(p, SOURCE_MARKER_EVERY)
            self.assert_every_chunk_cited(page(n, p), f"long-para {n}x{p}")

    def test_overhead_stays_modest(self):
        """The marker costs text. Measured 7-11% depending on paragraph shape
        (worst on many short paragraphs or one huge one, where insertions are
        densest). That is the accepted price of a citable URL in every chunk;
        the bound guards against a regression making it far worse, not against
        the cost itself."""
        for n, p in ((20, 400), (3, 3000), (100, 80)):
            t = page(n, p)
            out = _interleave_source(t, URL)
            overhead = 100 * (len(out) - len(t)) / len(t)
            self.assertLess(overhead, 13.0,
                            f"{n}x{p}: {overhead:.1f}% overhead is too much")

    def test_marker_carries_the_real_url(self):
        out = _interleave_source(page(20, 400), URL)
        self.assertIn(URL, out)


    def test_newline_separated_content_is_covered(self):
        """Regression: split(" ") returned ONE token for newline-separated text.

        keycloak's server_admin.adoc is a ~5,000-character run of
        "include::topics/x.adoc[]" lines containing no spaces at all. Splitting
        the oversized paragraph on " " yielded a single token, so the marker
        landed only AFTER the whole block -- measured 2026-08-28 on the live
        document as a 5,124-character stretch with no citable URL. Splitting on
        any whitespace fixes it.
        """
        body = "\n".join("include::topics/section%03d.adoc[]" % i
                           for i in range(200))
        out = _interleave_source(body, URL)
        pos = [m.start() for m in re.finditer(r"\[Source:", out)]
        self.assertGreater(len(pos), 1, "newline-only body got no interleaving")
        bounds = [0] + pos + [len(out)]
        gaps = [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]
        self.assertLess(max(gaps), 2000,
                        "gap %d exceeds the smallest production chunk" % max(gaps))

    def test_no_content_is_lost(self):
        """Markers are additive: stripping them must leave the original text."""
        cases = (
            ("newline", "\n".join("include::a%03d.adoc[]" % i for i in range(200))),
            ("prose", " ".join("word%03d" % i for i in range(1500))),
        )
        for name, body in cases:
            out = _interleave_source(body, URL).replace("[Source: %s]" % URL, "")
            self.assertEqual(re.sub(r"\s+", "", out),
                             re.sub(r"\s+", "", body),
                             "%s: content changed" % name)

    def test_whitespace_free_run_is_a_known_limit(self):
        """A long run with NO whitespace cannot be covered without splitting
        mid-token, which would corrupt it -- think base64 or minified blobs.
        Documented rather than fixed: such a chunk carries no prose worth
        citing. Pinned so it stays a decision, not a surprise."""
        out = _interleave_source("x" * 5000, URL)
        self.assertEqual(out.count("[Source:"), 1)


if __name__ == "__main__":
    unittest.main()
