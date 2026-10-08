"""Workforce sandbox VM helpers (workforce spec §5.1-5.2; Plan B).

wf-sandbox-policy.sh renders the PVE firewall policy for the sandbox VM in two modes:
  build  -- provisioning: internet only (all private space denied), so apt/npm/docker can fetch;
  locked -- measured runs: nothing but the router and the three CPU workers.
wf-sandbox-net.sh adds/removes the sandbox's dedicated source address and its SNAT rule, so the
router and workers see 192.168.6.79 -- never the Proxmox host (whose address passes the router's
/metrics allowlist).
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "..")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def policy(mode, **env):
    return subprocess.run([BASH, os.path.join(FILES, "wf-sandbox-policy.sh"), mode], capture_output=True,
                          text=True, env=dict(os.environ, **env))


def rules(text):
    return [line.split("#")[0].strip() for line in text.splitlines()
            if line.startswith(("IN ", "OUT "))]


def options(text):
    out = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(("IN ", "OUT ", "#", "[")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def test_locked_mode_allows_only_the_router_and_the_three_workers():
    r = policy("locked")
    assert r.returncode == 0, r.stderr
    assert options(r.stdout) == {"enable": "1", "policy_in": "DROP", "policy_out": "DROP"}
    assert rules(r.stdout) == [
        "OUT ACCEPT -p tcp -dest 192.168.6.153 -dport 8000",
        "OUT ACCEPT -p tcp -dest 172.16.10.205 -dport 8090",
        "OUT ACCEPT -p tcp -dest 172.16.10.206 -dport 8090",
        "OUT ACCEPT -p tcp -dest 172.16.10.207 -dport 8090",
    ]


def test_build_mode_allows_the_internet_but_no_private_space_at_all():
    r = policy("build")
    assert r.returncode == 0, r.stderr
    assert options(r.stdout) == {"enable": "1", "policy_in": "DROP", "policy_out": "ACCEPT"}
    assert rules(r.stdout) == [
        "OUT DROP -dest 10.0.0.0/8",
        "OUT DROP -dest 172.16.0.0/12",
        "OUT DROP -dest 192.168.0.0/16",
        "OUT DROP -dest 100.64.0.0/10",
        "OUT DROP -dest 169.254.0.0/16",
    ]


def test_no_mode_opens_any_inbound_port():
    for mode in ("build", "locked"):
        r = policy(mode)
        assert r.returncode == 0 and rules(r.stdout)                 # rendered, not vacuously empty
        assert not [x for x in rules(r.stdout) if x.startswith("IN ")]


def test_endpoints_come_from_the_environment_and_are_validated():
    r = policy("locked", WF_ROUTER="192.168.6.10", WF_WORKERS="172.16.10.1")
    assert "OUT ACCEPT -p tcp -dest 192.168.6.10 -dport 8000" in rules(r.stdout)
    assert len([x for x in rules(r.stdout) if "8090" in x]) == 1
    for bad in ({"WF_ROUTER": "192.168.6.0/24"}, {"WF_WORKERS": "any"}, {"WF_ROUTER": "x; rm -rf /"}):
        assert policy("locked", **bad).returncode != 0, bad


def test_an_unknown_mode_is_refused():
    assert policy("open").returncode != 0 and policy("").returncode != 0


# ---- wf-sandbox-net.sh with fake `ip` and `iptables` ---------------------------------------------

FAKE_IP = r'''#!/bin/sh
# fake ip: state in $FAKE_STATE/addrs ("<addr> <dev>" per line)
S="$FAKE_STATE/addrs"; touch "$S"
case "$1 $2" in
  "addr show") grep " ${4:-$3}\$" "$S" | sed 's/^\([^ ]*\) .*/    inet \1 scope global/' ;;
  "addr add")  echo "$3 $5" >> "$S" ;;
  "addr del")  grep -v "^$3 $5\$" "$S" > "$S.t"; mv "$S.t" "$S" ;;
esac
'''
FAKE_IPTABLES = r'''#!/bin/sh
# fake iptables: nat rules in $FAKE_STATE/nat, one per line (args after -A/-I/-C/-D and the chain)
S="$FAKE_STATE/nat"; touch "$S"
shift 2                                  # -t nat
op="$1"; shift
[ "$op" = "-I" ] && [ "$2" = "1" ] && { chain="$1"; shift 2; set -- "$chain" "$@"; }
rule="$*"
case "$op" in
  -C) grep -qxF "$rule" "$S" ;;
  -A|-I) echo "$rule" >> "$S" ;;
  -D) grep -vxF "$rule" "$S" > "$S.t"; mv "$S.t" "$S" ;;
esac
'''


@pytest.fixture
def fakes(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("ip", FAKE_IP), ("iptables", FAKE_IPTABLES)):
        p = bindir / name
        p.write_text(body, newline="\n")
        p.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    env = dict(os.environ, FAKEBIN=bindir.as_posix(), FAKE_STATE=state.as_posix(),
               WF_SCRIPT=os.path.join(FILES, "wf-sandbox-net.sh").replace("\\", "/"))
    return env, state


def net(env, cmd):
    # Prepend the fakes inside bash: a Windows-style PATH passed in the environment breaks Git Bash.
    prog = ('fb="$FAKEBIN"; command -v cygpath >/dev/null && fb="$(cygpath -u "$FAKEBIN")"; '
            'export PATH="$fb:$PATH"; bash "$WF_SCRIPT" ' + cmd)
    return subprocess.run([BASH, "-c", prog], capture_output=True, text=True, env=env)


def test_add_is_idempotent_and_check_reports_it(fakes):
    env, state = fakes
    assert net(env, "check").returncode != 0
    assert net(env, "add").returncode == 0
    assert net(env, "add").returncode == 0                      # second add changes nothing
    assert (state / "addrs").read_text().splitlines() == ["192.168.6.79/24 vmbr0"]
    assert (state / "nat").read_text().splitlines() == [
        "POSTROUTING -s 10.79.0.0/24 -o vmbr0 -j SNAT --to-source 192.168.6.79"]
    assert net(env, "check").returncode == 0


def test_del_removes_both(fakes):
    env, state = fakes
    net(env, "add")
    assert net(env, "del").returncode == 0
    assert (state / "addrs").read_text() == "" and (state / "nat").read_text() == ""
    assert net(env, "check").returncode != 0


def test_net_refuses_an_unknown_command(fakes):
    env, _ = fakes
    assert net(env, "flush").returncode != 0


def test_closed_mode_denies_everything_both_ways():
    # Final review I2: VM 176 must never boot without a policy; 74 writes `closed` as soon as it exists.
    r = policy("closed")
    assert r.returncode == 0, r.stderr
    assert options(r.stdout) == {"enable": "1", "policy_in": "DROP", "policy_out": "DROP"}
    assert rules(r.stdout) == []
