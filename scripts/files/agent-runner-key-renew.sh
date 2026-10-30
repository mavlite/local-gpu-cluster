#!/bin/bash
# Renew the agent runner's router key (LXC 153 router-keys) when it has under RENEW_HOURS left, and push it to the
# runner VM 178 (/etc/agent-runner/router.key). The key is never printed. Usage: agent-runner-key-renew [--dry-run|--force]
set -euo pipefail
NAME=agent-runner ALIASES=qwen3.8-nothink,qwen3.6 TTL=336 RENEW_HOURS=48
RUNNER=runner@10.81.0.10 SSHKEY=/root/.ssh/agent-runner-admin TMP=/root/agent-runner.key.new
mode=${1:-}
line=$(pct exec 153 -- /usr/local/sbin/router-keys list | awk -v n="$NAME" '$1==n && $2=="active"')
exp=$(sed -n 's/.*expires=\([^ \t]*\).*/\1/p' <<<"$line")
if [[ -n "$exp" ]]; then left=$(( ($(date -d "$exp" +%s) - $(date +%s)) / 3600 )); else left=-1; fi
echo "key $NAME: expires=${exp:-none} hours_left=$left threshold=$RENEW_HOURS"
if [[ "$mode" != --force && $left -gt $RENEW_HOURS ]]; then echo "no renewal needed"; exit 0; fi
if [[ "$mode" == --dry-run ]]; then echo "dry-run: would revoke, re-issue (ttl ${TTL}h) and push"; exit 0; fi
ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$SSHKEY" "$RUNNER" true   # runner reachable before we revoke anything
[[ -n "$exp" ]] && pct exec 153 -- /usr/local/sbin/router-keys revoke --name "$NAME" >/dev/null
pct exec 153 -- /usr/local/sbin/router-keys add --name "$NAME" --aliases "$ALIASES" --ttl-hours "$TTL" --out "$TMP" >/dev/null
pct exec 153 -- cat "$TMP" | ssh -o BatchMode=yes -i "$SSHKEY" "$RUNNER" \
  'sudo tee /etc/agent-runner/router.key.new >/dev/null && sudo chown root:runner /etc/agent-runner/router.key.new &&
   sudo chmod 640 /etc/agent-runner/router.key.new && sudo mv /etc/agent-runner/router.key.new /etc/agent-runner/router.key'
pct exec 153 -- shred -u "$TMP"
code=$(ssh -o BatchMode=yes -i "$SSHKEY" "$RUNNER" 'curl -s -o /dev/null -w "%{http_code}" -m 15 -H "Authorization: Bearer $(tr -d "[:space:]" </etc/agent-runner/router.key)" http://192.168.6.153:8000/v1/models')
echo "renewed: $(pct exec 153 -- /usr/local/sbin/router-keys list | awk -v n="$NAME" '$1==n' | cut -f1-3); runner /v1/models -> $code"
[[ "$code" == 200 ]]
