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
    # curl: record argv, keep the -K config read from stdin (never argv), answer like a 200 in 0.5 s
    (fb / "curl").write_text(rec + 'cat >> "$FAKE_CURL_CFG"; printf "200 0.5"\n', newline="\n")
    for f in fb.iterdir():
        f.chmod(0o755)
    d = {k: tmp_path / k for k in ("runs", "keys", "out", "replay")}
    for p in d.values():
        p.mkdir()
    env = dict(os.environ, FAKEBIN=fb.as_posix(), WF_RC_RUNS=d["runs"].as_posix(), WF_RC_KEYS=d["keys"].as_posix(),
               WF_RC_OUT=d["out"].as_posix(), WF_RC_BIN=(fb / "wf-run").as_posix(), WF_RC_REPLAY=d["replay"].as_posix(),
               FAKE_DUMP=(tmp_path / "dump.json").as_posix(), FAKE_ACTIVE=(tmp_path / "active").as_posix(),
               FAKE_CURL_CFG=(tmp_path / "curl-cfg.txt").as_posix())

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


def test_start_replay_takes_the_router_key_only_and_starts_the_replay_unit(guest):
    # Round 2 §5.1: the offline replay needs no worker key; the unit is named like a run so status/pack work.
    run, log, d, _ = guest
    (d["replay"] / "w3-2-t").mkdir()
    r = run("start-replay rp-1 w3-2-t", "WF_ROUTER_KEY=router-secret-1\n")
    assert r.returncode == 0, r.stderr
    assert (d["keys"] / "rp-1.env").read_text() == "WF_ROUTER_KEY=router-secret-1\n"
    starts = [c for c in log().splitlines() if c.startswith("systemd-run ")]
    assert len(starts) == 1 and "--unit=wf-run-rp-1" in starts[0] and starts[0].endswith("_exec_replay rp-1 w3-2-t")
    assert "secret" not in log() + r.stdout + r.stderr


def test_exec_replay_runs_replay_reviews_on_the_pushed_run_with_the_key_in_its_env(guest):
    run, _, d, tmp = guest
    (d["replay"] / "w3-2-t").mkdir()
    assert run("start-replay rp-1 w3-2-t", "WF_ROUTER_KEY=r\n").returncode == 0
    r = run("_exec_replay rp-1 w3-2-t")
    assert r.returncode == 0, r.stderr
    dump = json.loads((tmp / "dump.json").read_text())
    argv = dump["argv"]
    assert argv[0] == "replay-reviews" and argv[argv.index("--run") + 1] == d["replay"].as_posix() + "/w3-2-t"
    assert argv[argv.index("--out") + 1] == d["runs"].as_posix() + "/rp-1"
    assert "--worker" not in argv and "--agent-user-prefix" in argv and "docker:wf-grader:1" in argv
    assert dump["router"] == "r" and dump["worker"] is None
    assert not (d["keys"] / "rp-1.env").exists()


@pytest.mark.parametrize("args,stdin,msg", [
    ("start-replay rp-1 w3-2-t", KEYS_T, "unexpected"),                 # a worker key has no business here
    ("start-replay rp-1 ../w3", "WF_ROUTER_KEY=r\n", "run id"),
    ("start-replay rp-1 missing", "WF_ROUTER_KEY=r\n", "no such replay source"),
    ("start-replay rp-1 w3-2-t", "", "WF_ROUTER_KEY"),
])
def test_start_replay_refuses_bad_input_before_starting_anything(guest, args, stdin, msg):
    run, log, d, _ = guest
    (d["replay"] / "w3-2-t").mkdir()
    r = run(args, stdin)
    assert r.returncode != 0 and msg in r.stderr, r.stderr
    assert "systemd-run" not in log() and not list(d["keys"].iterdir())


def test_warm_sends_one_keyed_request_to_the_lead_and_each_worker_with_keys_off_argv(guest):
    # Round 2 §5.2: one untimed warm-up per worker and one to the lead before each run's clock starts.
    run, log, _, tmp = guest
    r = run("warm T", KEYS_T)
    assert r.returncode == 0, r.stderr
    curls = [c for c in log().splitlines() if c.startswith("curl ")]
    assert len(curls) == 4 and all("-K -" in c and "secret" not in c for c in curls)
    assert "http://192.168.6.153:8000/v1/chat/completions" in curls[0]
    assert [c.split("http://")[1].split(":")[0] for c in curls[1:]] == ["172.16.10.205", "172.16.10.206", "172.16.10.207"]
    cfg = (tmp / "curl-cfg.txt").read_text()
    assert cfg.count("Authorization: Bearer router-secret-1") == 1 and cfg.count("Authorization: Bearer worker-secret-2") == 3
    assert "secret" not in r.stdout + r.stderr
    assert [l.split()[1] for l in r.stdout.splitlines() if l.startswith("warm ")] == ["lead", "172.16.10.205", "172.16.10.206", "172.16.10.207"]


def test_warm_for_g_and_s_touches_only_the_lead(guest):
    run, log, _, _ = guest
    assert run("warm G", "WF_ROUTER_KEY=r\n").returncode == 0
    assert run("warm S", "WF_ROUTER_KEY=r\n").returncode == 0
    assert len([c for c in log().splitlines() if c.startswith("curl ")]) == 2


def test_start_accepts_arm_s_with_the_router_key_only(guest):
    # Round 2 §5.2: arm S (GPU implementer alone) is a run like G from the controller's point of view.
    run, _, _, tmp = guest
    assert run("start s-1 S", "WF_ROUTER_KEY=r\n").returncode == 0
    assert run("_exec s-1 S").returncode == 0
    dump = json.loads((tmp / "dump.json").read_text())
    assert "--arm" in dump["argv"] and dump["argv"][dump["argv"].index("--arm") + 1] == "S"
    assert "--worker" not in dump["argv"] and dump["worker"] is None


def test_warm_refuses_arm_t_without_a_worker_key(guest):
    run, log, _, _ = guest
    r = run("warm T", "WF_ROUTER_KEY=r\n")
    assert r.returncode != 0 and "WF_WORKER_KEY" in r.stderr and "curl" not in log()


def test_pack_accepts_a_replay_record_in_place_of_run_json(guest):
    # Round 2 §5.1: replay-reviews writes replay.json, not run.json; pack and status must take either.
    run, _, d, _ = guest
    (d["runs"] / "rp-1").mkdir()
    (d["runs"] / "rp-1" / "replay.json").write_text('{"totals": {}}')
    r = run("pack rp-1")
    assert r.returncode == 0, r.stderr
    with tarfile.open(d["out"] / "rp-1.tgz") as t:
        assert "rp-1/replay.json" in t.getnames()
    assert "record: present" in run("status rp-1").stdout


def test_start_replay_passes_reviewer_model_and_worker_options_and_then_takes_a_worker_key(guest):
    # Reviewer-model trial: the candidate reviewer is served by a worker, so the replay needs the worker
    # key too; nothing else about start-replay changes.
    run, log, d, tmp = guest
    (d["replay"] / "w3-2-t").mkdir()
    opts = "--reviewer-model=w1/qwen3.6 --worker=http://172.16.10.205:8090/v1"
    r = run(f"start-replay rp-2 w3-2-t {opts}", KEYS_T)
    assert r.returncode == 0, r.stderr
    assert (d["keys"] / "rp-2.env").read_text() == KEYS_T
    starts = [c for c in log().splitlines() if c.startswith("systemd-run ")]
    assert starts[0].endswith(f"_exec_replay rp-2 w3-2-t {opts}")
    assert run(f"_exec_replay rp-2 w3-2-t {opts}").returncode == 0
    argv = json.loads((tmp / "dump.json").read_text())["argv"]
    assert argv[argv.index("--reviewer-model") + 1] == "w1/qwen3.6" and argv[argv.index("--worker") + 1] == "http://172.16.10.205:8090/v1"
    assert json.loads((tmp / "dump.json").read_text())["worker"] == "worker-secret-2"


@pytest.mark.parametrize("args,stdin,msg", [
    ("start-replay rp-2 w3-2-t --network=host", KEYS_T, "option"),
    ("start-replay rp-2 w3-2-t --reviewer-model=w1/qwen3.6", KEYS_T, "unexpected"),       # no worker: no worker key
    ("start-replay rp-2 w3-2-t --reviewer-model=w1/qwen3.6 --worker=http://w/v1", "WF_ROUTER_KEY=r\n", "WF_WORKER_KEY"),
])
def test_start_replay_options_are_validated(guest, args, stdin, msg):
    run, log, d, _ = guest
    (d["replay"] / "w3-2-t").mkdir()
    r = run(args, stdin)
    assert r.returncode != 0 and msg in r.stderr, r.stderr
    assert "systemd-run" not in log() and not list(d["keys"].iterdir())


def test_start_replay_accepts_the_read_first_option(guest):
    run, log, d, tmp = guest
    (d["replay"] / "w3-2-t").mkdir()
    assert run("start-replay rp-3 w3-2-t --read-first", "WF_ROUTER_KEY=r\n").returncode == 0
    assert run("_exec_replay rp-3 w3-2-t --read-first").returncode == 0
    assert "--read-first" in json.loads((tmp / "dump.json").read_text())["argv"]


def test_start_replay_accepts_the_worker_alias_option(guest):
    run, log, d, tmp = guest
    (d["replay"] / "w3-2-t").mkdir()
    opts = "--reviewer-model=w1/gemma4 --worker=http://172.16.10.205:8090/v1 --worker-alias=gemma4 --read-first"
    assert run(f"start-replay rp-4 w3-2-t {opts}", KEYS_T).returncode == 0
    assert run(f"_exec_replay rp-4 w3-2-t {opts}").returncode == 0
    argv = json.loads((tmp / "dump.json").read_text())["argv"]
    assert argv[argv.index("--worker-alias") + 1] == "gemma4"


def test_start_replay_accepts_the_review_steps_option(guest):
    run, log, d, tmp = guest
    (d["replay"] / "w3-2-t").mkdir()
    assert run("start-replay rp-5 w3-2-t --review-steps=30", "WF_ROUTER_KEY=r\n").returncode == 0
    assert run("_exec_replay rp-5 w3-2-t --review-steps=30").returncode == 0
    argv = json.loads((tmp / "dump.json").read_text())["argv"]
    assert argv[argv.index("--review-steps") + 1] == "30"
    assert run("start-replay rp-6 w3-2-t --review-steps=abc", "WF_ROUTER_KEY=r\n").returncode != 0


def test_start_passes_lead_options_and_a_gpu_lead_needs_the_worker_key(guest):
    # Orchestration trial: the lead on a keyed GPU server takes the worker key, also on arms G/S.
    run, log, d, tmp = guest
    opts = "--lead-url=http://192.168.6.151:8090/v1 --read-first --review-steps=30"
    r = run(f"start l-1 G {opts}", "WF_ROUTER_KEY=r\n")
    assert r.returncode != 0 and "WF_WORKER_KEY" in r.stderr
    assert run(f"start l-2 T {opts}", KEYS_T).returncode == 0
    assert run(f"_exec l-2 T {opts}").returncode == 0
    argv = json.loads((tmp / "dump.json").read_text())["argv"]
    assert argv[argv.index("--lead-url") + 1] == "http://192.168.6.151:8090/v1"
    assert "--read-first" in argv and argv[argv.index("--review-steps") + 1] == "30"
    assert run("start l-3 T --lead-url=file:///etc/passwd", KEYS_T).returncode != 0


def test_quality_options_pass_through_start_and_start_replay(guest):
    run, log, d, tmp = guest
    (d["replay"] / "w3-2-t").mkdir()
    q = "--enforce-read --reviewer-temperature=0.2"
    assert run(f"start-replay rp-9 w3-2-t {q}", "WF_ROUTER_KEY=r\n").returncode == 0
    assert run(f"_exec_replay rp-9 w3-2-t {q}").returncode == 0
    argv = json.loads((tmp / "dump.json").read_text())["argv"]
    assert "--enforce-read" in argv and argv[argv.index("--reviewer-temperature") + 1] == "0.2"
    assert run(f"start q-1 T {q}", KEYS_T).returncode == 0
    assert run(f"_exec q-1 T {q}").returncode == 0
    argv = json.loads((tmp / "dump.json").read_text())["argv"]
    assert "--enforce-read" in argv and argv[argv.index("--reviewer-temperature") + 1] == "0.2"
    assert run("start q-2 T --reviewer-temperature=hot", KEYS_T).returncode != 0


def test_defects_only_passes_through_start_and_start_replay(guest):
    run, log, d, tmp = guest
    (d["replay"] / "w3-2-t").mkdir()
    assert run("start-replay rp-10 w3-2-t --defects-only", "WF_ROUTER_KEY=r\n").returncode == 0
    assert run("_exec_replay rp-10 w3-2-t --defects-only").returncode == 0
    assert "--defects-only" in json.loads((tmp / "dump.json").read_text())["argv"]
    assert run("start d-1 T --defects-only", KEYS_T).returncode == 0
    assert run("_exec d-1 T --defects-only").returncode == 0
    assert "--defects-only" in json.loads((tmp / "dump.json").read_text())["argv"]


def test_review_budget_passes_through_start_and_is_validated(guest):
    run, log, d, tmp = guest
    assert run("start rb-1 T --review-budget=1", KEYS_T).returncode == 0
    assert run("_exec rb-1 T --review-budget=1").returncode == 0
    argv = json.loads((tmp / "dump.json").read_text())["argv"]
    assert argv[argv.index("--review-budget") + 1] == "1"
    for bad in ("--review-budget=0", "--review-budget=abc", "--review-budget=10"):
        assert run(f"start rb-2 T {bad}", KEYS_T).returncode != 0, bad


def test_review_pool_is_arm_g_with_the_worker_key_and_the_guest_worker_list(guest):
    run, _, _, tmp = guest
    assert run("start rp-1 G --review-pool --review-budget=1", "WF_ROUTER_KEY=r\n").returncode != 0   # no worker key
    assert run("start rp-2 S --review-pool", KEYS_T).returncode != 0                                    # not arm G
    assert run("start rp-3 G --review-pool --review-budget=1 --read-first --defects-only", KEYS_T).returncode == 0
    assert run("_exec rp-3 G --review-pool --review-budget=1 --read-first --defects-only").returncode == 0
    dump = json.loads((tmp / "dump.json").read_text())
    argv = dump["argv"]
    assert "--review-pool" in argv and argv.count("--worker") == 3 and dump["worker"]
    assert argv[argv.index("--review-budget") + 1] == "1"


def test_warm_with_review_pool_touches_the_workers_too(guest):
    run, log, _, _ = guest
    r = run("warm G --review-pool", KEYS_T)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("warm ") == 4          # lead + three workers
