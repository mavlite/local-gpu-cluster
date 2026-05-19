#!/usr/bin/env bash
# clear-workspace.sh — remove all documents from an AnythingLLM workspace's
# embedding index. Use before a clean re-ingest when the existing corpus is
# contaminated (e.g., per-page boilerplate, build hashes) or replaced by a
# better source.
#
# What this does:
#   1. GET /workspace/<slug> → list documents currently in the workspace
#   2. POST /workspace/<slug>/update-embeddings with {"deletes": [...]}
#      → AnythingLLM removes the embedding rows from lancedb
#
# What this does NOT do:
#   - The underlying JSON files in storage/documents/custom-documents/ stay.
#     They're orphaned (no workspace references them), so retrieval ignores
#     them, but they consume disk. To also free disk, run delete-orphan-docs.sh
#     (or use the AnythingLLM UI's Documents → Trash).
#
# Usage:
#   clear-workspace.sh <workspace-slug>
#   clear-workspace.sh sdg-documentation

set -Eeuo pipefail

WORKSPACE="${1:?Usage: $0 <workspace-slug>}"

if [[ -z "${ALLM_API_KEY:-}" ]]; then
  if [[ -f /root/local-gpu-cluster/scripts/config.env ]]; then
    ALLM_API_KEY="$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env 2>/dev/null | cut -d= -f2-)"
  fi
fi
[[ -n "${ALLM_API_KEY:-}" ]] || { echo "ALLM_API_KEY not set" >&2; exit 1; }
ALLM="${ALLM:-http://192.168.6.154:3001/api/v1}"

echo "==> Fetching current document list for workspace '$WORKSPACE'..."
ws_json="$(curl -sS -m 60 \
  -H "Authorization: Bearer $ALLM_API_KEY" \
  "$ALLM/workspace/$WORKSPACE")"

count="$(echo "$ws_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ws = d.get('workspace', [])
if isinstance(ws, list): ws = ws[0] if ws else {}
docs = ws.get('documents', [])
print(len(docs))
")"

if [[ "$count" == "0" ]]; then
  echo "    Workspace is already empty. Nothing to do."
  exit 0
fi

echo "    Found $count documents in workspace."
echo "    Sample doc names (first 5):"
echo "$ws_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ws = d.get('workspace', [])
if isinstance(ws, list): ws = ws[0] if ws else {}
docs = ws.get('documents', [])
for doc in docs[:5]:
    print('     -', doc.get('docpath') or doc.get('filename') or doc.get('id'))
" || true

echo
read -r -p "Proceed with deleting embeddings for all $count docs? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy] ]]; then
  echo "Aborted."
  exit 1
fi

# Build the deletes payload — docs[i].docpath is what update-embeddings expects.
# Stash the workspace JSON to a temp file so the Python call can read it as
# argv (combining stdin-pipe with a heredoc-script doesn't work: heredoc
# overrides stdin and json.load reads nothing).
deletes_tmp="$(mktemp -t clear-workspace-deletes.XXXXXX.json)"
ws_tmp="$(mktemp -t clear-workspace-ws.XXXXXX.json)"
printf '%s' "$ws_json" > "$ws_tmp"
python3 - "$ws_tmp" "$deletes_tmp" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
ws = d.get('workspace', [])
if isinstance(ws, list): ws = ws[0] if ws else {}
docs = ws.get('documents', [])
deletes = []
for doc in docs:
    name = doc.get('docpath') or doc.get('filename')
    if name:
        deletes.append(name)
with open(sys.argv[2], 'w') as f:
    json.dump({"deletes": deletes}, f)
print(f'Queued {len(deletes)} deletes')
PY
rm -f "$ws_tmp"

echo "==> Posting deletes to /workspace/$WORKSPACE/update-embeddings..."
resp="$(curl -sS -X POST \
  -H "Authorization: Bearer $ALLM_API_KEY" \
  -H "Content-Type: application/json" \
  --max-time 600 \
  --data-binary "@$deletes_tmp" \
  "$ALLM/workspace/$WORKSPACE/update-embeddings")"
rm -f "$deletes_tmp"

echo "$resp" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
    ws = r.get('workspace', {})
    if isinstance(ws, list): ws = ws[0] if ws else {}
    remaining = len(ws.get('documents', []))
    print(f'Workspace doc count after delete: {remaining}')
except Exception as e:
    print(f'Response not JSON: {e}')
    print(sys.stdin.read()[:300] if False else '')"
