"""Plan computation: diff source-collected documents against persisted state.

A Plan describes ADDs, UPDATEs, REMOVEs needed to make the AnythingLLM
workspace match what the source currently contains. The plan is computed
once per refresh and applied (or written to _proposals/ for review).

The diff key is the citation URL (each Document has a unique URL). Adds
are URLs the source has but state doesn't. Updates are URLs in both where
content hash differs. Removes are URLs in state but not in this source's
current collection.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from handlers.base import Document


def content_hash(text: str) -> str:
    """Stable SHA-256 of cleaned text. Whitespace-normalized so trivial
    formatting churn doesn't churn embeddings."""
    normalized = " ".join(text.split())
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class Plan:
    adds: list[Document] = field(default_factory=list)
    updates: list[Document] = field(default_factory=list)
    removes: list[str] = field(default_factory=list)  # URLs
    errors: list[tuple[str, str]] = field(default_factory=list)  # (url, message)

    # Original-state counts so safety thresholds can be computed against them.
    existing_count: int = 0

    @property
    def is_noop(self) -> bool:
        return not (self.adds or self.updates or self.removes)

    @property
    def total_changes(self) -> int:
        return len(self.adds) + len(self.updates) + len(self.removes)

    def safety_check(self, max_delete_pct: float) -> tuple[bool, str | None]:
        """Returns (safe, reason). reason is None when safe.

        A plan is considered unsafe if it would delete more than
        max_delete_pct of the source's existing documents AND there are
        more than a small absolute number to delete. The absolute floor
        prevents "1 of 2 removed = 50%" tripping the threshold on tiny
        sources where the percentage is meaningless.
        """
        if not self.removes:
            return True, None
        if self.existing_count == 0:
            return True, None
        if len(self.removes) < 5:
            return True, None
        pct = len(self.removes) / self.existing_count
        if pct > max_delete_pct:
            return False, (
                f"plan would delete {len(self.removes)} of {self.existing_count} "
                f"documents ({pct:.1%}); threshold is {max_delete_pct:.1%}"
            )
        return True, None

    def summary(self) -> str:
        return (
            f"+{len(self.adds)} ADD  ~{len(self.updates)} UPDATE  "
            f"-{len(self.removes)} REMOVE  ({len(self.errors)} errors)"
        )


def compute(
    collected: list[Document],
    persisted: dict[str, dict[str, Any]],
) -> Plan:
    """Diff collected documents against persisted state."""
    plan = Plan(existing_count=len(persisted))
    collected_urls: set[str] = set()

    for doc in collected:
        collected_urls.add(doc.url)
        prev = persisted.get(doc.url)
        new_hash = content_hash(doc.content)
        doc.hash = new_hash  # stash on the doc for downstream use

        if prev is None:
            plan.adds.append(doc)
        elif prev.get("hash") != new_hash:
            plan.updates.append(doc)
        # else: unchanged — no plan entry needed

    # Anything in state but not in the new collection is a removal.
    for url in persisted:
        if url not in collected_urls:
            plan.removes.append(url)

    return plan
