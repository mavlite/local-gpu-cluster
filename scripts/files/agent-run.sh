#!/bin/bash
# agent-run DIR MESSAGE [opencode run args...]: one headless lab-runner session, sandboxed.
#
# Called as `runner` (the dispatcher). opencode itself runs as the repo's own unprivileged user (egress.conf:
# agent-rust, agent-arma) in a transient
# systemd unit (hardening 2026-10-30, after `cat > file` wrote despite edit: deny and the gh token was readable):
#   - the checkout DIR (must be under /home/runner/work) is bind-mounted READ-ONLY at /work/<repo>
#   - $AGENT_RESULTS_DIR (created here, agent-writable, group runner) is bind-mounted at /results
#   - private /tmp (HOME=/tmp/home), /home hidden, /etc/agent-runner and /run/agent-runner inaccessible
#   - no GitHub credentials (GH_CONFIG_DIR points at an empty dir; the token stays in /etc/agent-runner)
#   - the router key arrives via a root-only EnvironmentFile that systemd reads before dropping to `agent`;
#     per-repo lab secrets (SDG-RUST: /etc/agent-runner/ptero-156.env, SDG_PTERO_*) ride along, for that repo only
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

# Per-repo sandbox user + egress (egress.conf, one table -> systemd IPAddressAllow here, nftables per uid+port).
# A repo without an entry does not run; neither does any task while the nft table is missing (fail closed).
EG=/usr/local/lib/agent-runner/agent_runner_egress.py
EGCONF=/etc/agent-runner-agent/egress.conf
user=$(python3 "$EG" --conf "$EGCONF" user "$name") || { echo "agent-run: no sandbox entry for $name in $EGCONF" >&2; exit 2; }
read -ra ipallow <<<"$(python3 "$EG" --conf "$EGCONF" ip-allow "$name")"
if ! sudo /usr/sbin/nft list table inet agent_egress >/dev/null 2>&1; then
  echo "agent-run: nftables table inet agent_egress is not loaded (agent-runner-egress.service); refusing to start" >&2
  exit 2
fi

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

sudo install -d -o "$user" -g runner -m 2770 "$results"
sudo install -d -o root -g root -m 0700 /run/agent-runner
sudo install -o root -g root -m 0600 /dev/null "$envf"
printf 'WF_ROUTER_KEY=%s\n' "$(tr -d '[:space:]' </etc/agent-runner/router.key)" | sudo tee "$envf" >/dev/null

# Per-repo lab secrets, passed in exactly like the router key and ONLY for that repo's tasks.
# Each file may hold only PREFIX_*=value lines (comments/blank ok): anything else is refused, so a secret file
# can never smuggle LD_PRELOAD, PATH or the like into the sandbox.
declare -A REPO_ENV_FILE=( [SDG-RUST]=/etc/agent-runner/ptero-156.env )
declare -A REPO_ENV_PREFIX=( [SDG-RUST]=SDG_PTERO_ )
if [[ -n "${REPO_ENV_FILE[$name]:-}" && -e "${REPO_ENV_FILE[$name]}" ]]; then
  f=${REPO_ENV_FILE[$name]}; p=${REPO_ENV_PREFIX[$name]}
  if grep -qvE "^(${p}[A-Z0-9_]+=.*|[[:space:]]*|#.*)$" "$f"; then
    echo "agent-run: $f has a line that is not ${p}*=...; refusing to start" >&2; exit 2
  fi
  grep -E "^${p}[A-Z0-9_]+=" "$f" | sudo tee -a "$envf" >/dev/null
fi

cleanup() { sudo systemctl stop "$unit" 2>/dev/null || true; sudo rm -f "$envf"; }
trap cleanup EXIT
trap 'exit 143' TERM INT

ipprops=(); for ip in "${ipallow[@]}"; do ipprops+=(-p "IPAddressAllow=$ip"); done

props=(
  --uid="$user" --gid="$user" --unit="$unit" --quiet --wait --pipe --collect
  -p RuntimeMaxSec="$max"
  -p ProtectSystem=strict -p ProtectHome=tmpfs -p PrivateTmp=yes -p PrivateDevices=yes
  -p NoNewPrivileges=yes -p RestrictSUIDSGID=yes -p ProtectKernelTunables=yes -p ProtectKernelModules=yes
  -p ProtectKernelLogs=yes -p ProtectControlGroups=yes -p ProtectClock=yes -p LockPersonality=yes
  -p CapabilityBoundingSet= -p UMask=0007
  # Network (hardening 2026-10-30): the router, loopback 127.0.0.1/::1 and the repo's own lab endpoints ONLY (the
  # nft table adds the per-port check). No DNS: not "localhost" (that includes resolved's stub 127.0.0.53, which
  # resolved names for the sandbox), and resolved's varlink socket and the system bus are hidden too. The VM itself
  # is limited further out by 178.fw (GitHub ipset for the dispatcher, VPN endpoint, NTP via the host).
  -p IPAddressDeny=any "${ipprops[@]}"
  -p RestrictAddressFamilies="AF_UNIX AF_INET AF_INET6"
  -p InaccessiblePaths=/etc/agent-runner -p InaccessiblePaths=/run/agent-runner
  -p InaccessiblePaths=/run/systemd/resolve -p InaccessiblePaths=/run/dbus
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
