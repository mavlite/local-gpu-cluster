"""HTTP + SSE for the live dashboard (spec §2, §5). Read-only; numbers only; client cap on the stream."""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("live_dashboard.server")
_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError)
_MAX_DIGITS = 9


def _dumps(obj) -> bytes:
    """Strict JSON: NaN/Infinity are a ValueError, never emitted."""
    return json.dumps(obj, allow_nan=False).encode()


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, _GONE):      # peers hanging up is routine, not a traceback
            return
        log.error("request handler failed: %s", type(exc).__name__)


def make_server(hub, cfg, static_dir):
    lock = threading.Lock()
    clients = {"n": 0}
    stop_event = threading.Event()
    interval = float(cfg.get("stream_interval_s", 1.0))
    cap = int(cfg.get("max_stream_clients", 8))
    io_timeout = float(cfg.get("socket_timeout_s", 10.0))
    with open(os.path.join(static_dir, "index.html"), "rb") as f:
        page = f.read()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = io_timeout    # applies to every read AND write: a stalled peer cannot pin a thread forever

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=()):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, code, obj):
            try:
                data = _dumps(obj)
            except (ValueError, TypeError) as e:
                log.error("response not serialisable: %s", type(e).__name__)
                code, data = 500, b'{"error": "internal error"}'
            return self._send(code, data)

        def _int(self, q, name, default):
            v = q.get(name, [str(default)])[0]
            if not v.isdigit() or len(v) > _MAX_DIGITS:
                raise ValueError(name)
            return int(v)

        def _handle(self):
            u = urlparse(self.path)
            q = parse_qs(u.query, max_num_fields=20)
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, page, "text/html; charset=utf-8")
                if u.path == "/healthz":
                    return self._json(200, {"status": "ok"})
                if u.path == "/api/series":
                    return self._json(200, hub.series())
                if u.path == "/api/requests":
                    return self._json(200, {"rows": hub.history.recent(self._int(q, "limit", 50))})
                if u.path == "/api/history":
                    days = max(1, min(self._int(q, "days", 7), 30))
                    return self._json(200, {"bands": hub.history.bands(days, time.time()), "days": days})
                if u.path == "/api/stream":
                    return self._stream()
            except ValueError as e:
                return self._json(400, {"error": f"bad parameter {e}"})
            return self._json(404, {"error": "not found"})

        def do_GET(self):  # noqa: N802
            try:
                self._handle()
            except _GONE:
                self.close_connection = True    # peer vanished mid-response; nothing left to tell it

        do_HEAD = do_GET  # noqa: N815

        def _not_allowed(self):
            self.close_connection = True    # an unread body must not be parsed as the next request
            self._send(405, _dumps({"error": "method not allowed"}), extra=(("Allow", "GET, HEAD"), ("Connection", "close")))

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

        def _event(self):
            """One SSE event, or None when this snapshot cannot be produced/serialised (skipped, not fatal)."""
            try:
                return b"data: " + _dumps(hub.snapshot(time.time())) + b"\n\n"
            except Exception as e:  # noqa: BLE001
                log.warning("stream event skipped: %s", type(e).__name__)
                return None

        def _stream(self):
            if self.command == "HEAD":
                return self._send(200, b"", "text/event-stream")
            with lock:
                if clients["n"] >= cap:
                    return self._json(503, {"error": "too many live viewers", "retry_after_s": 10})
                clients["n"] += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while not stop_event.is_set():
                    ev = self._event()
                    if ev is not None:
                        self.wfile.write(ev)
                        self.wfile.flush()
                    stop_event.wait(interval)
            except _GONE:
                pass
            finally:
                with lock:
                    clients["n"] -= 1

    srv = _Server((cfg["bind_host"], int(cfg["bind_port"])), Handler)
    srv.daemon_threads = True
    srv.stop_event = stop_event
    srv.client_count = lambda: clients["n"]
    return srv
