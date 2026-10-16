"""Collectors and the Hub (spec §2, §5). All cluster I/O is injected (http_get, read_text, run_cmd) so the Hub is
testable without a cluster; main.py wires the real implementations. Keys come from the host's /proc, live in memory
only, and never appear in snapshots or logs."""
import glob
import json
import logging
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import parsers as P
from .history import History
from .tracker import Tracker

log = logging.getLogger("live_dashboard.collectors")
DEFAULTS = {
    "bind_host": "192.168.6.175", "bind_port": 8889, "chat_url": "http://192.168.6.151:8080",
    "embed_url": "http://192.168.6.151:8082", "rerank_url": "http://192.168.6.151:8083",
    "router_url": "http://192.168.6.153:8000", "gpu_buses": ["0000:03:00.0", "0000:07:00.0"],
    "drm_root": "/sys/class/drm", "hwmon_root": "/sys/class/hwmon", "disk_devices": ["nvme1n1", "nvme2n1"],
    "history_path": "/var/lib/live-dashboard/history.db", "retention_days": 30, "sample_window_s": 900,
    "max_stream_clients": 8, "slot_count": 3, "chat_ct": 151,
    "journal_argv": ["pct", "exec", "151", "--", "journalctl", "-u", "llamacpp-chat", "-f", "-o", "cat", "-n", "0"],
}
SOURCES = ("slots", "metrics", "journal", "gpu", "host", "services", "escalation")


def _argv(proc_root, pid):
    try:
        with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _pids(proc_root):
    try:
        return [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:                                  # no /proc (e.g. tests on Windows): nothing to find
        return []


def find_llama_key(proc_root: str = "/proc", port: str = "8080"):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if a and a[0].endswith("llama-server") and "--port" in a and a[a.index("--port") + 1:a.index("--port") + 2] == [port]:
            if "--api-key" in a and a.index("--api-key") + 1 < len(a):
                return a[a.index("--api-key") + 1]
    return None


def find_router_key(proc_root: str = "/proc"):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if any(x.endswith("uvicorn") for x in a) and "app:app" in a:
            try:
                with open(os.path.join(proc_root, pid, "environ"), "rb") as f:
                    for kv in f.read().split(b"\0"):
                        if kv.startswith(b"ROUTER_API_KEY="):
                            return kv.split(b"=", 1)[1].decode()
            except OSError:
                continue
    return None


def find_chat_pid(proc_root: str = "/proc"):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if a and a[0].endswith("llama-server") and "--port" in a and a[a.index("--port") + 1:a.index("--port") + 2] == ["8080"]:
            return pid
    return None


def _finite(x):
    """Copy of x with every NaN/inf float replaced by None (json.dumps(allow_nan=False) must succeed)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x


class Source:
    def __init__(self, name):
        self.name, self.ok, self.stale_since, self.error = name, False, None, "not polled yet"

    def mark_ok(self):
        self.ok, self.stale_since, self.error = True, None, None

    def mark_fail(self, err, now):
        if self.ok or self.stale_since is None:
            self.stale_since = now
        self.ok, self.error = False, str(err)[:200]

    def as_dict(self):
        return {"ok": self.ok, "stale_since": self.stale_since, "error": self.error}


class Ring:
    def __init__(self, window_s: float):
        self.window_s, self.pts = window_s, []

    def push(self, t, v):
        if v is None:
            return
        self.pts.append([t, v])
        cutoff = t - self.window_s
        while self.pts and self.pts[0][0] < cutoff:
            self.pts.pop(0)

    def values(self):
        return [list(p) for p in self.pts]

    def avg(self):
        return sum(v for _, v in self.pts) / len(self.pts) if self.pts else None


class JournalFollower:
    """Runs argv (pct exec ... journalctl -f), feeds each line to on_line; restarts with backoff when it exits."""
    def __init__(self, argv, on_line, on_state, backoff=(1, 2, 5, 10, 30)):
        self.argv, self.on_line, self.on_state, self.backoff = argv, on_line, on_state, backoff
        self._stop, self._proc, self._t = threading.Event(), None, None

    def start(self):
        self._t = threading.Thread(target=self._run, name="journal-follower", daemon=True)
        self._t.start()

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                              text=True, bufsize=1)
                self.on_state(True)
                for line in self._proc.stdout:
                    attempt = 0
                    self.on_line(line.rstrip("\n"))
                    if self._stop.is_set():
                        break
                self._proc.wait()
            except OSError as e:
                log.warning("journal follower failed to start: %s", e)
            self.on_state(False)
            if self._stop.wait(self.backoff[min(attempt, len(self.backoff) - 1)]):
                break
            attempt += 1

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._t:
            self._t.join(5)


def default_http_get(url, headers, timeout=3.0):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def default_read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def default_run_cmd(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


class Hub:
    def __init__(self, cfg, http_get, read_text, run_cmd, now_fn=time.time):
        self.cfg = cfg
        self.http_get, self.read_text, self.run_cmd, self.now_fn = http_get, read_text, run_cmd, now_fn
        self.lock = threading.Lock()
        self.src = {n: Source(n) for n in SOURCES}
        self.tracker = Tracker(slot_count=cfg["slot_count"])
        self.history = History(cfg["history_path"], cfg["retention_days"])
        w = cfg["sample_window_s"]
        self.rings = {n: Ring(w) for n in ("gen", "prefill", "cache", "mtp", "gpu0_load", "gpu1_load", "host_cpu")}
        self.recent_done = []
        self._key = None
        self._router_key = None
        self._prev_metrics = None
        self._prev_cpu = None
        self._prev_disk = None
        self._prev_embed = {}
        self.gpu_dirs = {}
        self.state = {"slots_raw": None, "metrics": {}, "gpus": [], "fans": {"pwm5_pct": None, "pwm6_pct": None},
                      "host": {}, "speed": {}, "services": {"router": {"ok": False, "upstream": {}, "profile": None},
                                                            "embed": {"ok": False, "activity_per_min": None},
                                                            "rerank": {"ok": False, "activity_per_min": None},
                                                            "restart": {"next_possible": None}},
                      "escalation": None}

    # ---- llama
    def _llama(self, path):
        if self._key is None:
            self._key = find_llama_key()
        status, body = self.http_get(self.cfg["chat_url"] + path, {"Authorization": f"Bearer {self._key or ''}"})
        if status == 401:
            self._key = find_llama_key()
        return status, body

    def _guard(self, name, fn, now):
        """Run one source's poll; an unexpected exception marks only that source failed. Only the exception type
        is surfaced -- its text could carry a header value (key)."""
        try:
            fn(now)
        except Exception as e:  # noqa: BLE001 -- a poller must never die
            log.warning("source %s failed: %s", name, type(e).__name__)
            self.src[name].mark_fail(f"{name}: {type(e).__name__}", now)

    def poll_fast(self, now):
        with self.lock:
            self._guard("slots", self._poll_slots, now)
            self._guard("metrics", self._poll_metrics, now)
            self._guard("gpu", self._poll_gpu, now)
            self._guard("host", self._poll_host, now)
            self._drain_finished()

    def _poll_slots(self, now):
        status, body = self._llama("/slots")
        if status != 200:
            self.src["slots"].mark_fail(f"/slots HTTP {status}", now)
            self.tracker.on_slots(None, now)
            return
        try:
            slots = P.parse_slots(json.loads(body))
        except (ValueError, KeyError, TypeError) as e:
            self.src["slots"].mark_fail(f"/slots parse: {type(e).__name__}", now)
            return
        self.src["slots"].mark_ok()
        self.state["slots_raw"] = slots
        self.tracker.on_slots(slots, now)

    def _poll_metrics(self, now):
        status, body = self._llama("/metrics")
        if status != 200:
            self.src["metrics"].mark_fail(f"/metrics HTTP {status}", now)
            return
        m = P.parse_prometheus(body)
        self.src["metrics"].mark_ok()
        self.state["metrics"] = m
        sp = {}
        prev = self._prev_metrics
        if prev:
            def rate(tok, sec):
                dt = m.get(sec, 0) - prev.get(sec, 0)
                dn = m.get(tok, 0) - prev.get(tok, 0)
                return dn / dt if dt > 0 and dn >= 0 else None
            sp["gen_tok_s"] = rate("llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total")
            sp["prefill_tok_s"] = rate("llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total")
            new = m.get("llamacpp:prompt_tokens_total", 0) - prev.get("llamacpp:prompt_tokens_total", 0)
            cached = m.get("llamacpp:prompt_tokens_cached_total", 0) - prev.get("llamacpp:prompt_tokens_cached_total", 0)
            if new + cached > 0 and new >= 0 and cached >= 0:
                self.rings["cache"].push(now, 100.0 * cached / (new + cached))
            self.rings["gen"].push(now, sp["gen_tok_s"])
            self.rings["prefill"].push(now, sp["prefill_tok_s"])
        self._prev_metrics = m
        drafted = m.get("llamacpp:spec_decode_num_draft_tokens_total")
        acc = m.get("llamacpp:spec_decode_num_accepted_tokens_total")
        sp["mtp_accept"] = acc / drafted if drafted else None
        drafts = m.get("llamacpp:spec_decode_num_drafts_total")
        sp["mtp_pos"] = [m.get(f'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="{i}"}}') / drafts
                         if drafts and m.get(f'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="{i}"}}') is not None
                         else None for i in range(3)]
        self.rings["mtp"].push(now, sp["mtp_accept"])
        self.state["speed"] = sp

    def _poll_gpu(self, now):
        if not self.gpu_dirs:
            self.gpu_dirs = P.find_gpu_dirs(self.cfg["drm_root"], self.cfg["gpu_buses"])
        gpus, errors = [], []
        for i, bus in enumerate(self.cfg["gpu_buses"]):
            d = self.gpu_dirs.get(bus)
            g = P.read_gpu(d) if d else {"load_pct": None, "vram_used": None, "vram_total": None, "junction_c": None,
                                         "power_w": None, "power_cap_w": None, "link": None, "error": "card not found"}
            if g["error"]:
                errors.append(f"{bus}: {g['error']}")
            g = {k: v for k, v in g.items() if k != "error"}
            g["bus"] = bus
            gpus.append(g)
            self.rings[f"gpu{i}_load"].push(now, g["load_pct"])
        self.state["gpus"] = gpus
        self.state["fans"] = P.read_fans(self.cfg["hwmon_root"])
        if errors:
            self.src["gpu"].mark_fail("; ".join(errors), now)
            if any("card not found" in e for e in errors):
                self.gpu_dirs = {}
        else:
            self.src["gpu"].mark_ok()

    def _poll_host(self, now):
        try:
            busy, total = P.cpu_times(self.read_text("/proc/stat"))
            used, mem_total = P.mem_from_meminfo(self.read_text("/proc/meminfo"))
            rs, ws = P.disk_sectors(self.read_text("/proc/diskstats"), self.cfg["disk_devices"])
            arc, arc_max = P.arc_from_arcstats(self.read_text("/proc/spl/kstat/zfs/arcstats"))
        except (OSError, ValueError, KeyError, IndexError) as e:
            self.src["host"].mark_fail(f"host: {type(e).__name__}", now)
            return
        h = {"cpu_pct": None, "mem_used": used, "mem_total": mem_total, "disk_read_mbs": None,
             "disk_write_mbs": None, "arc_bytes": arc, "arc_max": arc_max}
        if self._prev_cpu:
            (pb, pt, pnow) = self._prev_cpu
            if total > pt:
                h["cpu_pct"] = 100.0 * (busy - pb) / (total - pt)
            if self._prev_disk and now > pnow:
                (pr, pw) = self._prev_disk
                h["disk_read_mbs"] = (rs - pr) * 512 / 1e6 / (now - pnow)
                h["disk_write_mbs"] = (ws - pw) * 512 / 1e6 / (now - pnow)
        self._prev_cpu, self._prev_disk = (busy, total, now), (rs, ws)
        self.rings["host_cpu"].push(now, h["cpu_pct"])
        self.state["host"] = h
        self.src["host"].mark_ok()

    # ---- services, timer, escalation
    def poll_services(self, now):
        with self.lock:
            self._guard("services", self._poll_services, now)

    def _poll_services(self, now):
        sv = self.state["services"]
        status, body = self.http_get(self.cfg["router_url"] + "/healthz", {})
        try:
            hz = json.loads(body) if status == 200 else None
        except ValueError:
            hz = None
        sv["router"] = {"ok": bool(hz and hz.get("ok")), "upstream": (hz or {}).get("upstream", {}),
                        "profile": (hz or {}).get("active_chat_profile")}
        for name in ("embed", "rerank"):
            st, mb = self.http_get(self.cfg[f"{name}_url"] + "/metrics",
                                   {"Authorization": f"Bearer {self._key or ''}"})
            m = P.parse_prometheus(mb) if st == 200 else {}
            cur = m.get("llamacpp:n_decode_total")
            prev = self._prev_embed.get(name)
            act = None
            if cur is not None and prev is not None and now > prev[1] and cur >= prev[0]:
                act = (cur - prev[0]) * 60.0 / (now - prev[1])
            if cur is not None:
                self._prev_embed[name] = (cur, now)
            sv[name] = {"ok": st == 200, "activity_per_min": act}
        if sv["router"]["ok"]:
            self.src["services"].mark_ok()
        else:
            self.src["services"].mark_fail(f"router /healthz HTTP {status}", now)

    def poll_timer(self, now):
        try:
            self._poll_timer(now)
        except Exception as e:  # noqa: BLE001
            log.warning("restart timer poll failed: %s", type(e).__name__)

    def _poll_timer(self, now):
        rc, out = self.run_cmd(["pct", "exec", str(self.cfg["chat_ct"]), "--", "systemctl", "show",
                                "llamacpp-chat-restart.timer", "-p", "NextElapseUSecRealtime"])
        nxt = P.parse_next_elapse(out) if rc == 0 else None
        stamp = None
        pid = find_chat_pid()
        if pid:
            try:
                stamp = float(self.read_text(f"/proc/{pid}/root/var/lib/llamacpp/last-proactive-restart").strip())
            except (OSError, ValueError):
                stamp = None
        cands = [x for x in (nxt, stamp + 43200 if stamp else None) if x]
        with self.lock:
            self.state["services"]["restart"] = {"next_possible": max(cands) if cands else None}

    def poll_escalation(self, now):
        try:
            self._poll_escalation(now)
        except Exception as e:  # noqa: BLE001
            log.warning("source escalation failed: %s", type(e).__name__)
            with self.lock:
                self.src["escalation"].mark_fail(f"escalation: {type(e).__name__}", now)

    def _poll_escalation(self, now):
        if self._router_key is None:
            self._router_key = find_router_key()
        status, body = self.http_get(self.cfg["router_url"] + "/escalation/budget",
                                     {"Authorization": f"Bearer {self._router_key or ''}"})
        if status == 401:
            self._router_key = find_router_key()
        with self.lock:
            try:
                self.state["escalation"] = json.loads(body) if status == 200 else None
            except ValueError:
                self.state["escalation"] = None
            if self.state["escalation"] is not None:
                self.src["escalation"].mark_ok()
            else:
                self.src["escalation"].mark_fail(f"/escalation/budget HTTP {status}", now)

    # ---- journal
    def on_journal_line(self, line, now):
        try:
            ev = P.parse_journal_line(line)
            if ev is None:
                return
            with self.lock:
                self.tracker.on_journal(ev, now)
                self._drain_finished()
        except Exception as e:  # noqa: BLE001 -- one bad line must not kill the follower thread
            log.warning("journal line dropped: %s", type(e).__name__)

    def on_journal_state(self, up, now):
        with self.lock:
            self.tracker.set_journal_up(up, now)
            if up:
                self.src["journal"].mark_ok()
            else:
                self.src["journal"].mark_fail("journal stream down", now)

    def _drain_finished(self):
        for row in self.tracker.pop_finished():
            try:
                self.history.add(row)
            except Exception as e:  # noqa: BLE001 -- history trouble must not lose the live row
                log.warning("history add failed: %s", type(e).__name__)
            self.recent_done.insert(0, row)
        del self.recent_done[50:]

    # ---- outputs
    def snapshot(self, now):
        return _finite(self._snapshot(now))

    def _snapshot(self, now):
        with self.lock:
            m, raw = self.state["metrics"], self.state["slots_raw"] or []
            views = self.tracker.slot_views(now)
            n_ctx = None
            for v in views:
                s = next((x for x in raw if x["id"] == v["id"]), None)
                if s:
                    v["n_ctx"] = s["n_ctx"]
                    n_ctx = n_ctx or s["n_ctx"]
            sp = dict(self.state["speed"])
            sp.setdefault("gen_tok_s", None)
            sp.setdefault("prefill_tok_s", None)
            sp.setdefault("mtp_accept", None)
            sp.setdefault("mtp_pos", [None, None, None])
            sp["gen_avg15"], sp["prefill_avg15"] = self.rings["gen"].avg(), self.rings["prefill"].avg()
            sp["cache_reuse_pct15"] = self.rings["cache"].avg()
            recent = (self.tracker.open_rows() + self.recent_done)[:20]
            return {"ts": now,
                    "sources": {n: s.as_dict() for n, s in self.src.items()},
                    "model": {"profile": self.state["services"]["router"]["profile"], "n_ctx": n_ctx,
                              "slot_count": self.cfg["slot_count"]},
                    "slots": views,
                    "queue": {"deferred": int(m["llamacpp:requests_deferred"]) if "llamacpp:requests_deferred" in m else None,
                              "processing": int(m["llamacpp:requests_processing"]) if "llamacpp:requests_processing" in m else None},
                    "speed": sp, "gpus": self.state["gpus"], "fans": self.state["fans"],
                    "host": {k: self.state["host"].get(k) for k in ("cpu_pct", "mem_used", "mem_total", "disk_read_mbs",
                                                                    "disk_write_mbs", "arc_bytes", "arc_max")},
                    "services": self.state["services"], "escalation": self.state["escalation"],
                    "recent": recent, "history_ok": self.history.ok}

    def series(self):
        with self.lock:
            return {n: r.values() for n, r in self.rings.items()}
