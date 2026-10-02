"""Opt-in live checks against the real router/GPU.

Deselected by default (see conftest.py). Run with:
    LOCAL_DELEGATE_LIVE=1 python -m pytest scripts/delegate/tests/test_live.py -m live
Needs LOCAL_DELEGATE_BEARER_TOKEN/ROUTER_TOKEN, LOCAL_DELEGATE_ALLOWED_ROOTS,
LOCAL_DELEGATE_OPENCODE_EXE, LOCAL_DELEGATE_OVERLAY, and LOCAL_DELEGATE_LIVE_REPO.
"""
import asyncio
import os
import subprocess

import pytest

from scripts.delegate import config, jobs, service

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LOCAL_DELEGATE_LIVE") != "1",
                       reason="set LOCAL_DELEGATE_LIVE=1 to run live tests"),
]


@pytest.fixture
def cfg():
    return config.load_config(os.environ)


def test_ask_local_round_trip(cfg):
    import httpx
    from scripts.delegate.router_client import ask_local

    async def go():
        async with httpx.AsyncClient() as http:
            return await ask_local(http, cfg, prompt="Reply with the single word: pong")
    assert asyncio.run(go())


def test_tiny_real_agentic_job(cfg):
    repo = os.environ["LOCAL_DELEGATE_LIVE_REPO"]
    store = jobs.JobStore(cfg, jobs.real_deps(cfg))
    tools = service.AgenticTools(cfg, store, jobs.real_deps(cfg).ledger)
    jid = tools.submit_task(task="Create hello.txt containing the word hi.", repo=repo,
                            checks=[], timeout_s=600)["job_id"]
    store.join()
    res = tools.result(jid)
    assert res["status"] in ("done", "failed")
    assert res["diff"] is not None


def test_canary_note(cfg):
    # Canary: the job must not touch the source repo directly.
    repo = os.environ["LOCAL_DELEGATE_LIVE_REPO"]
    before = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    test_tiny_real_agentic_job(cfg)
    after = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                           capture_output=True, text=True, check=True).stdout
    assert before == after
