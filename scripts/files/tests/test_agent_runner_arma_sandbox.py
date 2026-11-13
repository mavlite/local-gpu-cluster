"""The SDG-ARMA3 sandbox wiring (SDG-ARMA3#6): the egress row, the fixed variables agent-run hands a3, and the pinned
tools and mod archives must agree with each other. These read the repo files; nothing here touches the VM."""
import json
import os
import re
import sys

HERE = os.path.dirname(__file__)
FILES = os.path.join(HERE, "..")
sys.path.insert(0, FILES)

import agent_runner_egress as eg  # noqa: E402

ARMA_LAB = {158, 159, 160}


def _read(name):
    with open(os.path.join(FILES, name), encoding="utf-8") as f:
        return f.read()


def _fixed_env(repo):
    """REPO_FIXED_ENV[repo] from agent-run.sh: the single-quoted pieces, joined, split on '|'."""
    pieces = re.findall(r"^REPO_FIXED_ENV\[" + re.escape(repo) + r"\]\+?='([^']*)'$", _read("agent-run.sh"), re.M)
    assert pieces, f"no REPO_FIXED_ENV[{repo}] in agent-run.sh"
    pairs = [kv.split("=", 1) for kv in "".join(pieces).split("|")]
    assert len({k for k, _ in pairs}) == len(pairs), "a variable is set twice"
    return dict(pairs)


def test_arma_egress_is_the_panel_and_the_two_lab_nodes_only():
    t = eg.parse(_read("agent-runner-egress.conf"))
    assert t["SDG-ARMA3"] == {"user": "agent-arma", "endpoints": [
        ("10.60.40.10", 22), ("10.60.40.10", 80), ("10.60.30.12", 2022), ("10.60.20.12", 2022)]}
    # patron-us1 holds only Rust servers: never open to the Arma sandbox
    assert ("10.60.20.11", 2022) in t["SDG-RUST"]["endpoints"]


def test_arma_runs_the_ptero_transport_on_the_lab_servers_only():
    e = _fixed_env("SDG-ARMA3")
    assert (e["SDG_LAB_TRANSPORT"], e["SDG_LAB_LOCK"], e["SDG_LAB_LOCK_HOLDER"], e["SDG_LAB_PANEL"]) == \
        ("ptero", "panel", "runner", "gate")
    assert {int(x) for x in e["SDG_PTERO_SERVERS"].split(",")} == ARMA_LAB
    # the credentials never sit in the fixed table: they come from the root-only env file
    assert not {"SDG_PTERO_API_KEY", "SDG_PTERO_SFTP_USER", "SDG_PTERO_SFTP_PASSWORD"} & set(e)
    run = _read("agent-run.sh")
    assert "[SDG-ARMA3]=/etc/agent-runner/ptero-156.env" in run and "[SDG-ARMA3]=SDG_PTERO_ " in run


def test_every_sftp_node_is_pinned_and_reachable_through_the_egress_table():
    nodes = json.loads(_fixed_env("SDG-ARMA3")["SDG_PTERO_SFTP_NODES"])
    assert sorted(nodes) == ["game-patron-us2", "game-play-us2"]
    allowed = eg.parse(_read("agent-runner-egress.conf"))["SDG-ARMA3"]["endpoints"]
    for host, n in nodes.items():
        ip, port = n["addr"].rsplit(":", 1)
        assert (ip, int(port)) in allowed, host
        assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", n["fp"]), host


def test_the_panel_is_reached_through_the_agent_key_and_its_pinned_host_key():
    e = _fixed_env("SDG-ARMA3")
    assert e["SDG_PANEL_SSH"] == "panel"
    assert e["SDG_PANEL_SSH_KEY"] == "/etc/agent-runner-agent/panel.pub"          # the public half: selects the agent's key
    assert e["SDG_PANEL_KNOWN_HOSTS"] == "/etc/agent-runner-agent/known_hosts_panel"
    conf = _read("agent-runner-panel-ssh.conf")
    assert "Host panel" in conf and e["SDG_PANEL_KNOWN_HOSTS"] in conf and e["SDG_PANEL_SSH_KEY"] in conf


def test_tools_and_mod_folder_match_the_lock_and_writes_stay_in_tmp_or_results():
    e = _fixed_env("SDG-ARMA3")
    lock = json.loads(_read("agent-runner-tools.lock.json"))
    assert e["SDG_ARMA_SQFVM"] == lock["sqfvm"]["install_path"]
    assert e["SDG_ARMA_HEMTT"] == lock["hemtt"]["install_path"]
    assert e["SDG_ARMA_MODS"] == lock["arma_mods"]["install_dir"]
    for k in ("SDG_ARMA_BUILD", "SDG_ARMA_CACHE", "SDG_ARMA_LAB_STATE"):
        assert e[k].startswith("/tmp/"), k
    assert e["SDG_ARMA_RESULTS"] == "/results"
    assert e["SDG_ARMA_SERVERS_SEED"] == "/work/SDG-ARMA3/tools/lab/servers.runner.json"   # read-only, in the checkout
    for v in e.values():
        assert "|" not in v and "'" not in v and "\n" not in v


def test_pinned_downloads_carry_full_hashes_and_plain_file_names():
    lock = json.loads(_read("agent-runner-tools.lock.json"))
    h = lock["hemtt"]
    assert h["url"].startswith("https://github.com/BrettMayson/HEMTT/releases/download/v" + h["version"] + "/")
    assert all(re.fullmatch(r"[0-9a-f]{64}", h[k]) for k in ("sha256", "member_sha256"))
    assert h["install_path"] == f"/opt/sdg-tools/hemtt/{h['version']}/hemtt"
    mods = lock["arma_mods"]
    assert mods["install_dir"].startswith("/opt/sdg-tools/")
    names = [f["file"] for f in mods["files"]]
    assert {"cba_a3-3.19.0.zip", "lambs_danger-2.6.2.zip"} <= set(names) and len(names) == len(set(names))
    for f in mods["files"]:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", f["file"]), f
        assert f["url"].startswith("https://github.com/") and re.fullmatch(r"[0-9a-f]{64}", f["sha256"]), f


def test_rust_fixed_variables_are_unchanged():
    # SDG-RUST is declared inline and must still name only its own five servers
    assert '[SDG-RUST]="SDG_RUST_MAIN=/work/SDG-RUST|SDG_PTERO_SERVERS=152 154 155 156 157|' in _read("agent-run.sh")


def test_the_shared_secrets_file_cannot_override_a_repos_server_list():
    # systemd lets an EnvironmentFile line override --setenv; the fixed table must stay the only source of these two
    assert 'grep -vE "^SDG_PTERO_(SERVERS|SFTP_NODES)="' in _read("agent-run.sh")
