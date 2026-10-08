"""Scripted OpenAI-compatible server for exercising opencode mechanics offline.

One listener per role: port 0 is the coordinator (router), ports 1..3 are workers.
Every request is appended to <log> as one JSON line (port, model, tool names, message roles),
so a test can assert what opencode actually sent -- which tools a worker was offered, and
when each worker request started and ended.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()


def _chunk(model, delta, finish=None):
    return {"id": "stub", "object": "chat.completion.chunk", "created": 0, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _tool_names(body):
    return sorted(t.get("function", {}).get("name", "") for t in body.get("tools") or [])


class Script:
    """Decides each reply. Override coordinator()/worker() for other scenarios."""

    def __init__(self, worker_sleep=4.0, task_ids=("T1", "T2", "T3"), capacity=1):
        self.worker_sleep = worker_sleep
        self.task_ids = task_ids
        self.capacity = capacity             # what /healthz reports as chat_admission.capacity

    def coordinator(self, body):
        msgs = body.get("messages", [])
        if not body.get("tools"):
            return {"text": "gate run"}                        # title / summary requests
        if any(m.get("role") == "tool" for m in msgs):
            return {"text": "DONE"}
        calls = [{"name": "task", "arguments": {
            "description": f"implement {tid}", "subagent_type": f"worker-{i}",
            "prompt": f"Implement the task in tasks/{tid}. Read tasks/{tid}/TASK.md."}}
            for i, tid in enumerate(self.task_ids, start=1)]
        return {"tool_calls": calls}

    def worker(self, port, body):
        msgs = body.get("messages", [])
        if not body.get("tools"):
            return {"text": "worker"}
        n_tool = sum(1 for m in msgs if m.get("role") == "tool")
        tid = self.task_ids[port - 1]
        if n_tool == 0:
            time.sleep(self.worker_sleep)
            return {"tool_calls": [{"name": "write", "arguments": {
                "filePath": f"tasks/{tid}/done.txt", "content": f"worker {port}\n"}}]}
        if n_tool == 1:
            return {"tool_calls": [{"name": "write", "arguments": {
                "filePath": "../escaped.txt", "content": "should be denied\n"}}]}
        return {"text": "SUMMARY: wrote done.txt"}


def make_handler(port_role, script, log_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.path.rstrip("/").endswith("/healthz"):
                self.wfile.write(json.dumps({"ok": True, "chat_admission": {
                    "capacity": script.capacity, "in_use": 0}}).encode())
                return
            self.wfile.write(b'{"data":[{"id":"qwen3.8-nothink"},{"id":"qwen3.6"}]}')

        def do_POST(self):
            t0 = time.time()
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            role = port_role[self.server.server_address[1]]
            reply = script.coordinator(body) if role == 0 else script.worker(role, body)
            entry = {"role": role, "model": body.get("model"), "tools": _tool_names(body),
                     "msg_roles": [m.get("role") for m in body.get("messages", [])],
                     "last": json.dumps(body.get("messages", [])[-1:])[:400],
                     "auth": bool(self.headers.get("Authorization")),
                     "reply": reply, "t0": t0, "t1": time.time()}
            with LOCK, open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            model = body.get("model", "stub")
            if not body.get("stream"):
                msg = {"role": "assistant", "content": reply.get("text", "")}
                out = json.dumps({"id": "stub", "object": "chat.completion", "model": model,
                                  "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                            "total_tokens": 15}}).encode()
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
                delta = {"role": "assistant", "tool_calls": [
                    {"index": i, "id": f"call_{role}_{int(t0*1000)}_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                    for i, c in enumerate(reply["tool_calls"])]}
                events = [_chunk(model, delta), _chunk(model, {}, "tool_calls")]
            else:
                events = [_chunk(model, {"role": "assistant", "content": reply["text"]}),
                          _chunk(model, {}, "stop")]
            events[-1]["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            for e in events:
                self.wfile.write(f"data: {json.dumps(e)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
    return H


def serve(ports, script, log_path):
    """ports: [coordinator, w1, w2, w3]. Returns the servers (call .shutdown() on each)."""
    port_role = {p: i for i, p in enumerate(ports)}
    servers = []
    for p in ports:
        s = ThreadingHTTPServer(("127.0.0.1", p), make_handler(port_role, script, log_path))
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s)
    return servers


if __name__ == "__main__":
    base = int(sys.argv[1])
    serve([base, base + 1, base + 2, base + 3], Script(), sys.argv[2])
    while True:
        time.sleep(3600)
