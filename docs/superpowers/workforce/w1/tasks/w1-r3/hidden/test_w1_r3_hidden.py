"""Hidden checks for w1-r3: cooldowns are kept per (check, status)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm  # noqa: E402


class Sink(cm.Notifier):
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)


def r(status):
    return cm.CheckResult("disk_free", "metrics", status, "d")


def test_warn_cooldown_survives_a_fail_in_between():
    eng = cm.AlertEngine(Sink(), cooldown_s=900.0)
    assert eng.evaluate(r(cm.STATUS_WARN), prev_status="ok", now=0.0).kind == "fired"
    assert eng.evaluate(r(cm.STATUS_FAIL), prev_status="ok", now=50.0).kind == "fired"
    assert eng.evaluate(r(cm.STATUS_WARN), prev_status="ok", now=100.0) is None


def test_the_same_status_fires_again_once_its_cooldown_has_passed():
    eng = cm.AlertEngine(Sink(), cooldown_s=900.0)
    eng.evaluate(r(cm.STATUS_WARN), prev_status="ok", now=0.0)
    ev = eng.evaluate(r(cm.STATUS_WARN), prev_status="ok", now=901.0)
    assert ev is not None and ev.kind == "fired"
