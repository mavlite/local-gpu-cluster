"""Handler abstract base + Document data class.

A handler knows how to enumerate documents for one source type
(GitHub repo, Sphinx sitemap, RSS feed, etc.). It does NOT know about
state persistence, planning, or AnythingLLM — those concerns live in
refresh.py and lib/.

The contract is simple: handler.collect(config) yields Document objects.
Each Document carries the citation URL, cleaned text, and optional
metadata. refresh.py diffs collected documents against persisted state
to compute a Plan.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass
class Document:
    """One unit of corpus content: a citation URL plus its cleaned text.

    Identity is the URL. content is whatever the handler extracted (trafilatura
    output, raw .rst, ingest-friendly text). metadata is opaque — surface
    bits like published date or commit timestamp travel here.
    """
    url: str
    content: str
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # Filled in by lib/plan.compute(). Not set by handlers.
    hash: str | None = None


@dataclass
class HandlerContext:
    """Side data a handler needs: where to cache repo clones / scratch
    fetches, how long to wait between requests."""
    cache_dir: Path
    crawl_delay_seconds: int = 3
    request_timeout_seconds: int = 30


class Handler(abc.ABC):
    """Subclasses implement collect() to yield Documents for one source."""

    name: str  # short identifier used in error messages

    @abc.abstractmethod
    def collect(
        self,
        config: dict[str, Any],
        context: HandlerContext,
    ) -> Iterator[Document]:
        """Yield Documents for every page in this source.

        - May yield zero docs (empty source) without raising — caller
          will detect that as a probable outage / config error.
        - Should raise for unrecoverable config / network problems so
          refresh.py records a failed run instead of overwriting state
          with an empty document set.
        - Should be cheap to re-call (idempotent); the cache_dir on the
          context survives between runs.
        """
        raise NotImplementedError
