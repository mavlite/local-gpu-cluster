# Document ingestion tools

Helper scripts for bulk-ingesting docs into AnythingLLM workspaces. Used during VCF and OPNsense corpus loads.

| Script | Purpose |
|---|---|
| `ingest-urls.sh` | URL-scrape path via AnythingLLM's `/document/upload-link`. Sequential, single-worker, default 10s crawl delay, resumable. Pre-filters URLs > 217 chars (NAME_MAX cap). Optional `--embed` triggers workspace embedding at the end. Best for short URLs from sites where AnythingLLM's built-in extractor produces clean text. |
| `ingest-urls-parallel.sh` | Parallel-worker variant of `ingest-urls.sh` with adaptive 30→300s backoff on 429/503. Run N workers in parallel against pre-split chunks of a URL list, then run one aggregated embed pass. |
| `recover-long-urls.sh` | Trafilatura-based clean-text path. POSTs to `/document/raw-text` with a short hash-based filename, custom `metadata.docSource` tag, and publication-date extracted from page metadata. Required for URLs > 217 chars (where the URL-scrape path hits ENAMETOOLONG), and **preferred for community content** where boilerplate stripping and source tagging matter. |
| `ingest-github-repo.sh` | Clone a GitHub doc repo (e.g., `opnsense/docs`) and ingest its `.rst`/`.md` files via `/document/raw-text`. Maps file paths to rendered docs URLs for citation usability and pulls per-file last-modified date from `git log`. Best path for any source where the maintainers publish the docs as text in a repo. |
| `clear-workspace.sh` | Interactively wipe all embeddings from a workspace via `/workspace/<slug>/update-embeddings` deletes. Use before a clean re-ingest when the existing corpus is contaminated. |
| `build-truenas-api-urls.sh` | Build a URL list for `api.truenas.com` (Sphinx-rendered middleware API reference). Prefers `sitemap.xml`, falls back to scraping per-section index pages. Output feeds into `recover-long-urls.sh`. Use any time a new TrueNAS API doc version needs to be ingested or refreshed. |

## When to use which

```
Is the source on GitHub as text (.rst/.md)?           → ingest-github-repo.sh
URLs ≤ 217 chars AND AnythingLLM's extractor is clean? → ingest-urls.sh
URLs > 217 chars, OR need custom source tag/date?     → recover-long-urls.sh (with DOC_PREFIX)
Need to wipe a workspace first?                       → clear-workspace.sh
```

The URL-scrape path (`ingest-urls.sh`) is fast and simple but ingests whatever HTML AnythingLLM extracts — including site navigation, footer build hashes, and version selectors. For corpora where those leak into retrieval results, prefer the trafilatura path (`recover-long-urls.sh`) so we control the extraction.

## Quick start

```bash
# Run once on the host (needed for the trafilatura path)
apt install -y python3-venv git
python3 -m venv /opt/vcf-scraper-venv
/opt/vcf-scraper-venv/bin/pip install trafilatura requests

# --- A. Sequential URL ingest (small corpus) ---
scripts/tools/ingest-urls.sh /tank/docs/urls.txt vcf-reference --embed

# --- B. Parallel URL ingest (large corpus, 4 workers) ---
for w in 1 2 3 4; do
  awk -v w=$w 'NR%4==(w-1)' /tank/docs/urls.txt > /tank/docs/urls-w${w}.txt
done
for w in 1 2 3 4; do
  WORKER_ID=$w STATE_DIR=/tank/docs/.state-w${w} CRAWL_DELAY=10 \
  nohup scripts/tools/ingest-urls-parallel.sh \
    /tank/docs/urls-w${w}.txt vcf-reference \
    > /tank/docs/ingest-w${w}.log 2>&1 &
done
# After all workers finish, aggregate + embed:
cat /tank/docs/.state-w*/document-names.txt | sort -u > /tmp/all-docs.txt
ALLM_API_KEY=$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env | cut -d= -f2-)
payload=$(python3 -c "
import json
with open('/tmp/all-docs.txt') as f:
    print(json.dumps({'adds': [n.strip() for n in f if n.strip()]}))")
curl -sS -X POST -H "Authorization: Bearer $ALLM_API_KEY" -H "Content-Type: application/json" \
     --max-time 1800 -d "$payload" \
     http://192.168.6.154:3001/api/v1/workspace/vcf-reference/update-embeddings

# --- C. Trafilatura recovery for long URLs OR community content ---
DOC_PREFIX="[COMMUNITY] homenetworkguy.com" \
  scripts/tools/recover-long-urls.sh /tank/docs/homenetworkguy-urls.txt sdg-documentation --embed

# --- D. GitHub doc repo ingest (preferred path for official OPNsense docs) ---
scripts/tools/ingest-github-repo.sh \
  https://github.com/opnsense/docs \
  sdg-documentation \
  "[OFFICIAL] opnsense/docs" \
  https://docs.opnsense.org \
  "source/" \
  --embed

# --- D'. GitHub repo with single-page-per-guide rendering (Keycloak AsciiDoc) ---
# Many topic .adoc files compile into ONE HTML guide page → URL_KEEP_DEPTH=1
FILE_GLOB="*.adoc" \
URL_KEEP_DEPTH=1 \
FILE_EXCLUDE="^docs/(guides|maven-plugin|documentation/(dist|header-maven-plugin|internal_resources|tests|aggregation|topics))/" \
scripts/tools/ingest-github-repo.sh \
  https://github.com/keycloak/keycloak \
  sdg-documentation \
  "[OFFICIAL] keycloak/docs" \
  https://www.keycloak.org/docs/latest \
  "docs/documentation/" \
  --embed

# --- E. Wipe a workspace before clean re-ingest ---
scripts/tools/clear-workspace.sh sdg-documentation

# --- F. TrueNAS API reference (api.truenas.com) ---
# Two-step: build URL list, then trafilatura-ingest.
scripts/tools/build-truenas-api-urls.sh /tank/docs/truenas-api-urls.txt
DOC_PREFIX="[OFFICIAL] api.truenas.com" \
  scripts/tools/recover-long-urls.sh \
  /tank/docs/truenas-api-urls.txt \
  sdg-documentation \
  --embed

# To re-ingest for a different API version (e.g. when TrueNAS bumps from
# v27.0 to v27.5), set API_VERSION and re-run both steps.
API_VERSION=v27.5 scripts/tools/build-truenas-api-urls.sh /tank/docs/truenas-api-v27.5-urls.txt
```

## Environment variables

| Var | Default | Purpose | Used by |
|---|---|---|---|
| `ALLM_API_KEY` | from `scripts/config.env` | AnythingLLM API key | all |
| `ALLM` | `http://192.168.6.154:3001/api/v1` | AnythingLLM API base URL | all |
| `CRAWL_DELAY` | `10` | Seconds between requests (respect upstream `robots.txt`) | URL ingesters |
| `STATE_DIR` | adjacent to URL list / clone dir | Where done/error state goes (resumable) | all |
| `WORKER_ID` | `0` | Numeric tag for parallel workers (appears in logs) | `ingest-urls-parallel.sh` |
| `VENV_PY` | `/opt/vcf-scraper-venv/bin/python` | Python for `trafilatura` | `recover-long-urls.sh` |
| `DOC_PREFIX` | (script-specific) | `metadata.docSource` tag, e.g. `"[OFFICIAL] opnsense/docs"` | `recover-long-urls.sh`, `ingest-github-repo.sh` |
| `FILE_GLOB` | `*.rst` | `find -name` pattern | `ingest-github-repo.sh` |
| `FILE_EXCLUDE` | `^_themes/` | Regex of relative paths to skip | `ingest-github-repo.sh` |
| `URL_EXT_FROM` | `.rst` | File extension to strip when building rendered URL | `ingest-github-repo.sh` |
| `URL_EXT_TO` | `.html` | URL extension to append | `ingest-github-repo.sh` |
| `URL_KEEP_DEPTH` | (unset) | When set to N, citation URL keeps only first N path components after `PATH_STRIP` + trailing slash. Use for "many files → one rendered page" docs like Keycloak AsciiDoc | `ingest-github-repo.sh` |
| `URL_LOWERCASE` | `0` | Set to `1` to lowercase the URL path. Use when the rendered site lowercases paths (e.g., TrueNAS Hugo) | `ingest-github-repo.sh` |
| `URL_ENCODE_SPACES` | `0` | Set to `1` to encode spaces as `%20` in URL paths. Use for sites with directory names containing spaces (e.g., OpenZFS `docs/Basic Concepts/`) | `ingest-github-repo.sh` |
| `CLONE_DIR` | `/tank/gh-cache/<repo>` | Where the repo is cloned | `ingest-github-repo.sh` |

## Source tagging convention

Use `DOC_PREFIX` to make citations distinguishable between authoritative and community sources:

| Prefix | When |
|---|---|
| `[OFFICIAL] <repo or domain>` | Vendor-published docs (Broadcom, Deciso, OPNsense, Microsoft, etc.) |
| `[OFFICIAL] <project>/changelog` | Version-specific facts (release notes, changelogs) |
| `[COMMUNITY] <domain>` | Third-party guides, blogs, wikis (homenetworkguy, Thomas-Krenn, etc.) |

Citations surface the prefix to a human reviewer when something looks off — easier to spot when a wrong answer came from a stale community blog vs the official manual.

## Behavior on errors

- Bad HTTP response: full body logged to `<STATE_DIR>/errors.log`, script keeps going
- `429`/`503`/`5xx` (parallel script): exponential backoff (30s, 60s, ..., 300s max)
- Crash / Ctrl-C: state files preserved; re-run picks up where it left off (entries in `done-*.txt` are skipped)

## How "long URL" filtering works

AnythingLLM builds the storage filename for a URL-uploaded doc as roughly:

```
url-<url-with-slash-replaced-by-_>-<uuid36>.json
```

Fixed overhead is 46 chars. Linux NAME_MAX is 255 bytes. So the source URL must be ≤ 217 chars total or the upload fails with `ENAMETOOLONG`. `ingest-urls.sh` pre-filters anything longer and logs it to errors; `recover-long-urls.sh` handles those by bypassing the URL-scrape path entirely and uploading the extracted text directly with a short controlled filename.
