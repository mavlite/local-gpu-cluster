#!/usr/bin/env bash
# redteam-egress-report.sh — show what the LXC 158 egress lock has rejected, per
# destination, from the nft audit sets that redteam-egress-lock.sh maintains.
#   redteam-egress-report.sh            # per-destination totals (busiest first)
#   redteam-egress-report.sh --ports    # every destination . proto . port entry
#   redteam-egress-report.sh --reset    # clear the audit trail (e.g. per engagement)
# Destinations are only the ones the lock REJECTED — allowed traffic is not recorded.
set -euo pipefail

TABLE=redteam_egress_audit
MODE=${1:-}

if ! nft list table inet "$TABLE" >/dev/null 2>&1; then
  echo "audit table inet $TABLE not present — run redteam-egress-lock.sh first" >&2
  exit 1
fi

if [[ $MODE == --reset ]]; then
  nft flush set inet "$TABLE" denied
  nft flush set inet "$TABLE" denied_other
  echo "audit trail cleared"
  exit 0
fi

REPORT_PY=$(cat <<'PY'
import json, sys, collections
ports_mode = sys.argv[1] == "--ports"
rows = []  # (ip, proto, port, packets)
for doc in sys.stdin.read().splitlines():
    if not doc.strip():
        continue
    for item in json.loads(doc)["nftables"]:
        for el in item.get("set", {}).get("elem", []) or []:
            e = el.get("elem", el)
            val, pk = e.get("val", e), e.get("counter", {}).get("packets", 0)
            parts = val["concat"]
            rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "-", pk))
if not rows:
    print("no rejected outbound connections recorded"); sys.exit(0)
if ports_mode:
    for ip, proto, port, pk in sorted(rows, key=lambda r: -r[3]):
        print(f"{pk:8d}  {ip:15s} {proto:5s} {port}")
else:
    tot = collections.Counter(); nports = collections.Counter()
    for ip, _, _, pk in rows:
        tot[ip] += pk; nports[ip] += 1
    print(" packets  destination     distinct proto/ports")
    for ip, pk in tot.most_common():
        print(f"{pk:8d}  {ip:15s} {nports[ip]}")
    print(f"total {sum(tot.values())} packets to {len(tot)} destinations")
PY
)

{ nft -j list set inet "$TABLE" denied; echo; nft -j list set inet "$TABLE" denied_other; echo; } |
  python3 -c "$REPORT_PY" "$MODE"
