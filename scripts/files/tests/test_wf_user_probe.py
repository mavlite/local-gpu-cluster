"""wf-user-probe.py: the user-lane latency probe (workforce spec §9 clause 3; Plan D). Runs in LXC 153
with the owner key from /etc/router.env (never argv), so its requests take the user's reserved lane.
A failed probe is recorded, never dropped: lane starvation must show up, not vanish."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "wf-user-probe.py")


class Router(BaseHTTPRequestHandler):
    calls = []
    mode = "ok"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Router.calls.append({"auth": self.headers.get("Authorization"), "body": body, "argv_free": True})
        if Router.mode == "busy":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error":"busy"}')
            return
        out = {"choices": [{"message": {"content": "ok"}}], "timings": {"prompt_ms": 12.5, "predicted_ms": 30.0}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def router(tmp_path):
    Router.calls, Router.mode = [], "ok"
    srv = HTTPServer(("127.0.0.1", 0), Router)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    env_file = tmp_path / "router.env"
    env_file.write_text('RATE_LIMIT_CHAT=60/minute\nROUTER_API_KEY="owner-secret-123"\n')
    yield f"http://127.0.0.1:{srv.server_address[1]}", env_file
    srv.shutdown()


def probe(url, env_file, out, *extra):
    return subprocess.run([sys.executable, SCRIPT, "run", "--out", str(out), "--count", "3", "--interval", "0",
                           *extra], capture_output=True, text=True,
                          env=dict(os.environ, WF_PROBE_URL=url, WF_PROBE_ENV_FILE=str(env_file)))


def test_probe_uses_the_owner_key_from_the_env_file_and_records_each_request(router, tmp_path):
    url, env_file = router
    out = tmp_path / "probe.jsonl"
    r = probe(url, env_file, out)
    assert r.returncode == 0, r.stderr
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(recs) == 3 and all(x["ok"] for x in recs)
    assert all(c["auth"] == "Bearer owner-secret-123" for c in Router.calls)
    assert recs[0]["prompt_ms"] == 12.5 and recs[0]["predicted_ms"] == 30.0
    assert recs[0]["latency_s"] >= 0 and recs[0]["ts"] > 0
    assert "owner-secret" not in r.stdout + r.stderr + out.read_text()


def test_a_refused_probe_is_recorded_as_failed_not_dropped(router, tmp_path):
    url, env_file = router
    Router.mode = "busy"
    out = tmp_path / "probe.jsonl"
    r = probe(url, env_file, out)
    assert r.returncode == 0, r.stderr
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(recs) == 3 and not any(x["ok"] for x in recs)
    assert all(x["status"] == 503 for x in recs)


def test_an_unreachable_router_is_recorded_as_failed(tmp_path):
    env_file = tmp_path / "router.env"
    env_file.write_text("ROUTER_API_KEY=k\n")
    out = tmp_path / "probe.jsonl"
    r = probe("http://127.0.0.1:9", env_file, out, "--timeout", "2")
    assert r.returncode == 0, r.stderr
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(recs) == 3 and all(not x["ok"] and x["status"] is None for x in recs)


def test_the_probe_request_is_small_fixed_and_uses_the_lead_alias(router, tmp_path):
    url, env_file = router
    probe(url, env_file, tmp_path / "p.jsonl")
    bodies = [c["body"] for c in Router.calls]
    assert all(b["model"] == "qwen3.8-nothink" and b["max_tokens"] == 16 and b["stream"] is False for b in bodies)
    assert bodies[0]["messages"] == bodies[1]["messages"]          # fixed: comparable across runs


def test_a_missing_key_refuses_to_start(tmp_path):
    env_file = tmp_path / "router.env"
    env_file.write_text("RATE_LIMIT_CHAT=60/minute\n")
    r = probe("http://127.0.0.1:9", env_file, tmp_path / "p.jsonl")
    assert r.returncode != 0 and "ROUTER_API_KEY" in r.stderr
