"""live_dashboard.server / main -- routes, SSE cadence, client cap, caps on query params, no secrets, hardening."""
import http.client
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard import main as M  # noqa: E402
from live_dashboard import server as S  # noqa: E402


class FakeHistory:
    def __init__(self):
        self.calls = []
        self.closed = False

    def recent(self, limit):
        self.calls.append(("recent", limit))
        return [{"key": "k", "status": "done"}]

    def bands(self, days, now):
        self.calls.append(("bands", days))
        return [{"band": "0-8K", "n": 1, "gen_tok_s": 40.0, "prefill_tok_s": 500.0}]

    def prune(self, now):
        return 0

    def close(self):
        self.closed = True


class FakeHub:
    def __init__(self, pad=0):
        self.history = FakeHistory()
        self.n = 0
        self.pad = pad
        self.bad = False
        self.series_val = {"gen": [[1, 2.0]]}

    def snapshot(self, now):
        self.n += 1
        if self.bad and self.n % 2:
            return {"ts": now, "v": float("nan")}
        return {"ts": now, "n": self.n, "pad": "x" * self.pad}

    def series(self):
        return self.series_val

    def poll_fast(self, now):
        pass

    poll_services = poll_timer = poll_escalation = poll_fast


def start(tmp_path, max_clients=2, interval=0.05, hub=None, **extra):
    (tmp_path / "index.html").write_text("<!DOCTYPE html><title>Live</title>")
    hub = hub or FakeHub()
    cfg = {"bind_host": "127.0.0.1", "bind_port": 0, "max_stream_clients": max_clients, "stream_interval_s": interval}
    cfg.update(extra)
    srv = S.make_server(hub, cfg, str(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hub


def stop(srv):
    srv.stop_event.set()
    srv.shutdown()
    srv.server_close()


def get(srv, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request(method, path)
    r = c.getresponse()
    return r.status, r.read().decode(), r.getheader("Content-Type")


def test_routes(tmp_path):
    srv, hub = start(tmp_path)
    try:
        assert get(srv, "/")[0] == 200 and "Live" in get(srv, "/")[1]
        assert json.loads(get(srv, "/healthz")[1]) == {"status": "ok"}
        assert json.loads(get(srv, "/api/series")[1]) == {"gen": [[1, 2.0]]}
        st, body, _ = get(srv, "/api/requests?limit=99999")
        assert st == 200 and json.loads(body)["rows"][0]["key"] == "k" and ("recent", 99999) in hub.history.calls
        st, body, _ = get(srv, "/api/history?days=400")
        assert st == 200 and json.loads(body)["days"] == 30
        assert get(srv, "/api/requests?limit=abc")[0] == 400
        assert get(srv, "/api/requests?limit=" + "9" * 5000)[0] == 400
        assert get(srv, "/nope")[0] == 404
        assert get(srv, "/../etc/passwd")[0] == 404
    finally:
        stop(srv)


def test_unknown_query_params_ignored(tmp_path):
    srv, hub = start(tmp_path)
    try:
        assert get(srv, "/api/history?days=3&evil=1&path=/etc/passwd")[0] == 200
        assert ("bands", 3) in hub.history.calls
    finally:
        stop(srv)


def test_head_has_no_body_and_other_methods_405(tmp_path):
    srv, _ = start(tmp_path)
    try:
        st, body, _ = get(srv, "/healthz", "HEAD")
        assert st == 200 and body == ""
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request(m, "/api/series", body=b"x=1")
            r = c.getresponse()
            assert r.status == 405 and r.getheader("Allow") == "GET, HEAD", m
            r.read()
            c.close()
    finally:
        stop(srv)


def test_non_finite_json_is_500_not_crash(tmp_path):
    hub = FakeHub()
    hub.series_val = {"gen": [[1, float("nan")]]}
    srv, _ = start(tmp_path, hub=hub)
    try:
        st, body, _ = get(srv, "/api/series")
        assert st == 500 and "NaN" not in body
        assert get(srv, "/healthz")[0] == 200            # server still alive
    finally:
        stop(srv)


def read_events(srv, n, timeout=5):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=timeout)
    c.request("GET", "/api/stream")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    events = []
    buf = b""
    while len(events) < n:
        buf += r.read1(4096)
        while b"\n\n" in buf:
            chunk, buf = buf.split(b"\n\n", 1)
            if chunk.startswith(b"data: "):
                events.append(json.loads(chunk[6:]))
    r.close()  # HTTPResponse holds the socket via makefile(); c.close() alone leaves it open
    c.close()
    return events


def test_stream_sends_snapshots(tmp_path):
    srv, _ = start(tmp_path)
    try:
        ev = read_events(srv, 3)
        assert [e["n"] for e in ev] == sorted(e["n"] for e in ev) and len(ev) == 3
    finally:
        stop(srv)


def test_stream_skips_unserialisable_event(tmp_path):
    hub = FakeHub()
    hub.bad = True
    srv, _ = start(tmp_path, hub=hub)
    try:
        ev = read_events(srv, 3)
        assert all("v" not in e for e in ev)
    finally:
        stop(srv)


def wait_for(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_client_cap_and_slot_released(tmp_path):
    srv, _ = start(tmp_path, max_clients=1)
    try:
        c1 = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c1.request("GET", "/api/stream")
        r1 = c1.getresponse()
        assert r1.status == 200
        r1.read1(100)
        assert get(srv, "/api/stream")[0] == 503
        r1.close()  # the response's makefile() reference keeps the socket alive until closed
        c1.close()
        deadline = time.time() + 5
        ok = False
        while time.time() < deadline and not ok:
            time.sleep(0.1)
            try:
                ok = len(read_events(srv, 1, timeout=2)) == 1
            except AssertionError:
                ok = False
        assert ok
    finally:
        stop(srv)


def test_stalled_client_slot_freed(tmp_path):
    """Client connects, never reads: the write blocks, times out, and the slot is returned."""
    hub = FakeHub(pad=512 * 1024)
    srv, _ = start(tmp_path, max_clients=1, interval=0.01, hub=hub, socket_timeout_s=0.5)
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.connect(("127.0.0.1", srv.server_address[1]))
        s.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\n\r\n")
        assert wait_for(lambda: srv.client_count() == 1)
        # never recv(); socket buffers fill, the server's send times out
        assert wait_for(lambda: srv.client_count() == 0, timeout=15), "stalled client still holds a slot"
    finally:
        s.close()
        stop(srv)


def test_shutdown_ends_open_streams(tmp_path):
    srv, _ = start(tmp_path, max_clients=1)
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request("GET", "/api/stream")
    r = c.getresponse()
    r.read1(10)
    srv.stop_event.set()
    assert wait_for(lambda: srv.client_count() == 0)
    r.close()
    c.close()
    srv.shutdown()
    srv.server_close()


def test_main_bind_failure_exits_nonzero(tmp_path, caplog):
    (tmp_path / "index.html").write_text("x")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    cfgp = tmp_path / "c.json"
    cfgp.write_text(json.dumps({"bind_host": "127.0.0.1", "bind_port": blocker.getsockname()[1],
                                "history_path": str(tmp_path / "h" / "h.db"), "chat_url": "http://127.0.0.1:1"}))
    try:
        with caplog.at_level("ERROR"):
            rc = M.main(["--config", str(cfgp), "--static-dir", str(tmp_path)])
    finally:
        blocker.close()
    assert rc == 1 and "cannot start server" in caplog.text


def test_main_bad_config_exits_nonzero(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert M.main(["--config", str(p)]) == 2


class FakeFollower:
    started = stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_run_service_clean_shutdown(tmp_path):
    (tmp_path / "index.html").write_text("x")
    hub = FakeHub()
    cfg = {"bind_host": "127.0.0.1", "bind_port": 0}
    srv = S.make_server(hub, cfg, str(tmp_path))
    ev = threading.Event()
    f = FakeFollower()
    t = threading.Thread(target=M.run_service, args=(hub, cfg, srv, ev, f))
    t.start()
    assert wait_for(lambda: f.started)
    assert wait_for(lambda: get(srv, "/healthz")[0] == 200)
    ev.set()
    t.join(15)
    assert not t.is_alive() and f.stopped and hub.history.closed
