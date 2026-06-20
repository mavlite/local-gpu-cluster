import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
