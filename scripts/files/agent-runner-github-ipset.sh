#!/usr/bin/env bash
# agent-runner-github-ipset.sh -- refresh the [IPSET github] block of VM 178's PVE firewall (/etc/pve/firewall/178.fw)
# from GitHub's published ranges (https://api.github.com/meta: api, git, web; IPv4 only). Runs on the Proxmox host
# from agent-runner-github-ipset.timer (daily). The VM may reach GitHub on 443 ONLY through this set; the agent's
# own sandbox cannot reach GitHub at all (agent-run: IPAddressAllow=router only).
#
# Safe by construction: the [IPSET github] header must exist exactly once (PVE requires it BEFORE [RULES], which
# reference it); only the lines between it and the next section are replaced; entries are
# validated with Python's ipaddress module; fewer than MIN_ENTRIES aborts (a broken fetch never empties the set);
# the new file must pass `pve-firewall compile` or the old one is put back.
set -euo pipefail
META_URL=${META_URL:-https://api.github.com/meta}
FW_FILE=${FW_FILE:-/etc/pve/firewall/178.fw}
PVE_COMPILE_CMD=${PVE_COMPILE_CMD:-pve-firewall compile}
MIN_ENTRIES=${MIN_ENTRIES:-20}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -fsS -m 30 "$META_URL" -o "$tmp/meta.json"
cp "$FW_FILE" "$tmp/old.fw"

python3 - "$tmp/meta.json" "$tmp/old.fw" "$tmp/new.fw" "$MIN_ENTRIES" <<'PY'
import ipaddress, json, sys
meta_p, old_p, new_p, min_n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
meta = json.load(open(meta_p))
nets = []
for key in ("api", "git", "web"):
    for c in meta.get(key, []):
        try:
            n = ipaddress.ip_network(str(c), strict=False)
        except ValueError:
            print(f"skipping invalid entry {c!r}", file=sys.stderr)
            continue
        if n.version == 4:
            nets.append(n)
nets = sorted(ipaddress.collapse_addresses(nets))
if len(nets) < min_n:
    sys.exit(f"only {len(nets)} IPv4 ranges from meta (< {min_n}); keeping the current set")
old = open(old_p).read()
header = "[IPSET github]\n"
if old.count(header) != 1:
    sys.exit("expected exactly one '[IPSET github]' header in the firewall file")
head, rest = old.split(header, 1)
lines = rest.splitlines(keepends=True)
cut = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
block, after = lines[:cut], "".join(lines[cut:])
comments = [l for l in block if l.lstrip().startswith("#")] or \
    ["# api.github.com/meta api+git+web IPv4, refreshed by agent-runner-github-ipset.sh\n"]
body = "".join(comments) + "".join(f"{n}\n" for n in nets) + ("\n" if after else "")
open(new_p, "w").write(head + header + body + after)
print(f"{len(nets)} ranges")
PY

if cmp -s <(grep -v '^#' "$tmp/old.fw") <(grep -v '^#' "$tmp/new.fw"); then
  echo "github ipset unchanged"; exit 0
fi
cp "$tmp/new.fw" "$FW_FILE"
if ! $PVE_COMPILE_CMD >/dev/null 2>&1; then
  cp "$tmp/old.fw" "$FW_FILE"
  echo "pve-firewall compile failed with the new set; restored the previous file" >&2; exit 1
fi
echo "github ipset updated: $(awk '/^\[IPSET github\]$/{f=1;next} /^\[/{f=0} f&&/^[0-9]/' "$FW_FILE" | wc -l) ranges"
