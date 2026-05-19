# Document ingestion tools

Helper scripts for bulk-ingesting URLs into AnythingLLM workspaces. Used during VCF and OPNsense corpus loads.

| Script | Purpose |
|---|---|
| `ingest-urls.sh` | Sequential, single-worker. POSTs URLs to `/document/upload-link`, respects a configurable crawl delay (default 10s), resumable via state files. Pre-filters URLs > 217 chars (NAME_MAX cap). Optional `--embed` triggers workspace embedding at the end. |
| `ingest-urls-parallel.sh` | Per-worker variant with adaptive 30→300s backoff on 429/503. Run N workers in parallel against pre-split chunks of a URL list, then run one aggregated embed pass. |
| `recover-long-urls.sh` | Fallback for URLs > 217 chars (where AnythingLLM's filename scheme would hit ENAMETOOLONG). Fetches the page directly with `trafilatura`, extracts clean text, POSTs to `/document/raw-text` with a short hash-based filename. Requires a venv at `/opt/vcf-scraper-venv` with `trafilatura` installed. |

## Quick start

```bash
# Run once on the host (needed for the recover-long-urls.sh path)
apt install -y python3-venv
python3 -m venv /opt/vcf-scraper-venv
/opt/vcf-scraper-venv/bin/pip install trafilatura requests

# Sequential ingest (small corpus, ~10s per URL)
scripts/tools/ingest-urls.sh /tank/docs/urls.txt vcf-reference --embed

# Parallel ingest (large corpus). First, split the URL list into 4 chunks:
for w in 1 2 3 4; do
  awk -v w=$w 'NR%4==(w-1)' /tank/docs/urls.txt > /tank/docs/urls-w${w}.txt
done
# Then launch 4 workers:
for w in 1 2 3 4; do
  WORKER_ID=$w STATE_DIR=/tank/docs/.state-w${w} CRAWL_DELAY=10 \
  nohup scripts/tools/ingest-urls-parallel.sh \
    /tank/docs/urls-w${w}.txt vcf-reference \
    > /tank/docs/ingest-w${w}.log 2>&1 &
done

# After all workers finish, aggregate docnames and trigger one embed pass:
cat /tank/docs/.state-w*/document-names.txt | sort -u > /tmp/all-docs.txt
ALLM_API_KEY=$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env | cut -d= -f2-)
payload=$(python3 -c "
import json
with open('/tmp/all-docs.txt') as f:
    print(json.dumps({'adds': [n.strip() for n in f if n.strip()]}))")
curl -sS -X POST -H "Authorization: Bearer $ALLM_API_KEY" -H "Content-Type: application/json" \
     --max-time 1800 -d "$payload" \
     http://192.168.6.154:3001/api/v1/workspace/vcf-reference/update-embeddings

# Recovery pass for URLs > 217 chars (those skipped by ingest-urls.sh)
scripts/tools/recover-long-urls.sh /tank/docs/long-urls.txt vcf-reference --embed
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `ALLM_API_KEY` | from `scripts/config.env` | AnythingLLM API key (Settings → API Keys in the UI) |
| `ALLM` | `http://192.168.6.154:3001/api/v1` | AnythingLLM API base URL |
| `CRAWL_DELAY` | `10` | Seconds between requests (respect upstream `robots.txt`) |
| `STATE_DIR` | adjacent to URL list | Where done/error state goes (resumable) |
| `WORKER_ID` | `0` | Numeric tag for parallel workers (appears in logs) |
| `VENV_PY` | `/opt/vcf-scraper-venv/bin/python` | Python for `trafilatura` in `recover-long-urls.sh` |

## Behavior on errors

- Bad HTTP response: full body logged to `<STATE_DIR>/errors.log`, script keeps going
- `429`/`503`/`5xx` (parallel script): exponential backoff (30s, 60s, ..., 300s max)
- Crash / Ctrl-C: state files preserved; re-run picks up where it left off (URLs in `done-urls.txt` are skipped)

## How "long URL" filtering works

AnythingLLM builds the storage filename for a URL-uploaded doc as roughly:

```
url-<url-with-slash-replaced-by-_>-<uuid36>.json
```

Fixed overhead is 46 chars. Linux NAME_MAX is 255 bytes. So the source URL must be ≤ 217 chars total or the upload fails with `ENAMETOOLONG`. `ingest-urls.sh` pre-filters anything longer and logs it to errors; `recover-long-urls.sh` handles those by bypassing the URL-scrape path entirely and uploading the extracted text directly with a short controlled filename.
