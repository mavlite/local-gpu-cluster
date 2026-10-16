"""Collectors and the Hub (spec §2, §5). All cluster I/O is injected (http_get, read_text, run_cmd) so the Hub is
testable without a cluster; main.py wires the real implementations. Keys come from the host's /proc, live in memory
only, and never appear in snapshots or logs. No network or /proc I/O happens while Hub.lock is held: pollers fetch
first, then take the lock only to parse/apply state."""
import json
import logging
import math
import os
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
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
    "max_stream_clients": 8, "slot_count": 3, "chat_ct": 151, "router_ct": 153, "proc_root": "/proc",
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
        return sorted((p for p in os.listdir(proc_root) if p.isdigit()), key=int)
    except OSError:                                  # no /proc (e.g. tests on Windows): nothing to find
        return []


def _in_ct(proc_root, pid, ct):
    """True when the process lives in LXC <ct> (its host cgroup path contains /lxc/<ct>/)."""
    try:
        with open(os.path.join(proc_root, pid, "cgroup"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    tag = f"/lxc/{ct}"
    return any(tag + "/" in ln or ln.endswith(tag) for ln in lines)


def _is_llama(a, port):
    if not a or not a[0].endswith("llama-server") or "--port" not in a:
        return False
    return a[a.index("--port") + 1:a.index("--port") + 2] == [str(port)]


def port_of(url, default=8080):
    try:
        return urllib.parse.urlparse(url).port or default
    except ValueError:
        return default


def find_llama_key(proc_root: str = "/proc", port="8080", ct=151):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if _is_llama(a, port) and _in_ct(proc_root, pid, ct):
            if "--api-key" in a and a.index("--api-key") + 1 < len(a):
                return a[a.index("--api-key") + 1]
    return None


def find_router_key(proc_root: str = "/proc", ct=153):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if any(x.endswith("uvicorn") for x in a) and "app:app" in a and _in_ct(proc_root, pid, ct):
            try:
                with open(os.path.join(proc_root, pid, "environ"), "rb") as f:
                    for kv in f.read().split(b"\0"):
                        if kv.startswith(b"ROUTER_API_KEY="):
                            return kv.split(b"=", 1)[1].decode()
            except OSError:
                continue
    return None


def find_chat_pid(proc_root: str = "/proc", port="8080", ct=151):
    for pid in _pids(proc_root):
        if _is_llama(_argv(proc_root, pid), port) and _in_ct(proc_root, pid, ct):
            return pid
    return None


def read_stamp(path):
    """Epoch seconds from a small regular file inside the (untrusted) container, or None on any problem. Never
    follows a final symlink, never blocks on a FIFO, reads at most 64 bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return float(os.read(fd, 64).decode("ascii", errors="replace").strip())
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def _finite(x):
    """Copy of x with every NaN/inf float replaced by None (json.dumps(allow_nan=False) must succeed)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    return x


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _txt(v, n=64):
    return v[:n] if isinstance(v, str) else None


def _pick(d, spec):
    """Whitelisted copy of dict d (None unless a dict); spec maps field -> converter."""
    if not isinstance(d, dict):
        return None
    return {k: conv(d.get(k)) for k, conv in spec.items()}


_SPEND = {"spent_usd": _num, "cap_usd": _num}
_CALL = {"ts": _num, "alias": _txt, "model": _txt, "origin": _txt, "input_tokens": _num, "output_tokens": _num,
         "cost_usd": _num, "outcome": _txt}


def sanitize_escalation(d):
    """Only the known numeric/short fields of /escalation/budget reach the LAN page (no free-text passthrough)."""
    if not isinstance(d, dict):
        return None
    recent = d.get("recent")
    return {"month": _pick(d.get("month"), _SPEND), "day": _pick(d.get("day"), _SPEND),
            "calls_left": _pick(d.get("calls_left"), {a: _num for a in ("claude-ask", "claude-auto", "claude-hard")}),
            "resets": _pick(d.get("resets"), {"day": _txt, "month": _txt}),
            "recent": [_pick(r, _CALL) for r in recent[:10] if isinstance(r, dict)] if isinstance(recent, list) else []}


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
    """Runs argv (pct exec ... journalctl -f), feeds each line to on_line; restarts with backoff when it exits.
    Reports "up" only once the first line arrives, so a fast-failing child never flaps the state."""
    def __init__(self, argv, on_line, on_state, backoff=(1, 2, 5, 10, 30)):
        self.argv, self.on_line, self.on_state, self.backoff = argv, on_line, on_state, backoff
        self._stop, self._proc, self._t = threading.Event(), None, None

    def start(self):
        self._t = threading.Thread(target=self._run, name="journal-follower", daemon=True)
        self._t.start()

    @staticmethod
    def _end(proc):
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(2)
        except Exception as e:  # noqa: BLE001
            log.warning("journal child cleanup failed: %s", type(e).__name__)

    def _cycle(self):
        """One child lifetime. Returns True when at least one line arrived."""
        got = False
        proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        self._proc = proc
        try:
            if self._stop.is_set():                    # stop() ran before _proc was published
                return False
            for line in proc.stdout:
                if not got:
                    got = True
                    self.on_state(True)
                try:
                    self.on_line(line.rstrip("\r\n"))
                except Exception as e:  # noqa: BLE001 -- a bad line must not end the stream
                    log.warning("journal on_line failed: %s", type(e).__name__)
                if self._stop.is_set():
                    break
        finally:
            self._end(proc)
        return got

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                if self._cycle():
                    attempt = 0
            except Exception as e:  # noqa: BLE001 -- spawn/read trouble of any kind still reaches the backoff
                log.warning("journal follower cycle failed: %s", type(e).__name__)
            self.on_state(False)
            if self._stop.wait(self.backoff[min(attempt, len(self.backoff) - 1)]):
                break
            attempt += 1

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc is not None:
            self._end(proc)
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


def _try(fn):
    """(result, None) or (None, exception type name). Exception text is never kept: it could carry a header value."""
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001 -- a poller must never die
        return None, type(e).__name__


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
        self._prev_host = None
        self._prev_embed = {}
        self.gpu_dirs = {}
        self.state = {"slots_raw": None, "metrics": {}, "gpus": [], "fans": {"pwm5_pct": None, "pwm6_pct": None},
                      "host": {}, "speed": {}, "services": {"router": {"ok": False, "upstream": {}, "profile": None},
                                                            "embed": {"ok": False, "activity_per_min": None},
                                                            "rerank": {"ok": False, "activity_per_min": None},
                                                            "restart": {"next_possible": None, "stale_since": None}},
                      "escalation": None}

    # ---- key lookup (reads /proc: always called outside the lock)
    def _find_llama_key(self):
        return find_llama_key(proc_root=self.cfg["proc_root"], port=str(port_of(self.cfg["chat_url"])),
                              ct=self.cfg["chat_ct"])

    def _find_router_key(self):
        return find_router_key(proc_root=self.cfg["proc_root"], ct=self.cfg["router_ct"])

    def _llama(self, path):
        if self._key is None:
            self._key = self._find_llama_key()
        status, body = self.http_get(self.cfg["chat_url"] + path, {"Authorization": f"Bearer {self._key or ''}"})
        if status == 401:
            self._key = self._find_llama_key()
        return status, body

    def _apply(self, name, fetched, fn, now, on_err=None):
        """Under the lock: record a failed fetch, or parse/apply the fetched data. Only that source is marked."""
        data, err = fetched
        if err is None:
            try:
                fn(data, now)
                return
            except Exception as e:  # noqa: BLE001
                err = type(e).__name__
        log.warning("source %s failed: %s", name, err)
        self.src[name].mark_fail(f"{name}: {err}", now)
        if on_err:
            on_err()

    # ---- fast sources
    def poll_fast(self, now):
        fetched = {"slots": _try(lambda: self._llama("/slots")),
                   "metrics": _try(lambda: self._llama("/metrics")),
                   "gpu": _try(self._read_gpus),
                   "host": _try(self._read_host)}
        with self.lock:
            self._apply("slots", fetched["slots"], self._apply_slots, now,
                        on_err=lambda: self.tracker.on_slots(None, now))
            self._apply("metrics", fetched["metrics"], self._apply_metrics, now)
            self._apply("gpu", fetched["gpu"], self._apply_gpu, now)
            self._apply("host", fetched["host"], self._apply_host, now)
            self._drain_finished()

    def _apply_slots(self, fetched, now):
        status, body = fetched
        if status != 200:
            self.src["slots"].mark_fail(f"/slots HTTP {status}", now)
            self.tracker.on_slots(None, now)
            return
        try:
            slots = P.parse_slots(json.loads(body))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            self.src["slots"].mark_fail(f"/slots parse: {type(e).__name__}", now)
            return
        self.src["slots"].mark_ok()
        self.state["slots_raw"] = slots
        self.tracker.on_slots(slots, now)

    def _apply_metrics(self, fetched, now):
        status, body = fetched
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

    def _read_gpus(self):
        """sysfs reads (outside the lock). Returns (gpus, fans, errors)."""
        if not self.gpu_dirs:
            self.gpu_dirs = P.find_gpu_dirs(self.cfg["drm_root"], self.cfg["gpu_buses"])
        gpus, errors = [], []
        for bus in self.cfg["gpu_buses"]:
            d = self.gpu_dirs.get(bus)
            g = P.read_gpu(d) if d else {"load_pct": None, "vram_used": None, "vram_total": None, "junction_c": None,
                                         "power_w": None, "power_cap_w": None, "link": None, "error": "card not found"}
            if g["error"]:
                errors.append(f"{bus}: {g['error']}")
            g = {k: v for k, v in g.items() if k != "error"}
            g["bus"] = bus
            gpus.append(g)
        if any("card not found" in e for e in errors):
            self.gpu_dirs = {}
        return gpus, P.read_fans(self.cfg["hwmon_root"]), errors

    def _apply_gpu(self, fetched, now):
        gpus, fans, errors = fetched
        for i, g in enumerate(gpus):
            ring = self.rings.get(f"gpu{i}_load")
            if ring:
                ring.push(now, g["load_pct"])
        self.state["gpus"], self.state["fans"] = gpus, fans
        if errors:
            self.src["gpu"].mark_fail("; ".join(errors), now)
        else:
            self.src["gpu"].mark_ok()

    def _read_host(self):
        """/proc reads (outside the lock), stamped with the time they were read."""
        busy, total = P.cpu_times(self.read_text("/proc/stat"))
        used, mem_total = P.mem_from_meminfo(self.read_text("/proc/meminfo"))
        rs, ws = P.disk_sectors(self.read_text("/proc/diskstats"), self.cfg["disk_devices"])
        arc, arc_max = P.arc_from_arcstats(self.read_text("/proc/spl/kstat/zfs/arcstats"))
        return {"busy": busy, "total": total, "used": used, "mem_total": mem_total, "rs": rs, "ws": ws,
                "arc": arc, "arc_max": arc_max, "t": self.now_fn()}

    def _apply_host(self, r, now):
        h = {"cpu_pct": None, "mem_used": r["used"], "mem_total": r["mem_total"], "disk_read_mbs": None,
             "disk_write_mbs": None, "arc_bytes": r["arc"], "arc_max": r["arc_max"]}
        p = self._prev_host
        if p:
            if r["total"] > p["total"] and r["busy"] >= p["busy"]:
                h["cpu_pct"] = 100.0 * (r["busy"] - p["busy"]) / (r["total"] - p["total"])
            dt = r["t"] - p["t"]
            if dt > 0:
                if r["rs"] >= p["rs"]:
                    h["disk_read_mbs"] = (r["rs"] - p["rs"]) * 512 / 1e6 / dt
                if r["ws"] >= p["ws"]:
                    h["disk_write_mbs"] = (r["ws"] - p["ws"]) * 512 / 1e6 / dt
        self._prev_host = r
        self.rings["host_cpu"].push(now, h["cpu_pct"])
        self.state["host"] = h
        self.src["host"].mark_ok()

    # ---- services, timer, escalation
    def poll_services(self, now):
        def fetch():
            out = {"router": self.http_get(self.cfg["router_url"] + "/healthz", {})}
            for name in ("embed", "rerank"):
                out[name] = self.http_get(self.cfg[f"{name}_url"] + "/metrics",
                                          {"Authorization": f"Bearer {self._key or ''}"})
            return out
        fetched = _try(fetch)
        with self.lock:
            self._apply("services", fetched, self._apply_services, now)

    def _apply_services(self, out, now):
        sv = self.state["services"]
        status, body = out["router"]
        try:
            hz = json.loads(body) if status == 200 else None
        except ValueError:
            hz = None
        if not isinstance(hz, dict):
            hz = None
        up = hz.get("upstream") if hz else None
        up = {str(k)[:32]: _txt(v, 32) for k, v in list(up.items())[:8]} if isinstance(up, dict) else {}
        sv["router"] = {"ok": bool(hz and hz.get("ok") is True), "upstream": up,
                        "profile": _txt(hz.get("active_chat_profile")) if hz else None}
        for name in ("embed", "rerank"):
            st, mb = out[name]
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
        def fetch():
            rc, out = self.run_cmd(["pct", "exec", str(self.cfg["chat_ct"]), "--", "systemctl", "show",
                                    "llamacpp-chat-restart.timer", "-p", "NextElapseUSecRealtime"])
            nxt = P.parse_next_elapse(out) if rc == 0 else None
            stamp = None
            pid = find_chat_pid(proc_root=self.cfg["proc_root"], port=str(port_of(self.cfg["chat_url"])),
                                ct=self.cfg["chat_ct"])
            if pid:
                stamp = read_stamp(os.path.join(self.cfg["proc_root"], pid, "root", "var", "lib", "llamacpp",
                                                "last-proactive-restart"))
            return nxt, stamp
        data, err = _try(fetch)
        with self.lock:
            r = self.state["services"]["restart"]
            nxt, stamp = data if data else (None, None)
            cands = [x for x in (nxt, stamp + 43200 if stamp else None) if x]
            fresh = err is None and nxt is not None
            if err:
                log.warning("restart timer poll failed: %s", err)
            self.state["services"]["restart"] = {
                "next_possible": max(cands) if cands else r.get("next_possible"),
                "stale_since": None if fresh else (r.get("stale_since") or now)}

    def poll_escalation(self, now):
        def fetch():
            if self._router_key is None:
                self._router_key = self._find_router_key()
            status, body = self.http_get(self.cfg["router_url"] + "/escalation/budget",
                                         {"Authorization": f"Bearer {self._router_key or ''}"})
            if status == 401:
                self._router_key = self._find_router_key()
            return status, body
        fetched = _try(fetch)
        with self.lock:
            self._apply("escalation", fetched, self._apply_escalation, now)

    def _apply_escalation(self, fetched, now):
        status, body = fetched
        try:
            esc = sanitize_escalation(json.loads(body)) if status == 200 else None
        except ValueError:
            esc = None
        self.state["escalation"] = esc
        if esc is not None:
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
        with self.lock:                 # _finite deep-copies, so nothing shared escapes the lock
            return _finite(self._snapshot(now))

    def _snapshot(self, now):
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
