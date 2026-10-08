#!/bin/sh
# wf-vram-load.sh — runs inside LXC 153 (router) for 77-wf-vram-check.sh. Three concurrent owner
# chats on qwen3.8-nothink plus one embedding; prints "ok" only if all four succeed. The owner key
# reaches curl through a 0600 header file, never argv.
set -eu
umask 077
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
set -a; . /etc/router.env; set +a
printf 'Authorization: Bearer %s\n' "$ROUTER_API_KEY" > "$D/h"
CHAT='{"model":"qwen3.8-nothink","max_tokens":64,"messages":[{"role":"user","content":"Count from 1 to 40."}]}'
for i in 1 2 3; do
  curl -s -o /dev/null -w '%{http_code}\n' -m 300 -H @"$D/h" -H 'Content-Type: application/json' \
    -d "$CHAT" http://127.0.0.1:8000/v1/chat/completions > "$D/chat$i" &
done
curl -s -o /dev/null -w '%{http_code}\n' -m 120 -H @"$D/h" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embed","input":"vram probe"}' http://127.0.0.1:8000/v1/embeddings > "$D/embed"
wait
for f in chat1 chat2 chat3 embed; do
  [ "$(cat "$D/$f")" = "200" ] || { echo "failed: $f $(cat "$D/$f")"; exit 1; }
done
echo ok
