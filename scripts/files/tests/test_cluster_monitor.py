import os
import sys
import tempfile
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

    def test_parse_rocm_vram_json_used_before_total(self):
        # Key order with "Used" before "Total" must not confuse the total lookup.
        text = _json.dumps({
            "card0": {"VRAM Total Used Memory (B)": "1073741824",
                      "VRAM Total Memory (B)": "34359738368"},
        })
        rows = cm.parse_rocm_vram_json(text)
        idx, used, total = rows[0]
        self.assertAlmostEqual(used, 1024.0, places=0)    # 1 GiB used
        self.assertAlmostEqual(total, 32768.0, places=0)  # 32 GiB total


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


class TestPromParser(unittest.TestCase):
    def test_parse_prom_labels_and_values(self):
        text = (
            "# HELP rag_refresh_last_run_timestamp x\n"
            "# TYPE rag_refresh_last_run_timestamp gauge\n"
            "rag_refresh_last_run_timestamp 1718764500\n"
            'rag_refresh_run_total{status="ok"} 5\n'
            'rag_refresh_run_total{status="error"} 1\n'
        )
        m = cm.parse_prom(text)
        self.assertEqual(m["rag_refresh_last_run_timestamp"][0][1], 1718764500.0)
        totals = {lbl["status"]: v for lbl, v in m["rag_refresh_run_total"]}
        self.assertEqual(totals["error"], 1.0)


class TestFreshnessChecks(unittest.TestCase):
    CFG = {
        "rag_metrics_path": "/var/lib/rag-refresh/metrics.prom",
        "rag_stale_after_s": 93600,
        "rag_timer_name": "rag-refresh.timer",
        "backup_timer_name": "memory-vault-backup.timer",
        "memvault_vmid": 156,
        "lxc_ram_ceilings": {"151": 32768},
        "router_url": "http://r:8000",
        "router_vmid": 153,
        "router_env_path": "/etc/router.env",
        "tavily_query": "site reachability probe",
    }

    def test_rag_refresh_fresh_ok(self):
        prom = "rag_refresh_last_run_timestamp 2000000\nrag_refresh_run_total{status=\"ok\"} 3\n"
        fp = FakeProbes(cmd_map={
            "cat /var/lib/rag-refresh/metrics.prom": cm.CmdResult(0, prom, ""),
            "systemctl list-timers rag-refresh.timer --no-pager":
                cm.CmdResult(0, "NEXT LEFT LAST PASSED UNIT\nx x x x rag-refresh.timer\n", "")})
        out = {r.id: r for r in cm.check_rag_refresh(fp, self.CFG, now=2000300.0)}
        self.assertEqual(out["rag_refresh"].status, cm.STATUS_OK)

    def test_rag_refresh_stale_warns(self):
        prom = "rag_refresh_last_run_timestamp 1000000\n"
        fp = FakeProbes(cmd_map={
            "cat /var/lib/rag-refresh/metrics.prom": cm.CmdResult(0, prom, ""),
            "systemctl list-timers rag-refresh.timer --no-pager": cm.CmdResult(0, "", "")})
        out = {r.id: r for r in cm.check_rag_refresh(fp, self.CFG, now=2000000.0)}
        self.assertEqual(out["rag_refresh"].status, cm.STATUS_WARN)

    def test_rag_refresh_missing_metrics_warns_with_action(self):
        fp = FakeProbes()  # cat fails (127)
        out = {r.id: r for r in cm.check_rag_refresh(fp, self.CFG, now=2000000.0)}
        self.assertEqual(out["rag_refresh"].status, cm.STATUS_WARN)
        self.assertIn("58-rag-refresh-timer", out["rag_refresh"].suggested_action)

    def test_restart_policies_fail_when_not_unless_stopped(self):
        docker_out = "/memory-vault-db-1 no\n/memory-vault-app-1 unless-stopped\n"
        fp = FakeProbes(cmd_map={
            "pct exec 156 -- bash -lc docker inspect --format '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' $(docker ps -aq)":
                cm.CmdResult(0, docker_out, "")})
        out = cm.check_restart_policies(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_FAIL)
        self.assertIn("memory-vault-db-1", out[0].detail)

    def test_lxc_ram_ceilings_drift_fails(self):
        fp = FakeProbes(cmd_map={
            "pct config 151": cm.CmdResult(0, "memory: 12288\n", "")})
        out = {r.id: r for r in cm.check_lxc_ram_ceilings(fp, self.CFG)}
        self.assertEqual(out["lxc_ram_ceiling_151"].status, cm.STATUS_FAIL)
        self.assertIn("pct_set_mem(151, 32768)",
                      out["lxc_ram_ceiling_151"].suggested_action)

    def test_lxc_ram_ceilings_match_ok(self):
        fp = FakeProbes(cmd_map={
            "pct config 151": cm.CmdResult(0, "memory: 32768\n", "")})
        out = {r.id: r for r in cm.check_lxc_ram_ceilings(fp, self.CFG)}
        self.assertEqual(out["lxc_ram_ceiling_151"].status, cm.STATUS_OK)

    def test_backup_timer_present_ok(self):
        fp = FakeProbes(cmd_map={
            "systemctl is-active memory-vault-backup.timer": cm.CmdResult(0, "active\n", "")})
        out = cm.check_backup_timer(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_OK)


class TestDashboardHTML(unittest.TestCase):
    def test_html_is_self_contained(self):
        html = cm.DASHBOARD_HTML
        self.assertIn("<html", html.lower())
        self.assertIn("/api/status", html)        # polls the API
        self.assertNotIn("http://", html)         # no external assets
        self.assertNotIn("https://", html)
        self.assertIn("setInterval", html)        # auto-refresh

    def test_html_references_status_classes(self):
        html = cm.DASHBOARD_HTML
        for token in ("ok", "warn", "fail"):
            self.assertIn(token, html)


class TestRouting(unittest.TestCase):
    def _cfg(self, token=""):
        return {"bearer_token": token, "sample_window_s": 3600,
                "dashboard_title": "Cluster Monitor"}

    def _store_with_one(self):
        s = cm.Store(":memory:")
        s.record(cm.CheckResult("anythingllm", "health", cm.STATUS_OK, "ok"), now=100.0)
        return s

    def test_healthz_ungated(self):
        st = self._store_with_one()
        code, ctype, body = cm.route("/healthz", None, st, self._cfg(token="secret"))
        self.assertEqual(code, 200)
        self.assertIn("ok", body)
        st.close()

    def test_api_status_returns_json(self):
        st = self._store_with_one()
        code, ctype, body = cm.route("/api/status", None, st, self._cfg())
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "application/json")
        data = _json.loads(body)
        self.assertEqual(data["checks"][0]["id"], "anythingllm")
        st.close()

    def test_api_status_401_without_token(self):
        st = self._store_with_one()
        code, _, _ = cm.route("/api/status", None, st, self._cfg(token="secret"))
        self.assertEqual(code, 401)
        st.close()

    def test_api_status_ok_with_token(self):
        st = self._store_with_one()
        code, _, _ = cm.route("/api/status", "Bearer secret", st, self._cfg(token="secret"))
        self.assertEqual(code, 200)
        st.close()

    def test_unknown_path_404(self):
        st = self._store_with_one()
        code, _, _ = cm.route("/nope", None, st, self._cfg())
        self.assertEqual(code, 404)
        st.close()

    def test_dashboard_root_served(self):
        st = self._store_with_one()
        code, ctype, body = cm.route("/", None, st, self._cfg())
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "text/html")
        self.assertIn("<html", body.lower())
        st.close()


class TestCollector(unittest.TestCase):
    def _cfg(self):
        return dict(TestHealthChecks.CFG, **{
            "intervals": {"health": 20, "metrics": 60, "freshness": 300},
            "sample_retention_s": 86400,
        })

    def test_run_checks_records_and_returns(self):
        store = cm.Store(":memory:")
        fp = FakeProbes(http_map={
            ("GET", "http://a:3001/"): cm.HttpResult(200, "")})
        eng = cm.AlertEngine(cm.NoopNotifier())
        only = [c for c in cm.REGISTRY if c.id == "anythingllm"]
        col = cm.Collector(only, store, fp, eng, self._cfg())
        results = col.run_checks(groups={"health"}, now=100.0)
        self.assertEqual(results[0].id, "anythingllm")
        snap = store.snapshot(3600, now=100.0)
        self.assertEqual(snap[0]["status"], "ok")
        store.close()

    def test_check_exception_becomes_fail_not_crash(self):
        store = cm.Store(":memory:")
        def boom(probes, cfg):
            raise RuntimeError("kaboom")
        col = cm.Collector([cm.Check("boom", "health", boom)], store,
                           FakeProbes(), cm.AlertEngine(cm.NoopNotifier()), self._cfg())
        results = col.run_checks(groups=None, now=100.0)
        self.assertEqual(results[0].id, "boom")
        self.assertEqual(results[0].status, cm.STATUS_FAIL)
        self.assertIn("kaboom", results[0].detail)
        store.close()

    def test_run_once_runs_all_groups(self):
        store = cm.Store(":memory:")
        col = cm.Collector(
            [cm.Check("a", "health", lambda p, c: [cm.CheckResult("a", "health", cm.STATUS_OK, "")]),
             cm.Check("b", "freshness", lambda p, c: [cm.CheckResult("b", "freshness", cm.STATUS_OK, "")])],
            store, FakeProbes(), cm.AlertEngine(cm.NoopNotifier()), self._cfg())
        ids = {r.id for r in col.run_once()}
        self.assertEqual(ids, {"a", "b"})
        store.close()


class TestConfigAndCli(unittest.TestCase):
    def test_default_config_has_required_keys(self):
        cfg = cm.DEFAULT_CONFIG
        for k in ("router_url", "bind_host", "bind_port", "intervals",
                  "sample_window_s", "lxc_ram_ceilings", "rag_metrics_path"):
            self.assertIn(k, cfg)
        self.assertEqual(cfg["lxc_ram_ceilings"]["151"], 32768)

    def test_load_config_missing_returns_defaults(self):
        cfg = cm.load_config("/nonexistent/path.json")
        self.assertEqual(cfg["router_url"], cm.DEFAULT_CONFIG["router_url"])

    def test_load_config_overrides(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(_json.dumps({"bind_port": 9999, "bearer_token": "x"}))
            path = f.name
        cfg = cm.load_config(path)
        os.unlink(path)
        self.assertEqual(cfg["bind_port"], 9999)
        self.assertEqual(cfg["bearer_token"], "x")
        self.assertEqual(cfg["router_url"], cm.DEFAULT_CONFIG["router_url"])

    def test_format_once_table(self):
        results = [cm.CheckResult("anythingllm", "health", cm.STATUS_OK, "HTTP 200"),
                   cm.CheckResult("gpu_vram_0", "metrics", cm.STATUS_FAIL, "99%")]
        table = cm.format_once_table(results)
        self.assertIn("anythingllm", table)
        self.assertIn("FAIL", table)

    def test_main_once_returns_1_on_fail(self):
        # Point at a config whose endpoints all fail -> at least one FAIL.
        rc = cm.main(["--once", "--config", "/nonexistent.json"])
        self.assertEqual(rc, 1)

    def test_load_config_non_dict_json_returns_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(_json.dumps([1, 2, 3]))   # valid JSON, not an object
            path = f.name
        cfg = cm.load_config(path)
        os.unlink(path)
        self.assertEqual(cfg["router_url"], cm.DEFAULT_CONFIG["router_url"])
        self.assertEqual(cfg["bind_port"], cm.DEFAULT_CONFIG["bind_port"])


if __name__ == "__main__":
    unittest.main()
