#!/usr/bin/env python3
"""RAG corpus refresh orchestrator.

Reads scripts/rag/sources.yaml, dispatches each enabled source to its
handler, diffs the result against persisted state, and applies the
resulting plan to AnythingLLM. Supports --dry-run, --plan, --force,
--source <id>.

Phase 1 scope: no scheduling (operator runs it), no metrics, no version
probe. The diff+apply mechanics, state tracking, and safety threshold
are all live.

Run on the PVE host (closest to /tank and existing ingest tools):
    /opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py [args]

If the venv is missing trafilatura/pyyaml/requests, install with:
    /opt/vcf-scraper-venv/bin/pip install -r scripts/rag/requirements.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make sibling modules importable when invoked as a script.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from handlers.base import HandlerContext  # noqa: E402
from handlers.github_repo import GitHubRepoHandler  # noqa: E402
from handlers.hugo_sitemap import HugoSitemapHandler  # noqa: E402
from handlers.rss import RssHandler  # noqa: E402
from handlers.sphinx_sitemap import SphinxSitemapHandler  # noqa: E402
from handlers.url_list_hashed import UrlListHashedHandler  # noqa: E402
from lib import allm, plan as plan_mod, state as state_mod  # noqa: E402


HANDLERS: dict[str, type] = {
    "github_repo": GitHubRepoHandler,
    "sphinx_sitemap": SphinxSitemapHandler,
    "hugo_sitemap": HugoSitemapHandler,
    "rss": RssHandler,
    "url_list_hashed": UrlListHashedHandler,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Refresh RAG corpus sources defined in sources.yaml"
    )
    p.add_argument("--sources-file", default=str(SCRIPT_DIR / "sources.yaml"))
    p.add_argument(
        "--source",
        help="Only refresh this source id (default: all due/forced sources)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore refresh_interval — refresh every source unconditionally",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute plan, print summary, do not apply to AnythingLLM",
    )
    p.add_argument(
        "--plan",
        action="store_true",
        help="Compute plan, emit JSON to stdout, do not apply",
    )
    p.add_argument(
        "--approve",
        metavar="PROPOSAL_PATH",
        help="Apply a previously-halted safety proposal (the JSON file written "
        "to <state_dir>/_proposals/<source>-<ts>.json). Bypasses the safety "
        "threshold for the source named in the proposal. Re-runs collect; "
        "if the new plan's removes are a subset of the approved removes, "
        "applies. If new URLs would be removed (drift), halts with a new "
        "proposal. On success, archives the proposal to _proposals/applied/.",
    )
    return p.parse_args()


def load_sources(path: Path) -> tuple[list[dict], dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("sources", []), data.get("defaults", {})


def get_handler(name: str):
    if name not in HANDLERS:
        raise ValueError(f"Unknown handler: {name!r}")
    return HANDLERS[name]()


def refresh_one(
    source: dict,
    defaults: dict,
    client: allm.AnythingLLMClient,
    dry_run: bool,
    plan_only: bool,
    approved_removes: set[str] | None = None,
    proposal_path: Path | None = None,
) -> dict:
    """Refresh a single source. Returns a result dict for reporting.

    If approved_removes is set, bypasses the safety threshold check. The
    new plan's removes MUST be a subset of approved_removes; if any extra
    URL would be removed, halts with a fresh proposal (drift detected).
    On successful apply, the original proposal_path is archived to
    _proposals/applied/.
    """
    source_id = source["id"]
    state_dir = Path(defaults.get("state_dir", "/tank/rag-state"))
    src_state = state_mod.SourceState(state_dir, source_id)

    handler = get_handler(source["handler"])
    context = HandlerContext(
        cache_dir=state_dir / source_id / "cache",
        crawl_delay_seconds=defaults.get("crawl_delay_seconds", 3),
        request_timeout_seconds=defaults.get("request_timeout_seconds", 30),
    )

    print(f"[{source_id}]  handler={source['handler']}  workspace={source['workspace']}")

    # Collect documents from the handler.
    try:
        collected = list(handler.collect(source["config"], context))
    except Exception as e:
        msg = f"handler.collect() failed: {e}"
        print(f"  ERROR: {msg}")
        src_state.append_error(msg)
        return {"source_id": source_id, "status": "error", "error": msg}

    print(f"  collected: {len(collected)} documents from source")

    # Compute plan against persisted state.
    # removal_policy controls whether URLs in state but not in the current
    # collection get marked for deletion. "additive_only" is required for
    # RSS-backed sources whose collection is a sliding recent-window; any
    # other value (or absence) uses the default "full" behavior where
    # missing URLs ARE removed (correct for github_repo, sphinx_sitemap).
    persisted = src_state.load_documents()
    removal_policy = source.get("removal_policy", "full")
    remove_missing = removal_policy != "additive_only"
    the_plan = plan_mod.compute(
        collected, persisted, remove_missing=remove_missing,
    )

    if not remove_missing:
        print(f"  removal_policy=additive_only (state-only URLs preserved)")
    print(f"  plan: {the_plan.summary()}")

    # Safety check OR approval-mode drift check.
    max_delete_pct = float(defaults.get("max_delete_pct", 0.10))
    if approved_removes is not None:
        # --approve mode: skip the percent threshold, but verify the new
        # plan's removes are a subset of what the operator already approved.
        new_removes = set(the_plan.removes)
        extra = new_removes - approved_removes
        if extra:
            # Source drifted since the proposal was written. Write a NEW
            # proposal so the operator can review the new state.
            proposal_dir = state_dir / "_proposals"
            proposal_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            new_proposal_path = proposal_dir / f"{source_id}-{ts}-drift.json"
            new_proposal_path.write_text(
                json.dumps(
                    {
                        "source_id": source_id,
                        "reason": (
                            f"approved-proposal drift: {len(extra)} URL(s) "
                            "would be removed that were not in the approved set"
                        ),
                        "extra_removes": sorted(extra),
                        "adds": [d.url for d in the_plan.adds],
                        "updates": [d.url for d in the_plan.updates],
                        "removes": the_plan.removes,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"  HALTED: approval drift — {len(extra)} new URL(s) would be "
                f"removed that weren't in the approved proposal"
            )
            print(f"  drift proposal written to {new_proposal_path}")
            return {
                "source_id": source_id,
                "status": "halted_drift",
                "extra_removes": sorted(extra),
                "proposal_path": str(new_proposal_path),
            }
        print(
            f"  approved: bypassing safety threshold "
            f"({len(new_removes)} removes, all in approved set)"
        )
    else:
        safe, reason = the_plan.safety_check(max_delete_pct)
        if not safe:
            proposal_dir = state_dir / "_proposals"
            proposal_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            halt_proposal_path = proposal_dir / f"{source_id}-{ts}.json"
            halt_proposal_path.write_text(
                json.dumps(
                    {
                        "source_id": source_id,
                        "reason": reason,
                        "adds": [d.url for d in the_plan.adds],
                        "updates": [d.url for d in the_plan.updates],
                        "removes": the_plan.removes,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"  HALTED: {reason}")
            print(f"  plan written to {halt_proposal_path}")
            print(
                f"  to apply: scripts/rag/refresh.py --approve {halt_proposal_path}"
            )
            return {
                "source_id": source_id,
                "status": "halted_safety",
                "reason": reason,
                "proposal_path": str(halt_proposal_path),
            }

    # Plan-only / dry-run modes stop here.
    if plan_only:
        return {
            "source_id": source_id,
            "status": "plan_only",
            "adds": [d.url for d in the_plan.adds],
            "updates": [d.url for d in the_plan.updates],
            "removes": the_plan.removes,
        }
    if dry_run:
        for d in the_plan.adds[:3]:
            print(f"    ADD     {d.url}")
        for d in the_plan.updates[:3]:
            print(f"    UPDATE  {d.url}")
        for url in the_plan.removes[:3]:
            print(f"    REMOVE  {url}")
        more = the_plan.total_changes - min(9, the_plan.total_changes)
        if more > 0:
            print(f"    ... and {more} more")
        return {"source_id": source_id, "status": "dry_run", "plan": the_plan.summary()}

    # Apply phase. Resilience strategy:
    #   1. Special-case migrated hashes (hash=="migrated" placeholder from
    #      migrate_backfill.py). Treat as "no content drift" — don't re-upload,
    #      just stamp the real hash. Saves the wasted upload cost on first
    #      real refresh after migration.
    #   2. Print progress every doc so long runs don't look frozen.
    #   3. Save state incrementally — after EACH upload completes and after
    #      the embedding call (whether it succeeds or fails). A Ctrl-C or
    #      network glitch loses at most one doc's tracking, not the whole run.
    new_state = dict(persisted)
    adds_docpaths: list[str] = []
    removes_docpaths: list[str] = []

    # 1) Promote migrated-hash entries: their content was vetted at migration
    #    time, no re-upload needed. Just stamp the real hash and move on.
    migrated_promoted = 0
    real_updates = []
    for doc in the_plan.updates:
        prev = persisted.get(doc.url, {})
        if prev.get("hash") == "migrated":
            entry = dict(prev)
            entry["hash"] = doc.hash
            entry["last_fetched"] = state_mod.utcnow_iso()
            entry["allm_doc_name"] = (doc.title or doc.url)[:120]
            entry["metadata"] = doc.metadata
            new_state[doc.url] = entry
            migrated_promoted += 1
        else:
            real_updates.append(doc)

    if migrated_promoted:
        print(f"  promoted {migrated_promoted} migrated-hash entries (no re-upload)")
        # Persist immediately so the cheap part is durable even if upload phase fails.
        src_state.save_documents(new_state)

    upload_queue = list(the_plan.adds) + real_updates
    total = len(upload_queue)
    if total:
        print(f"  uploading {total} documents (adds={len(the_plan.adds)}, real updates={len(real_updates)})")

    for i, doc in enumerate(upload_queue, 1):
        title = doc.title or doc.url
        if i == 1 or i % 25 == 0 or i == total:
            print(f"    [{i}/{total}] upload  {doc.url[:80]}")
        try:
            resp = client.upload_raw_text(
                workspace=source["workspace"],
                text_content=doc.content,
                title=title[:120],
                doc_source=source["doc_prefix"],
                url=doc.url,
                published=doc.metadata.get("published"),
            )
        except Exception as e:
            msg = f"upload failed for {doc.url}: {e}"
            src_state.append_error(msg)
            the_plan.errors.append((doc.url, str(e)))
            continue

        # AnythingLLM returns documents:[{location, ...}]. We persist
        # the location (docpath) so future updates/removes can address
        # the document precisely.
        docpath = None
        for d in (resp.get("documents") or [])[:1]:
            docpath = d.get("location") or d.get("docpath") or d.get("name")
        if docpath:
            adds_docpaths.append(docpath)
            # If this was a real UPDATE, queue the old docpath for removal.
            if doc.url in persisted and persisted[doc.url].get("hash") != "migrated":
                old_path = persisted[doc.url].get("allm_doc_path")
                if old_path:
                    removes_docpaths.append(old_path)

        new_state[doc.url] = {
            "hash": doc.hash,
            "last_fetched": state_mod.utcnow_iso(),
            "allm_doc_path": docpath,
            "allm_doc_name": title[:120],
            "metadata": doc.metadata,
        }
        # Save after every upload. Cheap (~10ms for state files of this size)
        # and means a Ctrl-C between uploads only loses one doc's tracking.
        src_state.save_documents(new_state)

    # Process pure removals (URLs no longer in source).
    for url in the_plan.removes:
        old = persisted.get(url, {})
        old_path = old.get("allm_doc_path")
        if old_path:
            removes_docpaths.append(old_path)
        new_state.pop(url, None)
    # Persist again to lock in the removal-from-state before we ask AnythingLLM
    # to actually delete. If the embedding call fails, state already shows the
    # URLs as removed — next run won't try to re-process them.
    src_state.save_documents(new_state)

    # Single update-embeddings call applies adds + removes atomically
    # from the workspace's perspective. This is the long-running step.
    if adds_docpaths or removes_docpaths:
        embed_timeout = defaults.get("embed_timeout_seconds", 1800)
        print(
            f"  embedding pass: +{len(adds_docpaths)} adds / "
            f"-{len(removes_docpaths)} removes (timeout {embed_timeout}s)"
        )
        print(
            "  NOTE: AnythingLLM continues processing server-side even if you "
            "Ctrl-C. Don't interrupt unless something is truly wrong — the "
            "heartbeat below tells you the request is in-flight."
        )
        try:
            _call_with_heartbeat(
                lambda: client.update_embeddings(
                    workspace=source["workspace"],
                    adds=adds_docpaths,
                    removes=removes_docpaths,
                    timeout=embed_timeout,
                ),
                label="embedding pass",
                interval_seconds=30,
            )
            print("  embedding pass complete")
        except KeyboardInterrupt:
            msg = (
                "embedding pass interrupted by user. State has been saved up to "
                "the last successful upload. Re-running this source will "
                "re-attempt the embedding pass with no additional uploads."
            )
            src_state.append_error(msg)
            print(f"  WARNING: {msg}")
            _update_manifest(
                src_state, source, new_state, the_plan,
                success=False, note="embed_interrupted",
            )
            return {"source_id": source_id, "status": "embed_interrupted"}
        except Exception as e:
            msg = f"update_embeddings failed: {e}"
            src_state.append_error(msg)
            print(f"  ERROR: {msg}")
            _update_manifest(
                src_state, source, new_state, the_plan,
                success=False, note=f"embed_error: {e}",
            )
            return {"source_id": source_id, "status": "error", "error": msg}

    _update_manifest(
        src_state, source, new_state, the_plan,
        success=True, note=None,
    )
    print(f"  applied: {the_plan.summary()}")

    # If this was an --approve run, archive the proposal so it doesn't
    # get re-applied or confused with a fresh halt.
    archived_to: str | None = None
    if proposal_path is not None and proposal_path.exists():
        applied_dir = state_dir / "_proposals" / "applied"
        applied_dir.mkdir(parents=True, exist_ok=True)
        archived_to = str(applied_dir / proposal_path.name)
        proposal_path.rename(archived_to)
        print(f"  archived approved proposal → {archived_to}")

    return {
        "source_id": source_id,
        "status": "applied",
        "plan": the_plan.summary(),
        "errors": len(the_plan.errors),
        **({"approved_from": archived_to} if archived_to else {}),
    }


def _call_with_heartbeat(fn, label: str, interval_seconds: int = 30):
    """Run `fn()` in the foreground but print a heartbeat from a daemon
    thread every `interval_seconds` while it's running.

    The HTTP call to AnythingLLM blocks the foreground thread waiting on
    the server response. Without visible heartbeats it's indistinguishable
    from a frozen process and operators reflexively Ctrl-C — but the
    server continues processing after the client disconnects, leaving
    the workspace in a partial state. The heartbeat is purely cosmetic
    but operationally critical: it tells the user the request is in
    flight and stops the Ctrl-C reflex.
    """
    done = threading.Event()
    start = time.time()

    def beat():
        while not done.wait(interval_seconds):
            elapsed = int(time.time() - start)
            print(f"  ... {label}: still waiting on AnythingLLM ({elapsed}s elapsed)")

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        done.set()
        t.join(timeout=1)


def _update_manifest(
    src_state: state_mod.SourceState,
    source: dict,
    new_state: dict,
    the_plan,
    success: bool,
    note: str | None,
) -> None:
    """Update manifest.json after a refresh attempt. Always records the
    attempt; only updates last_success when the embedding pass completed."""
    manifest = src_state.load_manifest()
    now = state_mod.utcnow_iso()
    manifest["last_refresh"] = now
    if success:
        manifest["last_success"] = now
    manifest["handler"] = source["handler"]
    manifest["workspace"] = source["workspace"]
    manifest["doc_prefix"] = source["doc_prefix"]
    manifest["stats"] = {
        "document_count": len(new_state),
        "last_change": the_plan.summary(),
        "errors_this_run": len(the_plan.errors),
        "last_note": note,
    }
    src_state.save_manifest(manifest)


def main() -> int:
    args = parse_args()

    sources_file = Path(args.sources_file)
    if not sources_file.exists():
        print(f"sources.yaml not found at {sources_file}", file=sys.stderr)
        return 2

    sources, defaults = load_sources(sources_file)

    # --approve resolves to a proposal file that names a specific source.
    # Read it, extract the source_id + approved-removes set, and constrain
    # this run to that single source.
    approved_removes: set[str] | None = None
    proposal_path: Path | None = None
    if args.approve:
        proposal_path = Path(args.approve)
        if not proposal_path.exists():
            print(f"proposal not found: {proposal_path}", file=sys.stderr)
            return 2
        try:
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"could not read proposal {proposal_path}: {e}", file=sys.stderr)
            return 2
        proposal_source_id = proposal.get("source_id")
        if not proposal_source_id:
            print(f"proposal missing source_id: {proposal_path}", file=sys.stderr)
            return 2
        if args.source and args.source != proposal_source_id:
            print(
                f"--source={args.source!r} conflicts with proposal's "
                f"source_id={proposal_source_id!r}",
                file=sys.stderr,
            )
            return 2
        args.source = proposal_source_id
        approved_removes = set(proposal.get("removes", []))
        print(
            f"[approve] applying {proposal_path.name} for source "
            f"{proposal_source_id!r} ({len(approved_removes)} approved removes)"
        )
        # --approve implies --force (we're bypassing both safety and the
        # refresh_interval gate to apply this specific operator decision).
        args.force = True

    if args.source:
        sources = [s for s in sources if s.get("id") == args.source]
        if not sources:
            print(f"No source with id={args.source!r}", file=sys.stderr)
            return 2

    api_key = allm.load_api_key()
    client = allm.AnythingLLMClient(
        base_url=defaults.get(
            "allm_base_url", "http://192.168.6.154:3001/api/v1"
        ),
        api_key=api_key,
    )

    state_dir = Path(defaults.get("state_dir", "/tank/rag-state"))
    state_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for source in sources:
        if not source.get("enabled", True):
            results.append({"source_id": source["id"], "status": "disabled"})
            continue

        if not args.force:
            src_state = state_mod.SourceState(state_dir, source["id"])
            manifest = src_state.load_manifest()
            if not state_mod.is_due(manifest, source["refresh_interval"]):
                last = manifest.get("last_success", "(never)")
                print(
                    f"[{source['id']}]  SKIP (last_success={last}, "
                    f"interval={source['refresh_interval']})"
                )
                results.append({"source_id": source["id"], "status": "skipped"})
                continue

        try:
            results.append(
                refresh_one(
                    source=source,
                    defaults=defaults,
                    client=client,
                    dry_run=args.dry_run,
                    plan_only=args.plan,
                    approved_removes=approved_removes,
                    proposal_path=proposal_path,
                )
            )
        except Exception as e:
            traceback.print_exc()
            results.append(
                {"source_id": source["id"], "status": "error", "error": str(e)}
            )

    if args.plan:
        json.dump({"results": results}, sys.stdout, indent=2)
        sys.stdout.write("\n")

    # Exit non-zero if any source errored or was halted by safety check or
    # drift detection.
    bad = sum(
        1 for r in results
        if r["status"] in ("error", "halted_safety", "halted_drift")
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
