"""Per-input token budget for /v1/embeddings.

Each embedding input is processed in its own llama-server slot, whose context
(16384 tokens, kept equal to MAX_EMBED_INPUT_TOKENS and AnythingLLM's
EMBEDDING_MODEL_MAX_CHUNK_LENGTH) bounds ONE input. The router used to join a
batch and compare the total against that limit, so AnythingLLM's normal
8-chunks-per-request batches were rejected with 413 once their sum passed 16K,
and documents silently failed to attach.
"""
from typing import List, Optional, Sequence, Tuple


def embed_texts(inp) -> List[str]:
    """The string inputs of an OpenAI-style `input` field, in order."""
    if isinstance(inp, str):
        return [inp]
    if isinstance(inp, list):
        return [x for x in inp if isinstance(x, str)]
    return []


def first_oversized(counts: Sequence[int], limit: int) -> Optional[Tuple[int, int]]:
    """(index, tokens) of the first input over `limit`, or None.

    A count of -1 means "unknown" (/tokenize unreachable) and is allowed --
    the same fail-open the single-input check always had.
    """
    for i, n in enumerate(counts):
        if n != -1 and n > limit:
            return i, n
    return None
