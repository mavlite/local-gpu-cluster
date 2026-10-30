#!/bin/bash
# agent-run DIR MESSAGE [opencode run args...]: one headless lab-runner session, sandboxed.
#
# Called as `runner` (the dispatcher). opencode itself runs as the unprivileged user `agent` in a transient
# systemd unit (hardening 2026-10-30, after `cat > file` wrote despite edit: deny and the gh token was readable):
#   - the checkout DIR (must be under /home/runner/work) is bind-mounted READ-ONLY at /work/<repo>
#   - $AGENT_RESULTS_DIR (created here, agent-writable, group runner) is bind-mounted at /results
#   - private /tmp (HOME=/tmp/home), /home hidden, /etc/agent-runner and /run/agent-runner inaccessible
#   - no GitHub credentials (GH_CONFIG_DIR points at an empty dir; the token stays in /etc/agent-runner)
#   - the router key arrives via a root-only EnvironmentFile that systemd reads before dropping to `agent`
# The model follows the router's loaded chat profile (scoped keys cannot swap it): rag-qwen3.6 -> qwen3.6,
# qwen3.8 -> qwen3.8-nothink, anything else -> lane closed, exit 75 so the caller retries later.
#
# Probe mode (runner only, for testing the walls): AGENT_RUN_SHELL=1 agent-run DIR 'shell command' runs
# /bin/bash -c in the identical sandbox instead of opencode.
set -euo pipefail
dir=${1:?usage: agent-run DIR MESSAGE [ARGS...]}; msg=${2:?usage: agent-run DIR MESSAGE [ARGS...]}; shift 2
dir=$(realpath -e "$dir")
case "$dir" in /home/runner/work/?*) ;; *) echo "agent-run: DIR must be a checkout under /home/runner/work" >&2; exit 2 ;; esac
name=$(basename "$dir")

if [[ "${AGENT_RUN_SHELL:-}" == 1 ]]; then
  model=none
else
  profile=$(curl -s -m 10 http://192.168.6.153:8000/healthz | jq -r '.active_chat_profile // empty')
  case "$profile" in
    rag-qwen3.6) model=router/qwen3.6 ;;
    qwen3.8)     model=router/qwen3.8-nothink ;;
    *) echo "agent-run: router chat profile '${profile:-unknown}' has no lane for scoped keys; retry later" >&2; exit 75 ;;
  esac
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
unit="agent-task-${name}-${stamp}-$$"
results=${AGENT_RESULTS_DIR:-/var/lib/agent-results/${name}-${stamp}-$$}
case "$results" in /var/lib/agent-results/?*) ;; *) echo "agent-run: results dir must be under /var/lib/agent-results" >&2; exit 2 ;; esac
max=${AGENT_MAX_SECONDS:-3600}
envf="/run/agent-runner/${unit}.env"

sudo install -d -o agent -g runner -m 2770 "$results"
sudo install -d -o root -g root -m 0700 /run/agent-runner
sudo install -o root -g root -m 0600 /dev/null "$envf"
printf 'WF_ROUTER_KEY=%s\n' "$(tr -d '[:space:]' </etc/agent-runner/router.key)" | sudo tee "$envf" >/dev/null

cleanup() { sudo systemctl stop "$unit" 2>/dev/null || true; sudo rm -f "$envf"; }
trap cleanup EXIT
trap 'exit 143' TERM INT

props=(
  --uid=agent --gid=agent --unit="$unit" --quiet --wait --pipe --collect
  -p RuntimeMaxSec="$max"
  -p ProtectSystem=strict -p ProtectHome=tmpfs -p PrivateTmp=yes -p PrivateDevices=yes
  -p NoNewPrivileges=yes -p RestrictSUIDSGID=yes -p ProtectKernelTunables=yes -p ProtectKernelModules=yes
  -p ProtectKernelLogs=yes -p ProtectControlGroups=yes -p ProtectClock=yes -p LockPersonality=yes
  -p CapabilityBoundingSet= -p UMask=0007
  -p InaccessiblePaths=/etc/agent-runner -p InaccessiblePaths=/run/agent-runner
  -p BindReadOnlyPaths="$dir:/work/$name"
  -p BindPaths="$results:/results"
  -p EnvironmentFile="$envf"
  -p WorkingDirectory="/work/$name"
  -p ExecStartPre="/bin/mkdir -p /tmp/home /tmp/gh"
  --setenv=HOME=/tmp/home --setenv=GH_CONFIG_DIR=/tmp/gh --setenv=PYTHONDONTWRITEBYTECODE=1
  # the checkout is owned by runner, so git (as agent) would refuse it as "dubious ownership"
  --setenv=GIT_CONFIG_COUNT=1 --setenv=GIT_CONFIG_KEY_0=safe.directory --setenv=GIT_CONFIG_VALUE_0="/work/$name"
  --setenv=OPENCODE_CONFIG=/etc/agent-runner-agent/opencode.json --setenv=OPENCODE_DISABLE_PROJECT_CONFIG=1
  --setenv=OPENCODE_DISABLE_CLAUDE_CODE=1 --setenv=OPENCODE_DISABLE_EXTERNAL_SKILLS=1
  --setenv=OPENCODE_DISABLE_AUTOUPDATE=1 --setenv=OPENCODE_DISABLE_MODELS_FETCH=1 --setenv=OPENCODE_DISABLE_SHARE=1
)
if [[ "$model" == none ]]; then
  sudo systemd-run "${props[@]}" /bin/bash -c "$msg" </dev/null
else
  sudo systemd-run "${props[@]}" /usr/local/bin/opencode run --pure --agent lab-runner --model "$model" \
    --dir "/work/$name" --format json "$@" "$msg" </dev/null
fi
