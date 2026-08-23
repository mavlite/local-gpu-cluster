# RAG corpus refresh system

Declarative, diff-driven refresh for the AnythingLLM knowledge bases.
Replaces the ad-hoc `scripts/tools/*` ingest pattern with a single
manifest (`sources.yaml`) + orchestrator (`refresh.py`) that knows
exactly what's been ingested and only re-uploads what changed.

This directory holds **Phase 1** of the plan: manifest, state, diff,
two real handlers (`github_repo`, `sphinx_sitemap`), and a migration
script. No scheduling yet — operator runs `refresh.py` manually.

## Quick start

One-time setup (PVE host):

```bash
# Install Python deps into the existing scraper venv
/opt/vcf-scraper-venv/bin/pip install -r scripts/rag/requirements.txt

# Bootstrap state from the already-populated workspace
/opt/vcf-scraper-venv/bin/python scripts/rag/migrate_backfill.py --dry-run
# inspect output; then re-run without --dry-run to write state files
/opt/vcf-scraper-venv/bin/python scripts/rag/migrate_backfill.py
```

Day-to-day:

```bash
# See what would change for one source, no writes
/opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py \
  --source opnsense-docs --dry-run

# Apply one source's refresh
/opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py --source opnsense-docs

# Honor refresh_interval per source — skip ones not yet due
/opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py

# Refresh everything regardless of interval
/opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py --force

# JSON plan (for piping into other tools)
/opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py --plan
```

## Files

| File | Role |
|---|---|
| `sources.yaml` | Manifest. Each source declares handler, workspace, doc_prefix, refresh interval, handler-specific config. |
| `requirements.txt` | Python deps (PyYAML, requests, trafilatura). |
| `refresh.py` | CLI orchestrator. Reads manifest, dispatches to handlers, computes plan, applies to AnythingLLM. |
| `migrate_backfill.py` | One-time bootstrap of state files from an already-populated workspace. |
| `lib/state.py` | Per-source state I/O (`manifest.json`, `documents.json`). |
| `lib/plan.py` | Plan dataclass + URL-keyed diff computation + safety threshold. |
| `lib/allm.py` | Thin AnythingLLM REST wrapper. |
| `handlers/base.py` | `Handler` ABC + `Document` dataclass. |
| `handlers/github_repo.py` | Clone repo, walk files, build citation URLs. |
| `handlers/sphinx_sitemap.py` | Sitemap → URL list → trafilatura. |
| `handlers/rss.py` | RSS / Atom feed handler. Requires `removal_policy: additive_only` at source level. |
| `handlers/{hugo_sitemap,url_list_hashed}.py` | Phase 2 stubs (raise NotImplementedError). |

## State layout

```
/tank/rag-state/
├── <source-id>/
│   ├── manifest.json       # last_refresh, last_success, stats
│   ├── documents.json      # url -> {hash, last_fetched, allm_doc_path, ...}
│   ├── errors.log          # append-only
│   └── cache/              # handler scratch (e.g. github clone caches)
└── _proposals/             # safety-threshold-halted plans for review
```

`documents.json` is the system's view of "what's currently in AnythingLLM
because we put it there." It's the source of truth for diff computation
on the next refresh — not AnythingLLM itself, since AnythingLLM doesn't
expose chunk-level provenance through its API.

## Adding a new source

Edit `sources.yaml`:

```yaml
sources:
  - id: my-new-source
    handler: github_repo
    enabled: true
    workspace: sdg-documentation
    doc_prefix: "[OFFICIAL] my-vendor/docs"
    refresh_interval: 7d
    config:
      repo: https://github.com/my-vendor/docs
      file_glob: "*.md"
      path_strip: "src/"
      rendered_base: https://docs.my-vendor.com
      url_ext_from: ".md"
      url_ext_to: "/"
```

Then preview, then apply:

```bash
/opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py \
  --source my-new-source --dry-run
/opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py \
  --source my-new-source
```

## Safety threshold

If a refresh plan would remove more than `defaults.max_delete_pct` (10%
by default) of a source's existing documents AND the absolute number
removed is ≥ 5, the run halts and writes the plan to:

```
/tank/rag-state/_proposals/<source-id>-<timestamp>.json
```

Inspect the proposal. Three options:

1. **Approve and apply** (recommended) — review the file's `removes` list, then:
   ```bash
   /opt/vcf-scraper-venv/bin/python scripts/rag/refresh.py \
     --approve /tank/rag-state/_proposals/<source-id>-<timestamp>.json
   ```
   This re-runs the source's collect step, bypasses the safety threshold,
   and verifies the new plan's removes are a subset of the approved set.
   If the source drifted since the proposal was written (new URLs would
   be removed that weren't in the proposal), the run halts again with a
   fresh `<source>-<ts>-drift.json` proposal naming the unexpected removes.
   On successful apply, the original proposal is archived to
   `/tank/rag-state/_proposals/applied/`.

2. **Reject** — delete the proposal file. The next regular refresh will
   re-halt with a fresh proposal if the condition still applies. Useful
   if the removes look wrong and you need time to investigate.

3. **Manual override** (advanced) — edit `documents.json` directly to
   remove the affected URLs, then re-run refresh. Use only for
   one-offs where `--approve` isn't appropriate (e.g., partial approval).

## Handler reference

### `github_repo` — clone a doc repo, file-by-file ingest

| Config key | Purpose |
|---|---|
| `repo` | Git URL |
| `file_glob` | `*.rst`, `*.md`, `*.adoc`, ... |
| `path_strip` | Leading path to strip when building URL (`source/`) |
| `rendered_base` | Base URL of rendered site |
| `url_ext_from` | Source extension to drop (`.rst`) |
| `url_ext_to` | URL extension to append (`.html`, `/`) |
| `url_keep_depth` | (optional) keep only N path components — for many-source-files-to-one-page sites like Keycloak |
| `url_lowercase` | (optional) lowercase URL path — for Hugo |
| `url_encode_spaces` | (optional) %20-encode spaces — for OpenZFS |
| `file_exclude_regex` | (optional) regex of relative paths to skip |

### `sphinx_sitemap` — Sphinx-hosted docs site

| Config key | Purpose |
|---|---|
| `sitemap_url` | sitemap.xml URL. May be a `<urlset>` **or a `<sitemapindex>`** — the handler follows an index one level down and unions the child `<urlset>`s (techdocs.broadcom.com ships 17 shards). Nested indexes are not followed. |
| `base_url` | Doc root URL |
| `fallback_index_pages` | List of section index page names to scrape if sitemap unavailable |
| `include_patterns` | List of regex-ish substrings; URL must match at least one |
| `exclude_patterns` | URL must NOT match any |
| `max_urls_per_run` | Optional fetch budget. The handler still ENUMERATES every URL (cheap — the sitemap shards list 13,494 techdocs URLs in ~3 min) but fetches only this many per run, prioritising never-fetched URLs first, then least-recently-fetched. Omit or `0` for no budget. See **Budgeted refresh** below. |

### `rss` — RSS / Atom feed for vendor blogs and news

| Config key | Purpose |
|---|---|
| `rss_url` | Feed URL (RSS 2.0, Atom 1.0, or RDF — feedparser auto-detects) |
| `url_domain_filter` | Optional regex; entries whose permalink doesn't match are skipped (handy for feeds that syndicate off-domain) |
| `max_entries` | Defensive cap on entries processed per refresh (default 50) |

**Required source-level field:** `removal_policy: additive_only` (at the same level as `enabled` / `workspace`, not nested under `config`). RSS feeds expose only a sliding recent-window — usually the last 10-50 posts — not the full history. Without `additive_only`, the diff layer treats the current window as the universe of URLs and marks every historical entry for deletion on each refresh.

Content extraction strategy: if the feed includes full content in `content:encoded` / `atom:content` (>1 KB heuristic), use it directly. Otherwise fetch the permalink and trafilatura-extract. On fetch failure, fall back to the feed's `summary` / `description` (even if short) rather than dropping the entry. Crawl delay applies only when an HTTP fetch happens.

### Scraping techdocs.broadcom.com (VCF)

`vcf-release-notes` targets the `vcf-reference` workspace and is **deliberately scoped to
the release-notes subtree only** — 191 URLs, ~31 min/pass at the mandated 10s delay. The
full us/en VCF 9.x tree is ~13,500 URLs, which would take ~37 hours per pass and is not
viable on a timer. The remaining topical docs are the one-time 2026-05 bulk ingest and stay
manual.

Three things to know before touching this source:

1. **Use a trafilatura handler, never AnythingLLM's upload-link scraper.** Broadcom renders
   the whole VCF nav tree into server-side HTML. Measured 2026-08-22 on the same 59 URLs:
   upload-link produced 992 KB / ~122k-token documents (~97% navigation), trafilatura
   produced 368–4,424 chars. The bloated pages hash as near-identical and get
   content-deduped — 59 uploads collapsed to 22 stored docs and every NSX page vanished.
2. **Filter to `/us/en/`.** The same tree ships fr/fr, es/es and jp/ja; 180 of the 239
   9.1 patch URLs were localizations.
3. **`<lastmod>` is not a change signal here.** Broadcom restamps essentially every URL on
   each site rebuild (13,484 of 13,494 read as `2026-08`). Only refresh.py's content-hash
   comparison detects real changes.

## Source-level fields (shared across handlers)

These fields live at the top level of each source entry in `sources.yaml`, not inside the handler-specific `config` block:

| Field | Default | Purpose |
|---|---|---|
| `id` | required | Unique slug; also the state-directory name |
| `handler` | required | One of `github_repo`, `sphinx_sitemap`, `rss` |
| `enabled` | `true` | Skip the source entirely when `false` |
| `workspace` | required | AnythingLLM workspace slug |
| `doc_prefix` | required | `metadata.docSource` used for upload (also the dedup key in migrate / cleanup tools) |
| `refresh_interval` | required | Used by `refresh.py` to skip sources not yet due (`30d`, `12h`, `90m`) |
| `removal_policy` | `full` | When `additive_only`, URLs in state but missing from current collection are LEFT IN STATE rather than added to the removes list. Required for `rss` handler; harmless on other handlers but rarely useful. |
| `crawl_delay_seconds` | from `defaults` (3) | Per-source politeness override. Set it when a host's robots.txt demands more than the global default — `vcf-release-notes` uses `10` because techdocs.broadcom.com mandates `Crawl-delay: 10`. Setting that globally would needlessly slow every other source. |
| `request_timeout_seconds` | from `defaults` (30) | Per-source override; raise it for hosts serving multi-MB sitemap shards. |

## Budgeted refresh (large corpora)

Some sources are too big to refresh in one pass. The us/en VCF 9.x tree is
~13,500 URLs; at the 10s crawl delay Broadcom's robots.txt mandates, a full
content pass is ~37 hours. Refreshing nothing is not an acceptable answer, so
the pipeline separates two operations with very different costs:

| Operation | Cost for techdocs | Completeness |
|---|---|---|
| Enumerate URLs (fetch sitemap shards) | ~3 min | complete, authoritative |
| Fetch + extract page content | 10s per URL | budgetable |

Set `max_urls_per_run` and a run fetches only a slice, but still enumerates
everything. The handler reports the full set via `context.discovered_urls`, and
`refresh.py` passes it to `plan.compute(known_urls=...)`, so:

- **adds** and **removes** are computed against the full upstream set — exact, every run
- **updates** come only from the slice actually fetched — they trickle in over the cycle

That matters because it means structural change (a new doc tree, a pruned
section, a whole new VCF version appearing) is caught within one run at near-zero
cost, while in-place content edits converge over the cycle.

**Why this needed a code change.** `plan.compute()` derives removals from
`persisted - collected`. Without `known_urls`, fetching 300 of 13,494 URLs reads
as "13,194 documents disappeared upstream" and the plan proposes deleting them.
The 10% safety threshold would halt it, so the corpus was never at risk — but the
source could never make progress either. `scripts/rag/tests/test_plan_budget.py`
pins this invariant, including that a genuinely-removed URL is still detected.

Prioritisation is never-fetched-first, then oldest `last_fetched`. It cycles
rather than starving a tail: a URL that loses one run rises to the front as
others get refreshed.

## Corpus currency and disclosure

A RAG corpus that is refreshed unevenly will confidently answer from stale
documents, and nothing in the answer says so. `vcf-reference` is the worst case:
its release-notes subtree refreshes weekly via `vcf-release-notes`, while ~10,800
topical docs are the one-time 2026-05-19 bulk ingest.

The mitigation is a mandatory `Currency:` footer in the workspace system prompt
(`scripts/57-configure-anythingllm.sh`, `VCF_PROMPT`), which branches on whether
the sources used were release notes or not.

Two things learned wiring this up, both worth keeping:

1. **Phrase it as an unconditional branch, not a conditional addition.** The
   first version said "add this note when the answer concerns version-specific
   behaviour". Verified against the live workspace, it did **not** fire on the
   topical-doc case it existed for. Rewritten as "every answer ends with one of
   these two lines", it fires correctly on both branches and correctly stays
   silent on the refusal sentinel.
2. **Verify it, do not assume it.** Prompt rules are not code; the only evidence
   they work is a live query. Re-test after any prompt edit.

If you re-ingest the topical docs, update the date in the footer.

## Roadmap

- **Phase 1** (this) — manifest + state + diff + 2 handlers + migration
- **Phase 2** — rss handler shipped (with source-level `removal_policy: additive_only`); `--approve` workflow shipped (drift-aware, archives proposals). Still pending: hugo_sitemap, url_list_hashed, sphinx_sitemap collect/fetch split for cheaper `--dry-run`. The pending handlers are stubs in `handlers/` — implement when a source actually needs them rather than building speculatively.
- **Phase 3** — shipped via [`scripts/58-rag-refresh-timer.sh`](../58-rag-refresh-timer.sh). Installs a systemd timer (default `*-*-* 03:15:00` with 10min jitter) that runs `refresh.py` on the PVE host via `/opt/vcf-scraper-venv`. Per-source `refresh_interval` in `sources.yaml` still gates whether each source actually executes — the timer just provides a regular opportunity. After each run, emits a Prometheus textfile-format metrics file (`/var/lib/rag-refresh/metrics.prom`) ready for a future node_exporter scrape, and overwrites a console log at `/var/lib/rag-refresh/last-run.log`.
- **Phase 4a** — vendor version probe (detect new vendor versions)
- **Phase 4b** — coverage gap detection from router query logs (conditional)

## Phase 3 metrics

After each scheduled run, `/var/lib/rag-refresh/metrics.prom` contains
Prometheus textfile-format gauges:

| Metric | Labels | Meaning |
|---|---|---|
| `rag_refresh_run_seconds` | — | Wall-clock duration of the last run |
| `rag_refresh_last_run_timestamp` | — | Unix epoch of last run completion |
| `rag_refresh_run_total` | `status` | Source-status counts from last run (applied / skipped / disabled / error / halted_safety / halted_drift) |
| `rag_refresh_document_count` | `source_id` | Current doc count per source from `manifest.json` stats |
| `rag_refresh_last_success_timestamp` | `source_id` | Unix epoch of last successful refresh per source (0 if never) |
| `rag_refresh_errors_this_run` | `source_id` | Per-source error count for the latest run |

The file is written atomically (tempfile + `os.replace`) so a concurrent scrape never sees a half-written gauge. The schema is also useful standalone — `cat /var/lib/rag-refresh/metrics.prom` gives a complete picture of corpus health without needing a Prometheus server.

Useful alerts to set up once node_exporter is deployed:

- `time() - rag_refresh_last_success_timestamp{source_id=...} > 7*86400` — source hasn't refreshed in over a week
- `rag_refresh_run_total{status="halted_safety"} > 0` — operator review needed
- `rag_refresh_run_total{status="halted_drift"} > 0` — approved proposal went stale
- `rag_refresh_run_total{status="error"} > 0` — handler crashed

## Notes on `migrate_backfill.py`

The migration script tries to match every existing workspace document
to a source declared in `sources.yaml`. Match strategy is:

1. Exact `metadata.docSource` match against a source's `doc_prefix`
2. URL domain match against a source's `rendered_base` / `base_url`

Documents that match neither are reported as **unmatched** and left
untracked. Unmatched docs are usually:

- From ad-hoc URL uploads outside any declared source
- From sources you've ingested but haven't added to `sources.yaml` yet
- Test / scratch uploads that should be cleaned up manually

The `hash` field for migrated entries is set to the literal string
`"migrated"`. The first real refresh recomputes the hash and either
accepts the doc as unchanged (overwrites with real hash) or treats it
as an UPDATE if the content has actually drifted since ingest.
