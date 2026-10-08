"""Integration: the real router app with scoped keys and the reserved lane (workforce rev 2, §5.3-5.4).

Runs the actual FastAPI app (TestClient) against a stub chat server and a stub swap webhook. Skips
where the router's dependencies are not installed (the repo-wide test command still runs everything
else). TOOL_EXECUTION_DEFAULT is set to "server" on purpose: scoped keys must be FORCED to client-side
tools even when the router's default would give them server-side ones.
"""
import hashlib
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
pytest.importorskip("prometheus_fastapi_instrumentator")
from fastapi.testclient import TestClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("ROUTER_APP_UNDER_TEST", os.path.join(HERE, "..", "router-app.py"))
OWNER = "owner-key-for-tests-0001"
SCOPED = "wf_scoped-key-for-tests-0001"


class Upstream:
    """Stub llama-server (chat) + swap webhook, recording what reaches them."""

    def __init__(self):
        self.slots, self.active = 3, "qwen3.8"
        self.chat_bodies, self.swaps = [], []
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/props"):
                    return self._send({"total_slots": up.slots})
                if self.path.startswith("/v1/models"):
                    return self._send({"data": [{"id": up.active}]})
                return self._send({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path.startswith("/tokenize"):
                    return self._send({"tokens": [1, 2, 3]})
                if self.path.startswith("/swap"):
                    up.swaps.append(self.path)
                    up.active = "devstral"
                    return self._send({"ok": True})
                if self.path.startswith("/v1/chat/completions"):
                    up.chat_bodies.append(body)
                    return self._send({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                                    "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
                return self._send({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("router")
    up = Upstream()
    keys = tmp / "router-keys.json"
    keys.write_text(json.dumps({"keys": [{"name": "wf-test", "sha256": hashlib.sha256(SCOPED.encode()).hexdigest(),
                                          "aliases": ["qwen3.8-nothink", "devstral"], "expires": None}]}))
    saved = dict(os.environ)
    os.environ.update({
        "ROUTER_API_KEY": OWNER, "LLAMACPP_API_KEY": "x", "V620_URL": up.url, "ROUTER_KEYS_FILE": str(keys),
        "ACCESS_LOG_PATH": str(tmp / "access.log"), "RATE_LIMIT_CHAT": "1000/minute",
        "SWAP_WEBHOOK_URL": up.url, "SWAP_WEBHOOK_KEY": "k", "SWAP_TIMEOUT": "5", "PROFILE_CACHE_TTL": "0",
        "CHAT_SLOT_REFRESH_S": "0.1", "TOOL_EXECUTION_DEFAULT": "server", "STRICT_PROFILE_MATCH": "1",
    })
    import sys
    sys.path.insert(0, os.path.dirname(APP))
    spec = importlib.util.spec_from_file_location("router_app_under_test", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = TestClient(mod.app)
    yield {"client": client, "up": up, "mod": mod, "keys": keys, "log": tmp / "access.log"}
    client.close()
    up.server.shutdown()
    os.environ.clear()
    os.environ.update(saved)


def chat(env, key, model="qwen3.8-nothink", **extra):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4, **extra}
    return env["client"].post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})


def test_owner_chat_unchanged(env):
    r = chat(env, OWNER, tool_execution="client")
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "ok"


def test_scoped_key_can_chat_on_an_allowed_alias_and_is_forced_to_client_tools(env):
    env["up"].chat_bodies.clear()
    r = chat(env, SCOPED)
    assert r.status_code == 200
    sent = env["up"].chat_bodies[-1]
    tool_names = [t.get("function", {}).get("name") for t in sent.get("tools", [])]
    assert "web_fetch" not in tool_names            # the router's server-side tool loop did not run


def test_scoped_key_refused_for_other_aliases_and_server_tools(env):
    n = len(env["up"].chat_bodies)
    assert chat(env, SCOPED, model="qwen3.8-think").status_code == 403
    assert chat(env, SCOPED, tool_execution="server").status_code == 403
    assert len(env["up"].chat_bodies) == n          # nothing reached the model


@pytest.mark.parametrize("method,path", [("POST", "/v1/embeddings"), ("POST", "/v1/messages"),
                                         ("POST", "/v1/completions"), ("POST", "/v1/tavily/search"),
                                         ("POST", "/v1/rerank")])
def test_scoped_key_refused_on_every_other_endpoint(env, method, path):
    r = env["client"].request(method, path, json={}, headers={"Authorization": f"Bearer {SCOPED}"})
    assert r.status_code == 403 and r.json()["error"]["type"] == "forbidden"


def test_unknown_key_rejected(env):
    assert chat(env, "wf_not-a-real-key").status_code == 403


def test_reserved_lane_closes_in_one_slot_mode_but_the_owner_still_chats(env):
    mod, up = env["mod"], env["up"]
    import asyncio
    up.slots = 1
    asyncio.run(mod.chat_sem.set_capacity(1))
    try:
        r = chat(env, SCOPED)
        assert r.status_code == 503 and "workforce_lane_closed" in r.text
        assert chat(env, OWNER, tool_execution="client").status_code == 200
    finally:
        up.slots = 3
        asyncio.run(mod.chat_sem.set_capacity(3))


def test_scoped_key_never_triggers_a_profile_swap(env):
    up = env["up"]
    up.active, up.swaps = "qwen3.8", []
    env["mod"]._active_backend_cache["expires"] = 0
    r = chat(env, SCOPED, model="devstral")
    assert r.status_code == 409 and up.swaps == []
    env["mod"]._active_backend_cache["expires"] = 0
    r = chat(env, OWNER, model="devstral", tool_execution="client")
    assert up.swaps != []                           # the owner may still swap
    up.active = "qwen3.8"
    env["mod"]._active_backend_cache["expires"] = 0


def test_access_log_names_the_principal(env):
    chat(env, SCOPED)
    lines = [json.loads(line) for line in env["log"].read_text().splitlines() if line.strip()]
    assert any(e.get("principal") == "wf-test" for e in lines)
    assert any(e.get("principal") == "owner" for e in lines)


def test_revocation_takes_effect_on_the_next_request(env):
    keys = env["keys"]
    saved = keys.read_text()
    keys.write_text(json.dumps({"keys": []}))
    st = os.stat(keys)
    os.utime(keys, (st.st_atime + 10, st.st_mtime + 10))
    try:
        assert chat(env, SCOPED).status_code == 403
    finally:
        keys.write_text(saved)
        st = os.stat(keys)
        os.utime(keys, (st.st_atime + 20, st.st_mtime + 20))
