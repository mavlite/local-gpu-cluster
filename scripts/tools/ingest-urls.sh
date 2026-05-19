#!/usr/bin/env bash
# ingest-urls.sh — bulk-ingest a list of URLs into an AnythingLLM workspace.
#
# For each URL in the input list:
#   1. POST to /api/v1/document/upload-link (AnythingLLM scrapes the page,
#      extracts text via its built-in Cheerio/Readability pipeline, stores
#      under custom-documents/url-<urlpath>-<uuid>.json).
#   2. Capture the resulting docpath into a state file (resumable).
#   3. Sleep CRAWL_DELAY between each (default 10s — respect robots.txt).
#
# After the upload phase finishes, optionally call --embed to attach all
# uploaded docs to the workspace and trigger embedding via
# /api/v1/workspace/<slug>/update-embeddings.
#
# Pre-filters URLs whose AnythingLLM-derived filename would exceed
# Linux NAME_MAX (255 bytes): URL length must be <= 217 chars. Longer URLs
# are logged to errors and skipped; use recover-long-urls.sh for those.
#
# Usage:
#   ALLM=http://192.168.6.154:3001/api/v1 \
#     ./ingest-urls.sh <url-list> <workspace-slug> [--embed]
#
# Environment variables:
#   ALLM_API_KEY        AnythingLLM API key (default: pulled from config.env)
#   ALLM                AnythingLLM API base URL
#   CRAWL_DELAY         Seconds between requests (default 10, per robots.txt)
#   STATE_DIR           Where to keep done/error state (default beside url list)

set -Eeuo pipefail

URL_LIST="${1:?Usage: $0 <url-list-file> <workspace-slug> [--embed]}"
WORKSPACE="${2:?Workspace slug is required (e.g., vcf-reference)}"
EMBED_FLAG="${3:-}"
CRAWL_DELAY="${CRAWL_DELAY:-10}"

STATE_DIR="${STATE_DIR:-$(dirname "$URL_LIST")/.ingest-state}"
DONE_LIST="${STATE_DIR}/done-urls.txt"
DOCNAMES_LIST="${STATE_DIR}/document-names.txt"
ERRORS_LIST="${STATE_DIR}/errors.log"
mkdir -p "$STATE_DIR"
touch "$DONE_LIST" "$DOCNAMES_LIST" "$ERRORS_LIST"

# Resolve API key: env override > config.env > error
if [[ -z "${ALLM_API_KEY:-}" ]]; then
  if [[ -f /root/local-gpu-cluster/scripts/config.env ]]; then
    ALLM_API_KEY="$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env 2>/dev/null | cut -d= -f2-)"
  fi
fi
[[ -n "${ALLM_API_KEY:-}" ]] || { echo "ALLM_API_KEY not set (in env or config.env)" >&2; exit 1; }

ALLM="${ALLM:-http://192.168.6.154:3001/api/v1}"
[[ -f "$URL_LIST" ]] || { echo "URL list not found: $URL_LIST" >&2; exit 1; }

TOTAL=$(wc -l < "$URL_LIST")
DONE_COUNT=$(wc -l < "$DONE_LIST")
echo "==> $TOTAL URLs total, $DONE_COUNT already done, $((TOTAL - DONE_COUNT)) to go"
echo "==> Workspace: $WORKSPACE  |  Crawl delay: ${CRAWL_DELAY}s  |  State: $STATE_DIR"
echo

I=0
while IFS= read -r url; do
  I=$((I + 1))
  [[ -n "$url" ]] || continue

  # AnythingLLM builds storage filenames as "url-{url-without-https}-{uuid36}.json"
  # which fails with ENAMETOOLONG when filename > 255 bytes (NAME_MAX).
  # Pre-filter to skip the doomed-from-the-start URLs.
  if (( ${#url} > 217 )); then
    echo "$(date -Iseconds) $url {\"skipped\":\"url too long (${#url} chars > 217 cap)\"}" >> "$ERRORS_LIST"
    printf "[%4d/%d] [skip-too-long] %s\n" "$I" "$TOTAL" "$url"
    continue
  fi

  if grep -qxF "$url" "$DONE_LIST" 2>/dev/null; then
    printf "[%4d/%d] [skip] %s\n" "$I" "$TOTAL" "$url"
    continue
  fi

  printf "[%4d/%d] uploading: %s\n" "$I" "$TOTAL" "$url"

  response="$(curl -sS -X POST \
      -H "Authorization: Bearer $ALLM_API_KEY" \
      -H "Content-Type: application/json" \
      --max-time 60 \
      -d "$(python3 -c "import json,sys; print(json.dumps({'link': sys.argv[1]}))" "$url")" \
      "$ALLM/document/upload-link" 2>&1 || true)"

  docname="$(echo "$response" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    docs = d.get('documents', [])
    if docs and 'location' in docs[0]:
        print(docs[0]['location'])
except Exception:
    pass
" 2>/dev/null || true)"

  if [[ -n "$docname" ]]; then
    echo "$url" >> "$DONE_LIST"
    echo "$docname" >> "$DOCNAMES_LIST"
    printf "         ok -> %s\n" "$docname"
  else
    echo "$(date -Iseconds) $url $response" >> "$ERRORS_LIST"
    printf "         ERROR (logged to %s)\n" "$ERRORS_LIST"
  fi

  sleep "$CRAWL_DELAY"
done < "$URL_LIST"

echo
echo "==> Upload phase complete."
echo "    Successful: $(wc -l < "$DONE_LIST")"
echo "    Errors:     $(wc -l < "$ERRORS_LIST")"

if [[ "$EMBED_FLAG" == "--embed" ]] && [[ -s "$DOCNAMES_LIST" ]]; then
  echo
  echo "==> Adding $(wc -l < "$DOCNAMES_LIST") docs to workspace '$WORKSPACE' and triggering embed..."
  payload="$(python3 -c "
import json
with open('$DOCNAMES_LIST') as f:
    names = [n.strip() for n in f if n.strip()]
print(json.dumps({'adds': names}))")"
  curl -sS -X POST \
      -H "Authorization: Bearer $ALLM_API_KEY" \
      -H "Content-Type: application/json" \
      --max-time 1800 \
      -d "$payload" \
      "$ALLM/workspace/${WORKSPACE}/update-embeddings" \
    | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
    n = len(r.get('workspace', {}).get('documents', []))
    print(f'Workspace doc count after embed: {n}')
except Exception as e:
    print(f'Embed response not JSON: {e}')"
fi
