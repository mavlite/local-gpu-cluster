"""Integration: the real router app's media handling (coding-engine phase 1, follow-up B).

Two app instances against a stub llama-server: vision disabled (the default) and enabled. Remote image URLs never
reach llama-server, images count toward MAX_CHAT_INPUT_TOKENS, a list-type system message does not crash
should_strip_thinking, and oversized bodies get 413. Skips where the router's dependencies are not installed."""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
pytest.importorskip("prometheus_fastapi_instrumentator")
from fastapi.testclient import TestClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("ROUTER_APP_UNDER_TEST", os.path.join(HERE, "..", "router-app.py"))
OWNER = "owner-key-for-vision-tests-01"
PNG = "data:image/png;base64,iVBORw0KGgo="


class Upstream:
    def __init__(self):
        self.chat_bodies = []
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
                    return self._send({"total_slots": 3})
                if self.path.startswith("/v1/models"):
                    return self._send({"data": [{"id": "qwen3.8"}]})
                return self._send({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path.startswith("/tokenize"):
                    return self._send({"tokens": [1, 2, 3]})
                if self.path.startswith("/v1/messages/count_tokens"):
                    up.chat_bodies.append(("count", body))
                    return self._send({"input_tokens": 3})
                if self.path.startswith("/v1/messages"):
                    up.chat_bodies.append(("messages", body))
                    return self._send({"id": "m", "type": "message", "role": "assistant",
                                       "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"})
                if self.path.startswith("/v1/completions"):
                    up.chat_bodies.append(("completions", body))
                    return self._send({"choices": [{"index": 0, "text": "ok", "finish_reason": "stop"}]})
                if self.path.startswith("/v1/chat/completions"):
                    up.chat_bodies.append(body)
                    return self._send({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                                    "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
                return self._send({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def make_app(tmp, name, **extra_env):
    up = Upstream()
    saved = dict(os.environ)
    os.environ.update({"ROUTER_API_KEY": OWNER, "LLAMACPP_API_KEY": "x", "V620_URL": up.url,
                       "ACCESS_LOG_PATH": str(tmp / f"{name}.log"), "RATE_LIMIT_CHAT": "1000/minute",
                       "PROFILE_CACHE_TTL": "0", "CHAT_SLOT_REFRESH_S": "0.1", "TOOL_EXECUTION_DEFAULT": "client",
                       "STRICT_PROFILE_MATCH": "1", "SWAP_WEBHOOK_URL": "", **extra_env})
    sys.path.insert(0, os.path.dirname(APP))
    spec = importlib.util.spec_from_file_location(f"router_app_{name}", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ.clear()
    os.environ.update(saved)
    return TestClient(mod.app), up


@pytest.fixture(scope="module")
def apps(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("router-vision")
    off = make_app(tmp, "off", MAX_CHAT_INPUT_TOKENS="100000")
    on = make_app(tmp, "on", ROUTER_VISION_ENABLED="1", MAX_CHAT_INPUT_TOKENS="2000",
                  ROUTER_IMAGE_TOKEN_ESTIMATE="1280", ROUTER_MAX_BODY_BYTES="20000")
    yield {"off": off, "on": on}
    for c, up in (off, on):
        c.close()
        up.server.shutdown()


def post(app, messages, **extra):
    client, _ = app
    return client.post("/v1/chat/completions", json={"model": "qwen3.8-nothink", "messages": messages,
                                                     "max_tokens": 4, **extra},
                       headers={"Authorization": f"Bearer {OWNER}"})


def image_msg(url, text="what is this?"):
    return [{"role": "user", "content": [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}]}]


def test_text_chat_unchanged_with_vision_off(apps):
    assert post(apps["off"], [{"role": "user", "content": "hi"}]).status_code == 200


def test_images_refused_when_vision_is_off_and_nothing_reaches_the_model(apps):
    up = apps["off"][1]
    n = len(up.chat_bodies)
    r = post(apps["off"], image_msg(PNG))
    assert r.status_code == 400 and "vision" in r.text
    assert len(up.chat_bodies) == n


def test_data_uri_image_passes_through_unchanged_when_vision_is_on(apps):
    up = apps["on"][1]
    r = post(apps["on"], image_msg(PNG))
    assert r.status_code == 200, r.text
    sent = up.chat_bodies[-1]["messages"][-1]["content"]
    assert {"type": "image_url", "image_url": {"url": PNG}} in sent


@pytest.mark.parametrize("url", ["http://192.168.6.175:8006/", "https://example.com/a.png"])
def test_remote_image_urls_never_reach_llama_server(apps, url):
    up = apps["on"][1]
    n = len(up.chat_bodies)
    r = post(apps["on"], image_msg(url))
    assert r.status_code == 400 and "data:" in r.text
    assert len(up.chat_bodies) == n


def test_video_parts_refused(apps):
    r = post(apps["on"], [{"role": "user", "content": [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AA"}}]}])
    assert r.status_code == 400


def test_images_count_toward_the_input_cap(apps):
    # tokenize says 3 tokens; one image adds 1280 (under 2000), two add 2560 (over).
    assert post(apps["on"], image_msg(PNG)).status_code == 200
    two = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG}},
                                        {"type": "image_url", "image_url": {"url": PNG}}]}]
    r = post(apps["on"], two)
    assert r.status_code == 413 and "MAX_CHAT_INPUT_TOKENS" in r.text


def test_list_type_system_message_does_not_crash(apps):
    msgs = [{"role": "system", "content": [{"type": "text", "text": "You are terse."}]},
            {"role": "user", "content": "hi"}]
    assert post(apps["off"], msgs).status_code == 200


def test_oversized_body_gets_413(apps):
    r = post(apps["on"], [{"role": "user", "content": "x" * 30000}])
    assert r.status_code == 413


# ---- review round 1: the other routes that forward content, chunked bodies, allowlist ---------------------------

def raw_post(app, path, body):
    client, _ = app
    return client.post(path, json=body, headers={"Authorization": f"Bearer {OWNER}"})


URL_IMAGE = {"type": "image", "source": {"type": "url", "url": "http://192.168.6.175:8006/"}}
B64_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens"])
def test_anthropic_url_images_never_reach_llama_server(apps, path):
    up = apps["on"][1]
    n = len(up.chat_bodies)
    r = raw_post(apps["on"], path, {"model": "qwen3.8-nothink", "max_tokens": 8,
                                    "messages": [{"role": "user", "content": [URL_IMAGE, {"type": "text", "text": "x"}]}]})
    assert r.status_code == 400
    assert len(up.chat_bodies) == n


def test_anthropic_base64_image_passes_when_vision_is_on_and_not_when_off(apps):
    body = {"model": "qwen3.8-nothink", "max_tokens": 8,
            "messages": [{"role": "user", "content": [B64_IMAGE, {"type": "text", "text": "x"}]}]}
    assert raw_post(apps["on"], "/v1/messages", body).status_code == 200
    assert raw_post(apps["off"], "/v1/messages", body).status_code == 400


def test_anthropic_plain_text_unchanged(apps):
    r = raw_post(apps["off"], "/v1/messages", {"model": "qwen3.8-nothink", "max_tokens": 8,
                                               "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_completions_refuses_multimodal_prompts(apps):
    up = apps["on"][1]
    n = len(up.chat_bodies)
    r = raw_post(apps["on"], "/v1/completions", {"model": "qwen3.8", "prompt": {"prompt_string": "x <__media__>",
                                                                              "multimodal_data": ["AA"]}})
    assert r.status_code == 400 and len(up.chat_bodies) == n
    assert raw_post(apps["on"], "/v1/completions", {"model": "qwen3.8", "prompt": "def f():", "max_tokens": 4}).status_code == 200


def test_unknown_chat_part_type_refused(apps):
    r = post(apps["on"], [{"role": "user", "content": [{"type": "input_image", "image_url": "http://x/y.png"}]}])
    assert r.status_code == 400


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/completions", "/v1/embeddings"])
def test_chunked_oversized_body_is_refused_without_a_content_length(apps, path):
    client, _ = apps["on"]
    def gen():
        yield b'{"model":"qwen3.8","prompt":"'
        for _ in range(50):
            yield b"x" * 1000
        yield b'"}'
    r = client.post(path, content=gen(), headers={"Authorization": f"Bearer {OWNER}", "Content-Type": "application/json"})
    assert r.status_code == 413
