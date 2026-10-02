import os

from scripts.delegate import jobs, service
from scripts.delegate.config import load_config

_ENV = {"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"}
CFG = load_config(_ENV)


def _tools(tmp_path, **dep_kw):
    cfg = load_config({**_ENV, "LOCAL_DELEGATE_JOBS_DIR": str(tmp_path)})
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps(**dep_kw))
    led = jobs.MemLedger()
    return store, led, service.AgenticTools(cfg, store, ledger=led)


def test_submit_returns_job_id_and_list_shows_it(tmp_path):
    store, _, tools = _tools(tmp_path)
    jid = tools.submit_task(task="t", repo=".", base_ref="HEAD")["job_id"]
    store.join()
    rows = tools.list_jobs()
    assert jid in [r["id"] for r in rows]
    assert {"id", "status", "task_type", "ts"} <= set(rows[0])
    assert tools.list_jobs(status="failed") == []


def test_submit_builds_spec(tmp_path):
    store, _, tools = _tools(tmp_path)
    jid = tools.submit_task(task="t", repo=".", checks=[["pytest"]], task_type="fix",
                            allow_web=True, timeout_s=9)["job_id"]
    store.join()
    spec = store.get(jid)["spec"]
    assert spec["checks"] == [["pytest"]] and spec["task_type"] == "fix"
    assert spec["allow_web"] is True and spec["timeout_s"] == 9


def test_result_reflects_recorded_job(tmp_path):
    store, _, tools = _tools(tmp_path)
    jid = tools.submit_task(task="t", repo=".", checks=[["pytest"]])["job_id"]
    store.join()
    r = tools.result(jid)
    assert r["status"] == "done" and r["diff"] == "DIFF" and r["summary"] == "SUMMARY"
    assert r["tokens"] == {"output": 5} and r["checks"][0]["exit_code"] == 0
    assert r["flagged"] == [] and r["gate_reasons"] == [] and r["duration_s"] >= 0
    assert "checks_skipped" in r


def test_result_not_terminal_returns_note(tmp_path):
    store, _, tools = _tools(tmp_path)
    store._save({"id": "J1", "status": "running", "spec": {}})
    r = tools.result("J1")
    assert r["status"] == "running" and "note" in r and "diff" not in r


def test_result_unknown_job(tmp_path):
    _, _, tools = _tools(tmp_path)
    assert "error" in tools.result("nope")


def test_record_review_writes_ab_row_and_removes_workdir(tmp_path):
    store, led, tools = _tools(tmp_path)
    jid = tools.submit_task(task="t", repo=".", base_ref="HEAD", task_type="fix")["job_id"]
    store.join()
    wd = os.path.join(str(tmp_path), jid)
    os.makedirs(os.path.join(wd, "work"), exist_ok=True)
    assert os.path.isdir(wd)
    out = tools.record_review(job_id=jid, verdict="accepted", claude_tokens=1234,
                              fix_lines=0, cause="")
    assert out == {"ok": True}
    row = led.rows[-1]
    assert row["kind"] == "review" and row["verdict"] == "accepted" and row["delegated"] is True
    assert row["job_id"] == jid and row["task_type"] == "fix"
    assert row["claude_tokens"] == 1234 and row["local_tokens"] == {"output": 5}
    assert "duration_s" in row and "fix_lines" in row and "cause" in row
    assert not os.path.exists(wd)


def test_record_direct_logs_self_done_arm(tmp_path):
    _, led, tools = _tools(tmp_path)
    assert tools.record_direct(task_type="refactor", claude_tokens=42, note="inline") == {"ok": True}
    row = led.rows[-1]
    assert row["kind"] == "review" and row["delegated"] is False and row["job_id"] is None
    assert row["task_type"] == "refactor" and row["claude_tokens"] == 42 and row["note"] == "inline"


def test_ab_ledger_arms_are_consistent_and_joinable(tmp_path):
    """One delegated row + one self-done row, same keyspace (kind/delegated/task_type/claude_tokens)."""
    store, led, tools = _tools(tmp_path)
    jid = tools.submit_task(task="t", repo=".", base_ref="HEAD", task_type="fix")["job_id"]
    store.join()
    tools.record_review(job_id=jid, verdict="accepted", claude_tokens=100)
    tools.record_direct(task_type="fix", claude_tokens=200)
    delegated = [r for r in led.rows if r["delegated"]]
    direct = [r for r in led.rows if not r["delegated"]]
    assert len(delegated) == 1 and len(direct) == 1
    for r in led.rows:
        assert r["kind"] == "review"
        assert {"task_type", "claude_tokens", "delegated"} <= set(r)
    assert delegated[0]["local_tokens"] == {"output": 5}


def test_record_review_refuses_non_terminal_job_and_keeps_workdir(tmp_path):
    """I6 (blocks merge): a running/queued job is not recorded and its dir is NOT removed."""
    store, led, tools = _tools(tmp_path)
    store._save({"id": "RUN1", "status": "running", "spec": {"task_type": "fix"}})
    wd = os.path.join(str(tmp_path), "RUN1")
    os.makedirs(os.path.join(wd, "work"), exist_ok=True)
    out = tools.record_review(job_id="RUN1", verdict="accepted", claude_tokens=1)
    assert "error" in out and "terminal" in out["error"]
    assert os.path.isdir(wd)  # dir survived
    assert led.rows == []  # nothing recorded


def test_tools_registered_in_server():
    import asyncio
    from mcp.types import ListToolsRequest
    server = service.build_server(CFG, deps=None)
    res = asyncio.run(server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list")))
    names = {t.name for t in res.root.tools}
    assert {"ask_local", "submit_task", "result", "list_jobs", "record_review",
            "record_direct"} <= names
