"""Both sides of an /update-embeddings call must be bounded.

The embedding pass used to send every removal in call 1, on the reasoning that
removals should still happen if a later add-batch failed. That holds for a
BACKFILL, where a run is mostly adds and removes are a handful. It breaks for a
RE-INGEST: every document becomes an update, so removes equal adds.

Measured 2026-08-25 on vcf-core-docs: 610 removes in one call ran past the
1800s client timeout and took the whole run down, leaving 610 documents
uploaded, tracked, and unattached. A 118-remove pass had taken 1140s. Cost
tracks the remove count, so capping only the adds does not bound the call.
"""
from __future__ import annotations

import os
import sys
import types
import unittest

RAG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAG_DIR)

for _m in ("requests", "yaml", "trafilatura", "feedparser", "lxml"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from refresh import plan_embed_batches  # noqa: E402


class TestPlanEmbedBatches(unittest.TestCase):
    def test_removes_are_chunked_not_all_in_first_call(self):
        """The regression: 610 removes must not land in a single call."""
        adds = [f"a{i}" for i in range(610)]
        removes = [f"r{i}" for i in range(610)]
        batches = plan_embed_batches(adds, removes, 50)
        self.assertTrue(all(len(r) <= 50 for _, r in batches),
                        "a batch carried more than batch_size removes")
        self.assertEqual(len(batches[0][1]), 50)

    def test_nothing_is_dropped(self):
        adds = [f"a{i}" for i in range(610)]
        removes = [f"r{i}" for i in range(610)]
        batches = plan_embed_batches(adds, removes, 50)
        self.assertEqual([x for c, _ in batches for x in c], adds)
        self.assertEqual([x for _, r in batches for x in r], removes)

    def test_uneven_sides_keep_every_item(self):
        """Updates make the sides equal, but adds-only and removes-heavy passes
        both occur — neither may silently truncate."""
        for na, nr in ((100, 7), (7, 100), (0, 120), (120, 0), (1, 1)):
            adds = [f"a{i}" for i in range(na)]
            removes = [f"r{i}" for i in range(nr)]
            b = plan_embed_batches(adds, removes, 50)
            self.assertEqual([x for c, _ in b for x in c], adds, f"{na}/{nr}")
            self.assertEqual([x for _, r in b for x in r], removes, f"{na}/{nr}")
            self.assertTrue(all(len(c) <= 50 and len(r) <= 50 for c, r in b))

    def test_empty_pass_still_yields_one_batch(self):
        self.assertEqual(plan_embed_batches([], [], 50), [([], [])])

    def test_batch_size_is_floored_at_one(self):
        b = plan_embed_batches(["a"], ["r"], 0)
        self.assertEqual(b, [(["a"], ["r"])])


if __name__ == "__main__":
    unittest.main()
