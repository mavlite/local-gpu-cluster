#!/usr/bin/env bash
# 65-searxng.sh — deploy SearXNG into the mcp-stack LXC (155).
#
# SearXNG is a self-hosted metasearch engine: it proxies a query to Google,
# Bing, DuckDuckGo, Startpage etc., merges the results, and returns them with
# no API key and no per-query quota.
#
# WHY IT EXISTS HERE: it is the unmetered alternative to Tavily. LESSONS.md
# (2026-04-21) records the original motivation -- routing search through the
# local instance "doesn't burn Tavily or context7 quotas". That mattered again
# on 2026-08-23 when the Tavily account hit its plan limit and every
# /v1/tavily/search call started returning 502, leaving the cluster with no
# working web search at all.
#
# It is NOT part of the VCF documentation path. VCF answers come from the
# vcf-reference AnythingLLM workspace via mcp-sdg; the old broadcom-techdocs
# MCP that used SearXNG for site-filtered Broadcom search was retired in the
# V620 migration (LESSONS.md 2026-05-19 update).
#
# Placed in LXC 155 rather than its own container: 155 is already the Docker
# host for the MCP stack, runs nothing in Docker today, and has 4 GB RAM with
# ~110 MB in use. A previous instance lived at 192.168.6.109, a host that no
# longer exists -- which is why the opencode SEARXNG_URL pointed at a dead
# address.
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

SX_VMID="${SEARXNG_VMID:-${MCP_VMID:-155}}"
SX_PORT="${SEARXNG_PORT:-8888}"
# PINNED BY DIGEST, deliberately. :latest would let a re-run of this script pull
# a breaking upstream change with no warning -- the same failure mode that took
# mcp-sdg down for 26 hours when an unbounded `mcp>=1.2` picked up mcp 2.0.0 and
# lost mcp.server.fastmcp. To upgrade: pull the new tag, verify the JSON API
# still answers (step 5 does this), then update this digest.
# Digest below verified working 2026-08-24 (searxng/searxng:latest at that date).
SX_IMAGE="${SEARXNG_IMAGE:-docker.io/searxng/searxng@sha256:11a9b34cdc0b1ec2b991470a2762ecb5a1a531898289fb51dcd015260450729e}"
SX_DIR="${SEARXNG_DIR:-/opt/searxng}"
SX_BIND="${SEARXNG_BIND:-0.0.0.0}"
# LAN-only service, same posture as mcp-sdg: no auth, trusted single-user LAN.
SX_BASE_URL="${SEARXNG_BASE_URL:-http://192.168.6.155:${SX_PORT}/}"

lxc_exists "$SX_VMID" || die "LXC $SX_VMID does not exist — run 55-lxc-mcp.sh first."
ensure_lxc_started "$SX_VMID"

step "1 — verify Docker in LXC $SX_VMID"
pct exec "$SX_VMID" -- docker --version >/dev/null 2>&1 \
  || die "Docker not available in LXC $SX_VMID."
ok "$(pct exec "$SX_VMID" -- docker --version)"

step "2 — generate settings.yml"
# Two settings are load-bearing:
#
#   search.formats MUST include json. SearXNG ships HTML-only by default, and
#   mcp-searxng (and any other API client) needs the JSON endpoint. Without it
#   every query returns HTTP 403 "Forbidden format" and the MCP server looks
#   broken for reasons the logs do not make obvious.
#
#   server.secret_key must be unique per install; SearXNG refuses to start
#   with the shipped placeholder.
if pct exec "$SX_VMID" -- test -f "$SX_DIR/settings.yml"; then
  skip "settings.yml exists — keeping its secret_key"
  SECRET="$(pct exec "$SX_VMID" -- awk -F'"' '/secret_key/{print $2}' "$SX_DIR/settings.yml")"
else
  SECRET="$(openssl rand -hex 32)"
fi
pct exec "$SX_VMID" -- mkdir -p "$SX_DIR"
pct exec "$SX_VMID" -- bash -c "cat > $SX_DIR/settings.yml" <<EOF
# Managed by scripts/65-searxng.sh — re-run the script to regenerate.
use_default_settings: true
server:
  secret_key: "${SECRET}"
  bind_address: "0.0.0.0"
  base_url: "${SX_BASE_URL}"
  limiter: false          # single-user LAN; the limiter needs redis and would
                          # rate-limit our own agent traffic
  image_proxy: false
search:
  safe_search: 0
  autocomplete: ""
  # json is REQUIRED for mcp-searxng / any API consumer. Do not drop it.
  formats:
    - html
    - json
general:
  instance_name: "sdg-searxng"
  donation_url: false
EOF
ok "settings.yml written"

step "3 — start container"
pct exec "$SX_VMID" -- docker rm -f searxng >/dev/null 2>&1 || true
pct exec "$SX_VMID" -- docker run -d \
  --name searxng \
  --restart unless-stopped \
  -p "${SX_BIND}:${SX_PORT}:8080" \
  -v "$SX_DIR/settings.yml:/etc/searxng/settings.yml:ro" \
  -e "SEARXNG_BASE_URL=${SX_BASE_URL}" \
  --cap-drop ALL --cap-add CHOWN --cap-add SETGID --cap-add SETUID \
  "$SX_IMAGE" >/dev/null
ok "container started"

step "4 — wait for readiness"
SX_IP="$(lxc_get_ip "$SX_VMID")"
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
          "http://${SX_IP}:${SX_PORT}/" || true)"
  [[ "$code" == "200" ]] && break
  sleep 2
done
[[ "$code" == "200" ]] || die "SearXNG did not become ready (last HTTP $code)"
ok "HTTP 200 at http://${SX_IP}:${SX_PORT}/"

step "5 — verify the JSON API (the part that actually matters)"
json_code="$(curl -s -o /tmp/sx-probe.json -w '%{http_code}' --max-time 20 \
  "http://${SX_IP}:${SX_PORT}/search?q=vmware+cloud+foundation&format=json" || true)"
[[ "$json_code" == "200" ]] \
  || die "JSON API returned HTTP $json_code — check search.formats includes json"
n="$(python3 -c "import json;print(len(json.load(open('/tmp/sx-probe.json')).get('results',[])))" 2>/dev/null || echo 0)"
[[ "$n" -gt 0 ]] || die "JSON API returned 200 but zero results — engines may be blocked"
ok "JSON API returned $n results"
rm -f /tmp/sx-probe.json

step "done"
cat <<EOF

  SearXNG is live at  http://${SX_IP}:${SX_PORT}/
  JSON API           http://${SX_IP}:${SX_PORT}/search?q=<query>&format=json

  Point clients at it:
    ~/.config/opencode/config.json  ->  mcp.searxng.environment.SEARXNG_URL
        "SEARXNG_URL": "http://${SX_IP}:${SX_PORT}"

  This replaces the dead 192.168.6.109:8888 instance.
EOF
