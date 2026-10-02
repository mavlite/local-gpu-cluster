import pytest, asyncio
from scripts.delegate.config import load_config
from scripts.delegate.router_client import ask_local, InputTooLarge, ProfileBusy

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"})

class FakeResp:
    def __init__(self, status, payload): self.status_code = status; self._p = payload
    def json(self): return self._p

class FakeHTTP:
    def __init__(self, healthz_profile="qwen3.8", post_results=None):
        self.healthz_profile = healthz_profile
        self.post_results = list(post_results or [])
        self.posts = []
    async def get(self, url): return FakeResp(200, {"active_chat_profile": self.healthz_profile})
    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return self.post_results.pop(0)

def run(coro): return asyncio.run(coro)

def test_code_mode_uses_think_alias():
    http = FakeHTTP(post_results=[FakeResp(200, {"choices":[{"message":{"content":"ok"}}], "usage":{"prompt_tokens":5,"completion_tokens":2}})])
    out = run(ask_local(http, CFG, prompt="hi", mode="code"))
    assert http.posts[0]["model"] == "qwen3.8-think"
    assert out["text"] == "ok" and out["usage"] == {"input":5,"output":2}

def test_summarize_mode_uses_rag_alias():
    http = FakeHTTP(post_results=[FakeResp(200, {"choices":[{"message":{"content":"s"}}],"usage":{}})])
    run(ask_local(http, CFG, prompt="sum", mode="summarize"))
    assert http.posts[0]["model"] == "rag-qwen3.8"

def test_oversize_input_raises_not_truncates():
    http = FakeHTTP()
    big = "x" * (CFG.ask_max_input_bytes + 1)
    with pytest.raises(InputTooLarge):
        run(ask_local(http, CFG, prompt="p", content=big))

def test_wrong_profile_raises_profilebusy():
    http = FakeHTTP(healthz_profile="qwen3-coder")
    with pytest.raises(ProfileBusy):
        run(ask_local(http, CFG, prompt="p"))

def test_retries_once_on_degraded_then_succeeds():
    http = FakeHTTP(post_results=[FakeResp(502, {"error":"service_degraded"}),
                                  FakeResp(200, {"choices":[{"message":{"content":"ok"}}],"usage":{}})])
    out = run(ask_local(http, CFG, prompt="p"))
    assert out["text"] == "ok" and len(http.posts) == 2
