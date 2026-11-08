import json
import os
import sys

import pytest

from fake_host import Host
from hwhealth.evaluate import evaluate
from lab_hwhealth import CACHE, MAX_AGE_S, main

LARGE = 3


class FakePlugin:
    instances = []

    def __init__(self, args=None):
        self.args = args
        self.sent = []
        FakePlugin.instances.append(self)

    def sendReport(self, name, report, structuredData=None):
        self.sent.append((name, report, structuredData))
        return True


class FailingPlugin(FakePlugin):
    def sendReport(self, *a, **k):
        return False


class NeverRun:
    def __call__(self, argv, timeout):
        raise AssertionError(f"default mode must not exec: {argv}")


@pytest.fixture(autouse=True)
def _reset():
    FakePlugin.instances.clear()


def write_cache(tmp_path, data):
    p = tmp_path / "cache.json"
    p.write_text(json.dumps(data))
    return p


def fresh(path):
    return lambda: os.path.getmtime(path) + 5


def test_cache_constants():
    assert CACHE == "/usr/lib/vmware/healthd-config/lab-hwhealth/cache.json"
    assert MAX_AGE_S == 180


def test_plugin_stdout(capsys):
    rc = main(["--stdout"], run=Host())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["structured"]["status"] == evaluate(out["unstructured"])[0] == 0
    assert out["structured"]["tags"] == ["hardware"]
    assert out["unstructured"]["schema"] == 1
    assert out["unstructured"]["cpu"]["supported"] is True
    assert not any(m == "vmware" or m.startswith("vmware.") for m in sys.modules)


def test_plugin_sends_report(tmp_path):
    u = {"schema": 1, "cpu": {"supported": True, "tctl_c": 96.0}, "nvme": [], "errors": []}
    path = write_cache(tmp_path, u)
    rc = main(["-u", "http://!vmwLocalSocketHealthd", "--from-cache", str(path)],
              run=NeverRun(), plugin_cls=FakePlugin, clock=fresh(path))
    assert rc == 0
    plugin, = FakePlugin.instances
    assert plugin.args == ["-u", "http://!vmwLocalSocketHealthd"]
    assert plugin.sent == [("lab.hwHealth", u, {"status": 2, "magnitude": LARGE})]


def test_ok_cache_has_magnitude_none(tmp_path):
    u = {"schema": 1, "cpu": {"supported": True, "tctl_c": 50.0}, "nvme": [], "errors": []}
    path = write_cache(tmp_path, u)
    assert main(["--from-cache", str(path)], plugin_cls=FakePlugin, run=NeverRun(),
                clock=fresh(path)) == 0
    assert FakePlugin.instances[0].sent[0][2] == {"status": 0, "magnitude": 0}


def test_from_cache_stale(tmp_path):
    path = write_cache(tmp_path, {"schema": 1, "cpu": {"supported": True, "tctl_c": 50.0}})
    main(["--from-cache", str(path)], plugin_cls=FakePlugin, run=NeverRun(),
         clock=lambda: os.path.getmtime(path) + MAX_AGE_S + 1)
    _, report, sd = FakePlugin.instances[0].sent[0]
    assert report == {"schema": 1, "errors": [{"reading": "cache", "reason": "cache stale"}]}
    assert sd == {"status": 1, "magnitude": LARGE}


def test_cache_missing(tmp_path):
    main(["--from-cache", str(tmp_path / "nope.json")], plugin_cls=FakePlugin, run=NeverRun())
    _, report, sd = FakePlugin.instances[0].sent[0]
    assert report["errors"] == [{"reading": "cache", "reason": "cache missing"}]
    assert sd == {"status": 1, "magnitude": LARGE}


def test_cache_unreadable(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json")
    main(["--from-cache", str(path)], plugin_cls=FakePlugin, run=NeverRun(), clock=fresh(path))
    _, report, sd = FakePlugin.instances[0].sent[0]
    assert report["errors"] == [{"reading": "cache", "reason": "cache unreadable"}]
    assert sd["status"] == 1


def test_send_failure_returns_1(tmp_path):
    assert main(["--from-cache", str(tmp_path / "x")], plugin_cls=FailingPlugin,
                run=NeverRun()) == 1


def test_send_exception_returns_1(tmp_path):
    class Raising(FakePlugin):
        def sendReport(self, *a, **k):
            raise ValueError("Magnitude should not be none for warning/error status")

    assert main(["--from-cache", str(tmp_path / "x")], plugin_cls=Raising, run=NeverRun()) == 1


def test_default_mode_never_calls_runner(tmp_path):
    path = write_cache(tmp_path, {"schema": 1, "cpu": {"supported": True, "tctl_c": 50.0}})
    assert main(["--from-cache", str(path)], run=NeverRun(), plugin_cls=FakePlugin,
                clock=fresh(path)) == 0
