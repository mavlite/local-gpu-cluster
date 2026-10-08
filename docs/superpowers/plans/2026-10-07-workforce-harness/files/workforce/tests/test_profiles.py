"""opencode configuration for the workforce arms (spec §4, §5.1, §5.3, §6)."""
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import profiles  # noqa: E402

ROUTER = "http://192.168.6.153:8000/v1"
WORKERS = [f"http://172.16.10.{n}:8090/v1" for n in (205, 206, 207)]


def test_arm_t_has_three_cpu_implementers_and_the_lead_on_the_router():
    cfg = profiles.build_config("T", ROUTER, WORKERS)
    assert set(cfg["provider"]) == {"router", "w1", "w2", "w3"}
    assert [cfg["agent"][f"impl-{i}"]["model"] for i in (1, 2, 3)] == ["w1/qwen3.6", "w2/qwen3.6", "w3/qwen3.6"]
    assert cfg["agent"]["reviewer"]["model"] == cfg["agent"]["fixer"]["model"] == "router/qwen3.8-nothink"


def test_arm_g_has_one_gpu_implementer():
    cfg = profiles.build_config("G", ROUTER, [])
    assert set(cfg["provider"]) == {"router"}
    impls = sorted(a for a in cfg["agent"] if a.startswith("impl-"))
    assert impls == ["impl-1"] and cfg["agent"]["impl-1"]["model"] == "router/qwen3.8-nothink"


def test_keys_are_env_references_never_literals():
    text = json.dumps(profiles.build_config("T", ROUTER, WORKERS))
    assert "{env:WF_ROUTER_KEY}" in text and "{env:WF_WORKER_KEY}" in text


def test_permissions_per_role():
    a = profiles.build_config("T", ROUTER, WORKERS)["agent"]
    for role in ("impl-1", "fixer"):
        p = a[role]["permission"]
        assert p["edit"] == "allow" and p["bash"] == {"*": "allow"}
    assert a["reviewer"]["permission"]["edit"] == "deny" and a["reviewer"]["permission"]["bash"] == {"*": "allow"}
    for role in ("impl-1", "reviewer", "fixer"):
        p = a[role]["permission"]
        assert p["webfetch"] == p["websearch"] == p["task"] == p["external_directory"] == "deny"
        assert p["skill"] == {"*": "deny"} and p["doom_loop"] == "deny"
        assert a[role]["mode"] == "primary" and a[role]["steps"] > 0


def test_builtin_subagents_are_disabled():
    a = profiles.build_config("G", ROUTER, [])["agent"]
    assert a["general"] == {"disable": True} and a["explore"] == {"disable": True}


def test_wrong_arm_or_worker_count_is_refused():
    with pytest.raises(ValueError):
        profiles.build_config("X", ROUTER, [])
    with pytest.raises(ValueError):
        profiles.build_config("T", ROUTER, WORKERS[:2])


def test_install_writes_a_read_only_config(tmp_path):
    path = profiles.install(str(tmp_path / "cfg"), profiles.build_config("G", ROUTER, []))
    with open(path) as f:
        assert json.load(f)["agent"]["impl-1"]
    assert not os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def test_env_locks_out_project_config_and_passes_only_the_workforce_keys(tmp_path):
    base = {"PATH": "/usr/bin", "WF_ROUTER_KEY": "r", "WF_WORKER_KEY": "w", "ROUTER_API_KEY": "owner",
            "OPENCODE_CONFIG_CONTENT": "{}", "HOME": "/root"}
    env = profiles.opencode_env(base, str(tmp_path / "home"), "/cfg/opencode.json")
    assert env["OPENCODE_CONFIG"] == "/cfg/opencode.json"
    for k in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_EXTERNAL_SKILLS",
              "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_SHARE"):
        assert env[k] == "1"
    assert env["WF_ROUTER_KEY"] == "r" and env["WF_WORKER_KEY"] == "w"
    assert "ROUTER_API_KEY" not in env and "OPENCODE_CONFIG_CONTENT" not in env
    assert env["HOME"] == str(tmp_path / "home")
