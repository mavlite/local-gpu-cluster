#!/usr/bin/env bash
# ingest-github-repo.sh — clone a GitHub doc repo and ingest its text files
# into an AnythingLLM workspace via /document/raw-text.
#
# Why this instead of the URL-scrape path (`ingest-urls.sh`)?
#   - Source files are clean text (.rst / .md) — no nav chrome, no footer
#     boilerplate, no per-page build hash leaking into chunks.
#   - We can compute the rendered docs URL deterministically for citations
#     (e.g. source/manual/firstrun.rst → https://docs.opnsense.org/manual/firstrun.html).
#   - We get a real publication date from `git log` for free — surfaces in
#     metadata so future retrieval / reranking can prefer fresher content.
#
# Usage:
#   ingest-github-repo.sh <repo-url> <workspace-slug> <doc-prefix> \
#                         <url-base> <path-strip> [--embed]
#
# Positional:
#   repo-url      e.g.  https://github.com/opnsense/docs
#   workspace     e.g.  sdg-documentation
#   doc-prefix    metadata.docSource tag, e.g. "[OFFICIAL] opnsense/docs"
#   url-base      citation URL stem, e.g. https://docs.opnsense.org
#   path-strip    file-path prefix that's NOT in the URL (e.g. "source/")
#
# Optional env:
#   FILE_GLOB         find -name pattern, default *.rst (use *.adoc for AsciiDoc)
#   FILE_EXCLUDE      grep -E regex of relative paths to skip
#   URL_EXT_FROM      file extension to strip when building URL, default .rst
#   URL_EXT_TO        URL extension to append, default .html
#   URL_KEEP_DEPTH    Citation URL mode (default: 1:1 file-to-URL). When set
#                     to N, keeps only the first N path components after
#                     PATH_STRIP and appends a trailing slash. Use when many
#                     source files compile into ONE rendered page per guide
#                     (e.g., Keycloak AsciiDoc → single HTML per guide).
#                     Example: docs/documentation/server_admin/topics/realms.adoc
#                     with PATH_STRIP=docs/documentation/ and URL_KEEP_DEPTH=1
#                     → URL_BASE/server_admin/
#   CLONE_DIR         where to clone (default /tank/gh-cache/<repo-name>)
#   STATE_DIR         resumable state files (default $CLONE_DIR/.ingest-state)
#   ALLM_API_KEY      from config.env if unset
#   ALLM              base API URL, default http://192.168.6.154:3001/api/v1

set -Eeuo pipefail

REPO_URL="${1:?Usage: $0 <repo-url> <workspace> <doc-prefix> <url-base> <path-strip> [--embed]}"
WORKSPACE="${2:?workspace slug required}"
DOC_PREFIX="${3:?doc-prefix required (e.g. \"[OFFICIAL] opnsense/docs\")}"
URL_BASE="${4:?url-base required}"
PATH_STRIP="${5:?path-strip required (e.g. \"source/\")}"
EMBED_FLAG="${6:-}"

FILE_GLOB="${FILE_GLOB:-*.rst}"
FILE_EXCLUDE="${FILE_EXCLUDE:-^_themes/}"
URL_EXT_FROM="${URL_EXT_FROM:-.rst}"
URL_EXT_TO="${URL_EXT_TO:-.html}"
URL_BASE="${URL_BASE%/}"

repo_name="$(basename "${REPO_URL%.git}")"
CLONE_DIR="${CLONE_DIR:-/tank/gh-cache/$repo_name}"
STATE_DIR="${STATE_DIR:-$CLONE_DIR/.ingest-state}"
DONE_LIST="$STATE_DIR/done-files.txt"
DOCNAMES_LIST="$STATE_DIR/document-names.txt"
ERRORS_LIST="$STATE_DIR/errors.log"

if [[ -z "${ALLM_API_KEY:-}" ]]; then
  if [[ -f /root/local-gpu-cluster/scripts/config.env ]]; then
    ALLM_API_KEY="$(grep '^ALLM_API_KEY=' /root/local-gpu-cluster/scripts/config.env 2>/dev/null | cut -d= -f2-)"
  fi
fi
[[ -n "${ALLM_API_KEY:-}" ]] || { echo "ALLM_API_KEY not set" >&2; exit 1; }
ALLM="${ALLM:-http://192.168.6.154:3001/api/v1}"

# Clone or update the repo FIRST — before any state-dir mkdir, otherwise we'd
# self-trap on the "does $CLONE_DIR exist?" check below (mkdir -p $STATE_DIR
# would create $CLONE_DIR as a side effect since STATE_DIR is inside it).
#
# Use --filter=blob:none (treeless) for fast clone that still retains commit
# history (needed for per-file last-modified date via `git log`). Three states:
#   - dir has .git        → pull
#   - dir exists, no .git → bail (don't blindly delete user data)
#   - dir absent or empty → clone
if [[ -d "$CLONE_DIR/.git" ]]; then
  echo "==> $CLONE_DIR exists; pulling latest"
  git -C "$CLONE_DIR" pull --ff-only
elif [[ -d "$CLONE_DIR" ]] && [[ -n "$(ls -A "$CLONE_DIR" 2>/dev/null)" ]]; then
  echo "ERROR: $CLONE_DIR exists, is non-empty, and has no .git/." >&2
  echo "       Either remove it (rm -rf $CLONE_DIR) or set CLONE_DIR=<other path>." >&2
  exit 1
else
  echo "==> Cloning $REPO_URL → $CLONE_DIR"
  mkdir -p "$(dirname "$CLONE_DIR")"
  rmdir "$CLONE_DIR" 2>/dev/null || true   # remove empty placeholder if any
  git clone --filter=blob:none "$REPO_URL" "$CLONE_DIR"
fi

# Now safe to create state dir inside the populated clone
mkdir -p "$STATE_DIR"
touch "$DONE_LIST" "$DOCNAMES_LIST" "$ERRORS_LIST"

# Enumerate target files. Use printf with -print0 so paths with spaces survive.
mapfile -d '' files < <(
  cd "$CLONE_DIR"
  find . -type f -name "$FILE_GLOB" -not -path './.git/*' -print0
)
TOTAL=${#files[@]}
echo "==> $TOTAL candidate files (glob=$FILE_GLOB) in $CLONE_DIR"
echo "==> $(wc -l < "$DONE_LIST") already done; workspace=$WORKSPACE"

I=0
for rel in "${files[@]}"; do
  I=$((I + 1))
  rel="${rel#./}"

  # Optional exclude pattern
  if [[ -n "$FILE_EXCLUDE" ]] && echo "$rel" | grep -qE "$FILE_EXCLUDE"; then
    continue
  fi
  if grep -qxF "$rel" "$DONE_LIST" 2>/dev/null; then
    continue
  fi

  filepath="$CLONE_DIR/$rel"

  # Compute the rendered URL the citation should link to.
  # Two modes:
  #   (a) 1:1 file-to-URL mapping (default — OPNsense .rst → .html style):
  #       just strip PATH_STRIP and swap URL_EXT_FROM → URL_EXT_TO.
  #   (b) keep-N-components mapping (URL_KEEP_DEPTH set — Keycloak AsciiDoc
  #       style where many .adoc files compile into ONE single-page guide):
  #       strip PATH_STRIP, then keep only the first N path components and
  #       append a trailing slash. The extension transform is skipped because
  #       the URL points to a guide directory, not a file.
  url_rel="$rel"
  if [[ -n "$PATH_STRIP" ]]; then
    url_rel="${url_rel#$PATH_STRIP}"
  fi
  if [[ -n "${URL_KEEP_DEPTH:-}" ]]; then
    url_rel="$(echo "$url_rel" | awk -F/ -v n="$URL_KEEP_DEPTH" '
      {
        max = (NF < n) ? NF : n
        result = ""
        for (i = 1; i <= max; i++) result = result (i > 1 ? "/" : "") $i
        print result
      }
    ')"
    url_rel="${url_rel}/"
  else
    if [[ -n "$URL_EXT_FROM" ]] && [[ "$url_rel" == *"$URL_EXT_FROM" ]]; then
      url_rel="${url_rel%$URL_EXT_FROM}${URL_EXT_TO}"
    fi
  fi
  rendered_url="$URL_BASE/$url_rel"

  # Last-modified date from git (ISO 8601). Empty string if file is untracked.
  last_mod="$(git -C "$CLONE_DIR" log -1 --format='%aI' -- "$rel" 2>/dev/null || true)"

  # Title is human-readable and unique-ish: path with separators normalized
  title="$(echo "$url_rel" | sed 's|/|-|g; s|\.html$||; s|\.md$||; s|\.rst$||')"
  title="${title:0:120}"  # keep filenames sane

  printf "[%4d/%d] %s → %s (mtime=%s)\n" "$I" "$TOTAL" "$rel" "$rendered_url" "${last_mod:-?}"

  # Build the payload entirely in python — handles JSON escaping of file
  # contents that may contain backticks, control chars, or backslashes.
  # Write to a temp file because some .rst files are large and curl/--data-binary
  # @file is the cleanest path that avoids ARG_MAX/heredoc weirdness.
  #
  # We prepend "Last modified: <date>" to textContent because AnythingLLM
  # overrides metadata.published with its own ingestion timestamp. Embedding
  # the date IN the chunk text means the embedder + LLM can actually see it
  # during retrieval (so version-drift hints survive into the model's view).
  payload_tmp="$(mktemp -t ghingest-payload.XXXXXX.json)"
  python3 - "$filepath" "$title" "$DOC_PREFIX" "$rendered_url" "${last_mod:-}" "$payload_tmp" <<'PY'
import json, sys
filepath, title, doc_prefix, url, published, out_path = sys.argv[1:7]
with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
    text = f.read()
# Prepend date + source markers so retrieved chunks always carry provenance.
header_lines = [f"Source: {doc_prefix}", f"URL: {url}"]
if published:
    header_lines.append(f"Last modified: {published}")
header = "\n".join(header_lines) + "\n\n"
text_with_header = header + text
payload = {
    "textContent": text_with_header,
    "metadata": {
        "title": title,
        "docSource": doc_prefix,
        "chunkSource": f"link://{url}",
        "published": published or None,
        "wordCount": len(text_with_header.split()),
        "url": url,
    },
}
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(payload, f)
PY

  response="$(curl -sS -m 90 -X POST \
      -H "Authorization: Bearer $ALLM_API_KEY" \
      -H "Content-Type: application/json" \
      --data-binary "@$payload_tmp" \
      "$ALLM/document/raw-text" 2>&1 || true)"
  rm -f "$payload_tmp"

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
    echo "$rel" >> "$DONE_LIST"
    echo "$docname" >> "$DOCNAMES_LIST"
    printf "         ok -> %s\n" "$docname"
  else
    echo "$(date -Iseconds) $rel $response" >> "$ERRORS_LIST"
    printf "         ERROR (logged)\n"
  fi
done

echo
echo "==> Upload phase complete."
echo "    Successful: $(wc -l < "$DONE_LIST")"
echo "    Errors:     $(wc -l < "$ERRORS_LIST")"

if [[ "$EMBED_FLAG" == "--embed" ]] && [[ -s "$DOCNAMES_LIST" ]]; then
  echo
  echo "==> Adding $(wc -l < "$DOCNAMES_LIST") docs to workspace '$WORKSPACE' + triggering embed..."
  embed_tmp="$(mktemp -t ghingest-embed.XXXXXX.json)"
  python3 - "$DOCNAMES_LIST" "$embed_tmp" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
names = []
with open(src) as f:
    for line in f:
        line = line.strip()
        if line:
            names.append(line)
with open(dst, 'w') as f:
    json.dump({"adds": names}, f)
PY
  curl -sS -X POST \
      -H "Authorization: Bearer $ALLM_API_KEY" \
      -H "Content-Type: application/json" \
      --max-time 1800 \
      --data-binary "@$embed_tmp" \
      "$ALLM/workspace/${WORKSPACE}/update-embeddings" \
    | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
    n = len(r.get('workspace', {}).get('documents', []))
    print(f'Workspace doc count after embed: {n}')
except Exception as e:
    print(f'Embed response not JSON: {e}')"
  rm -f "$embed_tmp"
fi
