#!/usr/bin/env bash
# recover-long-urls.sh — fallback ingest path for URLs whose AnythingLLM
# filename would exceed Linux NAME_MAX (255 bytes).
#
# AnythingLLM's /document/upload-link endpoint constructs the storage filename
# by URL-encoding the source URL into the basename. For deeply-nested doc URLs
# (e.g., VCF runbook procedures, 400+ char URLs), the result exceeds the 255
# byte filesystem limit and the upload fails with ENAMETOOLONG.
#
# This script bypasses /document/upload-link entirely:
#   1. Fetch each URL ourselves via trafilatura (cleans boilerplate)
#   2. POST the extracted text to /api/v1/document/raw-text with a SHORT
#      hash-derived filename (vcf-<slug>-<sha1prefix>.json)
#   3. Optionally add to a workspace + trigger embedding with --embed
#
# Requirements: a trafilatura venv at /opt/vcf-scraper-venv. Install with:
#   apt install -y python3-venv
#   python3 -m venv /opt/vcf-scraper-venv
#   /opt/vcf-scraper-venv/bin/pip install trafilatura requests
#
# Usage:
#   scripts/tools/recover-long-urls.sh <url-list> <workspace-slug> [--embed]

set -Eeuo pipefail

URL_LIST="${1:?Usage: $0 <url-list-file> <workspace-slug> [--embed]}"
WORKSPACE="${2:?Workspace slug is required}"
EMBED_FLAG="${3:-}"
CRAWL_DELAY="${CRAWL_DELAY:-10}"

STATE_DIR="${STATE_DIR:-$(dirname "$URL_LIST")/.recover-state}"
DONE_LIST="${STATE_DIR}/done-urls.txt"
DOCNAMES_LIST="${STATE_DIR}/document-names.txt"
ERRORS_LIST="${STATE_DIR}/errors.log"
mkdir -p "$STATE_DIR"
touch "$DONE_LIST" "$DOCNAMES_LIST" "$ERRORS_LIST"

if [[ -z "${ALLM_API_KEY:-}" ]]; then
  if [[ -f /root/local-gpu-cluster/scripts/config.env ]]; then
    ALLM_API_KEY="$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env 2>/dev/null | cut -d= -f2-)"
  fi
fi
[[ -n "${ALLM_API_KEY:-}" ]] || { echo "ALLM_API_KEY not set" >&2; exit 1; }

ALLM="${ALLM:-http://192.168.6.154:3001/api/v1}"
VENV_PY="${VENV_PY:-/opt/vcf-scraper-venv/bin/python}"
[[ -x "$VENV_PY" ]] || { echo "trafilatura venv missing: $VENV_PY (see header for install)" >&2; exit 1; }

TOTAL=$(wc -l < "$URL_LIST")
echo "==> $TOTAL URLs to recover  |  $(wc -l < "$DONE_LIST") already done  |  state: $STATE_DIR"

I=0
while IFS= read -r url; do
  I=$((I + 1))
  [[ -n "$url" ]] || continue
  if grep -qxF "$url" "$DONE_LIST" 2>/dev/null; then
    printf "[%4d/%d] [skip] %s\n" "$I" "$TOTAL" "$url"
    continue
  fi

  printf "[%4d/%d] fetching+extracting: %s\n" "$I" "$TOTAL" "$url"

  result=$("$VENV_PY" - "$url" "$ALLM" "$ALLM_API_KEY" <<'PY'
import sys, json, hashlib, urllib.request
import trafilatura

url, allm, key = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    fetched = trafilatura.fetch_url(url)
    if not fetched:
        print(json.dumps({"err": "fetch returned empty"})); sys.exit(0)
    text = trafilatura.extract(fetched, include_tables=True, include_links=False, favor_recall=True)
    if not text or len(text) < 200:
        print(json.dumps({"err": f"extracted text too short: {len(text or '')} chars"})); sys.exit(0)

    slug = url.split('/')[-1].replace('.html', '')[:80]
    url_hash = hashlib.sha1(url.encode()).hexdigest()[:8]
    title = f"recovered-{slug}-{url_hash}"

    payload = {
        "textContent": text,
        "metadata": {
            "title": title,
            "docSource": "URL via raw-text recovery (long-URL fallback)",
            "chunkSource": f"link://{url}",
            "published": None,
            "wordCount": len(text.split()),
            "url": url,
        }
    }
    req = urllib.request.Request(
        f"{allm}/document/raw-text",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=60)
    body = json.loads(resp.read())
    docs = body.get("documents", [])
    if docs:
        print(json.dumps({"ok": True, "location": docs[0].get("location"), "chars": len(text)}))
    else:
        print(json.dumps({"err": "no document returned", "body": str(body)[:200]}))
except Exception as e:
    print(json.dumps({"err": f"{type(e).__name__}: {e}"}))
PY
)

  docname=$(echo "$result" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('location','') if d.get('ok') else '')")

  if [[ -n "$docname" ]]; then
    echo "$url" >> "$DONE_LIST"
    echo "$docname" >> "$DOCNAMES_LIST"
    chars=$(echo "$result" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('chars','?'))")
    printf "         ok -> %s (%s chars)\n" "$docname" "$chars"
  else
    echo "$(date -Iseconds) $url $result" >> "$ERRORS_LIST"
    err=$(echo "$result" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('err','?'))" 2>/dev/null || echo unknown)
    printf "         ERROR: %s\n" "$err"
  fi

  sleep "$CRAWL_DELAY"
done < "$URL_LIST"

echo
echo "==> Recovery upload phase complete."
echo "    Successful: $(wc -l < "$DONE_LIST")"
echo "    Errors:     $(wc -l < "$ERRORS_LIST")"

if [[ "$EMBED_FLAG" == "--embed" ]] && [[ -s "$DOCNAMES_LIST" ]]; then
  echo
  echo "==> Adding $(wc -l < "$DOCNAMES_LIST") recovered docs to workspace '$WORKSPACE' + embed..."
  payload="$(python3 -c "
import json
with open('$DOCNAMES_LIST') as f:
    names = [n.strip() for n in f if n.strip()]
print(json.dumps({'adds': names}))")"
  curl -sS -X POST \
      -H "Authorization: Bearer $ALLM_API_KEY" \
      -H "Content-Type: application/json" \
      --max-time 1800 -d "$payload" \
      "$ALLM/workspace/${WORKSPACE}/update-embeddings" \
    | python3 -c "
import json, sys
r = json.load(sys.stdin)
n = len(r.get('workspace', {}).get('documents', []))
print(f'Workspace doc count after recovery: {n}')"
fi
