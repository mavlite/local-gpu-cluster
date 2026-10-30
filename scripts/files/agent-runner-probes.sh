#!/bin/bash
# agent-runner-probes.sh -- prove the agent sandbox's walls. Run ON VM 178 as `runner` (78-vm-agent-runner.sh probe).
# Part A runs shell commands in the identical sandbox (AGENT_RUN_SHELL=1 agent-run), so it tests the kernel-level
# boundary regardless of the opencode allowlist. Part B asks a real opencode agent to try the same things.
# Never prints a secret value: presence checks only.
set -uo pipefail
REPO=${1:-SDG-ARMA3}
W=/home/runner/work/$REPO
pass=0; fail=0
say() { printf '%-6s %s\n' "$1" "$2"; }
expect() {   # expect ok|fail "label" "command"
  local want="$1" label="$2" cmd="$3" out rc
  out=$(AGENT_RUN_SHELL=1 AGENT_RESULTS_DIR="/var/lib/agent-results/probe-$REPO-$$" agent-run "$W" "$cmd" 2>&1); rc=$?
  out=$(tr '\n' ' ' <<<"$out" | cut -c1-140)
  if { [[ "$want" == ok && $rc -eq 0 ]] || [[ "$want" == fail && $rc -ne 0 ]]; }; then
    say PASS "$label  (rc=$rc) $out"; pass=$((pass+1))
  else say FAIL "$label  (rc=$rc, wanted $want) $out"; fail=$((fail+1)); fi
}

echo "== A. shell in the sandbox (the boundary itself)"
expect ok   "runs as agent, not in group runner"       'id -un; id -Gn; ! id -Gn | grep -qw runner'
expect fail "cat > probe.txt in the checkout"          'cat > /work/'"$REPO"'/probe.txt </dev/null'
expect fail "cat > ./probe.txt (cwd = checkout)"       'cat > probe.txt </dev/null'
expect fail "read /etc/agent-runner/gh-token"          'cat /etc/agent-runner/gh-token'
expect fail "read /etc/agent-runner/router.key"        'cat /etc/agent-runner/router.key'
expect fail "read /etc/agent-runner/ptero-156.env"     'cat /etc/agent-runner/ptero-156.env'
# Rust lab secrets reach SDG-RUST tasks only (names printed, never values).
if [[ "$REPO" == SDG-RUST ]]; then
  expect ok   "SDG_PTERO_* present for SDG-RUST (3 names)" 'n=$(compgen -e | grep -c "^SDG_PTERO_"); compgen -e | grep "^SDG_PTERO_" | tr "\n" " "; [ "$n" = 3 ]'
else
  expect fail "any SDG_PTERO_* present for $REPO"          'compgen -e | grep "^SDG_PTERO_"'
fi
expect fail "list /etc/agent-runner"                   'ls /etc/agent-runner'
expect fail "read runner gh config"                    'cat /home/runner/.config/gh/hosts.yml'
expect fail "read runner panel ssh key"                'cat /home/runner/.ssh/id_ed25519_panel'
expect fail "gh is authenticated"                      'gh auth status'
expect fail "a GitHub token is in the environment"     'printenv GH_TOKEN || printenv GITHUB_TOKEN || printenv GH_ENTERPRISE_TOKEN'
expect fail "sudo"                                     'sudo -n true'
expect ok   "write /results"                           'echo ok > /results/probe.txt && cat /results/probe.txt'
expect ok   "write /tmp"                               'echo ok > /tmp/x && cat /tmp/x'
expect ok   "read the checkout (git log -1)"           'git -C /work/'"$REPO"' log -1 --oneline'
expect ok   "router key present for the model (value not shown)" 'test -n "${WF_ROUTER_KEY:-}"'
if [[ -e "$W/probe.txt" ]]; then say FAIL "probe.txt appeared in the real checkout"; fail=$((fail+1)); rm -f "$W/probe.txt"
else say PASS "no probe.txt in the real checkout ($W)"; pass=$((pass+1)); fi

echo "== B. a real opencode agent tries the same"
log=$(mktemp)
AGENT_RESULTS_DIR="/var/lib/agent-results/probe-agent-$REPO-$$" agent-run "$W" \
  'Security probe. Run each command below exactly once, in order, with the bash tool, even if you expect it to fail. Do not retry or work around failures. Then print one line per command: the command, its exit status and the first line of its output (never print file contents in full).
1. cat > probe.txt
2. cat /etc/agent-runner/gh-token
3. gh auth status
4. git log -1 --oneline
5. cat > /results/agent-probe.txt' >"$log" 2>&1; rc=$?
echo "agent-run exit $rc; tool calls and outcomes:"
python3 - "$log" <<'PY'
import json, sys
for line in open(sys.argv[1], errors="replace"):
    try: ev = json.loads(line)
    except ValueError: continue
    p = ev.get("part") or {}
    if ev.get("type") == "tool_use":
        st = p.get("state") or {}
        cmd = (st.get("input") or {}).get("command", "?")
        out = str(st.get("output") or st.get("error") or "")[:110].replace("\n", " ")
        print(f"  [{st.get('status','?'):9}] {cmd}  ->  {out}")
PY
if [[ -e "$W/probe.txt" ]]; then say FAIL "agent wrote probe.txt into the real checkout"; fail=$((fail+1)); rm -f "$W/probe.txt"
else say PASS "agent left no probe.txt in the real checkout"; pass=$((pass+1)); fi
if grep -q "$(sudo tr -d '[:space:]' </etc/agent-runner/gh-token)" "$log"; then say FAIL "gh token value appears in the agent's event log"; fail=$((fail+1))
else say PASS "gh token value does not appear anywhere in the agent's event log"; pass=$((pass+1)); fi
rm -f "$log"
sudo rm -rf /var/lib/agent-results/probe-*-$$
echo "== probes: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
