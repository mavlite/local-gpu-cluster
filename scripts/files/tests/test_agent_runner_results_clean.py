"""agent-runner-results-clean.service: runs the unit's own ExecStart line against a temp dir, so the test checks
the command that ships, not a copy of it."""
import os
import shlex
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNIT = os.path.join(HERE, "..", "agent-runner-results-clean.service")
TIMER = os.path.join(HERE, "..", "agent-runner-results-clean.timer")
ROOT = "/var/lib/agent-results"
DAY = 86400


def exec_start():
    lines = [ln for ln in open(UNIT, encoding="utf-8") if ln.startswith("ExecStart=")]
    assert len(lines) == 1
    return shlex.split(lines[0].split("=", 1)[1])


def run_on(tmp):
    argv = [tmp if a == ROOT else a for a in exec_start()]
    assert tmp in argv, "ExecStart must name /var/lib/agent-results literally"
    subprocess.run(argv, check=True)


def make(root, name, age_days, files=("report.txt",)):
    d = root / name
    d.mkdir()
    for f in files:
        (d / f).write_text("x")
    t = time.time() - age_days * DAY
    for p in [d / f for f in files] + [d]:
        os.utime(p, (t, t))
    return d


def test_removes_task_dirs_older_than_14_days_and_keeps_newer(tmp_path):
    old = make(tmp_path, "SDG-RUST-3-20261001T000000Z", 20)
    edge = make(tmp_path, "SDG-ARMA3-1-20261017T000000Z", 13)
    new = make(tmp_path, "SDG-ARMA3-1-20261030T174005Z", 0)
    run_on(str(tmp_path))
    assert not old.exists()
    assert edge.exists() and new.exists()


def test_never_removes_the_root_itself_or_recurses_into_young_dirs(tmp_path):
    young = make(tmp_path, "task-young", 1)
    stale_inner = young / "old.log"
    stale_inner.write_text("x")
    t = time.time() - 30 * DAY
    os.utime(stale_inner, (t, t))
    os.utime(young, (time.time(), time.time()))
    run_on(str(tmp_path))
    assert tmp_path.exists() and stale_inner.exists()


def test_unit_is_confined_to_the_results_dir_and_the_timer_is_daily_and_persistent():
    unit = open(UNIT, encoding="utf-8").read()
    assert "ProtectSystem=strict" in unit and f"ReadWritePaths={ROOT}" in unit
    timer = open(TIMER, encoding="utf-8").read()
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
