"""Entry point: python3 -m live_dashboard.main --config /etc/live-dashboard.json"""
import argparse
import json
import logging
import os
import signal
import threading
import time

from . import collectors as C
from .server import PageMissing, make_server

log = logging.getLogger("live_dashboard")


def load_config(path):
    cfg = dict(C.DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ConfigError("config (must be a JSON object)")
        cfg.update(loaded)
    return cfg


class ConfigError(ValueError):
    """Names the offending key only -- never its value."""


_NUMERIC = ("retention_days", "sample_window_s", "max_stream_clients", "slot_count", "chat_ct", "router_ct",
            "stream_interval_s", "socket_timeout_s", "header_deadline_s", "max_connections")
_STRINGS = ("bind_host", "history_path", "chat_url", "embed_url", "rerank_url", "router_url", "drm_root",
            "hwmon_root", "proc_root")
_STR_LISTS = ("gpu_buses", "disk_devices", "journal_argv")


def validate_config(cfg):
    if not isinstance(cfg, dict):
        raise ConfigError("config (must be a JSON object)")
    port = cfg.get("bind_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("bind_port")
    for k in _STRINGS:
        if k in cfg and not (isinstance(cfg[k], str) and cfg[k]):
            raise ConfigError(k)
    for k in _NUMERIC:
        v = cfg.get(k)
        if k in cfg and (isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v <= 0
                         or v == float("inf")):
            raise ConfigError(k)
    for k in _STR_LISTS:
        v = cfg.get(k)
        if k in cfg and not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
            raise ConfigError(k)


def _every(period, fn, stop, name="x"):
    def run():
        while not stop.is_set():
            t0 = time.time()
            try:
                fn(t0)
            except Exception as e:  # noqa: BLE001
                log.error("poller %s failed: %s", name, type(e).__name__)
            stop.wait(max(0.0, period - (time.time() - t0)))
    t = threading.Thread(target=run, daemon=True, name=f"poll-{name}")
    t.start()
    return t


def run_service(hub, cfg, srv, stop, follower=None):
    """Start pollers + follower, serve until `stop` is set, then shut everything down cleanly."""
    pollers = [
        _every(1.0, hub.poll_fast, stop, "fast"),
        _every(5.0, hub.poll_services, stop, "services"),
        _every(60.0, hub.poll_timer, stop, "timer"),
        _every(30.0, hub.poll_escalation, stop, "escalation"),
        _every(86400.0, hub.history.prune, stop, "prune"),
    ]
    if follower is not None:
        follower.start()
    server_t = threading.Thread(target=srv.serve_forever, daemon=True, name="http")
    server_t.start()
    log.info("live dashboard on %s:%s", cfg["bind_host"], srv.server_address[1])
    try:
        while not stop.wait(0.5):
            pass
    finally:
        stop.set()
        srv.stop_event.set()            # ends open SSE streams
        srv.shutdown()
        server_t.join(5)
        srv.server_close()
        if follower is not None:
            follower.stop()
        for t in pollers:
            t.join(5)
        hub.history.close()
        log.info("live dashboard stopped")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/live-dashboard.json")
    ap.add_argument("--static-dir", default=os.path.join(os.path.dirname(__file__), "static"))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(a.config)
        validate_config(cfg)
    except ConfigError as e:
        log.error("invalid config: %s", e)
        return 2
    except (OSError, ValueError) as e:
        log.error("cannot load config %s: %s", a.config, type(e).__name__)
        return 2
    try:
        os.makedirs(os.path.dirname(cfg["history_path"]), exist_ok=True)
    except OSError as e:
        log.error("cannot create history directory: %s", type(e).__name__)
        return 2
    hub = C.Hub(cfg, C.default_http_get, C.default_read_text, C.default_run_cmd)
    try:
        srv = make_server(hub, cfg, a.static_dir)
    except PageMissing:
        log.error("missing static/index.html in %s", a.static_dir)
        hub.history.close()
        return 1
    except OSError as e:
        log.error("cannot start server on %s:%s: %s", cfg.get("bind_host"), cfg.get("bind_port"), e.strerror or type(e).__name__)
        hub.history.close()
        return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    follower = C.JournalFollower(cfg["journal_argv"], on_line=lambda l: hub.on_journal_line(l, time.time()),
                                 on_state=lambda up: hub.on_journal_state(up, time.time()))
    run_service(hub, cfg, srv, stop, follower)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
