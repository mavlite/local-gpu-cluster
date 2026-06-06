#!/usr/bin/env bash
# ingest-vcf-urls-parallel.sh — parallel-worker variant.
# Usage: WORKER_ID=N STATE_DIR=/path script.sh <url-list> [workspace]
#
# Differences from the single-worker script:
#   - STATE_DIR comes from env (one per worker, no shared state)
#   - Adaptive backoff: on 429/503 or 5xx, pause this worker 30-120s
#   - No --embed pass here (run separately at the end across all workers)

set -Eeuo pipefail
URL_LIST="${1:?Usage: STATE_DIR=... WORKER_ID=... $0 <url-list> [workspace-slug]}"
WORKSPACE="${2:-vcf-reference}"
CRAWL_DELAY="${CRAWL_DELAY:-10}"
WORKER_ID="${WORKER_ID:-0}"
STATE_DIR="${STATE_DIR:?STATE_DIR must be set (one per worker)}"
DONE_LIST="${STATE_DIR}/done-urls.txt"
DOCNAMES_LIST="${STATE_DIR}/document-names.txt"
ERRORS_LIST="${STATE_DIR}/errors.log"
mkdir -p "$STATE_DIR"
touch "$DONE_LIST" "$DOCNAMES_LIST" "$ERRORS_LIST"

ALLM_API_KEY="$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env | cut -d= -f2-)"
ALLM="${ALLM:-http://192.168.6.154:3001/api/v1}"
[[ -n "$ALLM_API_KEY" ]] || { echo "[w$WORKER_ID] ALLM_API_KEY missing" >&2; exit 1; }

TOTAL=$(wc -l < "$URL_LIST")
echo "[w$WORKER_ID] starting — $TOTAL URLs in list, $(wc -l < "$DONE_LIST") already done"

CONSECUTIVE_5XX=0
I=0
while IFS= read -r url; do
    I=$((I + 1))
    [[ -n "$url" ]] || continue
    if grep -qxF "$url" "$DONE_LIST" 2>/dev/null; then
        continue
    fi

    HTTP_CODE=$(curl -sS -o /tmp/resp-w$WORKER_ID.json \
        -w "%{http_code}" \
        -X POST \
        -H "Authorization: Bearer $ALLM_API_KEY" \
        -H "Content-Type: application/json" \
        --max-time 90 \
        -d "$(python3 -c "import json,sys; print(json.dumps({'link': sys.argv[1]}))" "$url")" \
        "$ALLM/document/upload-link" || echo "000")

    response="$(cat /tmp/resp-w$WORKER_ID.json 2>/dev/null || true)"
    docname="$(echo "$response" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    docs = d.get('documents', [])
    if docs and 'location' in docs[0]:
        print(docs[0]['location'])
except: pass
" 2>/dev/null || true)"

    if [[ -n "$docname" ]]; then
        echo "$url" >> "$DONE_LIST"
        echo "$docname" >> "$DOCNAMES_LIST"
        CONSECUTIVE_5XX=0
        printf "[w%d %4d/%d] ok %s\n" "$WORKER_ID" "$I" "$TOTAL" "$(basename "$url")"
    else
        echo "$(date -Iseconds) $url HTTP=$HTTP_CODE $response" >> "$ERRORS_LIST"
        printf "[w%d %4d/%d] ERROR (HTTP %s) %s\n" "$WORKER_ID" "$I" "$TOTAL" "$HTTP_CODE" "$(basename "$url")"

        # Adaptive backoff on rate-limit / upstream-error
        if [[ "$HTTP_CODE" == "429" ]] || [[ "$HTTP_CODE" == "503" ]] || [[ "$HTTP_CODE" =~ ^5 ]]; then
            CONSECUTIVE_5XX=$((CONSECUTIVE_5XX + 1))
            BACKOFF=$(( CONSECUTIVE_5XX * 30 ))
            [[ $BACKOFF -gt 300 ]] && BACKOFF=300
            echo "[w$WORKER_ID] backoff ${BACKOFF}s (consecutive=${CONSECUTIVE_5XX})" >&2
            sleep "$BACKOFF"
        fi
    fi

    sleep "$CRAWL_DELAY"
done < "$URL_LIST"

echo "[w$WORKER_ID] done: $(wc -l < "$DONE_LIST") successful, $(wc -l < "$ERRORS_LIST") errored"
