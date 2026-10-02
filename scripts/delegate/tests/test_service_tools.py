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
    assert tools.record_review(job_id=jid, verdict="accepted", fix_lines=0, cause="") == {"ok": True}
    row = led.rows[-1]
    assert row["verdict"] == "accepted" and row["delegated"] is True
    assert row["job_id"] == jid and row["task_type"] == "fix" and row["tokens"] == {"output": 5}
    assert not os.path.exists(wd)


def test_tools_registered_in_server():
    import asyncio
    from mcp.types import ListToolsRequest
    server = service.build_server(CFG, deps=None)
    res = asyncio.run(server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list")))
    names = {t.name for t in res.root.tools}
    assert {"ask_local", "submit_task", "result", "list_jobs", "record_review"} <= names
