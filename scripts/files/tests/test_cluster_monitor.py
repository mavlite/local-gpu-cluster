import os
import sys
import unittest
import json as _json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm


class FakeProbes:
    """Test double for cluster_monitor.Probes."""
    def __init__(self, cmd_map=None, http_map=None):
        self._cmd = cmd_map or {}
        self._http = http_map or {}

    def cmd(self, args, timeout=10.0):
        return self._cmd.get(" ".join(args), cm.CmdResult(127, "", "fake: not found"))

    def http(self, method, url, *, headers=None, json_body=None, timeout=5.0):
        return self._http.get((method, url), cm.HttpResult(0, "", "fake: no route"))


class TestProbes(unittest.TestCase):
    def test_real_cmd_runs_echo(self):
        p = cm.Probes()
        r = p.cmd(["python3", "-c", "print('hi')"])
        self.assertEqual(r.rc, 0)
        self.assertIn("hi", r.stdout)

    def test_real_cmd_missing_binary_is_nonzero_not_raises(self):
        p = cm.Probes()
        r = p.cmd(["definitely-not-a-real-binary-xyz"])
        self.assertNotEqual(r.rc, 0)
        self.assertTrue(r.stderr)

    def test_fake_cmd_lookup(self):
        fp = FakeProbes(cmd_map={"pct config 151": cm.CmdResult(0, "memory: 32768", "")})
        self.assertEqual(fp.cmd(["pct", "config", "151"]).stdout, "memory: 32768")
        self.assertEqual(fp.cmd(["unknown"]).rc, 127)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = cm.Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _res(self, status, value=None):
        return cm.CheckResult(id="gpu_vram_0", group="metrics",
                              status=status, detail="d", value=value, unit="%")

    def test_first_record_returns_empty_prev_status(self):
        prev = self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.assertEqual(prev, "")

    def test_record_returns_previous_status(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        prev = self.store.record(self._res(cm.STATUS_FAIL, 99.0), now=101.0)
        self.assertEqual(prev, "ok")

    def test_last_ok_at_only_set_on_ok(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.store.record(self._res(cm.STATUS_FAIL, 99.0), now=200.0)
        snap = {s["id"]: s for s in self.store.snapshot(3600.0, now=250.0)}
        self.assertEqual(snap["gpu_vram_0"]["last_ok_at"], 100.0)
        self.assertEqual(snap["gpu_vram_0"]["status"], "fail")

    def test_samples_recorded_and_windowed(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.store.record(self._res(cm.STATUS_OK, 20.0), now=160.0)
        snap = {s["id"]: s for s in self.store.snapshot(120.0, now=200.0)}
        vals = [v for _, v in snap["gpu_vram_0"]["samples"]]
        self.assertEqual(vals, [10.0, 20.0])
        # Narrow window drops the old sample.
        snap2 = {s["id"]: s for s in self.store.snapshot(50.0, now=200.0)}
        self.assertEqual([v for _, v in snap2["gpu_vram_0"]["samples"]], [20.0])

    def test_value_none_records_no_sample(self):
        self.store.record(self._res(cm.STATUS_OK, None), now=100.0)
        snap = {s["id"]: s for s in self.store.snapshot(3600.0, now=200.0)}
        self.assertEqual(snap["gpu_vram_0"]["samples"], [])

    def test_prune_deletes_old_samples(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.store.record(self._res(cm.STATUS_OK, 20.0), now=1000.0)
        deleted = self.store.prune(now=1000.0, retention_s=500.0)
        self.assertEqual(deleted, 1)


class TestCoreTypes(unittest.TestCase):
    def test_checkresult_to_dict_roundtrip(self):
        r = cm.CheckResult(
            id="gpu_vram_0", group="metrics", status=cm.STATUS_WARN,
            detail="card 0 at 91%", value=91.0, unit="%",
            suggested_action="none",
        )
        d = r.to_dict()
        self.assertEqual(d["id"], "gpu_vram_0")
        self.assertEqual(d["status"], "warn")
        self.assertEqual(d["value"], 91.0)
        self.assertEqual(d["unit"], "%")
        self.assertEqual(d["suggested_action"], "none")

    def test_checkresult_is_frozen(self):
        r = cm.CheckResult(id="x", group="health", status=cm.STATUS_OK, detail="")
        with self.assertRaises(Exception):
            r.status = cm.STATUS_FAIL  # frozen -> FrozenInstanceError

    def test_optional_fields_default_none(self):
        r = cm.CheckResult(id="x", group="health", status=cm.STATUS_OK, detail="ok")
        self.assertIsNone(r.value)
        self.assertIsNone(r.unit)
        self.assertIsNone(r.suggested_action)


class RecordingNotifier(cm.Notifier):
    def __init__(self):
        self.events = []
    def send(self, event):
        self.events.append(event)


class TestAlertEngine(unittest.TestCase):
    def setUp(self):
        self.n = RecordingNotifier()
        self.eng = cm.AlertEngine(self.n, cooldown_s=900.0)

    def _r(self, status):
        return cm.CheckResult(id="router_chat_upstream", group="health",
                              status=status, detail="d")

    def test_ok_to_fail_fires(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=100.0)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.kind, "fired")
        self.assertEqual(self.n.events[-1].status, "fail")

    def test_fail_to_ok_resolves(self):
        self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=100.0)
        ev = self.eng.evaluate(self._r(cm.STATUS_OK), prev_status="fail", now=200.0)
        self.assertEqual(ev.kind, "resolved")

    def test_steady_state_no_event(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_OK), prev_status="ok", now=100.0)
        self.assertIsNone(ev)

    def test_cooldown_suppresses_repeat_fire(self):
        self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=100.0)
        # Flap back then fail again inside the cooldown window -> suppressed.
        self.eng.evaluate(self._r(cm.STATUS_OK), prev_status="fail", now=150.0)
        ev = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=200.0)
        self.assertIsNone(ev)
        # After cooldown elapses, it fires again.
        ev2 = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=1200.0)
        self.assertIsNotNone(ev2)

    def test_first_sight_fail_fires(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="", now=100.0)
        self.assertEqual(ev.kind, "fired")

    def test_warn_counts_as_problem(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_WARN), prev_status="ok", now=100.0)
        self.assertEqual(ev.kind, "fired")

    def test_cooldown_is_per_status_not_per_id(self):
        # fail fires at t=100; a different-status (warn) fire at t=200 must not
        # reset fail's cooldown -> a fail re-fire at t=300 (200s < 900s) stays suppressed.
        self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=100.0)
        self.eng.evaluate(self._r(cm.STATUS_WARN), prev_status="ok", now=200.0)
        ev = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=300.0)
        self.assertIsNone(ev)


class TestNotifiers(unittest.TestCase):
    def test_noop_send_is_silent(self):
        cm.NoopNotifier().send(
            cm.AlertEvent("x", "fired", "fail", "d", 1.0))  # must not raise

    def test_log_notifier_writes(self):
        import logging
        logger = logging.getLogger("test_cm_alert")
        with self.assertLogs(logger, level="WARNING") as caught:
            cm.LogNotifier(logger).send(
                cm.AlertEvent("router_chat_upstream", "fired", "fail", "down", 1.0))
        self.assertTrue(any("router_chat_upstream" in m for m in caught.output))


class TestCheckHelpers(unittest.TestCase):
    def test_status_for_higher_is_worse(self):
        self.assertEqual(cm.status_for(50, warn=90, fail=98), cm.STATUS_OK)
        self.assertEqual(cm.status_for(92, warn=90, fail=98), cm.STATUS_WARN)
        self.assertEqual(cm.status_for(99, warn=90, fail=98), cm.STATUS_FAIL)

    def test_status_for_lower_is_worse(self):
        # e.g. free memory percent: low is bad
        self.assertEqual(
            cm.status_for(5, warn=10, fail=3, higher_is_worse=False), cm.STATUS_WARN)
        self.assertEqual(
            cm.status_for(2, warn=10, fail=3, higher_is_worse=False), cm.STATUS_FAIL)
        self.assertEqual(
            cm.status_for(50, warn=10, fail=3, higher_is_worse=False), cm.STATUS_OK)

    def test_http_alive_2xx_ok(self):
        fp = FakeProbes(http_map={("GET", "http://h:3001/"): cm.HttpResult(200, "")})
        status, _ = cm.http_alive(fp, "http://h:3001/")
        self.assertEqual(status, cm.STATUS_OK)

    def test_http_alive_transport_fail(self):
        fp = FakeProbes()  # no route -> status 0
        status, detail = cm.http_alive(fp, "http://h:3001/")
        self.assertEqual(status, cm.STATUS_FAIL)
        self.assertIn("no route", detail)

    def test_http_alive_any_http_treats_4xx_as_alive(self):
        fp = FakeProbes(http_map={("GET", "http://h:3005/mcp"): cm.HttpResult(406, "")})
        status, _ = cm.http_alive(fp, "http://h:3005/mcp", alive_any_http=True)
        self.assertEqual(status, cm.STATUS_OK)

    def test_registry_exists_and_is_list(self):
        self.assertIsInstance(cm.REGISTRY, list)


class TestHealthChecks(unittest.TestCase):
    CFG = {
        "router_url": "http://r:8000",
        "anythingllm_url": "http://a:3001",
        "mcp_sdg_url": "http://m:3004",
        "memvault_rest_url": "http://v:8000",
        "memvault_bridge_url": "http://v:3005/mcp",
    }

    def test_router_healthz_parses_upstreams(self):
        body = _json.dumps({
            "upstream": {"chat": True, "embed": True, "rerank": False},
            "active_chat_profile": "qwen3-coder",
            "seconds_since_chat": 42,
        })
        fp = FakeProbes(http_map={
            ("GET", "http://r:8000/healthz"): cm.HttpResult(200, body)})
        out = {r.id: r for r in cm.check_router_healthz(fp, self.CFG)}
        self.assertEqual(out["router_chat_upstream"].status, cm.STATUS_OK)
        self.assertEqual(out["router_rerank_upstream"].status, cm.STATUS_FAIL)
        self.assertEqual(out["loaded_chat_profile"].detail, "qwen3-coder")
        self.assertEqual(out["last_chat_completion"].value, 42.0)

    def test_router_healthz_unreachable_one_fail_tile(self):
        fp = FakeProbes()
        out = cm.check_router_healthz(fp, self.CFG)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].id, "router_up")
        self.assertEqual(out[0].status, cm.STATUS_FAIL)

    def test_anythingllm_ok(self):
        fp = FakeProbes(http_map={("GET", "http://a:3001/"): cm.HttpResult(200, "")})
        self.assertEqual(cm.check_anythingllm(fp, self.CFG)[0].status, cm.STATUS_OK)

    def test_mcp_sdg_any_http_alive(self):
        fp = FakeProbes(http_map={("GET", "http://m:3004/"): cm.HttpResult(404, "")})
        self.assertEqual(cm.check_mcp_sdg(fp, self.CFG)[0].status, cm.STATUS_OK)

    def test_memvault_rest_down(self):
        fp = FakeProbes()
        self.assertEqual(cm.check_memvault_rest(fp, self.CFG)[0].status, cm.STATUS_FAIL)

    def test_memvault_bridge_alive_on_406(self):
        fp = FakeProbes(http_map={("GET", "http://v:3005/mcp"): cm.HttpResult(406, "")})
        self.assertEqual(cm.check_memvault_bridge(fp, self.CFG)[0].status, cm.STATUS_OK)

    def test_health_checks_registered(self):
        ids = {c.id for c in cm.REGISTRY}
        for cid in ("router_healthz", "anythingllm", "mcp_sdg",
                    "memvault_rest", "memvault_bridge"):
            self.assertIn(cid, ids)


class TestMetricsParsers(unittest.TestCase):
    def test_parse_rocm_vram_json(self):
        text = _json.dumps({
            "card0": {"VRAM Total Memory (B)": "34359738368",
                      "VRAM Total Used Memory (B)": "17179869184"},
            "card1": {"VRAM Total Memory (B)": "34359738368",
                      "VRAM Total Used Memory (B)": "1073741824"},
        })
        rows = cm.parse_rocm_vram_json(text)
        self.assertEqual(len(rows), 2)
        idx, used, total = rows[0]
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(used, 16384.0, places=0)   # MiB
        self.assertAlmostEqual(total, 32768.0, places=0)

    def test_parse_rocm_temp_json(self):
        text = _json.dumps({
            "card0": {"Temperature (Sensor junction) (C)": "95.0"},
            "card1": {"Temperature (Sensor junction) (C)": "60.0"},
        })
        rows = dict(cm.parse_rocm_temp_json(text))
        self.assertEqual(rows[0], 95.0)
        self.assertEqual(rows[1], 60.0)

    def test_parse_meminfo(self):
        text = "MemTotal:       65809920 kB\nMemAvailable:    6580992 kB\n"
        d = cm.parse_meminfo(text)
        self.assertEqual(d["MemTotal"], 65809920.0)
        self.assertEqual(d["MemAvailable"], 6580992.0)


class TestMetricsChecks(unittest.TestCase):
    CFG = {
        "gpu_vram_warn_pct": 90, "gpu_vram_fail_pct": 98,
        "gpu_temp_warn_c": 95, "gpu_temp_fail_c": 105,
        "host_mem_warn_pct": 10, "host_mem_fail_pct": 3,
        "lxc_ids": [151],
    }

    def test_gpu_vram_per_card_tiles(self):
        text = _json.dumps({
            "card0": {"VRAM Total Memory (B)": "34359738368",
                      "VRAM Total Used Memory (B)": "33500000000"},  # ~97.5%
        })
        fp = FakeProbes(cmd_map={
            "rocm-smi --showmeminfo vram --json": cm.CmdResult(0, text, "")})
        out = cm.check_gpu_vram(fp, self.CFG)
        self.assertEqual(out[0].id, "gpu_vram_0")
        self.assertEqual(out[0].status, cm.STATUS_WARN)  # >=90 <98
        self.assertEqual(out[0].unit, "%")

    def test_gpu_temp_fail_high(self):
        text = _json.dumps({"card0": {"Temperature (Sensor junction) (C)": "106"}})
        fp = FakeProbes(cmd_map={
            "rocm-smi --showtemp --json": cm.CmdResult(0, text, "")})
        out = cm.check_gpu_temp(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_FAIL)

    def test_host_mem_low_available_warns(self):
        text = "MemTotal: 1000 kB\nMemAvailable: 80 kB\n"  # 8% avail
        fp = FakeProbes(cmd_map={"cat /proc/meminfo": cm.CmdResult(0, text, "")})
        out = cm.check_host_mem(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_WARN)

    def test_host_cpu_value(self):
        fp = FakeProbes(cmd_map={
            "cat /proc/loadavg": cm.CmdResult(0, "1.50 1.0 0.9 1/100 1234", ""),
            "nproc": cm.CmdResult(0, "8\n", "")})
        out = cm.check_host_cpu(fp, self.CFG)
        self.assertEqual(out[0].value, 1.5)

    def test_zfs_arc_value(self):
        arcstats = "name type data\nsize 4 8589934592\nc_max 4 17179869184\n"
        fp = FakeProbes(cmd_map={
            "cat /proc/spl/kstat/zfs/arcstats": cm.CmdResult(0, arcstats, "")})
        out = cm.check_zfs_arc(fp, self.CFG)
        self.assertEqual(out[0].id, "zfs_arc")
        self.assertAlmostEqual(out[0].value, 50.0, places=0)  # 8G of 16G cap

    def test_lxc_mem_uses_ceiling_and_used(self):
        fp = FakeProbes(cmd_map={
            "pct config 151": cm.CmdResult(0, "memory: 32768\ncores: 8\n", ""),
            "pct exec 151 -- cat /proc/meminfo":
                cm.CmdResult(0, "MemTotal: 33554432 kB\nMemAvailable: 16777216 kB\n", "")})
        out = cm.check_lxc_mem(fp, self.CFG)
        self.assertEqual(out[0].id, "lxc_mem_151")
        self.assertEqual(out[0].unit, "%")


if __name__ == "__main__":
    unittest.main()
