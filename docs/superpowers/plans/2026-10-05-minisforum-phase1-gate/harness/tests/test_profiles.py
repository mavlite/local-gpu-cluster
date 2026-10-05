import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import profiles  # noqa: E402

ROUTER = "http://192.168.6.153:8000/v1"
WORKERS = ["http://172.16.10.205:8090/v1", "http://172.16.10.206:8090/v1", "http://172.16.10.207:8090/v1"]


def read_agent(dest, name):
    with open(os.path.join(dest, ".opencode", "agent", f"{name}.md"), encoding="utf-8") as f:
        return f.read()


def test_arm_a_all_on_router(tmp_path):
    profiles.install("A", str(tmp_path), ROUTER)
    cfg = json.load(open(tmp_path / "opencode.json"))
    assert list(cfg["provider"]) == ["router"]
    for i in (1, 2, 3):
        assert "model: router/qwen3.8-nothink" in read_agent(tmp_path, f"worker-{i}")


def test_arm_b_workers_point_at_their_own_node(tmp_path):
    profiles.install("B", str(tmp_path), ROUTER, WORKERS)
    cfg = json.load(open(tmp_path / "opencode.json"))
    for i, url in enumerate(WORKERS, start=1):
        assert cfg["provider"][f"w{i}"]["options"]["baseURL"] == url
        assert cfg["provider"][f"w{i}"]["models"]["qwen3.6"]["limit"]["context"] == 65536 - 8192
        assert f"model: w{i}/qwen3.6" in read_agent(tmp_path, f"worker-{i}")
    with pytest.raises(ValueError):
        profiles.build_config("B", ROUTER, WORKERS[:2])


def test_deny_by_default_permissions(tmp_path):
    profiles.install("B", str(tmp_path), ROUTER, WORKERS)
    coord = read_agent(tmp_path, "coordinator")
    assert 'edit: "deny"' in coord and 'bash: {"*": "deny"}' in coord and 'webfetch: "deny"' in coord
    for i in (1, 2, 3):
        w = read_agent(tmp_path, f"worker-{i}")
        assert 'edit: "allow"' in w
        assert 'bash: {"*": "deny"}' in w and 'webfetch: "deny"' in w
        assert 'external_directory: "deny"' in w and "mode: subagent" in w
    cfg = json.load(open(tmp_path / "opencode.json"))
    assert cfg["permission"]["bash"] == {"*": "deny"}


def test_no_literal_keys_written(tmp_path):
    profiles.install("B", str(tmp_path), ROUTER, WORKERS)
    blob = open(tmp_path / "opencode.json").read()
    assert "{env:GATE_ROUTER_KEY}" in blob and "{env:GATE_WORKER_KEY}" in blob


def test_env_is_isolated_and_passes_only_gate_keys(tmp_path):
    base = {"PATH": "p", "GATE_ROUTER_KEY": "r", "GATE_WORKER_KEY": "w",
            "GH_TOKEN": "x", "SSH_AUTH_SOCK": "y", "USERPROFILE": "C:\\Users\\me"}
    env = profiles.opencode_env(base, str(tmp_path))
    assert env["HOME"] == str(tmp_path) and env["USERPROFILE"] == str(tmp_path)
    assert env["GATE_ROUTER_KEY"] == "r" and "GH_TOKEN" not in env and "SSH_AUTH_SOCK" not in env
