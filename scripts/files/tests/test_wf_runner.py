"""wf-runner-provision.sh: the Minisforum runner VM image (coding engine phase 2).

Runners grade and execute; they never host agents. They must grade exactly like sandbox VM 176: same pinned
package set (the sandbox venv's constraints.txt is pushed, never re-resolved), same grader Dockerfile and tag.
They are built on the Proxmox side (VLAN 10 has no internet) and templated, so the script must leave no
per-instance identity behind."""
import os
import re
import shutil
import subprocess

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
RUNNER = os.path.join(FILES, "wf-runner-provision.sh")
SANDBOX = os.path.join(FILES, "wf-sandbox-provision.sh")


def src(path=RUNNER):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_syntax():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    assert subprocess.run([bash, "-n", RUNNER], capture_output=True).returncode == 0


@pytest.mark.parametrize("pkg", ["docker.io", "python3-venv", "shellcheck", "ripgrep", "open-vm-tools", "cloud-init", "git"])
def test_installs_the_toolchain(pkg):
    assert re.search(r"apt-get install[^\n]*\b" + re.escape(pkg) + r"\b", src().replace("\\\n", " "))


def test_installs_powershell_and_pester_and_checks_them():
    s = src()
    assert "pwsh" in s and "Pester" in s
    assert re.search(r"pwsh[^\n]*Get-Module[^\n]*Pester", s), "Pester must be verified, not just installed"


def test_python_deps_are_pinned_to_the_sandbox_constraints():
    s = src()
    assert "constraints.txt" in s
    assert re.search(r"pip install[^\n]*-c \"\$PUSH/constraints.txt\"", s), "install must be constrained, not re-resolved"
    assert "pip freeze" not in s, "the runner must not mint its own constraint set"


def test_grader_image_tag_and_dockerfile_match_the_sandbox():
    tag = lambda s: re.search(r'GRADER_IMAGE="\$\{WF_GRADER_IMAGE:-([^}]+)\}"', s).group(1)
    assert tag(src()) == tag(src(SANDBOX))
    assert "wf-grader.Dockerfile" in src()
    assert "--network none" in src(), "the grader self-test runs without a network, as in grading"


def test_runner_carries_no_agent_tooling_and_no_ssh():
    s = src()
    assert "opencode" not in s.lower()
    assert "useradd" not in s
    assert re.search(r"systemctl disable --now ssh", s)


def test_template_cleanup_removes_instance_identity():
    s = src()
    assert "cloud-init clean" in s
    assert re.search(r"(truncate -s ?0|: ?>) ?/etc/machine-id", s)


def test_writes_a_summary_for_the_build_script():
    assert "/etc/wf-runner.json" in src()
