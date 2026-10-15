"""A stand-in for the Anthropic Messages API: records every request (headers + JSON body) and replies from a
queue of (status, body) pairs, falling back to a short successful answer."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT = {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "end_turn",
           "content": [{"type": "text", "text": "Root cause: X. Fix: Y. Verify: pytest -q"}],
           "usage": {"input_tokens": 1000, "output_tokens": 200}}


class AnthropicStub:
    def __init__(self):
        self.requests, self.responses = [], []
        self.delay = 0.0                                    # seconds to wait before answering
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                stub.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                status, obj = stub.responses.pop(0) if stub.responses else (200, DEFAULT)
                if stub.delay:
                    time.sleep(stub.delay)
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()   # bytes = raw body
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionError):
                    pass                                    # the client gave up (timeout / cancel)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/messages"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
