"""End-to-end: real gitstore/overlay/checks/resultgate/runner, stand-in opencode only."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.delegate import checks, gitstore, jobs, overlay, resultgate, runner, service
from scripts.delegate.config import Config
from scripts.delegate.ledger import Ledger
from scripts.delegate.lease import GpuLease

STUB = os.path.join(os.path.dirname(__file__), "stub_opencode.py")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


def _stub_runner(scenario):
    def spawn(cmd, **kw):
        dest = cmd[cmd.index("--dir") + 1]
        return subprocess.Popen([sys.executable, STUB, "--dir", dest, scenario], **kw)
    return SimpleNamespace(
        run_opencode=lambda cfg, dest, prompt, **kw: runner.run_opencode(
            cfg, dest, prompt, spawn=spawn, **kw))


def _build(tmp_path, scenario):
    parent = tmp_path / "roots"
    repo = parent / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    overlay_dir = tmp_path / "overlay"
    (overlay_dir / "agent").mkdir(parents=True)
    (overlay_dir / "agent" / "delegate.md").write_text("agent\n")
    cfg = Config(bearer_token="b", router_token="r", allowed_repo_roots=(str(parent),),
                 jobs_dir=str(tmp_path / "jobs"), ledger_path=str(tmp_path / "ledger.jsonl"),
                 lease_path=str(tmp_path / "gpu.lock"), overlay_dir=str(overlay_dir),
                 opencode_exe="unused", job_timeout_s=60)
    deps = SimpleNamespace(lease=GpuLease(cfg.lease_path), gitstore=gitstore,
                           overlay=overlay, runner=_stub_runner(scenario), checks=checks,
                           resultgate=resultgate, ledger=Ledger(cfg.ledger_path))
    store = jobs.JobStore(cfg, deps)
    ledger = Ledger(cfg.ledger_path)
    return cfg, repo, store, ledger, service.AgenticTools(cfg, store, ledger)


def test_full_pipeline_success_then_accepted_review(tmp_path):
    cfg, repo, store, ledger, tools = _build(tmp_path, "write")
    jid = tools.submit_task(task="add newfile.txt", repo=str(repo), checks=[],
                            task_type="e2e")["job_id"]
    store.join()
    res = tools.result(jid)
    assert res["status"] == "done", res
    assert "newfile.txt" in res["diff"] and "made by stub" in res["diff"]
    assert res["gate_reasons"] == [] and res["flagged"] == []
    assert "SUMMARY" in res["summary"]
    assert store.get(jid)["gate"]["rejected"] is False
    assert (repo / "a.txt").read_text() == "a\n" and not (repo / "newfile.txt").exists()
    work = os.path.join(cfg.jobs_dir, jid, "work")
    assert os.path.isdir(work)
    assert tools.record_review(jid, "accepted", claude_tokens=1234, fix_lines=0, cause="") == {"ok": True}
    rows = [r for r in ledger.read_all() if r.get("delegated") is True]
    assert len(rows) == 1 and rows[0]["verdict"] == "accepted" and rows[0]["job_id"] == jid
    assert rows[0]["task_type"] == "e2e" and rows[0]["kind"] == "review"
    assert rows[0]["claude_tokens"] == 1234 and rows[0]["local_tokens"] is not None
    assert "duration_s" in rows[0]
    assert not os.path.exists(os.path.join(cfg.jobs_dir, jid))


def test_failed_run_is_recorded_and_never_merged(tmp_path):
    cfg, repo, store, ledger, tools = _build(tmp_path, "exit1")
    jid = tools.submit_task(task="x", repo=str(repo), checks=[])["job_id"]
    store.join()
    res = tools.result(jid)
    assert res["status"] == "failed", res
    assert not (repo / "newfile.txt").exists() and (repo / "a.txt").read_text() == "a\n"
    st = store.get(jid)
    assert st["exit_code"] == 3 and "stub failure" in st["stderr_tail"]
    # The worker does not touch the measurement ledger; the job state file is the record.
    # The single measurement row per task is produced only by record_review/record_direct.
    assert ledger.read_all() == []


def test_rejected_run_fails(tmp_path):
    _, repo, store, _, tools = _build(tmp_path, "rejected")
    jid = tools.submit_task(task="x", repo=str(repo), checks=[])["job_id"]
    store.join()
    assert tools.result(jid)["status"] == "failed"
    assert store.get(jid)["rejected"] is True
