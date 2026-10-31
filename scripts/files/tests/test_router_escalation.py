"""Integration: the real router app sends claude-* aliases to the escalation broker (owner key only), lists them
for the owner, serves /escalation/budget, and never lets the request reach llama.cpp."""
import hashlib
import importlib.util
import json
import os
import re
import sys
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
pytest.importorskip("prometheus_fastapi_instrumentator")
from fastapi.testclient import TestClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from anthropic_stub import AnthropicStub  # noqa: E402
from test_escalation import GOOD  # noqa: E402
from test_router_scoped_keys import OWNER, SCOPED, Upstream  # noqa: E402

APP = os.environ.get("ROUTER_APP_UNDER_TEST", os.path.join(HERE, "..", "router-app.py"))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("router-esc")
    up, ant = Upstream(), AnthropicStub()
    keys = tmp / "router-keys.json"
    keys.write_text(json.dumps({"keys": [{"name": "wf-test", "sha256": hashlib.sha256(SCOPED.encode()).hexdigest(),
                                          "aliases": ["qwen3.8-nothink", "claude-ask"],
                                          "expires": time.time() + 3600}]}))
    akey = tmp / "anthropic.key"
    akey.write_text("sk-ant-test\n")
    saved = dict(os.environ)
    os.environ.update({
        "ROUTER_API_KEY": OWNER, "LLAMACPP_API_KEY": "x", "V620_URL": up.url, "ROUTER_KEYS_FILE": str(keys),
        "ACCESS_LOG_PATH": str(tmp / "access.log"), "RATE_LIMIT_CHAT": "1000/minute",
        "SWAP_WEBHOOK_URL": up.url, "SWAP_WEBHOOK_KEY": "k", "SWAP_TIMEOUT": "5", "PROFILE_CACHE_TTL": "0",
        "CHAT_SLOT_REFRESH_S": "0.1", "TOOL_EXECUTION_DEFAULT": "server", "STRICT_PROFILE_MATCH": "1",
        "ANTHROPIC_URL": ant.url, "ANTHROPIC_API_KEY_FILE": str(akey), "ESCALATION_LEDGER": str(tmp / "ledger.jsonl"),
        "ESCALATION_PRICES": '{"claude-sonnet-5-5": [3, 15], "claude-opus-5-5": [15, 75]}',
        "ESCALATION_TZ": "UTC", "ESCALATION_RETRY_DELAY_S": "0",
    })
    sys.path.insert(0, os.path.dirname(APP))
    spec = importlib.util.spec_from_file_location("router_app_esc_under_test", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = TestClient(mod.app)
    yield {"client": client, "up": up, "ant": ant, "log": tmp / "access.log", "mod": mod}
    client.close()
    up.server.shutdown()
    ant.close()
    os.environ.clear()
    os.environ.update(saved)


def post(env, key, **body):
    return env["client"].post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})


_N = iter(range(10 ** 6))


def esc_body(brief=None, **extra):
    """A fresh brief per call by default: the module shares one ledger, and the broker refuses an identical brief
    sent again within ESCALATION_DEDUPE_S."""
    brief = GOOD + f"\n<!-- {next(_N)} -->" if brief is None else brief
    return {"model": "claude-ask", "messages": [{"role": "system", "content": "big opencode prompt"},
                                                {"role": "user", "content": brief}], **extra}


def test_owner_escalation_is_answered_by_the_broker_not_llamacpp(env):
    before = len(env["up"].chat_bodies)
    r = post(env, OWNER, **esc_body())
    assert r.status_code == 200, r.text
    assert "Claude claude-sonnet-5-5:" in r.json()["choices"][0]["message"]["content"]
    assert len(env["up"].chat_bodies) == before                        # llama.cpp never saw it
    assert env["ant"].requests[-1][1]["messages"][0]["content"].startswith(GOOD)
    assert len(env["ant"].requests[-1][1]["messages"]) == 1


def test_streaming_request_gets_valid_sse(env):
    r = post(env, OWNER, **esc_body(stream=True, stream_options={"include_usage": True}))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [e for e in r.text.split("\n\n") if e]
    assert events[-1] == "data: [DONE]"
    chunk = json.loads(events[0][len("data: "):])
    assert chunk["object"] == "chat.completion.chunk" and "Claude claude-sonnet-5-5:" in chunk["choices"][0]["delta"]["content"]


def test_scoped_key_is_refused_even_with_the_alias_allowlisted(env):
    n = len(env["ant"].requests)
    r = post(env, SCOPED, **esc_body())
    assert r.status_code == 403 and r.json()["error"]["message"] == "escalation aliases need the owner key"
    assert len(env["ant"].requests) == n


def test_broker_refusal_passes_status_through(env):
    r = post(env, OWNER, model="claude-ask", messages=[{"role": "user", "content": "no template"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "brief_section"


def test_models_lists_claude_aliases_for_owner_only(env):
    owner_ids = [m["id"] for m in env["client"].get("/v1/models", headers={"Authorization": f"Bearer {OWNER}"}).json()["data"]]
    scoped_ids = [m["id"] for m in env["client"].get("/v1/models", headers={"Authorization": f"Bearer {SCOPED}"}).json()["data"]]
    assert {"claude-ask", "claude-auto", "claude-hard"} <= set(owner_ids)
    assert not {"claude-ask", "claude-auto", "claude-hard"} & set(scoped_ids)


def test_budget_endpoint_owner_only(env):
    r = env["client"].get("/escalation/budget", headers={"Authorization": f"Bearer {OWNER}"})
    assert r.status_code == 200 and r.json()["day"]["cap_usd"] == 7.0 and "calls_left" in r.json()
    assert env["client"].get("/escalation/budget", headers={"Authorization": f"Bearer {SCOPED}"}).status_code == 403


def test_access_log_records_escalations_without_the_brief(env):
    log = env["log"].read_text(encoding="utf-8")
    assert '"claude-ask"' in log and "Make the parser accept trailing commas" not in log


def test_invalid_config_is_a_424_without_leaking_the_error(env, monkeypatch):
    monkeypatch.setenv("ESCALATION_DAY_CAP_USD", "nan")
    n = len(env["ant"].requests)
    r = post(env, OWNER, **esc_body())
    assert r.status_code == 424 and r.json()["error"]["code"] == "config"
    assert r.json()["error"]["message"] == "escalation: invalid configuration" and "ESCALATION_DAY_CAP_USD" not in r.text
    assert len(env["ant"].requests) == n
    b = env["client"].get("/escalation/budget", headers={"Authorization": f"Bearer {OWNER}"})
    assert b.status_code == 424 and b.json()["error"]["code"] == "config"
    monkeypatch.undo()
    assert env["client"].get("/escalation/budget", headers={"Authorization": f"Bearer {OWNER}"}).status_code == 200



# opencode retries 408/409/429/>=500 and error text matching these; a broker error must not look retryable.
RETRY_TRIGGER = re.compile(r"408|409|429|500|502|503|504|524|rate limit|service unavailable|try again", re.I)


def _clean_non_retryable(r):
    assert r.status_code not in (408, 409, 429) and r.status_code < 500, r.status_code
    assert not RETRY_TRIGGER.search(r.text), r.text


def test_budget_refusal_and_upstream_failure_are_non_retryable_through_the_router(env, monkeypatch):
    n = len(env["ant"].requests)
    monkeypatch.setenv("ESCALATION_DAY_CAP_USD", "0.0001")
    monkeypatch.setenv("ESCALATION_MONTH_CAP_USD", "500")
    r = post(env, OWNER, **esc_body())
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget"
    _clean_non_retryable(r)
    assert len(env["ant"].requests) == n
    monkeypatch.undo()
    env["ant"].responses = [(529, {}), (503, {})]
    r = post(env, OWNER, **esc_body())
    assert r.status_code == 424 and r.json()["error"]["code"] == "upstream"
    assert r.json()["error"]["message"] == "escalation: Anthropic call failed (Anthropic server error)"
    _clean_non_retryable(r)
    assert len(env["ant"].requests) == n + 2


def test_identical_brief_resent_through_the_router_is_a_400_duplicate(env):
    body = esc_body()
    assert post(env, OWNER, **body).status_code == 200
    n = len(env["ant"].requests)
    r = post(env, OWNER, **body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "duplicate"
    _clean_non_retryable(r)
    assert len(env["ant"].requests) == n


def test_rag_aliases_are_swap_exempt_but_interactive_mains_are_not(env):
    # The "dropping out" fix (2026-10-19): background rag-* requests must NOT
    # force a chat-slot swap (they are served on whatever is loaded), while
    # interactive mains still swap.
    mod = env["mod"]
    assert mod.is_swap_exempt("rag-qwen3.8") is True
    assert mod.is_swap_exempt("rag-qwen3.6") is True
    assert mod.is_swap_exempt("rag-qwen3.6-fast") is True
    for m in ("qwen3.8", "qwen3.8-think", "qwen3.6-think", "hemmingway", "qwen3.8-redteam", "coder"):
        assert mod.is_swap_exempt(m) is False, m


def test_slow_stream_answer_sends_keepalives_then_the_answer(env, monkeypatch):
    # A long Claude answer can take minutes; opencode drops a stream that is silent for 3 minutes (chunkTimeout).
    import asyncio
    real = env["mod"].escalation.handle

    async def slow(body, cfg, now=None):
        await asyncio.sleep(0.5)
        return await real(body, cfg, now)
    monkeypatch.setattr(env["mod"].escalation, "handle", slow)
    monkeypatch.setattr(env["mod"], "ESCALATION_KEEPALIVE_S", 0.1)
    monkeypatch.setattr(env["mod"], "ESCALATION_STREAM_GRACE_S", 0.1)
    r = post(env, OWNER, **esc_body(stream=True))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [e for e in r.text.split("\n\n") if e]
    assert events[0] == ": keepalive" and events[-1] == "data: [DONE]"
    chunk = json.loads(events[-2][len("data: "):])
    assert "Claude claude-sonnet-5-5:" in chunk["choices"][0]["delta"]["content"]


def test_slow_stream_failure_arrives_as_an_error_event(env, monkeypatch):
    import asyncio

    async def slow_fail(body, cfg, now=None):
        await asyncio.sleep(0.3)
        return 424, env["mod"].escalation.error_body("upstream", "escalation: Claude did not answer")
    monkeypatch.setattr(env["mod"].escalation, "handle", slow_fail)
    monkeypatch.setattr(env["mod"], "ESCALATION_KEEPALIVE_S", 0.1)
    monkeypatch.setattr(env["mod"], "ESCALATION_STREAM_GRACE_S", 0.1)
    r = post(env, OWNER, **esc_body(stream=True))
    events = [e for e in r.text.split("\n\n") if e]
    err = json.loads(events[-1][len("data: "):])
    assert err["error"]["code"] == "upstream" and "[DONE]" not in r.text


def test_a_fast_refusal_is_still_a_plain_json_error_even_when_streaming(env):
    r = post(env, OWNER, **esc_body(brief="no headings", stream=True))
    assert r.status_code == 422 and r.json()["error"]["code"] in ("brief_section", "brief_loop")
