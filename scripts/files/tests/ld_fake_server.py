"""Manual check helper (not a test): serve the live dashboard page against a fake hub.

    python scripts/files/tests/ld_fake_server.py [port]      # default 8899, bound to 127.0.0.1

The snapshot exercises every panel: 3 slots (reading / generating / idle), both GPUs, escalation,
recent rows (including a slow and an interrupted one) and one stale source (journal).
Stdlib + the repo's live_dashboard.server only; never contacts the cluster.
"""
import copy
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard import server as S  # noqa: E402

STATIC = os.path.join(HERE, "..", "live_dashboard", "static")
GIB = 1024 ** 3


class FakeHistory:
    def recent(self, limit):
        return _rows(time.time())[:limit]

    def bands(self, days, now):
        gen = (48.0, 44.0, 38.0, 31.0, 24.0)
        pre = (610.0, 560.0, 420.0, 330.0, 260.0)
        names = ("0-8K", "8-32K", "32-64K", "64-96K", "96-128K")
        return [{"band": n, "n": 40 - 7 * i, "gen_tok_s": g, "prefill_tok_s": p}
                for i, (n, g, p) in enumerate(zip(names, gen, pre))]


def _row(now, ago, slot, status, prompt, cached, out, pre, gen, dur, slow=False, open_=False):
    return {"key": f"fake-{slot}-{int(ago)}", "server_run": now - 86400, "slot": slot, "task": int(ago),
            "started": now - ago - (dur or 5), "finished": None if open_ else now - ago, "status": status,
            "prompt_tokens": prompt, "new_prompt_tokens": None if prompt is None or cached is None else prompt - cached,
            "cached_tokens": cached, "output_tokens": out, "prefill_tok_s": pre, "gen_tok_s": gen,
            "duration_s": dur, "draft_accept": None, "slow": slow}


def _rows(now):
    return [
        _row(now, 0, 0, "reading", 36742, 29104, None, 612.0, None, 12.8, open_=True),
        _row(now, 4, 1, "generating", 12772, 11940, 1392, 540.0, 48.6, 30.1, open_=True),
        _row(now, 40, 2, "done", 626, 0, 11, 410.0, 26.7, 4.0),
        _row(now, 200, 0, "done", 10437, 9980, 6615, 498.0, 40.0, 195.2),
        _row(now, 330, 1, "done", 68210, 2400, 540, 331.0, 33.9, 215.5, slow=True),
        _row(now, 500, 0, "interrupted", 9881, None, None, None, None, None),
        _row(now, 560, 2, "cancelled", 1200, 0, 3, 300.0, None, 1.2),
        _row(now, 700, 1, "done", 9881, 9880, 230, None, 47.2, 5.1),
    ]


class FakeHub:
    def __init__(self):
        self.history = FakeHistory()
        self.t0 = time.time()

    def snapshot(self, now):
        w = math.sin((now - self.t0) / 4.0)
        slot = lambda i, state, **kw: dict({  # noqa: E731
            "id": i, "state": state, "task": 100 + i, "progress": None, "prompt_tokens": None,
            "processed_tokens": None, "cached_tokens": None, "output_tokens": None, "tok_s": None,
            "ctx_used": None, "n_ctx": 131072, "started": now - 20, "last_done": None}, **kw)
        gpu = lambda bus, load, used, temp, power: {  # noqa: E731
            "bus": bus, "load_pct": load, "vram_used": int(used * GIB), "vram_total": 32 * GIB,
            "junction_c": temp, "power_w": power, "power_cap_w": 180.0, "link": "PCIe Gen4 x16"}
        ok = {"ok": True, "stale_since": None, "error": None}
        sources = {k: dict(ok) for k in ("slots", "metrics", "journal", "gpu", "host", "services", "escalation")}
        sources["journal"] = {"ok": False, "stale_since": now - 90, "error": "journalctl exited (fake)"}
        return {
            "ts": now, "sources": sources,
            "model": {"profile": "qwen3.8", "n_ctx": 131072, "slot_count": 3},
            "slots": [
                slot(0, "reading", progress=0.97, prompt_tokens=36742, processed_tokens=35610,
                     cached_tokens=29104, tok_s=612.0 + 20 * w, ctx_used=37 * 1024),
                slot(1, "generating", prompt_tokens=12772, cached_tokens=11940, output_tokens=1392,
                     tok_s=48.6 + 3 * w, ctx_used=14 * 1024),
                slot(2, "idle", last_done=now - 4, prompt_tokens=626, cached_tokens=0, ctx_used=18 * 1024),
            ],
            "queue": {"deferred": 0, "processing": 2},
            "speed": {"gen_tok_s": 48.6 + 3 * w, "prefill_tok_s": 612.0 + 20 * w, "gen_avg15": 44.1,
                      "prefill_avg15": 498.0, "cache_reuse_pct15": 88.0, "mtp_accept": 0.81,
                      "mtp_pos": [0.92, 0.81, 0.70]},
            "gpus": [gpu("0000:03:00.0", 91.0 + 4 * w, 28.1, 84.0, 176.0), gpu("0000:06:00.0", 88.0 - 4 * w, 24.6, 81.0, 171.0)],
            "fans": {"pwm5_pct": 90.0, "pwm6_pct": 90.0},
            "host": {"cpu_pct": 14.0 + 3 * w, "mem_used": 24 * GIB, "mem_total": 124 * GIB, "disk_read_mbs": 0.1,
                     "disk_write_mbs": 0.4, "arc_bytes": 11 * GIB, "arc_max": 32 * GIB},
            "services": {"router": {"ok": True, "upstream": {}, "profile": "qwen3.8"},
                         "embed": {"ok": True, "activity_per_min": 12.0},
                         "rerank": {"ok": True, "activity_per_min": 0.0},
                         "restart": {"next_possible": now + 3 * 3600, "stale_since": now - 30}},
            "escalation": {"month": {"spent_usd": 37.5, "cap_usd": 150.0}, "day": {"spent_usd": 1.2, "cap_usd": 7.0},
                           "calls_left": {"claude-ask": 14, "claude-auto": 5, "claude-hard": 3},
                           "resets": {"day": "2026-10-17", "month": "2026-11-01"},
                           "recent": [{"ts": now - 300, "alias": "claude-ask", "model": "claude-sonnet", "origin": "opencode",
                                       "input_tokens": 1540, "output_tokens": 327, "cost_usd": 0.0065, "outcome": "ok"}]},
            "recent": _rows(now), "history_ok": True,
        }

    def series(self):
        now = time.time()
        pts = lambda base, amp: [[now - 900 + i * 10, base + amp * math.sin(i / 7.0)] for i in range(90)]  # noqa: E731
        return copy.deepcopy({"gen": pts(46, 5), "prefill": pts(500, 100), "cache": pts(88, 6), "mtp": pts(0.8, 0.05),
                              "gpu0_load": pts(85, 10), "gpu1_load": pts(82, 10), "host_cpu": pts(14, 4)})


def main(argv):
    port = int(argv[1]) if len(argv) > 1 else 8899
    cfg = {"bind_host": "127.0.0.1", "bind_port": port, "stream_interval_s": 1.0, "max_stream_clients": 8}
    srv = S.make_server(FakeHub(), cfg, STATIC)
    print(f"fake dashboard on http://127.0.0.1:{port}/  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop_event.set()
        srv.server_close()


if __name__ == "__main__":
    main(sys.argv)
