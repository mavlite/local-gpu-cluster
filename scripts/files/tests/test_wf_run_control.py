"""wf-run-control.sh: runs a harness run inside the sandbox VM (Plan D). The host's 76 script pipes the
keys in on stdin; they land in a root-only file that the run's first act deletes, so keys live only in
the harness process environment -- never argv, never a log, never on disk for the run's duration."""
import json
import os
import shutil
import subprocess
import sys
import tarfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "wf-run-control.sh").replace("\\", "/")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

KEYS_T = "WF_ROUTER_KEY=router-secret-1\nWF_WORKER_KEY=worker-secret-2\n"


@pytest.fixture
def guest(tmp_path):
    fb = tmp_path / "bin"
    fb.mkdir()
    calls = tmp_path / "calls.log"
    rec = ('#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "%s"\n' % calls.as_posix())
    (fb / "systemd-run").write_text(rec + "exit 0\n", newline="\n")
    (fb / "systemctl").write_text(rec + '[ -f "$FAKE_ACTIVE" ] && { echo active; exit 0; }; echo inactive; exit 3\n',
                                  newline="\n")
    (fb / "wf-run").write_text(
        rec + '"' + sys.executable.replace(chr(92), '/') + '" -c "import json,os,sys; json.dump({\'argv\': sys.argv[1:], '
        '\'router\': os.environ.get(\'WF_ROUTER_KEY\'), \'worker\': os.environ.get(\'WF_WORKER_KEY\')}, '
        'open(os.environ[\'FAKE_DUMP\'], \'w\'))" "$@"\n', newline="\n")
    for f in fb.iterdir():
        f.chmod(0o755)
    d = {k: tmp_path / k for k in ("runs", "keys", "out")}
    for p in d.values():
        p.mkdir()
    env = dict(os.environ, FAKEBIN=fb.as_posix(), WF_RC_RUNS=d["runs"].as_posix(), WF_RC_KEYS=d["keys"].as_posix(),
               WF_RC_OUT=d["out"].as_posix(), WF_RC_BIN=(fb / "wf-run").as_posix(),
               FAKE_DUMP=(tmp_path / "dump.json").as_posix(), FAKE_ACTIVE=(tmp_path / "active").as_posix())

    def run(args, stdin=""):
        prog = ('fb="$FAKEBIN"; command -v cygpath >/dev/null && fb="$(cygpath -u "$FAKEBIN")"; '
                f'export PATH="$fb:$PATH"; bash "{SCRIPT}" {args}')
        # bytes, not text: on Windows, text-mode stdin turns every LF into CRLF
        r = subprocess.run([BASH, "-c", prog], input=stdin.encode(), capture_output=True, env=env)
        return subprocess.CompletedProcess(r.args, r.returncode, r.stdout.decode(), r.stderr.decode())

    def log():
        return calls.read_text() if calls.exists() else ""

    return run, log, d, tmp_path


def test_start_stores_keys_privately_and_never_in_argv(guest):
    run, log, d, _ = guest
    r = run("start run-1 T", KEYS_T)
    assert r.returncode == 0, r.stderr
    key_file = d["keys"] / "run-1.env"
    assert key_file.read_text() == KEYS_T
    if os.name != "nt":
        assert oct(key_file.stat().st_mode & 0o777) == "0o600"
    starts = [c for c in log().splitlines() if c.startswith("systemd-run ")]
    assert len(starts) == 1 and "--unit=wf-run-run-1" in starts[0] and "_exec run-1 T" in starts[0]
    assert "secret" not in log() + r.stdout + r.stderr


def test_exec_deletes_the_key_file_then_runs_the_harness_with_the_keys_in_its_env(guest):
    run, _, d, tmp = guest
    assert run("start run-1 T", KEYS_T).returncode == 0
    r = run("_exec run-1 T")
    assert r.returncode == 0, r.stderr
    dump = json.loads((tmp / "dump.json").read_text())
    assert not (d["keys"] / "run-1.env").exists()
    assert dump["router"] == "router-secret-1" and dump["worker"] == "worker-secret-2"
    argv = " ".join(dump["argv"])
    assert dump["argv"][:3] == ["run", "--arm", "T"]
    assert f"--out {d['runs'].as_posix()}/run-1" in argv and "--grader docker:wf-grader:1" in argv
    assert "--agent-user-prefix wf" in argv and "--router http://192.168.6.153:8000/v1" in argv
    for n in (205, 206, 207):
        assert f"--worker http://172.16.10.{n}:8090/v1" in argv
    assert "--w1" not in argv


def test_arm_g_needs_no_worker_key_and_gets_no_workers(guest):
    run, _, _, tmp = guest
    assert run("start g-1 G --w1", "WF_ROUTER_KEY=r\n").returncode == 0
    assert run("_exec g-1 G --w1").returncode == 0
    dump = json.loads((tmp / "dump.json").read_text())
    assert "--worker" not in dump["argv"] and "--w1" in dump["argv"] and dump["worker"] is None


@pytest.mark.parametrize("args,stdin,msg", [
    ("start ../x T", KEYS_T, "run id"),
    ("start run-1 X", KEYS_T, "arm"),
    ("start run-1 T", "WF_ROUTER_KEY=r\n", "WF_WORKER_KEY"),
    ("start run-1 G", "", "WF_ROUTER_KEY"),
    ("start run-1 T", KEYS_T + "LD_PRELOAD=/tmp/x.so\n", "unexpected"),
    ("start run-1 T --network", KEYS_T, "option"),
])
def test_start_refuses_bad_input_before_starting_anything(guest, args, stdin, msg):
    run, log, d, _ = guest
    r = run(args, stdin)
    assert r.returncode != 0 and msg in r.stderr, r.stderr
    assert "systemd-run" not in log()
    assert not list(d["keys"].iterdir())


def test_start_refuses_to_reuse_a_run_id(guest):
    run, _, d, _ = guest
    (d["runs"] / "run-1").mkdir()
    r = run("start run-1 T", KEYS_T)
    assert r.returncode != 0 and "exists" in r.stderr


def test_pack_refuses_while_running_then_bundles_the_run(guest):
    run, _, d, tmp = guest
    (d["runs"] / "run-1").mkdir()
    (d["runs"] / "run-1" / "run.json").write_text('{"valid": true}')
    (d["runs"] / "run-1.log").write_text("log\n")
    (tmp / "active").write_text("")
    r = run("pack run-1")
    assert r.returncode != 0 and "still running" in r.stderr
    (tmp / "active").unlink()
    r = run("pack run-1")
    assert r.returncode == 0, r.stderr
    tgz = d["out"] / "run-1.tgz"
    with tarfile.open(tgz) as t:
        names = t.getnames()
    assert "run-1/run.json" in names and "run-1.log" in names
    assert str(tgz.stat().st_size) in r.stdout


def test_pack_refuses_a_run_without_its_record(guest):
    run, _, d, _ = guest
    (d["runs"] / "run-1").mkdir()
    r = run("pack run-1")
    assert r.returncode != 0 and "run.json" in r.stderr


def test_status_reports_the_unit_and_the_record(guest):
    run, _, d, _ = guest
    (d["runs"] / "run-1").mkdir()
    r = run("status run-1")
    assert r.returncode == 0 and "unit: inactive" in r.stdout and "record: absent" in r.stdout
