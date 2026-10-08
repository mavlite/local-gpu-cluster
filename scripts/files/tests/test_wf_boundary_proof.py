"""The sandbox boundary proof (workforce spec §10 W0; Plan B). Runs inside the sandbox VM as root."""
import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import wf_boundary_proof as proof  # noqa: E402


def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    threading.Thread(target=lambda: [s.accept() for _ in range(4)], daemon=True).start()
    return s, s.getsockname()[1]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_tcp_classifies_open_and_refused():
    s, port = listener()
    assert proof.tcp("127.0.0.1", port, timeout=2) == "open"
    s.close()
    # Windows retries a refused localhost SYN for ~2 s before reporting it; Linux refuses at once.
    assert proof.tcp("127.0.0.1", free_port(), timeout=6) == "refused"


def test_tcp_classifies_a_silent_drop_as_timeout():
    assert proof.tcp("192.0.2.1", 9, timeout=1) in ("timeout", "unreachable")      # TEST-NET-1


def test_a_deny_passes_only_on_a_timeout():
    # "refused" means the packet reached the destination. "unreachable" can come from the VM's own
    # routing (no default route) rather than the firewall: a PVE DROP is always a timeout.
    # (Security review: unreachable passed vacuously.)
    assert proof.passes("deny", "timeout")
    assert not proof.passes("deny", "unreachable")
    assert not proof.passes("deny", "refused") and not proof.passes("deny", "open")
    assert proof.passes("allow", "open") and not proof.passes("allow", "timeout")
    assert proof.passes(403, 403) and not proof.passes(403, 200)


def test_the_check_table_covers_every_row_of_spec_5_2():
    checks = proof.checks()
    names = {c["name"] for c in checks}
    expected_allow = {"router chat port", "worker 172.16.10.205", "worker 172.16.10.206", "worker 172.16.10.207"}
    assert expected_allow <= names
    allow = {c["name"] for c in checks if c["expect"] == "allow"}
    assert allow == expected_allow                                  # nothing else may be reachable
    for must_deny in ("internet https", "internet dns", "name resolution", "lan admin ui", "proxmox host ssh",
                      "proxmox host ui", "sandbox gateway (host)", "nested lab host", "router ssh",
                      "llama-server direct", "anythingllm", "mcp lxc", "memory vault", "workstation",
                      "worker ssh", "vcf lab gateway", "lan gateway 192.168.6.11"):
        assert must_deny in names, must_deny
    assert any(c["name"] == "router /metrics without a key" and c["expect"] == 403 for c in checks)
    for agent_check in ("agent cannot read bundles", "agent cannot list run records",
                        "agent cannot read the baseline git", "agents cannot write each other's workspace",
                        "scope kill reaches a detached child", "grader container has no network"):
        assert agent_check in names, agent_check


def test_report_counts_failures_and_never_raises_on_a_broken_probe():
    def boom():
        raise OSError("probe exploded")

    table = [{"name": "ok", "expect": "deny", "probe": lambda: "timeout"},
             {"name": "bad", "expect": "deny", "probe": lambda: "open"},
             {"name": "broken", "expect": "allow", "probe": boom}]
    rep = proof.run(table)
    assert rep["passed"] == 1 and rep["failed"] == ["bad", "broken"]
    assert [r["got"] for r in rep["results"]][2].startswith("error: OSError")


def test_worker_checks_can_be_skipped_before_a_window_but_are_reported_as_skipped(monkeypatch):
    # The CPU workers only run in lab windows; a pre-window proof marks them skipped, never passed.
    monkeypatch.setattr(proof, "SKIP_WORKERS", True)
    table = {c["name"]: c for c in proof.checks()}
    for w in proof.WORKERS:
        c = table[f"worker {w}"]
        assert c["expect"] == "skip" and c["probe"]() == "skip"
    rep = proof.run([table["worker 172.16.10.205"]])
    assert rep["skipped"] == ["worker 172.16.10.205"] and rep["failed"] == [] and rep["passed"] == 0



def test_ipv6_must_be_disabled_not_merely_unconnectable(monkeypatch, tmp_path):
    table = {c["name"]: c for c in proof.checks()}
    assert table["ipv6"]["expect"] == "disabled"
    flag = tmp_path / "disable_ipv6"
    monkeypatch.setattr(proof, "IPV6_FLAG", str(flag))
    flag.write_text("1\n")
    assert proof.ipv6() == "disabled"
    flag.write_text("0\n")
    assert proof.ipv6() == "enabled"


def test_run_as_needs_a_positive_control_and_a_permission_error(monkeypatch):
    # Security review: any non-zero rc (user missing, runuser missing) counted as "denied".
    calls = []

    class R:
        def __init__(self, rc, err=""):
            self.returncode, self.stderr, self.stdout = rc, err, ""

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["id", "-u"]:
            return R(1, "no such user")
        return R(1, "cat: x: Permission denied")
    monkeypatch.setattr(proof.subprocess, "run", fake_run)
    assert proof.run_as("ghost", ["cat", "x"]).startswith("error")          # user missing
    monkeypatch.setattr(proof.subprocess, "run", lambda argv, **kw: R(0) if argv[:2] == ["id", "-u"]
                        else (R(1, "runuser: command not found") if "cat" in argv else R(0)))
    assert proof.run_as("wf-impl-1", ["cat", "x"]).startswith("error")      # failed, but not on permissions
    monkeypatch.setattr(proof.subprocess, "run", lambda argv, **kw: R(0) if argv[:2] == ["id", "-u"]
                        else (R(1, "cat: x: Permission denied") if "cat" in argv else R(0)))
    assert proof.run_as("wf-impl-1", ["cat", "x"]) == "denied"


def test_grader_network_needs_the_image_and_a_network_error_inside(monkeypatch):
    # Security review: a missing image (rc 125) or a stopped daemon counted as "no-network".
    class R:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    monkeypatch.setattr(proof.subprocess, "run", lambda argv, **kw: R(1) if argv[:3] == ["docker", "image", "inspect"] else R(0))
    assert proof.grader_network().startswith("error")
    monkeypatch.setattr(proof.subprocess, "run", lambda argv, **kw: R(0) if argv[:3] == ["docker", "image", "inspect"] else R(125))
    assert proof.grader_network().startswith("error")
    monkeypatch.setattr(proof.subprocess, "run", lambda argv, **kw: R(0) if argv[:3] == ["docker", "image", "inspect"] else R(0, "NONET\n"))
    assert proof.grader_network() == "no-network"
    monkeypatch.setattr(proof.subprocess, "run", lambda argv, **kw: R(0) if argv[:3] == ["docker", "image", "inspect"] else R(0, "CONNECTED\n"))
    assert proof.grader_network() == "network"
