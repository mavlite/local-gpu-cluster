#!/usr/bin/env bash
# 77-wf-vram-check.sh — decision H (workforce spec §10): does 3 x 128K chat fit on the V620s with
# embed and rerank still loaded?
#
# Reuses the proven redteam layout switch (redteam-mode-enter.sh: chat -> 3 slots x 128K, embed and
# rerank stopped), then starts embed + rerank again on top of it. llama.cpp reserves the whole KV
# cache and its compute buffers at load, so "all three servers load, the router reports chat, embed
# and rerank ok at capacity 3, and three concurrent chats complete while an embedding is served" is
# the fit test. Always restores the normal layout with redteam-mode-exit.sh, even on failure.
#
# PRODUCTION-IMPACTING: chat restarts twice (~1 min each); RAG is down for the first switch.
# Prints one JSON verdict; exit 0 if it fits, 1 if not, 2 if the check itself failed (including a
# failed restore). GPU readings are data, parsed by files/wf-vram-verdict.py -- never code.
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"
require_root
require_pve_host

AMD=151
ROUTER=153
ENTER=/usr/local/sbin/redteam-mode-enter.sh
EXIT=/usr/local/sbin/redteam-mode-exit.sh
require_cmd pct python3
[[ -x "$ENTER" && -x "$EXIT" ]] || die "redteam layout scripts missing ($ENTER, $EXIT)"

healthz() { pct exec "$ROUTER" -- curl -s -m 5 http://127.0.0.1:8000/healthz; }
field() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null; }
wait_for() {  # wait_for <python expr over d> <seconds>
  local deadline=$((SECONDS + $2))
  while (( SECONDS < deadline )); do
    [[ "$(healthz | field "$1")" == "True" ]] && return 0
    date +%s > /run/redteam-mode.last                  # keep the idle check from reverting us
    sleep 5
  done
  return 1
}
vram() { pct exec "$AMD" -- rocm-smi --showmeminfo vram --json 2>/dev/null || echo '{}'; }

[[ "$(healthz | field 'd["chat_admission"]["capacity"]')" == "1" ]] \
  || die "chat is not in the normal 1-slot layout; refusing to start from an unknown state"
[[ ! -f /run/redteam-mode.state ]] || die "redteam mode is active; exit it first"

restored=0
restore_failed=0
restore() {
  [[ $restored -eq 1 ]] && return
  restored=1
  "$EXIT" >/dev/null 2>&1 || { restore_failed=1; warn "redteam-mode-exit.sh failed — check the chat layout by hand"; }
  rm -f /run/redteam-mode.last
}
trap restore EXIT

step "1 — enter 3 x 128K (embed + rerank stop)"
date +%s > /run/redteam-mode.last
"$ENTER" >/dev/null || { echo '{"fits": null, "error": "redteam-mode-enter.sh failed"}'; exit 2; }
wait_for 'd["chat_admission"]["capacity"] == 3 and d["upstream"]["chat"] == "ok"' 300 \
  || { echo '{"fits": null, "error": "chat never reached capacity 3"}'; exit 2; }
tmpd="$(mktemp -d)"
vram > "$tmpd/before.json"

step "2 — start embed + rerank on top"
pct exec "$AMD" -- systemctl start llamacpp-embed llamacpp-rerank || true
fits=true
wait_for 'd["upstream"]["embed"] == "ok" and d["upstream"]["rerank"] == "ok" and d["upstream"]["chat"] == "ok"' 240 \
  || fits=false
vram > "$tmpd/after.json"
units="$(pct exec "$AMD" -- systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank | tr '\n' ' ' || true)"

step "3 — three concurrent chats + one embedding"
concurrent="skipped"
if $fits; then
  pct push "$ROUTER" "$LGC_DIR/files/wf-vram-load.sh" /root/wf-vram-load.sh --perms 0700
  concurrent="$(pct exec "$ROUTER" -- /root/wf-vram-load.sh || echo failed)"
  pct exec "$ROUTER" -- rm -f /root/wf-vram-load.sh
  [[ "$concurrent" == "ok" ]] || fits=false
fi

step "4 — restore the normal layout"
restore
normal=1
wait_for 'd["chat_admission"]["capacity"] == 1 and d["upstream"]["embed"] == "ok" and d["upstream"]["rerank"] == "ok"' 300 \
  || { normal=0; warn "normal layout not confirmed within 5 min — check /healthz"; }
rm -f /run/redteam-mode.last                       # wait_for refreshed it; nothing should keep it now

python3 "$LGC_DIR/files/wf-vram-verdict.py" "$fits" "$units" "$concurrent" "$tmpd/before.json" "$tmpd/after.json"
rm -rf "$tmpd"
[[ $restore_failed -eq 0 && $normal -eq 1 ]] || exit 2
$fits
