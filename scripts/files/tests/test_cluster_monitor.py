import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm


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
