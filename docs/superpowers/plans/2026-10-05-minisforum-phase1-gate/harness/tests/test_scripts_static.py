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
