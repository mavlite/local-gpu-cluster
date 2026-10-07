"""Static checks on the gate's shell and PowerShell scripts (they run on machines tests can't reach)."""
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh") or shutil.which("powershell")

# Spec §3 "Worker runtime (shipped config)", verbatim.
SPEC_FLAGS = ("-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 -rtr "
              "--spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs --min-p 0")


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_worker_flags_are_the_spec_string():
    m = re.search(r'^WORKER_FLAGS="([^"]*)"$', read("sh", "worker_serve.sh"), re.M)
    assert m and m.group(1) == SPEC_FLAGS


def test_worker_extra_has_the_mandatory_memory_flags():
    m = re.search(r'^WORKER_EXTRA="([^"]*)"$', read("sh", "worker_serve.sh"), re.M)
    assert m and "-cram 256" in m.group(1) and "-ctx-ckpt 8" in m.group(1)


def test_worker_server_is_keyed_non_thinking_and_single_slot():
    s = read("sh", "worker_serve.sh")
    assert '--api-key-file "$KEY"' in s and "-np 1" in s
    assert """--chat-template-kwargs '{"enable_thinking":false}'""" in s


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("name", ["gate_env.sh", "worker_serve.sh", "polyglot_run.sh", "quality_batch.sh", "quality_pack.sh"])
def test_bash_syntax(name):
    r = subprocess.run([BASH, "-n", os.path.join(ROOT, "sh", name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_lock_rejects_non_ip_before_touching_firewall():
    r = subprocess.run([BASH, os.path.join(ROOT, "sh", "worker_serve.sh"), "lock", "1.2.3.4;rm"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "not an IPv4 address" in r.stderr


@pytest.mark.skipif(PWSH is None, reason="PowerShell not available")
def test_powershell_parses():
    path = os.path.join(ROOT, "gate_guest.ps1")
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{path}', [ref]$null, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
    r = subprocess.run([PWSH, "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_guest_password_never_on_a_command_line_or_from_a_scratch_file():
    s = read("gate_guest.ps1")
    assert "LLM_BENCH_GUEST_PASS" in s and "guest.pass" not in s


def test_guest_bytes_are_not_unrolled_into_object_array():
    # A PowerShell function returning byte[] is unrolled into object[] by the pipeline, so
    # -Exec printed one ASCII code per line and -Fetch could not write the file (seen live
    # 2026-10-05). The content must be normalised to byte[] and returned with the unary comma.
    s = read("gate_guest.ps1")
    body = s[s.index("function Get-GuestBytes"):s.index("switch ($PSCmdlet.ParameterSetName)")]
    assert "return ,[byte[]]" in body


def _render_nft(tmp_path, *args):
    """Run worker_serve.sh with a fake sudo that captures what would be fed to `nft -f -`."""
    out = tmp_path / "ruleset.nft"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "sudo").write_text(
        f'#!/usr/bin/env bash\nif [ "$1" = nft ]; then cat > "{out.as_posix()}"; fi\n', newline="\n")
    (fake / "sudo").chmod(0o755)
    script = os.path.join(ROOT, "sh", "worker_serve.sh").replace("\\", "/")
    p = fake.as_posix()
    cmd = (f'export PATH="$(cygpath -u "{p}" 2>/dev/null || echo "{p}"):$PATH"; '
           f'bash "{script}" ' + " ".join(args))
    r = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out.read_text().splitlines()


# Both forms below passed `nft -c -f` on the Proxmox host (nftables) on 2026-10-05; the previous
# one-line "} }" closing failed there and on the workers with "unexpected '}'".
@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_learn_ruleset_is_the_form_nft_accepted(tmp_path):
    assert _render_nft(tmp_path, "learn") == [
        "table inet gate", "delete table inet gate", "table inet gate {", " chain input {",
        "  type filter hook input priority 0; policy accept;",
        '  tcp dport 8090 ct state new log prefix "gate8090 "', " }", "}"]


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_lock_ruleset_is_the_form_nft_accepted(tmp_path):
    assert _render_nft(tmp_path, "lock", "10.0.0.1", "10.0.0.2") == [
        "table inet gate", "delete table inet gate", "table inet gate {", " chain input {",
        "  type filter hook input priority 0; policy accept;",
        "  tcp dport 8090 ip saddr { 10.0.0.1,10.0.0.2 } accept", "  tcp dport 8090 counter drop",
        " }", "}"]


def _run_worker(tmp_path, *args):
    """Run worker_serve.sh with fake sudo/systemctl/systemd-run/curl that record their argv."""
    rec = tmp_path / "calls.log"
    fake = tmp_path / "fbin"
    fake.mkdir(exist_ok=True)
    key = tmp_path / "worker.key"
    key.write_text("k\n")
    body = ('#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "{rec}"\n'
            'case "$*" in *"show -p MainPID"*) echo 4242;; *"is-active"*) grep -q "^systemd-run " "{rec}"; exit $?;; esac\n'
            'if [ "$(basename "$0")" = sudo ]; then "$@"; exit $?; fi\nexit 0\n')
    for tool in ("sudo", "systemctl", "systemd-run", "curl", "tee", "chown"):
        (fake / tool).write_text(body.format(rec=rec.as_posix()), newline="\n")
        (fake / tool).chmod(0o755)
    p = fake.as_posix()
    env = (f'export GATE_KEY_FILE="{key.as_posix()}" GATE_LOGDIR="{(tmp_path / "log").as_posix()}" '
           f'GATE_PIDFILE="{(tmp_path / "pid").as_posix()}"; ')
    script = os.path.join(ROOT, "sh", "worker_serve.sh").replace("\\", "/")
    cmd = (env + f'export PATH="$(cygpath -u "{p}" 2>/dev/null || echo "{p}"):$PATH"; '
           f'bash "{script}" ' + " ".join(args))
    r = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True)
    return r, (rec.read_text() if rec.exists() else "")


# 2026-10-06: all three workers were OOM-killed; started from GuestOperations the server lived in
# open-vm-tools.service's cgroup, whose default OOMPolicy=stop took VMware Tools down with it.
@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_start_runs_server_in_its_own_memory_capped_unit(tmp_path):
    r, calls = _run_worker(tmp_path, "start")
    assert r.returncode == 0, r.stderr
    run = [c for c in calls.splitlines() if c.startswith("systemd-run ")]
    assert len(run) == 1, calls
    assert "--unit=gate-llama" in run[0] and "--uid=bench" in run[0]
    assert "MemoryMax=30G" in run[0]          # 32 GB worker VMs (amendment 2026-10-06)
    assert SPEC_FLAGS in run[0] and "--api-key-file" in run[0]
    assert "setsid" not in read("sh", "worker_serve.sh")


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_stop_stops_the_unit(tmp_path):
    r, calls = _run_worker(tmp_path, "stop")
    assert r.returncode == 0, r.stderr
    assert "systemctl stop gate-llama" in calls


# v2 experiment (2026-10-07): the CPU workers looped with presence_penalty 0 while the GPU alias
# (router-injected Qwen preset, presence_penalty 1.5) did not. Workers now run the same preset.
def test_worker_runs_qwen_nonthinking_preset_including_presence_penalty():
    m = re.search(r'^WORKER_EXTRA="([^"]*)"$', read("sh", "worker_serve.sh"), re.M)
    assert m and "--presence-penalty 1.5" in m.group(1)
    assert "--repeat-last-n" not in m.group(1)       # default 64-token window, same as the GPU side
