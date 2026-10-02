import json
import os
import subprocess
import sys
import time

from scripts.delegate import cli, ids
from scripts.delegate.config import load_config


def _env(tmp_path):
    return {**os.environ, "LOCAL_DELEGATE_JOBS_DIR": str(tmp_path),
            "LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"}


def _cfg(tmp_path):
    return load_config({"LOCAL_DELEGATE_JOBS_DIR": str(tmp_path),
                        "LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"})


def _write(tmp_path, jid, **st):
    with open(tmp_path / f"{jid}.json", "w") as f:
        json.dump({"id": jid, **st}, f)


def test_wait_exits_when_job_terminal(tmp_path):
    jid = ids.new_id()
    _write(tmp_path, jid, status="done")
    r = subprocess.run([sys.executable, "-m", "scripts.delegate.cli", "wait", jid],
                       env=_env(tmp_path), capture_output=True, timeout=15)
    assert r.returncode == 0


def test_wait_polls_until_terminal(tmp_path):
    jid = ids.new_id()
    _write(tmp_path, jid, status="running")
    calls = []

    def sleep(_):
        calls.append(1)
        if len(calls) == 2:
            _write(tmp_path, jid, status="failed")

    assert cli.wait(_cfg(tmp_path), jid, sleep=sleep, poll_s=0) == 0
    assert len(calls) == 2


def test_wait_unknown_job_exits_nonzero(tmp_path):
    assert cli.wait(_cfg(tmp_path), "nope", sleep=lambda _: None, poll_s=0) == 1


def test_reap_prunes_old_terminal_and_abandons_dead_owner(tmp_path):
    old, fresh, dead = ids.new_id(), ids.new_id(), ids.new_id()
    _write(tmp_path, old, status="done")
    _write(tmp_path, fresh, status="done")
    _write(tmp_path, dead, status="running", owner_pid=2**30, owner_started=1.0)
    (tmp_path / old).mkdir()
    (tmp_path / old / "x").write_text("x")
    past = time.time() - 10 * 86400
    os.utime(tmp_path / f"{old}.json", (past, past))
    out = cli.reap(_cfg(tmp_path), days=7)
    assert not (tmp_path / f"{old}.json").exists() and not (tmp_path / old).exists()
    assert (tmp_path / f"{fresh}.json").exists()
    assert json.loads((tmp_path / f"{dead}.json").read_text())["status"] == "abandoned"
    assert out["pruned"] == [old]


def test_report_summarizes_ab_ledger(tmp_path):
    from scripts.delegate.config import load_config
    from scripts.delegate.ledger import Ledger
    from scripts.delegate import cli
    led = str(tmp_path / "ledger.jsonl")
    L = Ledger(led)
    L.append({"kind": "review", "delegated": True, "claude_tokens": 1000})
    L.append({"kind": "review", "delegated": True, "claude_tokens": 1200})
    L.append({"kind": "review", "delegated": False, "claude_tokens": 4000})
    L.append({"kind": "review", "delegated": False, "claude_tokens": 4400})
    L.append({"tool": "ask_local", "mode": "summarize"})  # non-review row ignored
    cfg = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r",
                       "LOCAL_DELEGATE_LEDGER": led})
    rep = cli.report(cfg)
    assert rep["n_reviews"] == 4
    assert rep["delegated"]["n"] == 2 and rep["delegated"]["claude_tokens_mean"] == 1100
    assert rep["direct"]["n"] == 2 and rep["direct"]["claude_tokens_mean"] == 4200
    assert round(rep["savings_pct"], 1) == 73.8 and rep["meets_20pct_gate"] is True
    assert "A/B measurement" in cli._fmt_report(rep)
