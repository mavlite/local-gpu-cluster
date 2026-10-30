#!/bin/bash
# agent-run DIR MESSAGE [opencode run args...]: one headless lab-runner session, locked to /etc/agent-runner/opencode.json.
# The model follows the router's loaded chat profile (scoped keys cannot swap it): rag-qwen3.6 -> qwen3.6,
# qwen3.8 -> qwen3.8-nothink, anything else -> lane closed, exit 75 so the caller retries later.
set -euo pipefail
dir=${1:?usage: agent-run DIR MESSAGE [ARGS...]}; msg=${2:?usage: agent-run DIR MESSAGE [ARGS...]}; shift 2
profile=$(curl -s -m 10 http://192.168.6.153:8000/healthz | jq -r '.active_chat_profile // empty')
case "$profile" in
  rag-qwen3.6) model=router/qwen3.6 ;;
  qwen3.8)     model=router/qwen3.8-nothink ;;
  *) echo "agent-run: router chat profile '${profile:-unknown}' has no lane for scoped keys; retry later" >&2; exit 75 ;;
esac
export OPENCODE_CONFIG=/etc/agent-runner/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_CLAUDE_CODE=1 \
  OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_SHARE=1
WF_ROUTER_KEY=$(tr -d '[:space:]' < /etc/agent-runner/router.key); export WF_ROUTER_KEY
exec opencode run --pure --agent lab-runner --model "$model" --dir "$dir" --format json "$@" "$msg" </dev/null
