"""agent-runner-github-ipset.sh: refresh the [IPSET github] block of VM 178's PVE firewall from api.github.com/meta.

Runs the real script against a file:// meta fixture and a temp firewall file; `PVE_COMPILE_CMD` stands in for
`pve-firewall compile`. Linux/bash only (run on the Proxmox host like the other script tests).
"""
import json
import os
import subprocess

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "agent-runner-github-ipset.sh")

BASE = """[OPTIONS]
enable: 1
policy_out: DROP

[RULES]
OUT ACCEPT -dest +guest/github -p tcp -dport 443

[IPSET github]
"""
META = {
    "api": ["140.82.112.0/20", "192.30.252.0/22", "2a0a:a440::/29"],
    "git": ["140.82.112.0/20", "185.199.108.0/22", "20.201.28.151/32"],
    "web": ["192.30.252.0/22", "4.208.26.197/32"],
    "actions": ["13.64.0.0/16"],          # not ours: must be ignored
}


def run(tmp_path, meta=META, fw=BASE, compile_cmd="true", min_entries="3"):
    m = tmp_path / "meta.json"
    m.write_text(json.dumps(meta))
    f = tmp_path / "178.fw"
    if fw is not None and not f.exists():
        f.write_text(fw)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "META_URL": m.as_uri(), "FW_FILE": str(f),
           "PVE_COMPILE_CMD": compile_cmd, "MIN_ENTRIES": min_entries}
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env)
    return r, f


def entries(text):
    return text.split("[IPSET github]\n", 1)[1].splitlines()


def test_writes_sorted_unique_ipv4_from_api_git_web_only(tmp_path):
    r, f = run(tmp_path)
    assert r.returncode == 0, r.stderr
    got = [e for e in entries(f.read_text()) if not e.startswith("#")]
    # collapsed, de-duplicated, numeric order
    assert got == ["4.208.26.197/32", "20.201.28.151/32", "140.82.112.0/20", "185.199.108.0/22", "192.30.252.0/22"], got
    assert "13.64.0.0/16" not in f.read_text() and "2a0a" not in f.read_text()
    assert f.read_text().startswith(BASE)


def test_second_run_is_unchanged(tmp_path):
    run(tmp_path)
    before = (tmp_path / "178.fw").read_text()
    r, f = run(tmp_path)
    assert r.returncode == 0 and "unchanged" in r.stdout and f.read_text() == before


def test_old_entries_are_replaced(tmp_path):
    (tmp_path / "178.fw").write_text(BASE + "# refreshed earlier\n1.2.3.0/24\n")
    r, f = run(tmp_path)
    assert r.returncode == 0 and "1.2.3.0/24" not in f.read_text()


def test_too_few_entries_aborts_and_keeps_the_file(tmp_path):
    (tmp_path / "178.fw").write_text(BASE + "140.82.112.0/20\n")
    r, f = run(tmp_path, min_entries="50")
    assert r.returncode != 0 and f.read_text() == BASE + "140.82.112.0/20\n"


def test_missing_ipset_header_aborts(tmp_path):
    r, f = run(tmp_path, fw="[OPTIONS]\nenable: 1\n")
    assert r.returncode != 0 and f.read_text() == "[OPTIONS]\nenable: 1\n"


def test_a_section_after_the_ipset_aborts(tmp_path):
    fw = BASE + "[IPSET other]\n10.0.0.0/8\n"
    r, f = run(tmp_path, fw=fw)
    assert r.returncode != 0 and f.read_text() == fw


def test_compile_failure_restores_the_old_file(tmp_path):
    (tmp_path / "178.fw").write_text(BASE + "1.2.3.0/24\n")
    r, f = run(tmp_path, compile_cmd="false")
    assert r.returncode != 0 and f.read_text() == BASE + "1.2.3.0/24\n"


def test_garbage_in_meta_is_rejected(tmp_path):
    bad = dict(META, api=["140.82.112.0/20", "not-an-ip; rm -rf /", "999.1.1.1/8"])
    r, f = run(tmp_path, meta=bad)
    assert r.returncode == 0, r.stderr
    assert "not-an-ip" not in f.read_text() and "999.1.1.1" not in f.read_text()
