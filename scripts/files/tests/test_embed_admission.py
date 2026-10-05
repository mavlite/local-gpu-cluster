"""The embedding token budget is PER INPUT, not per request.

Each input is embedded in its own llama-server slot (16384 tokens), so the
limit applies to each input separately. The router used to join a batch and
count the total, rejecting e.g. eight 3K-token chunks (24K total) with a 413 --
AnythingLLM sends 8 chunks per request, and documents silently failed to attach.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from embed_admission import embed_texts, first_oversized  # noqa: E402

LIMIT = 16384


def test_batch_whose_total_exceeds_the_limit_is_accepted_when_each_input_fits():
    assert first_oversized([3000] * 8, LIMIT) is None          # 24K total: fine


def test_a_single_oversized_input_is_rejected_and_named():
    assert first_oversized([100, 20000, 50], LIMIT) == (1, 20000)


def test_exactly_at_the_limit_is_accepted():
    assert first_oversized([LIMIT], LIMIT) is None


def test_unknown_counts_fail_open():
    # count_tokens returns -1 when /tokenize is unreachable
    assert first_oversized([-1, -1], LIMIT) is None
    assert first_oversized([-1, 20000], LIMIT) == (1, 20000)   # a KNOWN oversize still counts


def test_empty_batch_is_accepted():
    assert first_oversized([], LIMIT) is None


def test_embed_texts_extracts_strings_from_every_input_shape():
    assert embed_texts("one") == ["one"]
    assert embed_texts(["a", "b"]) == ["a", "b"]
    assert embed_texts(["a", 42, None, "b"]) == ["a", "b"]      # token-id lists etc. are not counted
    assert embed_texts(None) == []
    assert embed_texts({"x": 1}) == []
