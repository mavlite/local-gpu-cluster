#!/usr/bin/env bash
# build-truenas-api-urls.sh — Build URL list for api.truenas.com.
#
# Generates a one-URL-per-line file pointing at every API method, object,
# event, and cookbook page in the Sphinx-rendered TrueNAS middleware API
# reference. Output is intended to be passed to
# scripts/tools/recover-long-urls.sh for ingestion into AnythingLLM.
#
# Strategy:
#   1. Try Sphinx sitemap.xml (default for sphinx-sitemap; usually present).
#   2. Fallback: scrape the per-section index pages
#      (api_methods.html, api_objects.html, api_events.html) for hrefs,
#      because some Sphinx builds disable the sitemap extension.
#
# Always also fetches the main landing page so cookbook / how-to URLs
# linked from index.html are picked up regardless of which path succeeded.
#
# Usage:
#   scripts/tools/build-truenas-api-urls.sh [OUTPUT_FILE]
#
# Environment overrides:
#   API_VERSION       Doc version path component (default: v27.0)
#   INCLUDE_EVENTS    1 to include api_events_* pages (default: 1)
#   INCLUDE_COOKBOOK  1 to include cookbook + landing pages (default: 1)
#   CURL_TIMEOUT      Seconds per HTTP request (default: 30)
#
# Exit codes:
#   0  Wrote OUT_FILE with at least one URL
#   1  Both sitemap and fallback produced zero URLs (treat as outage)
#   2  Argument / environment error

set -Eeuo pipefail

API_VERSION="${API_VERSION:-v27.0}"
BASE_URL="https://api.truenas.com/${API_VERSION}"
OUT_FILE="${1:-/tank/docs/truenas-api-urls.txt}"
INCLUDE_EVENTS="${INCLUDE_EVENTS:-1}"
INCLUDE_COOKBOOK="${INCLUDE_COOKBOOK:-1}"
CURL_TIMEOUT="${CURL_TIMEOUT:-30}"

# --- helpers ---------------------------------------------------------------
die() { echo "ERROR: $*" >&2; exit 2; }
log() { echo "$*"; }

# Build the regex that filters sitemap entries to the page types we want.
# Always: methods + objects. Optional: events, cookbook.
build_filter() {
  local f='/(api_methods_|api_objects_)[^/]+\.html$'
  [[ "$INCLUDE_EVENTS" == "1" ]] && f+='|/api_events_[^/]+\.html$'
  echo "$f"
}

# Resolve an href (which may be relative or absolute) against BASE_URL.
resolve_url() {
  local href="$1"
  case "$href" in
    http://*|https://*) echo "$href" ;;
    /*) echo "https://api.truenas.com${href}" ;;
    *) echo "${BASE_URL}/${href}" ;;
  esac
}

# --- preconditions ---------------------------------------------------------
command -v curl >/dev/null || die "curl is required"
mkdir -p "$(dirname "$OUT_FILE")" || die "Cannot create output directory"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

log "Fetching api.truenas.com (${API_VERSION}) URL list..."
log "  base: $BASE_URL"
log "  out:  $OUT_FILE"
log "  include events:   $INCLUDE_EVENTS"
log "  include cookbook: $INCLUDE_COOKBOOK"
log

: > "$OUT_FILE"

# --- attempt 1: sitemap.xml ------------------------------------------------
sitemap_used=0
if curl -sfL -m "$CURL_TIMEOUT" "${BASE_URL}/sitemap.xml" -o "$TMP" \
   && grep -q '<loc>' "$TMP"; then
  log "  + sitemap.xml present"
  filter="$(build_filter)"
  grep -oE '<loc>[^<]+</loc>' "$TMP" \
    | sed -E 's,</?loc>,,g' \
    | grep -E "$filter" \
    | sort -u \
    >> "$OUT_FILE" || true
  sitemap_used=1
else
  log "  - sitemap.xml unavailable (status: $(curl -sI -m "$CURL_TIMEOUT" "${BASE_URL}/sitemap.xml" | head -1 || echo '<no response>'))"
fi

# --- attempt 2 (fallback): scrape section index pages ----------------------
if [[ "$sitemap_used" == "0" || $(wc -l < "$OUT_FILE") -eq 0 ]]; then
  log "  → falling back to scraping per-section index pages"

  index_pages=("api_methods.html" "api_objects.html")
  [[ "$INCLUDE_EVENTS" == "1" ]] && index_pages+=("api_events.html")

  for page in "${index_pages[@]}"; do
    url="${BASE_URL}/${page}"
    if curl -sfL -m "$CURL_TIMEOUT" "$url" -o "$TMP"; then
      count_before=$(wc -l < "$OUT_FILE")
      grep -oE 'href="[^"]*api_(methods|objects|events)_[^"]+\.html"' "$TMP" \
        | sed -E 's/^href="//;s/"$//' \
        | sort -u \
        | while read -r href; do resolve_url "$href"; done \
        >> "$OUT_FILE"
      count_after=$(wc -l < "$OUT_FILE")
      log "    + ${page}: $((count_after - count_before)) URLs"
    else
      log "    - ${page}: fetch failed (skipping)"
    fi
  done

  sort -u "$OUT_FILE" -o "$OUT_FILE"
fi

# --- always: cookbook / landing pages linked from index.html ---------------
if [[ "$INCLUDE_COOKBOOK" == "1" ]]; then
  url="${BASE_URL}/index.html"
  if curl -sfL -m "$CURL_TIMEOUT" "$url" -o "$TMP"; then
    log "  + scraping index.html for cookbook / how-to pages"
    # Any href ending .html that ISN'T a method/object/event page
    # (those are already handled above) is potentially a cookbook page.
    {
      grep -oE 'href="[^"]+\.html(#[^"]*)?"' "$TMP" \
        | sed -E 's/^href="//;s/(#.*)?"$//' \
        | grep -vE '^(http|api_(methods|objects|events)_)' \
        | sort -u \
        | while read -r href; do resolve_url "$href"; done
      echo "${BASE_URL}/index.html"
    } >> "$OUT_FILE"
    sort -u "$OUT_FILE" -o "$OUT_FILE"
  else
    log "  - index.html unreachable; cookbook pages will be skipped"
  fi
fi

# --- validate + report -----------------------------------------------------
count=$(wc -l < "$OUT_FILE")
if [[ "$count" -eq 0 ]]; then
  log
  log "ERROR: No URLs collected. Both sitemap and fallback returned empty."
  log "  Probable cause: API_VERSION='${API_VERSION}' does not exist, or"
  log "  api.truenas.com layout has changed. Check ${BASE_URL}/ in a browser."
  exit 1
fi

log
log "Wrote $count URLs to $OUT_FILE"
log
log "Sample entries:"
head -5 "$OUT_FILE" | sed 's/^/  /'
[[ "$count" -gt 5 ]] && log "  ..."

log
log "Next step — ingest into the sdg-documentation workspace:"
log
log "  DOC_PREFIX=\"[OFFICIAL] api.truenas.com\" \\"
log "    scripts/tools/recover-long-urls.sh \\"
log "    $OUT_FILE \\"
log "    sdg-documentation \\"
log "    --embed"
