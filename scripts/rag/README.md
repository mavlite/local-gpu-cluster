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
| `handlers/{hugo_sitemap,rss,url_list_hashed}.py` | Phase 2 stubs (raise NotImplementedError). |

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

Inspect the proposal. If the deletion is legitimate (e.g., vendor
deprecated old version, you intend the cleanup) you can manually edit
`documents.json` to remove the affected URLs, then re-run refresh.
Phase 2 will add a proper `--approve <proposal>` workflow.

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
| `sitemap_url` | sitemap.xml URL |
| `base_url` | Doc root URL |
| `fallback_index_pages` | List of section index page names to scrape if sitemap unavailable |
| `include_patterns` | List of regex-ish substrings; URL must match at least one |
| `exclude_patterns` | URL must NOT match any |

## Roadmap

- **Phase 1** (this) — manifest + state + diff + 2 handlers + migration
- **Phase 2** — remaining handlers (hugo_sitemap, rss, url_list_hashed), `--approve` workflow for halted plans
- **Phase 3** — systemd timer + Prometheus metrics
- **Phase 4a** — vendor version probe (detect new vendor versions)
- **Phase 4b** — coverage gap detection from router query logs (conditional)

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
