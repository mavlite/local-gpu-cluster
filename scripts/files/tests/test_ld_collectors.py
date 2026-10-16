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

    def http_get(self, url, headers):
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

    def boom(url, headers):
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

    def slow(url, headers):
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
    assert C.find_chat_pid(root, port="8080", ct=151) == "20"
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


def test_read_stamp_regular_symlink_dir_and_size(tmp_path):
    good = tmp_path / "stamp"
    good.write_text("1700000000\n")
    assert C.read_stamp(str(good)) == 1700000000.0
    assert C.read_stamp(str(tmp_path / "missing")) is None
    assert C.read_stamp(str(tmp_path)) is None            # not a regular file
    link = tmp_path / "link"
    try:
        link.symlink_to(good)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW unavailable on this platform")
    assert C.read_stamp(str(link)) is None


def test_stamp_read_is_bounded_to_64_bytes(tmp_path):
    big = tmp_path / "big"
    big.write_text("9" * 200)
    assert C.read_stamp(str(big)) == float("9" * 64)


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
