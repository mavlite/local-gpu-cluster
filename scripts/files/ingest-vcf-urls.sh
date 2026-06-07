#!/usr/bin/env bash
# Bulk-ingest a list of URLs into AnythingLLM (Tier A: VCF release notes).
#
# - Reads URLs from $1 (one per line)
# - For each URL: POSTs to /api/v1/document/upload-link
# - Captures the resulting document name into a state file (resumable)
# - Sleeps CRAWL_DELAY between each (default 10s, per Broadcom robots.txt)
# - After all uploads complete, optionally adds all docs to a workspace and embeds

set -Eeuo pipefail

URL_LIST="${1:?Usage: $0 <url-list-file> [workspace-slug]}"
WORKSPACE="${2:-vcf-reference}"
CRAWL_DELAY="${CRAWL_DELAY:-10}"
STATE_DIR="/tank/vcf-docs/.ingest-state"
DONE_LIST="${STATE_DIR}/done-urls.txt"
DOCNAMES_LIST="${STATE_DIR}/document-names.txt"
ERRORS_LIST="${STATE_DIR}/errors.log"
mkdir -p "$STATE_DIR"
touch "$DONE_LIST" "$DOCNAMES_LIST" "$ERRORS_LIST"

ALLM_API_KEY="$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env | cut -d= -f2-)"
ALLM="${ALLM:-http://192.168.6.154:3001/api/v1}"

[[ -n "$ALLM_API_KEY" ]] || { echo "ALLM_API_KEY not set in config.env" >&2; exit 1; }
[[ -f "$URL_LIST" ]]    || { echo "URL list not found: $URL_LIST" >&2; exit 1; }

TOTAL=$(wc -l < "$URL_LIST")
DONE_COUNT=$(wc -l < "$DONE_LIST")
echo "==> $TOTAL URLs total, $DONE_COUNT already done, $((TOTAL - DONE_COUNT)) to go"
echo "==> Crawl delay between requests: ${CRAWL_DELAY}s"
echo "==> State dir: $STATE_DIR"
echo ""

I=0
while IFS= read -r url; do
    I=$((I + 1))
    [[ -n "$url" ]] || continue

    # Resume: skip URLs we've already done successfully
    if grep -qxF "$url" "$DONE_LIST" 2>/dev/null; then
        printf "[%4d/%d] [skip] %s\n" "$I" "$TOTAL" "$url"
        continue
    fi

    printf "[%4d/%d] uploading: %s\n" "$I" "$TOTAL" "$url"

    # POST to upload-link, capture response
    response="$(curl -sS -X POST \
        -H "Authorization: Bearer $ALLM_API_KEY" \
        -H "Content-Type: application/json" \
        --max-time 60 \
        -d "$(python3 -c "import json,sys; print(json.dumps({'link': sys.argv[1]}))" "$url")" \
        "$ALLM/document/upload-link" 2>&1 || true)"

    # Extract document name (under .documents[0].name in successful response)
    docname="$(echo "$response" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    docs = d.get('documents', [])
    if docs and 'location' in docs[0]:
        print(docs[0]['location'])
except Exception as e:
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

echo ""
echo "==> Upload phase complete."
echo "    Successful: $(wc -l < "$DONE_LIST")"
echo "    Errors:     $(wc -l < "$ERRORS_LIST")"
echo ""
echo "==> Next: trigger embed by adding all documents to workspace '$WORKSPACE'"
echo "    Use the 'embed' helper command below, or re-run with --embed."

# Optional: trigger embedding now
if [[ "${3:-}" == "--embed" ]]; then
    docnames_json="$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    names = [n.strip() for n in f if n.strip()]
print(json.dumps({'adds': names}))" "$DOCNAMES_LIST")"

    echo "==> Adding $(wc -l < "$DOCNAMES_LIST") docs to workspace '$WORKSPACE' and triggering embed..."
    curl -sS -X POST \
        -H "Authorization: Bearer $ALLM_API_KEY" \
        -H "Content-Type: application/json" \
        --max-time 600 \
        -d "$docnames_json" \
        "$ALLM/workspace/${WORKSPACE}/update-embeddings" | python3 -m json.tool | head -30
fi
