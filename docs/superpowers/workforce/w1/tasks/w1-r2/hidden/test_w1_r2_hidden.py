"""Hidden checks for w1-r2: both sides of every embedding call are bounded, nothing lost or repeated."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("requests", "yaml", "trafilatura", "feedparser", "lxml"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from refresh import plan_embed_batches  # noqa: E402


def flat(batches, side):
    return [x for b in batches for x in b[side]]


def test_a_removes_only_run_is_chunked():
    removes = [f"r{i}" for i in range(120)]
    batches = plan_embed_batches([], removes, 50)
    assert all(len(r) <= 50 for _, r in batches)
    assert flat(batches, 1) == removes and flat(batches, 0) == []


def test_uneven_sides_keep_every_item_exactly_once():
    adds = [f"a{i}" for i in range(7)]
    removes = [f"r{i}" for i in range(130)]
    batches = plan_embed_batches(adds, removes, 50)
    assert len(batches) == 3
    assert flat(batches, 0) == adds and flat(batches, 1) == removes
    assert all(len(a) <= 50 and len(r) <= 50 for a, r in batches)
