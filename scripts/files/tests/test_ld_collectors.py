"""live_dashboard.collectors -- Hub assembly from injected I/O; key discovery; journal follower restart."""
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
FX = os.path.join(HERE, "fixtures", "live_dashboard")

from live_dashboard import collectors as C  # noqa: E402


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


def fake_proc(tmp_path, entries):
    for pid, (argv, environ, *cg) in entries.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "cgroup").write_text(cg[0] if cg else "0::/lxc/151/ns/system.slice/x.service\n")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")
    (tmp_path / "self").mkdir()
    return str(tmp_path)


def test_key_discovery_from_proc(tmp_path):
    root = fake_proc(tmp_path, {
        11: (["/opt/llama.cpp/build/bin/llama-server", "--port", "8082", "--api-key", "EMBEDKEY"], {}),
        12: (["/opt/llama.cpp/build/bin/llama-server", "--port", "8080", "--api-key", "CHATKEY"], {}),
        13: (["/opt/llm-router/venv/bin/python3", "/opt/llm-router/venv/bin/uvicorn", "app:app"],
             {"ROUTER_API_KEY": "ROUTERKEY", "OTHER": "x"}, "0::/lxc/153/ns/system.slice/r.service\n"),
    })
    assert C.find_llama_key(root) == "CHATKEY"
    assert C.find_router_key(root) == "ROUTERKEY"
    assert C.find_llama_key(root, port="9999") is None


def test_ring_window_and_avg():
    r = C.Ring(10)
    for t, v in ((0, 10.0), (5, 20.0), (12, 30.0)):
        r.push(t, v)
    assert r.values() == [[5, 20.0], [12, 30.0]] and r.avg() == 25.0


class World:
    """Injected I/O standing in for the cluster."""
    def __init__(self, tmp_path):
        self.status = {}
        d = json.loads(fx("llama_slots_metrics_session.json"))
        self.slots = json.dumps(d["samples"][5]["slots"])
        self.metrics = ("llamacpp:prompt_tokens_total 72775\nllamacpp:prompt_seconds_total 144.129\n"
                        "llamacpp:tokens_predicted_total 15936\nllamacpp:tokens_predicted_seconds_total 338.927\n"
                        "llamacpp:prompt_tokens_cached_total 650897\nllamacpp:requests_deferred 0\n"
                        "llamacpp:requests_processing 1\nllamacpp:spec_decode_num_draft_tokens_total 13947\n"
                        "llamacpp:spec_decode_num_accepted_tokens_total 11297\n"
                        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 4258\n'
                        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 3761\n'
                        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="2"} 3278\n'
                        "llamacpp:spec_decode_num_drafts_total 4649\n")
        self.files = {"/proc/stat": "cpu  100 0 50 800 50 0 0 0 0 0\n",
                      "/proc/meminfo": fx("proc_meminfo.txt"),
                      "/proc/diskstats": fx("proc_diskstats_and_zfs.txt"),
                      "/proc/spl/kstat/zfs/arcstats": fx("proc_diskstats_and_zfs.txt")}
        self.key_reads = 0
        self.timeouts = {}

    def http_get(self, url, headers, timeout=None):
        self.timeouts[url] = timeout
        if url in self.status:
            return self.status[url]
        if url.endswith("/slots"):
            return (200, self.slots) if headers.get("Authorization") == "Bearer CHATKEY" else (401, "")
        if url.endswith("/metrics"):
            return 200, self.metrics
        if url.endswith("/healthz"):
            return 200, json.dumps({"ok": True, "upstream": {"chat": "ok", "embed": "ok", "rerank": "ok"},
                                    "active_chat_profile": "qwen3.8"})
        if url.endswith("/escalation/budget"):
            return 200, fx("router_escalation_budget.txt").split("\n", 1)[1]
        return 0, ""

    def read_text(self, path):
        if path not in self.files:
            raise OSError(path)
        return self.files[path]

    def run_cmd(self, argv):
        return 0, "NextElapseUSecRealtime=Fri 2026-10-16 03:02:27 UTC\n"


def make_hub(tmp_path, world, monkeypatch):
    monkeypatch.setattr(C, "find_llama_key", lambda *a, **k: (setattr(world, "key_reads", world.key_reads + 1), "CHATKEY")[1])
    monkeypatch.setattr(C, "find_router_key", lambda *a, **k: "ROUTERKEY")
    cfg = dict(C.DEFAULTS, history_path=str(tmp_path / "h.db"), drm_root=str(tmp_path / "nodrm"),
               hwmon_root=str(tmp_path / "nohwmon"), proc_root=str(tmp_path / "noproc"))
    return C.Hub(cfg, world.http_get, world.read_text, world.run_cmd, now_fn=lambda: 1000.0)


def test_snapshot_shape_and_values(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    hub.poll_services(1000.0)
    hub.poll_timer(1000.0)
    hub.poll_escalation(1000.0)
    s = hub.snapshot(1000.0)
    assert set(s) == {"ts", "sources", "model", "slots", "queue", "speed", "gpus", "fans", "host", "services",
                      "escalation", "recent", "history_ok"}
    assert s["sources"]["slots"]["ok"] is True and s["queue"] == {"deferred": 0, "processing": 1}
    assert len(s["slots"]) == 3 and s["slots"][1]["n_ctx"] == 131072
    assert abs(s["speed"]["mtp_accept"] - 11297 / 13947) < 1e-9
    assert s["services"]["router"]["ok"] is True and s["services"]["router"]["profile"] == "qwen3.8"
    assert s["escalation"]["month"]["cap_usd"] == 150.0
    assert s["services"]["restart"]["next_possible"] is not None
    assert s["sources"]["gpu"]["ok"] is False                       # no fake drm tree in this test
    assert s["sources"]["host"]["ok"] is True and s["host"]["arc_max"] == 17179869184
    text = json.dumps(s)
    assert "CHATKEY" not in text and "ROUTERKEY" not in text


def test_401_rereads_key_and_failure_marks_only_that_source(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    reads = w.key_reads
    w.status["http://192.168.6.151:8080/slots"] = (401, "")
    hub.poll_fast(1001.0)
    assert w.key_reads > reads
    s = hub.snapshot(1001.0)
    assert s["sources"]["slots"]["ok"] is False and s["sources"]["slots"]["stale_since"] == 1001.0
    assert s["sources"]["metrics"]["ok"] is True and s["sources"]["host"]["ok"] is True


def test_journal_lines_flow_to_recent_and_history(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    for i, line in enumerate(fx("journal_chat_last10min_raw.txt").splitlines()):
        hub.on_journal_line(line, 2000.0 + i)
    hub.poll_fast(2100.0)
    s = hub.snapshot(2100.0)
    assert s["recent"][0]["task"] == 4772 and s["recent"][0]["status"] == "done"
    assert hub.history.flush() and hub.history.recent(5)[0]["key"] == s["recent"][0]["key"]


def test_journal_follower_restarts_with_backoff():
    lines, states = [], []
    script = [sys.executable, "-c", "print('552.57.641.854 I slot launch_slot_: id  1 | task 9 | processing task')"]
    f = C.JournalFollower(script, on_line=lines.append, on_state=lambda up: states.append(up), backoff=(0.05,))
    f.start()
    deadline = time.time() + 5
    while time.time() < deadline and len(lines) < 2:
        time.sleep(0.05)
    f.stop()
    assert len(lines) >= 2 and False in states and True in states


def test_prometheus_skips_non_finite():
    from live_dashboard import parsers as P
    m = P.parse_prometheus("a NaN\nb +Inf\nc -Inf\nd 1.5\n")
    assert m == {"d": 1.5}


def test_find_gpu_dirs_survives_undecodable_uevent(tmp_path):
    from live_dashboard import parsers as P
    dev = tmp_path / "card0" / "device"
    dev.mkdir(parents=True)
    (dev / "uevent").write_bytes(b"PCI_SLOT_NAME=\xff\xfe\n")
    assert P.find_gpu_dirs(str(tmp_path), ["0000:03:00.0"]) == {}


def test_snapshot_has_no_nan_even_with_nan_metric(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.metrics += "llamacpp:requests_processing NaN\nllamacpp:tokens_predicted_total NaN\n"
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    hub.poll_fast(1001.0)
    s = hub.snapshot(1001.0)
    json.dumps(s, allow_nan=False)
    # direct sanitising: a NaN/inf that reaches state anywhere becomes None
    hub.state["host"]["cpu_pct"] = float("nan")
    hub.state["speed"]["gen_tok_s"] = float("inf")
    hub.state["gpus"] = [{"bus": "x", "load_pct": float("-inf")}]
    s = hub.snapshot(1002.0)
    json.dumps(s, allow_nan=False)
    assert s["host"]["cpu_pct"] is None and s["speed"]["gen_tok_s"] is None and s["gpus"][0]["load_pct"] is None


def test_poll_host_short_proc_lines_marks_host_only(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.files["/proc/stat"] = "cpu  1 2\n"
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    s = hub.snapshot(1000.0)
    assert s["sources"]["host"]["ok"] is False and s["sources"]["slots"]["ok"] is True


def test_raising_http_get_never_escapes_and_leaks_no_key(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)

    def boom(url, headers, timeout=None):
        raise RuntimeError("kaboom " + headers.get("Authorization", ""))
    hub.http_get = boom
    hub.poll_fast(1000.0)
    hub.poll_services(1000.0)
    hub.poll_timer(1000.0)
    hub.poll_escalation(1000.0)
    s = hub.snapshot(1000.0)
    for n in ("slots", "metrics", "services", "escalation"):
        assert s["sources"][n]["ok"] is False
    assert s["sources"]["host"]["ok"] is True
    text = json.dumps(s)
    assert "CHATKEY" not in text and "ROUTERKEY" not in text


def test_raising_run_cmd_and_read_text_never_escape(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.run_cmd = lambda argv: (_ for _ in ()).throw(ValueError("x"))
    hub.read_text = lambda p: (_ for _ in ()).throw(KeyError("x"))
    hub.poll_timer(1000.0)
    hub.poll_fast(1000.0)
    assert hub.snapshot(1000.0)["sources"]["host"]["ok"] is False


# ---- fix round 1
import threading  # noqa: E402


def _follow(code, seconds, backoff=(0.05,)):
    lines, states = [], []
    f = C.JournalFollower([sys.executable, "-c", code], on_line=lines.append, on_state=states.append, backoff=backoff)
    f.start()
    time.sleep(seconds)
    f.stop()
    return lines, states


def test_follower_survives_invalid_utf8_and_reports_up_then_down():
    code = r"import sys; sys.stdout.buffer.write(b'ok1\n\xff\xfe b\nok2\n')"
    lines, states = _follow(code, 0.6)
    assert lines[:3] == ["ok1", "�� b", "ok2"]
    assert len(lines) >= 6                               # restarted after the child exited
    assert states[0] is True and False in states


def test_follower_silent_child_never_reports_up():
    lines, states = _follow("pass", 0.4)
    assert lines == [] and True not in states and False in states


def test_follower_missing_binary_still_backs_off_and_reports_down():
    f_states = []
    f = C.JournalFollower(["/nonexistent/definitely-not-here"], on_line=lambda l: None,
                          on_state=f_states.append, backoff=(0.05,))
    f.start()
    time.sleep(0.3)
    f.stop()
    assert f_states and set(f_states) == {False}


def test_follower_stop_kills_a_quiet_child_promptly():
    f = C.JournalFollower([sys.executable, "-c", "import time; time.sleep(60)"], on_line=lambda l: None,
                          on_state=lambda u: None, backoff=(0.05,))
    f.start()
    time.sleep(0.3)
    t0 = time.time()
    f.stop()
    assert time.time() - t0 < 4


@pytest.mark.parametrize("which", ["fast", "services"])
def test_snapshot_not_blocked_by_slow_network(tmp_path, monkeypatch, which):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    inner = w.http_get

    def slow(url, headers, timeout=None):
        time.sleep(1)
        return inner(url, headers)
    hub.http_get = slow
    t = threading.Thread(target=hub.poll_fast if which == "fast" else hub.poll_services, args=(1000.0,))
    t.start()
    time.sleep(0.2)
    t0 = time.time()
    hub.snapshot(1000.0)
    took = time.time() - t0
    t.join()
    assert took < 0.2


def test_host_rates_use_read_time_and_clamp_negative(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    clock = iter([100.0, 110.0, 120.0])
    hub.now_fn = lambda: next(clock)
    hub.poll_fast(1.0)
    hub.poll_fast(2.0)                                   # same files: 10 s elapsed by read time, zero deltas
    s = hub.snapshot(2.0)
    assert s["host"]["disk_read_mbs"] == 0.0 and s["host"]["cpu_pct"] is None
    w.files["/proc/stat"] = "cpu  10 0 5 80 5 0 0 0 0 0\n"      # counters went backwards
    hub.poll_fast(3.0)
    assert hub.snapshot(3.0)["host"]["cpu_pct"] is None


def _two_llamas(tmp_path):
    base = ["/opt/llama.cpp/build/bin/llama-server", "--port", "8080", "--api-key"]
    return fake_proc(tmp_path, {
        10: (base + ["OTHERCT"], {}, "0::/lxc/157/ns/x\n"),
        20: (base + ["CHAT151"], {}, "0::/lxc/151/ns/x\n"),
        30: (["/usr/bin/python3", "/opt/venv/bin/uvicorn", "app:app"], {"ROUTER_API_KEY": "R157"}, "0::/lxc/157/ns/r\n"),
    })


def test_keys_are_scoped_to_their_container(tmp_path):
    root = _two_llamas(tmp_path)
    assert C.find_llama_key(root, port="8080", ct=151) == "CHAT151"
    assert C.find_llama_key(root, port="8080", ct=157) == "OTHERCT"
    assert C.find_llama_key(root, port="8080", ct=999) is None
    assert C.find_router_key(root, ct=153) is None        # the only router is in 157
    assert C.find_router_key(root, ct=157) == "R157"


def test_hub_derives_port_and_ct_from_cfg(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    monkeypatch.undo()
    (tmp_path / "p").mkdir()
    root = _two_llamas(tmp_path / "p")
    hub.cfg = dict(hub.cfg, proc_root=root, chat_url="http://x:8080", chat_ct=151)
    assert hub._find_llama_key() == "CHAT151"
    hub.cfg = dict(hub.cfg, chat_url="http://x:8081")
    assert hub._find_llama_key() is None


def _timer_hub(tmp_path, monkeypatch, stamp_rc, stamp_out):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    calls = []

    def run(argv):
        calls.append(argv)
        if "cat" in argv:
            return stamp_rc, stamp_out
        return 0, "NextElapseUSecRealtime=Fri 2026-10-16 03:02:27 UTC\n"
    hub.run_cmd = run
    return hub, calls


def test_restart_stamp_is_read_through_the_container(tmp_path, monkeypatch):
    hub, calls = _timer_hub(tmp_path, monkeypatch, 0, "9999999999\n")
    hub.poll_timer(1000.0)
    assert ["pct", "exec", "151", "--", "cat", "/var/lib/llamacpp/last-proactive-restart"] in calls
    assert hub.snapshot(1000.0)["services"]["restart"]["next_possible"] == 9999999999.0 + 43200


@pytest.mark.parametrize("rc,out", [(1, "9999999999\n"), (0, "garbage"), (0, "")])
def test_restart_stamp_unknown_on_failure_or_garbage(tmp_path, monkeypatch, rc, out):
    hub, _ = _timer_hub(tmp_path, monkeypatch, rc, out)
    hub.poll_timer(1000.0)
    r = hub.snapshot(1000.0)["services"]["restart"]
    assert r["next_possible"] is not None and r["next_possible"] < 9999999999.0      # only the timer fire time


def test_restart_stamp_considers_only_64_chars(tmp_path, monkeypatch):
    hub, _ = _timer_hub(tmp_path, monkeypatch, 0, "9" * 200)
    hub.poll_timer(1000.0)
    assert hub.snapshot(1000.0)["services"]["restart"]["next_possible"] == float("9" * 64) + 43200


def test_cgroup_match_is_anchored_to_the_path_start(tmp_path):
    base = ["/opt/llama.cpp/build/bin/llama-server", "--port", "8080", "--api-key"]
    root = fake_proc(tmp_path, {
        5: (base + ["SPOOF1"], {}, "0::/lxc/157/ns/lxc/151/fake\n"),
        6: (base + ["SPOOF2"], {}, "0::/lxc/157/ns/evil/lxc/151\n"),
        7: (base + ["REAL"], {}, "0::/lxc/151/ns/system.slice/llamacpp-chat.service\n"),
        8: (["/usr/bin/python3", "/opt/venv/bin/uvicorn", "app:app"], {"ROUTER_API_KEY": "RSPOOF"},
            "0::/lxc/157/ns/lxc/153/r\n"),
    })
    assert C.find_llama_key(root, port="8080", ct=151) == "REAL"
    assert C.find_router_key(root, ct=153) is None


def test_stale_slots_not_applied_when_journal_flips_mid_fetch(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    inner, gate, entered = w.http_get, threading.Event(), threading.Event()

    def blocked(url, headers, timeout=None):
        if url.endswith("/slots"):
            entered.set()
            gate.wait(5)
        return inner(url, headers)
    hub.http_get = blocked
    t = threading.Thread(target=hub.poll_fast, args=(1000.0,))
    t.start()
    assert entered.wait(5)
    hub.on_journal_state(False, 1000.5)
    gate.set()
    t.join()
    assert hub.tracker.open_rows() == []
    assert hub.snapshot(1001.0)["sources"]["slots"]["ok"] is False


def test_healthz_non_dict_is_not_ok_and_never_stale_ok(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_services(1000.0)
    assert hub.snapshot(1000.0)["services"]["router"]["ok"] is True
    w.status["http://192.168.6.153:8000/healthz"] = (200, "[1, 2]")
    hub.poll_services(1001.0)
    s = hub.snapshot(1001.0)
    assert s["services"]["router"]["ok"] is False and s["sources"]["services"]["ok"] is False


def test_escalation_is_whitelisted_and_non_dict_is_none(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    doc = json.loads(fx("router_escalation_budget.txt").split("\n", 1)[1])
    doc["secret_note"] = "free text"
    doc["month"]["note"] = "free text"
    doc["recent"] = [dict(doc["recent"][0], prompt="TOPSECRETPROMPT") for _ in range(15)]
    w.status["http://192.168.6.153:8000/escalation/budget"] = (200, json.dumps(doc))
    hub.poll_escalation(1000.0)
    e = hub.snapshot(1000.0)["escalation"]
    text = json.dumps(e)
    assert "free text" not in text and "TOPSECRETPROMPT" not in text
    assert set(e) == {"month", "day", "calls_left", "resets", "recent"} and len(e["recent"]) == 10
    assert set(e["month"]) == {"spent_usd", "cap_usd"} and e["month"]["cap_usd"] == 150.0
    w.status["http://192.168.6.153:8000/escalation/budget"] = (200, "[]")
    hub.poll_escalation(1001.0)
    s = hub.snapshot(1001.0)
    assert s["escalation"] is None and s["sources"]["escalation"]["ok"] is False


def test_restart_timer_stale_marker(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_timer(1000.0)
    r = hub.snapshot(1000.0)["services"]["restart"]
    assert r["next_possible"] is not None and r["stale_since"] is None
    good = r["next_possible"]
    hub.run_cmd = lambda argv: (1, "")
    hub.poll_timer(1010.0)
    hub.poll_timer(1020.0)
    r = hub.snapshot(1020.0)["services"]["restart"]
    assert r["next_possible"] == good and r["stale_since"] == 1010.0
    hub.run_cmd = w.run_cmd
    hub.poll_timer(1030.0)
    assert hub.snapshot(1030.0)["services"]["restart"]["stale_since"] is None


# ---- final fix wave
def _ev(kind, **kw):
    return dict(kind=kind, since_start_s=1.0, **kw)


def _finish_request(hub, slot, task, now, pe_n=6000, pe_ms=10000.0, ev_n=50, ev_ms=1000.0, rel_n=40050):
    t = hub.tracker
    t.on_journal(_ev("start", slot=slot, task=task), now)
    t.on_journal(_ev("prompt_eval", slot=slot, task=task, ms=pe_ms, n=pe_n), now)
    t.on_journal(_ev("eval", slot=slot, task=task, ms=ev_ms, n=ev_n), now)
    t.on_journal(_ev("total", slot=slot, task=task, ms=pe_ms + ev_ms, n=pe_n + ev_n), now)
    t.on_journal(_ev("release", slot=slot, task=task, n=rel_n), now)
    with hub.lock:
        hub._drain_finished()


def test_headline_speed_nothing_yet_is_null_and_not_live(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, World(tmp_path), monkeypatch)
    hub.poll_fast(1000.0)
    sp = hub.snapshot(1000.0)["speed"]
    assert sp["gen_tok_s"] is None and sp["prefill_tok_s"] is None
    assert sp["gen_live"] is False and sp["prefill_live"] is False and sp["cache_reuse_pct"] is None


def test_headline_speed_live_sums_while_slots_work(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, World(tmp_path), monkeypatch)
    _finish_request(hub, 2, 1, 990.0)                      # an older finished request must not win over live ones
    t = hub.tracker
    for slot, task in ((0, 10), (1, 11)):
        t.on_journal(_ev("start", slot=slot, task=task), 999.0)
        t.on_journal(_ev("gen", slot=slot, task=task, n=5, tok_s=30.0 + slot), 999.5)
    t.on_journal(_ev("start", slot=2, task=12), 999.0)
    t.on_journal(_ev("prefill", slot=2, task=12, n=2048, progress=0.5, tok_s=600.0), 999.5)
    sp = hub.snapshot(1000.0)["speed"]
    assert sp["gen_tok_s"] == 61.0 and sp["gen_live"] is True
    assert sp["prefill_tok_s"] == 600.0 and sp["prefill_live"] is True


def test_headline_speed_held_from_last_finished_request(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, World(tmp_path), monkeypatch)
    _finish_request(hub, 0, 1, 990.0, pe_n=1000, pe_ms=2000.0, ev_n=40, ev_ms=1000.0, rel_n=1040)
    _finish_request(hub, 1, 2, 995.0, pe_n=6000, pe_ms=10000.0, ev_n=50, ev_ms=1000.0, rel_n=40050)
    hub.poll_fast(1000.0)
    sp = hub.snapshot(1000.0)["speed"]
    assert sp["gen_tok_s"] == 50.0 and sp["gen_live"] is False
    assert sp["prefill_tok_s"] == 600.0 and sp["prefill_live"] is False
    assert sp["cache_reuse_pct"] == 85.0                     # 34000 of 40000 reused, latest request


def test_headline_speed_held_value_seeded_from_history(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, World(tmp_path), monkeypatch)
    _finish_request(hub, 1, 2, 995.0)
    assert hub.history.flush()
    hub.history.close()
    hub2 = make_hub(tmp_path, World(tmp_path), monkeypatch)
    sp = hub2.snapshot(1000.0)["speed"]
    assert sp["gen_tok_s"] == 50.0 and sp["prefill_tok_s"] == 600.0 and sp["cache_reuse_pct"] == 85.0
    assert sp["gen_live"] is False and sp["prefill_live"] is False


def test_cache_series_is_per_request_reuse_and_card_keeps_15min_average(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    w.metrics = w.metrics.replace("llamacpp:prompt_tokens_total 72775", "llamacpp:prompt_tokens_total 72875")
    w.metrics = w.metrics.replace("llamacpp:prompt_tokens_cached_total 650897",
                                  "llamacpp:prompt_tokens_cached_total 651197")
    _finish_request(hub, 1, 2, 1000.5)
    hub.poll_fast(1001.0)
    sp = hub.snapshot(1001.0)["speed"]
    assert sp["cache_reuse_pct15"] == 75.0                   # metric deltas: 300 cached of 400
    assert sp["cache_reuse_pct"] == 85.0
    assert [v for _, v in hub.series()["cache"]] == [85.0]


def test_llama_fetches_use_short_timeout(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    assert w.timeouts["http://192.168.6.151:8080/slots"] == 1.0
    assert w.timeouts["http://192.168.6.151:8080/metrics"] == 1.0
    hub.cfg = dict(hub.cfg, llama_timeout_s=0.25)
    hub.poll_fast(1001.0)
    assert w.timeouts["http://192.168.6.151:8080/slots"] == 0.25


def test_llama_timeout_is_a_validated_config_key():
    from live_dashboard import main as M
    assert C.DEFAULTS["llama_timeout_s"] == 1.0
    cfg = dict(C.DEFAULTS, llama_timeout_s=0)
    with pytest.raises(M.ConfigError):
        M.validate_config(cfg)


def _gpu_tree(tmp_path):
    for path, content in json.loads(fx("gpu_sysfs.json")).items():
        f = tmp_path / path.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return os.path.join(str(tmp_path), "sys", "class", "drm")


def test_gpu_dirs_stored_as_realpath_and_rediscovered_on_renumbering(tmp_path, monkeypatch):
    import shutil
    from live_dashboard import parsers as P
    drm = _gpu_tree(tmp_path)
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.cfg = dict(hub.cfg, drm_root=drm)
    hub.poll_fast(1000.0)
    bus = "0000:03:00.0"
    first = hub.gpu_dirs[bus]
    assert first == os.path.realpath(first) and hub.snapshot(1000.0)["gpus"][0]["vram_total"] == 32195477504
    # the driver reloads: card numbers move; the stored dir no longer carries this bus
    newdev = os.path.join(drm, "card9", "device")
    shutil.copytree(first, newdev)
    with open(os.path.join(first, "uevent"), "w", encoding="utf-8") as f:
        f.write("PCI_SLOT_NAME=0000:99:00.0\n")
    calls = []
    real = P.find_gpu_dirs
    monkeypatch.setattr(P, "find_gpu_dirs", lambda *a: (calls.append(1), real(*a))[1])
    hub.poll_fast(1001.0)
    assert calls and hub.gpu_dirs[bus] == os.path.realpath(newdev)
    calls.clear()
    hub.poll_fast(1002.0)
    assert calls == []                                      # valid paths are not re-discovered every poll


def test_read_fans_skips_an_unreadable_hwmon_dir(tmp_path):
    from live_dashboard import parsers as P
    hw = tmp_path / "hwmon"
    (hw / "hwmon0").mkdir(parents=True)                      # no name file: unreadable
    (hw / "hwmon14").mkdir()
    (hw / "hwmon14" / "name").write_text("nct6798\n")
    (hw / "hwmon14" / "pwm5").write_text("255\n")
    (hw / "hwmon14" / "pwm6").write_text("255\n")
    assert P.read_fans(str(hw)) == {"pwm5_pct": 100.0, "pwm6_pct": 100.0}


def test_find_chat_pid_is_gone():
    assert not hasattr(C, "find_chat_pid")


def test_follower_idle_child_reports_up_after_up_after_s():
    lines, states = [], []
    f = C.JournalFollower([sys.executable, "-c", "import time; time.sleep(60)"], on_line=lines.append,
                          on_state=states.append, backoff=(0.05,), up_after_s=0.3)
    f.start()
    deadline = time.time() + 5
    while time.time() < deadline and True not in states:
        time.sleep(0.05)
    f.stop()
    assert lines == [] and states and states[0] is True


def test_follower_idle_child_not_up_before_up_after_s():
    states = []
    f = C.JournalFollower([sys.executable, "-c", "import time; time.sleep(60)"], on_line=lambda l: None,
                          on_state=states.append, backoff=(0.05,), up_after_s=5.0)
    f.start()
    time.sleep(0.5)
    snapshot = list(states)
    f.stop()
    assert True not in snapshot


def test_follower_child_exiting_before_up_after_s_never_reports_up():
    states = []
    f = C.JournalFollower([sys.executable, "-c", "pass"], on_line=lambda l: None, on_state=states.append,
                          backoff=(0.05,), up_after_s=0.3)
    f.start()
    time.sleep(1.0)
    f.stop()
    assert states and True not in states


def test_follower_default_up_after_is_two_seconds():
    f = C.JournalFollower(["x"], on_line=lambda l: None, on_state=lambda u: None)
    assert f.up_after_s == 2.0
