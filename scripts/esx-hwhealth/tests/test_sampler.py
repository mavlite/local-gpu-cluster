import json
import os

import lab_hwhealth_sampler as sampler
from fake_host import Host


def test_sampler_atomic_write(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "cache.json"
    replaced = []
    real = os.replace

    def spy(src, dst):
        replaced.append((src, dst))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    sampler.run_once(Host(), str(path))
    assert json.loads(path.read_text())["schema"] == 1
    (src, dst), = replaced
    assert os.path.dirname(src) == os.path.dirname(dst) == str(path.parent)
    assert os.listdir(path.parent) == ["cache.json"]  # no temp file left behind


def test_failed_write_keeps_old_cache(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text('{"old": 1}')
    monkeypatch.setattr(sampler, "collect", lambda run: {"bad": object()})
    try:
        sampler.run_once(Host(), str(path))
        raised = False
    except TypeError:
        raised = True
    assert raised
    assert json.loads(path.read_text()) == {"old": 1}
    assert os.listdir(tmp_path) == ["cache.json"]


def test_once_mode(tmp_path):
    path = tmp_path / "c.json"
    assert sampler.main(["--once", "--cache", str(path)], run=Host()) == 0
    assert path.exists()


def test_loop_runs_two_cycles_and_survives_errors(tmp_path, capsys, monkeypatch):
    path = tmp_path / "c.json"
    calls = {"n": 0}
    original = sampler.run_once

    def flaky(run, p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("hostd 503")
        original(run, p)

    monkeypatch.setattr(sampler, "run_once", flaky)
    sleeps = []
    rc = sampler.main(["--cache", str(path)], run=Host(), sleep=sleeps.append, max_cycles=2)
    assert rc == 0 and calls["n"] == 2 and sleeps == [60]
    assert "cycle failed" in capsys.readouterr().err
    assert path.exists()
