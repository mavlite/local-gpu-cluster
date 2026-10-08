#!/usr/bin/env python3
"""Boundary proof for the workforce sandbox VM (workforce spec §5.2, §10 W0; Plan B).

Run inside the sandbox as root, after the firewall is in `locked` mode. Prints one JSON report and
exits 1 if any check fails. Stdlib only.

Network checks: the router's chat port and the three CPU workers must be reachable; everything else
in spec §5.2 must NOT be. A denied destination passes only on a timeout (silently dropped) or an
`unreachable` error -- "connection refused" means the packet reached the destination, so the
firewall did not stop it; "unreachable" can come from the VM's own routing, and a PVE DROP is always a
timeout, so only a timeout passes. Every probe distinguishes "blocked" from "the probe itself broke"
(security review): a broken probe reports an error, which fails. The router's /metrics must answer 403: the sandbox is SNATed to its own
address (192.168.6.79), not the host's, which the router allowlists for /metrics.

Agent checks (Plan C Review Focus 1-2): an agent user cannot read bundles, run records or the
baseline git, cannot write another agent's workspace, a systemd-scope kill reaches a child the agent
detached with setsid, and the grader container has no network.
"""
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROUTER, ROUTER_PORT = os.environ.get("WF_ROUTER", "192.168.6.153"), int(os.environ.get("WF_ROUTER_PORT", "8000"))
WORKERS = os.environ.get("WF_WORKERS", "172.16.10.205 172.16.10.206 172.16.10.207").split()
WORKER_PORT = int(os.environ.get("WF_WORKER_PORT", "8090"))
PREFIX = os.environ.get("WF_USER_PREFIX", "wf")
ROOT = os.environ.get("WF_ROOT", "/srv/wf")
GRADER = os.environ.get("WF_GRADER_IMAGE", "wf-grader:1")
TIMEOUT = float(os.environ.get("WF_PROOF_TIMEOUT", "4"))
# The CPU workers only run during lab windows. A pre-window proof sets WF_PROOF_WORKERS=skip: the
# worker checks are then reported as skipped -- never as passed -- and the full proof is repeated at
# the start of every measured window.
SKIP_WORKERS = os.environ.get("WF_PROOF_WORKERS") == "skip"

_UNREACHABLE = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM}
IPV6_FLAG = "/proc/sys/net/ipv6/conf/all/disable_ipv6"


# ---- probes ---------------------------------------------------------------------------------------
def tcp(host, port, timeout=TIMEOUT, family=socket.AF_INET):
    """open | refused | timeout | unreachable"""
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return "open"
    except socket.timeout:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return "unreachable" if e.errno in _UNREACHABLE else f"error: {e.errno}"
    finally:
        s.close()


def http_status(url, timeout=TIMEOUT):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        return f"error: {getattr(e, 'reason', e)}"


def udp_dns(server, timeout=TIMEOUT):
    """answered | timeout | unreachable -- one A query for example.com."""
    query = bytes.fromhex("abcd01000001000000000000076578616d706c6503636f6d0000010001")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(query, (server, 53))
        s.recvfrom(512)
        return "answered"
    except socket.timeout:
        return "timeout"
    except OSError as e:
        return "unreachable" if e.errno in _UNREACHABLE else f"error: {e.errno}"
    finally:
        s.close()


def resolve(name):
    try:
        socket.getaddrinfo(name, 443)
        return "resolved"
    except OSError:
        return "failed"


def ipv6():
    """disabled | enabled -- IPv6 must be switched off in the guest (the policy is IPv4-only)."""
    try:
        with open(IPV6_FLAG) as f:
            return "disabled" if f.read().strip() == "1" else "enabled"
    except OSError as e:
        return f"error: {e}"


def run_as(user, argv):
    """denied | allowed | error -- runs argv as `user` (runuser, from root). "denied" needs the user to
    exist and the failure to be a permission error; any other failure is an error, never a pass."""
    if subprocess.run(["id", "-u", user], capture_output=True, text=True).returncode != 0:
        return f"error: no such user {user}"
    r = subprocess.run(["runuser", "-u", user, "--", *argv], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return "allowed"
    if "Permission denied" in (r.stderr or ""):
        return "denied"
    return f"error: rc {r.returncode}: {(r.stderr or '').strip()[:200]}"


def scope_kill(user):
    """killed | survived -- a setsid-detached child of an agent scope must die with the scope."""
    unit = f"wfproof{os.getpid()}"
    subprocess.Popen(["systemd-run", "--scope", "--quiet", f"--unit={unit}", f"--uid={user}", f"--gid={user}",
                      "--", "bash", "-c", "setsid nohup sleep 977 >/dev/null 2>&1 & exec sleep 977"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    before = subprocess.run(["pgrep", "-u", user, "-f", "sleep 977"], capture_output=True, text=True).stdout.split()
    subprocess.run(["systemctl", "kill", "--signal=SIGKILL", f"{unit}.scope"], capture_output=True)
    subprocess.run(["systemctl", "stop", f"{unit}.scope"], capture_output=True)
    time.sleep(2)
    after = subprocess.run(["pgrep", "-u", user, "-f", "sleep 977"], capture_output=True, text=True).stdout.split()
    if len(before) < 2:
        return f"error: expected 2 sleeps before the kill, saw {len(before)}"
    return "killed" if not after else "survived"


def grader_network():
    """no-network | network | error -- the container itself must run and report the network error."""
    if subprocess.run(["docker", "image", "inspect", GRADER], capture_output=True).returncode != 0:
        return f"error: grader image {GRADER} not present"
    code = ("import socket\n"
            "try:\n"
            f"    socket.create_connection(('{ROUTER}', {ROUTER_PORT}), 3)\n"
            "    print('CONNECTED')\n"
            "except OSError:\n"
            "    print('NONET')\n")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", GRADER, "python3", "-c", code],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return f"error: docker run rc {r.returncode}"
    if "NONET" in r.stdout:
        return "no-network"
    return "network" if "CONNECTED" in r.stdout else "error: no verdict from the container"


# ---- fixtures for the agent checks ----------------------------------------------------------------
def _fixtures():
    """Private harness files plus one agent workspace, laid out like a real run."""
    base = os.path.join(ROOT, "runs", ".proof")
    bundle = os.path.join(ROOT, "bundles", ".proof")
    for d in (os.path.join(base, "tasks", "t", "git"), os.path.join(base, "agent", "t", "ws"), bundle):
        os.makedirs(d, exist_ok=True)
    os.chmod(os.path.join(base, "tasks"), 0o700)
    with open(os.path.join(bundle, "secret"), "w") as f:
        f.write("hidden test\n")
    os.chmod(os.path.join(bundle, "secret"), 0o600)
    with open(os.path.join(base, "tasks", "t", "git", "HEAD"), "w") as f:
        f.write("ref: refs/heads/main\n")
    ws = os.path.join(base, "agent", "t", "ws")
    shutil.chown(ws, f"{PREFIX}-impl-1", f"{PREFIX}-impl-1")
    os.chmod(ws, 0o700)
    return base, bundle, ws


def _cleanup():
    shutil.rmtree(os.path.join(ROOT, "runs", ".proof"), ignore_errors=True)
    shutil.rmtree(os.path.join(ROOT, "bundles", ".proof"), ignore_errors=True)


# ---- the table -------------------------------------------------------------------------------------
def checks():
    impl1, impl2 = f"{PREFIX}-impl-1", f"{PREFIX}-impl-2"
    base = os.path.join(ROOT, "runs", ".proof")
    bundle = os.path.join(ROOT, "bundles", ".proof")
    ws = os.path.join(base, "agent", "t", "ws")

    def deny(name, host, port):
        return {"name": name, "expect": "deny", "probe": lambda: tcp(host, port)}

    table = [{"name": "router chat port", "expect": "allow", "probe": lambda: tcp(ROUTER, ROUTER_PORT)},
             {"name": "router /metrics without a key", "expect": 403,
              "probe": lambda: http_status(f"http://{ROUTER}:{ROUTER_PORT}/metrics")}]
    if SKIP_WORKERS:
        table += [{"name": f"worker {w}", "expect": "skip", "probe": lambda: "skip"} for w in WORKERS]
    else:
        table += [{"name": f"worker {w}", "expect": "allow", "probe": (lambda w=w: tcp(w, WORKER_PORT))}
                  for w in WORKERS]
    table += [
        deny("internet https", "1.1.1.1", 443),
        {"name": "internet dns", "expect": "deny", "probe": lambda: udp_dns("9.9.9.9")},
        {"name": "name resolution", "expect": "failed", "probe": lambda: resolve("deb.debian.org")},
        deny("lan admin ui", "192.168.6.1", 80),
        deny("proxmox host ssh", "192.168.6.175", 22),
        deny("proxmox host ui", "192.168.6.175", 8006),
        deny("sandbox gateway (host)", "10.79.0.254", 22),
        deny("nested lab host", "10.50.10.1", 22),
        deny("router ssh", ROUTER, 22),
        deny("llama-server direct", "192.168.6.151", 8080),
        deny("anythingllm", "192.168.6.154", 3001),
        deny("mcp lxc", "192.168.6.155", 3128),
        deny("memory vault", "192.168.6.223", 3005),
        deny("workstation", "192.168.6.226", 22),
        deny("worker ssh", WORKERS[0], 22),
        deny("vcf lab gateway", "172.16.10.1", 443),
        deny("lan gateway 192.168.6.11", "192.168.6.11", 80),
        {"name": "ipv6", "expect": "disabled", "probe": ipv6},
        {"name": "agent cannot read bundles", "expect": "denied",
         "probe": lambda: run_as(impl1, ["cat", os.path.join(bundle, "secret")])},
        {"name": "agent cannot list run records", "expect": "denied",
         "probe": lambda: run_as(impl1, ["ls", os.path.join(base, "tasks")])},
        {"name": "agent cannot read the baseline git", "expect": "denied",
         "probe": lambda: run_as(impl1, ["cat", os.path.join(base, "tasks", "t", "git", "HEAD")])},
        {"name": "agents cannot write each other's workspace", "expect": "denied",
         "probe": lambda: run_as(impl2, ["touch", os.path.join(ws, "intruder")])},
        {"name": "scope kill reaches a detached child", "expect": "killed", "probe": lambda: scope_kill(impl1)},
        {"name": "grader container has no network", "expect": "no-network", "probe": grader_network},
    ]
    return table


def passes(expect, got):
    if expect == "deny":
        return got == "timeout"
    if expect == "allow":
        return got == "open"
    return got == expect


def run(table):
    results, failed, skipped = [], [], []
    for c in table:
        try:
            got = c["probe"]()
        except Exception as e:                       # a broken probe is a failed check, never a crash
            got = f"error: {type(e).__name__}: {e}"
        if c["expect"] == "skip" and got == "skip":
            results.append({"check": c["name"], "expect": "skip", "got": "skip", "pass": None})
            skipped.append(c["name"])
            continue
        ok = passes(c["expect"], got)
        results.append({"check": c["name"], "expect": c["expect"], "got": got, "pass": ok})
        if not ok:
            failed.append(c["name"])
    return {"passed": len(results) - len(failed) - len(skipped), "failed": failed, "skipped": skipped,
            "results": results}


def main():
    if os.geteuid() != 0:
        sys.exit("run as root inside the sandbox VM")
    _fixtures()
    try:
        report = run(checks())
    finally:
        _cleanup()
    print(json.dumps(report, indent=1))
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
