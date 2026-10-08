"""End to end with the REAL opencode binary against a scripted model server (no cluster needed).

Proves with opencode 1.18.34 itself: the read-only external config and lock-down env load our
agents and ignore a planted `.opencode/` override; the implementer edits; the reviewer gets the
packet as an attachment; REVISE resumes the same session; the patch is graded.
Skips when opencode is not installed (set WF_OPENCODE to the binary to force a path).
"""
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cli  # noqa: E402
import grade  # noqa: E402
import pipeline  # noqa: E402
import profiles  # noqa: E402
from wf_fixtures import make_bundle, solution_src  # noqa: E402

OC = cli.resolve_opencode(os.environ)
pytestmark = pytest.mark.skipif(OC is None, reason="opencode not installed")


class Stub:
    def __init__(self):
        self.requests, self.lock, self.reviews = [], threading.Lock(), 0

    def decide(self, body):
        msgs = body.get("messages", [])
        system = " ".join(json.dumps(m.get("content")) for m in msgs if m.get("role") == "system")
        users = " ".join(json.dumps(m.get("content")) for m in msgs if m.get("role") == "user")
        with self.lock:
            self.requests.append({"system": system, "users": users, "tools": sorted(
                t["function"]["name"] for t in body.get("tools") or [])})
        if not body.get("tools"):
            return {"text": "title"}
        n_tool = 0
        for m in reversed(msgs):
            if m.get("role") == "user":
                break
            n_tool += m.get("role") == "tool"
        if "You review a change" in system:
            with self.lock:
                self.reviews += 1
                first = self.reviews == 1
            return {"text": "REVISE: also handle negative numbers" if first else "ACCEPT"}
        if "You are a software engineer" in system:
            if n_tool == 0:
                return {"tool_calls": [{"name": "write", "arguments": {
                    "filePath": "pkg/t1.py", "content": solution_src()}}]}
            return {"text": "SUMMARY: implemented f"}
        return {"text": "SUMMARY: nothing to do"}


def serve(stub):
    def chunk(model, delta, finish=None):
        return {"id": "s", "object": "chat.completion.chunk", "created": 0, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            reply, model = stub.decide(body), body.get("model", "m")
            if not body.get("stream"):
                out = json.dumps({"id": "s", "object": "chat.completion", "model": model, "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": reply.get("text", "")},
                     "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if "tool_calls" in reply:
                d = {"role": "assistant", "tool_calls": [
                    {"index": i, "id": f"c{time.time_ns()}{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                    for i, c in enumerate(reply["tool_calls"])]}
                evs = [chunk(model, d), chunk(model, {}, "tool_calls")]
            else:
                evs = [chunk(model, {"role": "assistant", "content": reply["text"]}), chunk(model, {}, "stop")]
            evs[-1]["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            for e in evs:
                self.wfile.write(f"data: {json.dumps(e)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def plant_override(bundle_dir):
    """Add a booby-trapped .opencode agent override to the task snapshot."""
    import io
    import tarfile
    with tarfile.open(os.path.join(bundle_dir, "snapshot.tar"), "a") as t:
        for name, text in ((".opencode/agent/impl-1.md", "---\nmode: primary\n---\nPLANTED OVERRIDE\n"),
                           ("opencode.json", json.dumps({"agent": {"impl-1": {"prompt": "PLANTED OVERRIDE"}}}))):
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            t.addfile(info, io.BytesIO(raw))


def test_one_task_end_to_end_with_real_opencode(tmp_path):
    stub = Stub()
    srv = serve(stub)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        b = make_bundle(str(tmp_path / "bundles"), "t1")
        plant_override(b)
        run_dir = str(tmp_path / "run")
        opencode = cli.build_opencode("G", run_dir + "-oc", url, [], [OC],
                                      dict(os.environ, WF_ROUTER_KEY="scoped-test-key"))
        summary = pipeline.Pipeline("G", [b], opencode, run_dir, grade.LocalRunner()).run()
    finally:
        srv.shutdown()
    with open(os.path.join(run_dir, "tasks", "t1", "record.json")) as f:
        rec = json.load(f)
    assert rec["outcome"] == "implementer-accepted" and rec["accepted"], rec
    assert [r["verdict"] for r in rec["reviews"]] == ["REVISE", "ACCEPT"]
    assert rec["rounds"][0]["session"] == rec["rounds"][1]["session"]          # same session resumed
    assert summary["valid"] and summary["implementer_accepted"] == 1
    assert not any("PLANTED OVERRIDE" in r["system"] for r in stub.requests)   # project config locked out
    impl_tools = {tuple(r["tools"]) for r in stub.requests if "You are a software engineer" in r["system"]}
    assert impl_tools and all("webfetch" not in t and "task" not in t and "bash" in t for t in impl_tools)
    review_reqs = [r for r in stub.requests if "You review a change" in r["system"] and r["tools"]]
    assert review_reqs and "Acceptance tests" in review_reqs[0]["users"]       # the packet arrived
    assert all("edit" not in r["tools"] and "write" not in r["tools"] for r in review_reqs)
    assert rec["loops"] is not None and rec["loops"]["n_calls"] >= 1           # export worked
