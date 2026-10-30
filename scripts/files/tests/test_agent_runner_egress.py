"""agent_runner_egress: per-repo sandbox egress table -> sandbox user, systemd IPAddressAllow list, nftables rules."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

import agent_runner_egress as eg  # noqa: E402

CONF = """# repo      user        allowed tcp endpoints (IPv4:port)
SDG-RUST    agent-rust  10.60.40.10:80 10.60.20.12:2022
SDG-ARMA3   agent-arma  10.60.40.10:22
"""
UIDS = {"agent-rust": 990, "agent-arma": 991}


def test_parse_reads_users_and_endpoints():
    t = eg.parse(CONF)
    assert t["SDG-RUST"] == {"user": "agent-rust", "endpoints": [("10.60.40.10", 80), ("10.60.20.12", 2022)]}
    assert t["SDG-ARMA3"] == {"user": "agent-arma", "endpoints": [("10.60.40.10", 22)]}


@pytest.mark.parametrize("bad", [
    "SDG-RUST agent-rust example.com:80\n",          # names need DNS; IP literals only
    "SDG-RUST agent-rust 10.60.40.10\n",             # port required
    "SDG-RUST agent-rust 10.60.40.10:0\n",
    "SDG-RUST agent-rust 10.60.40.10:70000\n",
    "SDG-RUST agent-rust 0.0.0.0/0:443\n",           # no ranges
    "SDG-RUST runner 10.60.40.10:80\n",              # must be an agent-* user
    "SDG-RUST agent-rust 10.60.40.10:80\nSDG-RUST agent-x 1.2.3.4:5\n",   # duplicate repo
    "SDG-RUST agent-rust 10.60.40.10:80\nSDG-ARMA3 agent-rust 1.2.3.4:5\n",  # shared user
    "SDG RUST agent-rust 10.60.40.10:80\n",
])
def test_parse_rejects_bad_lines(bad):
    with pytest.raises(ValueError):
        eg.parse(bad)


def test_ip_allow_is_loopback_literals_router_and_the_repo_ips():
    t = eg.parse(CONF)
    # never "localhost": that includes systemd-resolved's stub 127.0.0.53, a DNS path out (probe 2026-10-30)
    assert eg.ip_allow(t, "SDG-RUST") == ["127.0.0.1", "::1", "192.168.6.153", "10.60.40.10", "10.60.20.12"]
    assert eg.ip_allow(t, "SDG-ARMA3") == ["127.0.0.1", "::1", "192.168.6.153", "10.60.40.10"]


def test_unknown_repo_raises():
    with pytest.raises(KeyError):
        eg.ip_allow(eg.parse(CONF), "SDG-IAC")


def test_nft_rules_are_per_uid_and_port_and_end_in_reject():
    nft = eg.render_nft(eg.parse(CONF), UIDS.__getitem__)
    assert "table inet agent_egress" in nft
    assert "meta skuid { 990, 991 } jump agent_out" in nft
    assert "ip daddr 192.168.6.153 tcp dport 8000 accept" in nft
    assert "meta skuid 990 ip daddr 10.60.40.10 tcp dport 80 accept" in nft
    assert "meta skuid 990 ip daddr 10.60.20.12 tcp dport 2022 accept" in nft
    assert "meta skuid 991 ip daddr 10.60.40.10 tcp dport 22 accept" in nft
    assert "meta skuid 990 ip daddr 10.60.40.10 tcp dport 22" not in nft
    assert 'oifname "lo" ip daddr 127.0.0.1 accept' in nft and "127.0.0.53" not in nft
    body = nft.split("chain agent_out", 1)[1]
    assert body.rstrip().splitlines()[-3].strip().startswith("reject")


def test_nft_is_idempotent_to_load():
    nft = eg.render_nft(eg.parse(CONF), UIDS.__getitem__)
    # declare-then-delete-then-create, so `nft -f` replaces the table every time instead of appending
    assert nft.index("table inet agent_egress\n") < nft.index("delete table inet agent_egress") \
        < nft.index("table inet agent_egress {")


def test_cli_user_and_ipallow(tmp_path):
    conf = tmp_path / "egress.conf"
    conf.write_text(CONF)
    run = lambda *a: subprocess.run([sys.executable, os.path.join(HERE, "..", "agent_runner_egress.py"),
                                     "--conf", str(conf), *a], capture_output=True, text=True)
    assert run("user", "SDG-ARMA3").stdout.strip() == "agent-arma"
    assert run("ip-allow", "SDG-ARMA3").stdout.split() == ["127.0.0.1", "::1", "192.168.6.153", "10.60.40.10"]
    assert run("users").stdout.split() == ["agent-rust", "agent-arma"]
    r = run("user", "SDG-IAC")
    assert r.returncode != 0 and "SDG-IAC" in r.stderr
