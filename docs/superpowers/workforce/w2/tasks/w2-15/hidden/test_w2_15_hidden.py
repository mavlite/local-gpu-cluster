"""Hidden checks for w2-15: problem-to-problem is silent, resolves reach the notifier, cooldown ends."""
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
    return cm.CheckResult("router_up", "health", status, "d")


def test_warn_to_fail_is_not_a_new_fire():
    eng = cm.AlertEngine(Sink(), cooldown_s=900.0)
    assert eng.evaluate(r(cm.STATUS_FAIL), prev_status=cm.STATUS_WARN, now=10.0) is None


def test_a_resolve_is_sent_to_the_notifier():
    sink = Sink()
    eng = cm.AlertEngine(sink, cooldown_s=900.0)
    eng.evaluate(r(cm.STATUS_FAIL), prev_status=cm.STATUS_OK, now=10.0)
    ev = eng.evaluate(r(cm.STATUS_OK), prev_status=cm.STATUS_FAIL, now=20.0)
    assert ev.kind == "resolved" and [e.kind for e in sink.events] == ["fired", "resolved"]


def test_the_same_fire_returns_once_the_cooldown_is_over():
    eng = cm.AlertEngine(Sink(), cooldown_s=900.0)
    eng.evaluate(r(cm.STATUS_FAIL), prev_status=cm.STATUS_OK, now=0.0)
    assert eng.evaluate(r(cm.STATUS_FAIL), prev_status=cm.STATUS_OK, now=100.0) is None
    assert eng.evaluate(r(cm.STATUS_FAIL), prev_status=cm.STATUS_OK, now=1000.0).kind == "fired"
