"""Thin AnythingLLM REST API wrapper.

Only the endpoints needed by refresh.py and migrate_backfill.py. Reuses
ALLM_API_KEY from scripts/config.env (the same convention as the existing
ingest tools).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import requests


def load_api_key(config_env: str | Path | None = None) -> str:
    """Read ALLM_API_KEY from env, then fall back to scripts/config.env."""
    key = os.environ.get("ALLM_API_KEY")
    if key:
        return key

    if config_env is None:
        # default location relative to this file: ../../config.env
        config_env = Path(__file__).resolve().parents[2] / "config.env"
    config_env = Path(config_env)
    if not config_env.exists():
        raise RuntimeError(
            f"ALLM_API_KEY not in env and config file not found: {config_env}"
        )
    for line in config_env.read_text(encoding="utf-8").splitlines():
        if line.startswith("ALLM_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"ALLM_API_KEY not set in env and not found in {config_env}")


class AnythingLLMClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )
        self.timeout = timeout

    # ─── upload ───────────────────────────────────────────────────────────
    def upload_raw_text(
        self,
        workspace: str,
        text_content: str,
        title: str,
        doc_source: str,
        url: str | None = None,
        published: str | None = None,
    ) -> dict[str, Any]:
        """POST /document/raw-text. Returns the API response JSON which
        includes documents[0].location — the doc_id we persist for later
        update/delete operations."""
        payload = {
            "textContent": text_content,
            "metadata": {
                "title": title,
                "docSource": doc_source,
                # Embed url + published into the visible text so they survive
                # AnythingLLM's metadata stripping on chunk write.
            },
            "addToWorkspaces": workspace,
        }
        if url:
            payload["metadata"]["sourceURL"] = url
        if published:
            payload["metadata"]["published"] = published

        r = self.session.post(
            f"{self.base_url}/document/raw-text",
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    # ─── workspace queries ────────────────────────────────────────────────
    def list_workspace_documents(self, workspace: str) -> list[dict[str, Any]]:
        """GET /workspace/{slug} returns the workspace including its
        documents[] array. Each entry has at minimum docpath and metadata.
        Used by migrate_backfill.py to map URL → docpath."""
        r = self.session.get(
            f"{self.base_url}/workspace/{workspace}",
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        ws = data.get("workspace") or data
        if isinstance(ws, list):
            ws = ws[0] if ws else {}
        return ws.get("documents", []) or []

    def vector_search(
        self, workspace: str, query: str, top_n: int = 5
    ) -> list[dict[str, Any]]:
        r = self.session.post(
            f"{self.base_url}/workspace/{workspace}/vector-search",
            json={"query": query, "topN": top_n},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json().get("results", []) or []

    # ─── updates / deletes ────────────────────────────────────────────────
    def update_embeddings(
        self,
        workspace: str,
        adds: list[str] | None = None,
        removes: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """POST /workspace/{slug}/update-embeddings.

        adds: doc names (the 'docpath' value AnythingLLM uses internally,
              e.g. 'raw-truenas-api-pool.dataset.query-abc123.json')
        removes: same shape — doc names to drop from the workspace

        Note: 'remove' here detaches the doc from the workspace and removes
        its embeddings. The underlying document file in AnythingLLM's
        storage isn't deleted by this call.
        """
        payload = {"adds": adds or [], "deletes": removes or []}
        r = self.session.post(
            f"{self.base_url}/workspace/{workspace}/update-embeddings",
            json=payload,
            timeout=timeout or 1800,
        )
        r.raise_for_status()
        return r.json()

    def delete_documents(self, doc_names: list[str]) -> dict[str, Any]:
        """POST /system/remove-documents. Removes documents from
        AnythingLLM storage entirely (not just from a workspace)."""
        r = self.session.delete(
            f"{self.base_url}/system/remove-documents",
            json={"names": doc_names},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()


def short_hash(s: str, length: int = 10) -> str:
    """Stable short hash for filenames. NAME_MAX cap workaround per
    scripts/tools/recover-long-urls.sh convention."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:length]
