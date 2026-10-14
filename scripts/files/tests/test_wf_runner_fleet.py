"""wf-runner-fleet.sh: deploy, power, run a command on, and parity-check the three Minisforum runner VMs.

Tests drive a temp copy of the scripts tree (scripts/files/wf-runner-fleet.sh plus a FAKE scripts/79-vm-wf-runner-
image.sh and a stub wf-runner-grade.py), so the script's own-directory resolution is exercised and the real 79
script is never run. govc and openssl are fakes on PATH that log every call (argv and the credential env)."""
import os
import shutil
import subprocess

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
REAL_SCRIPT = os.path.join(FILES, "wf-runner-fleet.sh")

GUEST_PW = "Gu3st-Pa55-xyz"
ESX_PW = "Esx-Pa55-abc"
FAKE_HASH = "$6$fakesalt$" + "H" * 86

FAKE_GOVC = r'''#!/usr/bin/env bash
st="$FAKE_STATE"
printf '%s\n' "$*" >> "$st/calls"
printf 'URL=%s USER=%s PW=%s LOGIN=%s INSECURE=%s PERSIST=%s\n' "${GOVC_URL-}" "${GOVC_USERNAME-}" "${GOVC_PASSWORD-}" \
  "${GOVC_GUEST_LOGIN-}" "${GOVC_INSECURE-}" "${GOVC_PERSIST_SESSION-}" >> "$st/envs"
case "$1" in
  object.collect)
    case "$*" in
      *guest.toolsRunningStatus*) echo guestToolsRunning ;;
      *runtime.powerState*) echo poweredOff ;;
    esac
    exit 0 ;;
  guest.run) echo "ran: ${*: -1}"; exit 0 ;;
  vm.power) exit 0 ;;
esac
exit 0
'''

FAKE_OPENSSL = r'''#!/usr/bin/env bash
cat >/dev/null
echo "$FAKE_HASH"
'''

FAKE_79 = r'''#!/usr/bin/env bash
st="$FAKE_STATE"
printf '%s\n' "$*" >> "$st/calls79"
printf 'HASH=%s PW=%s USER=%s\n' "${WF_RUNNER_GUEST_HASH-}" "${GOVC_PASSWORD-}" "${GOVC_USERNAME-}" >> "$st/envs79"
env | grep -v '^FAKE_GUEST_PW=' | grep -F "$FAKE_GUEST_PW" >> "$st/leaks79"
if [[ -f "$st/fail_name" && " $* " == *" $(cat "$st/fail_name") "* ]]; then echo "boom" >&2; exit 1; fi
echo "79 ok $*"
'''


def find_bash():
    sh = shutil.which("sh")
    if sh is None:
        return None
    cand = os.path.join(os.path.dirname(sh), "bash" + (".exe" if os.name == "nt" else ""))
    return cand if os.path.exists(cand) else shutil.which("bash")


class Fleet:
    def __init__(self, tmp_path):
        self.bash = find_bash()
        if self.bash is None:
            pytest.skip("bash not available")
        self.state = tmp_path / "state"
        self.state.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (("govc", FAKE_GOVC), ("openssl", FAKE_OPENSSL)):
            (bin_dir / name).write_text(body, newline="\n")
            (bin_dir / name).chmod(0o755)
        tree = tmp_path / "tree" / "scripts"
        (tree / "files").mkdir(parents=True)
        self.script = tree / "files" / "wf-runner-fleet.sh"
        shutil.copy(REAL_SCRIPT, self.script) if os.path.exists(REAL_SCRIPT) else None
        (tree / "79-vm-wf-runner-image.sh").write_text(FAKE_79, newline="\n")
        (tree / "files" / "wf-runner-grade.py").write_text("# stub\n", newline="\n")
        self.env = {"PATH": f"{bin_dir.as_posix()}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "FAKE_STATE": self.state.as_posix(), "FAKE_HASH": FAKE_HASH, "FAKE_GUEST_PW": GUEST_PW}
        for k in ("SYSTEMROOT", "TEMP", "TMP"):
            if k in os.environ:
                self.env[k] = os.environ[k]

    def run(self, *args, lines=("junk", "root", ESX_PW, GUEST_PW), eol="\n"):
        stdin = "".join(line + eol for line in lines).encode()      # bytes: no newline translation on Windows
        r = subprocess.run([self.bash, self.script.as_posix(), *args], input=stdin, capture_output=True, env=self.env)
        r.stdout, r.stderr = r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
        return r

    def _read(self, name):
        p = self.state / name
        return p.read_text().splitlines() if p.exists() else []

    def calls(self):
        return self._read("calls")

    def envs(self):
        return self._read("envs")

    def calls79(self):
        return self._read("calls79")


@pytest.fixture
def fleet(tmp_path):
    return Fleet(tmp_path)


def test_syntax():
    bash = find_bash()
    if bash is None:
        pytest.skip("bash not available")
    assert subprocess.run([bash, "-n", REAL_SCRIPT], capture_output=True).returncode == 0


def test_unknown_subcommand_prints_usage_and_makes_no_govc_calls(fleet):
    r = fleet.run("frobnicate")
    assert r.returncode == 2
    assert "usage" in r.stderr.lower()
    assert fleet.calls() == [] and fleet.calls79() == []


@pytest.mark.parametrize("args", [(), ("guest",), ("parity",), ("parity", "only-one")])
def test_missing_args_print_usage_and_exit_2(fleet, args):
    r = fleet.run(*args)
    assert r.returncode == 2 and "usage" in r.stderr.lower()
    assert fleet.calls() == [] and fleet.calls79() == []


@pytest.mark.parametrize("args", [("guest", "echo hi"), ("deploy",)])
def test_missing_guest_password_exits_2_before_any_govc_call(fleet, args):
    r = fleet.run(*args, lines=("junk", "root", ESX_PW))
    assert r.returncode == 2 and "guest password" in r.stderr
    assert fleet.calls() == [] and fleet.calls79() == []


def test_parity_missing_guest_password_exits_2(fleet, tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "patches").mkdir(parents=True)
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    r = fleet.run("parity", run_dir.as_posix(), bundles.as_posix(), lines=("junk", "root", ESX_PW))
    assert r.returncode == 2 and "guest password" in r.stderr
    assert fleet.calls() == []


def test_guest_runs_the_command_on_each_runner_in_order_with_secrets_only_in_env(fleet):
    r = fleet.run("guest", "echo hi")
    assert r.returncode == 0, r.stderr
    runs = [c for c in fleet.calls() if c.startswith("guest.run")]
    assert runs == ["guest.run -vm wf-runner-01 /bin/bash -c echo hi",
                    "guest.run -vm wf-runner-02 /bin/bash -c echo hi",
                    "guest.run -vm wf-runner-03 /bin/bash -c echo hi"]
    envs = [e for e, c in zip(fleet.envs(), fleet.calls()) if c.startswith("guest.run")]
    for e, host in zip(envs, ("172.16.10.145", "172.16.10.143", "172.16.10.141")):
        assert f"URL=https://{host} " in e
        assert f"LOGIN=wfctl:{GUEST_PW} " in e
        assert "INSECURE=1" in e and "PERSIST=false" in e
        assert "USER=root" in e and f"PW={ESX_PW}" in e
    blob = "\n".join([r.stdout, r.stderr, *fleet.calls()])
    assert GUEST_PW not in blob and ESX_PW not in blob


def test_guest_waits_for_tools_before_running(fleet):
    fleet.run("guest", "true")
    calls = fleet.calls()
    first_run = next(i for i, c in enumerate(calls) if c.startswith("guest.run"))
    assert any("guest.toolsRunningStatus" in c for c in calls[:first_run])


def test_deploy_calls_79_once_per_runner_with_hash_and_no_plain_password(fleet):
    r = fleet.run("deploy")
    assert r.returncode == 0, r.stderr
    assert fleet.calls79() == [
        "deploy 172.16.10.145 hyp01-local wf-runner-01 172.16.10.208",
        "deploy 172.16.10.143 hyp02-local wf-runner-02 172.16.10.209",
        "deploy 172.16.10.141 hyp03-local wf-runner-03 172.16.10.210"]
    envs = fleet._read("envs79")
    assert envs == [f"HASH={FAKE_HASH} PW={ESX_PW} USER=root"] * 3
    assert fleet._read("leaks79") == []
    blob = "\n".join([r.stdout, r.stderr, *fleet.calls79()])
    assert GUEST_PW not in blob and ESX_PW not in blob


def test_power_on_with_one_failing_runner_still_attempts_the_others_and_returns_1(fleet):
    (fleet.state / "fail_name").write_text("wf-runner-02\n", newline="\n")
    r = fleet.run("power-on", lines=("junk", "root", ESX_PW))
    assert r.returncode == 1
    assert fleet.calls79() == ["power-on 172.16.10.145 wf-runner-01",
                               "power-on 172.16.10.143 wf-runner-02",
                               "power-on 172.16.10.141 wf-runner-03"]


def test_power_on_succeeds_when_all_runners_do(fleet):
    r = fleet.run("power-on", lines=("junk", "root", ESX_PW))
    assert r.returncode == 0, r.stderr


def test_shutdown_issues_a_guest_shutdown_then_polls_power_state(fleet):
    r = fleet.run("shutdown", lines=("junk", "root", ESX_PW))
    assert r.returncode == 0, r.stderr
    calls = fleet.calls()
    for name in ("wf-runner-01", "wf-runner-02", "wf-runner-03"):
        assert f"vm.power -s {name}" in calls
        assert f"{name}: poweredOff" in r.stdout
    assert calls.index("vm.power -s wf-runner-01") < calls.index("vm.power -s wf-runner-02")
    assert fleet.calls79() == []


def test_crlf_line_endings_on_stdin_are_tolerated(fleet):
    r = fleet.run("guest", "true", eol="\r\n")
    assert r.returncode == 0, r.stderr
    assert all("\r" not in e for e in fleet.envs())