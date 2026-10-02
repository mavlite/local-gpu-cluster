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


# ---- path containment and bounded reads ----
import dataclasses, os
from scripts.delegate.router_client import PathNotAllowed

OK = FakeResp(200, {"choices": [{"message": {"content": "ok"}}], "usage": {}})

def _cfg(root, **kw):
    return dataclasses.replace(CFG, allowed_repo_roots=(str(root),), **kw)

def test_file_under_allowed_root_is_read(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("hello")
    http = FakeHTTP(post_results=[OK])
    run(ask_local(http, _cfg(tmp_path), prompt="p", files=[str(f)]))
    assert "hello" in http.posts[0]["messages"][0]["content"]

def test_dotdot_traversal_rejected(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    (tmp_path / "secret.txt").write_text("s")
    with pytest.raises(PathNotAllowed):
        run(ask_local(FakeHTTP(), _cfg(root), prompt="p", files=[str(root / ".." / "secret.txt")]))

def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside.txt"; outside.write_text("s")
    link = root / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation not permitted")
    with pytest.raises(PathNotAllowed):
        run(ask_local(FakeHTTP(), _cfg(root), prompt="p", files=[str(link)]))

@pytest.mark.parametrize("bad", [r"Z:\nope\x.txt", r"\\server\share\x.txt"])
def test_other_drive_or_unc_rejected(tmp_path, bad):
    with pytest.raises(PathNotAllowed):
        run(ask_local(FakeHTTP(), _cfg(tmp_path), prompt="p", files=[bad]))

def test_empty_allowed_roots_rejects_everything(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("x")
    with pytest.raises(PathNotAllowed):
        run(ask_local(FakeHTTP(), CFG, prompt="p", files=[str(f)]))

def test_bare_string_and_non_string_files_rejected(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("x")
    with pytest.raises(PathNotAllowed):
        run(ask_local(FakeHTTP(), _cfg(tmp_path), prompt="p", files=str(f)))
    with pytest.raises(PathNotAllowed):
        run(ask_local(FakeHTTP(), _cfg(tmp_path), prompt="p", files=[123]))

def test_file_read_is_bounded_and_total_capped(tmp_path):
    f = tmp_path / "big.txt"; f.write_bytes(b"x" * 100)
    cfg = _cfg(tmp_path, ask_max_input_bytes=10)
    seen = []
    def reader(path, limit):
        seen.append(limit); return open(path, "rb").read(limit)
    with pytest.raises(InputTooLarge):
        run(ask_local(FakeHTTP(), cfg, prompt="p", files=[str(f)], read_file=reader))
    assert seen == [11]

def test_running_total_spans_content_and_files(tmp_path):
    f = tmp_path / "a.txt"; f.write_bytes(b"y" * 6)
    cfg = _cfg(tmp_path, ask_max_input_bytes=10)
    with pytest.raises(InputTooLarge):
        run(ask_local(FakeHTTP(), cfg, prompt="p", content="x" * 6, files=[str(f)]))
