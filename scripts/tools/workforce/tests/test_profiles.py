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


def test_only_the_keys_an_arm_needs_reach_the_agents(tmp_path):
    base = {"WF_ROUTER_KEY": "r", "WF_WORKER_KEY": "w"}
    env = profiles.opencode_env(base, str(tmp_path), "/cfg", keys=profiles.keys_for("G"))
    assert env["WF_ROUTER_KEY"] == "r" and "WF_WORKER_KEY" not in env
    assert profiles.keys_for("T") == ("WF_ROUTER_KEY", "WF_WORKER_KEY")


def test_prompts_carry_the_round_2_sentences():
    flat = " ".join(profiles.REVIEWER_PROMPT.split())          # the prompt is wrapped at 100 columns
    assert "If you are told your steps are exhausted" in flat
    assert "do not re-run it" in flat
    assert "Issue independent reads and greps in the same step" in flat
    assert "Your SUMMARY must list each one" in " ".join(profiles.IMPLEMENTER_PROMPT.split())


@pytest.mark.parametrize("arm", ["T", "G"])
def test_reviewer_verdict_agent_has_no_tools_and_few_steps(arm):
    cfg = profiles.build_config(arm, ROUTER, WORKERS if arm == "T" else [])
    a = cfg["agent"]["reviewer-verdict"]
    assert a["model"] == f"router/{profiles.LEAD_ALIAS}" and a["steps"] == profiles.VERDICT_STEPS == 3
    for tool in ("edit", "bash", "read", "grep", "glob", "list", "webfetch", "task"):
        v = a["permission"][tool]
        assert v == "deny" or v == {"*": "deny"}, tool


def test_arm_s_is_the_gpu_implementer_alone_on_the_router():
    # Round 2 §5.2: arm S implements with the lead model and never reviews; same config shape as G.
    cfg = profiles.build_config("S", ROUTER, [])
    assert set(cfg["provider"]) == {"router"}
    assert cfg["agent"]["impl-1"]["model"] == "router/qwen3.8-nothink"
    assert {"reviewer", "reviewer-verdict", "fixer"} <= set(cfg["agent"])
    assert profiles.implementers("S") == ["impl-1"] and profiles.keys_for("S") == ("WF_ROUTER_KEY",)


def test_reviewer_model_override_puts_only_the_reviewer_on_a_worker():
    # Reviewer-model trial: the reviewer (and its verdict turn) on a worker-served candidate model;
    # implementer and fixer stay on the lead; the worker provider exists without implementer agents.
    cfg = profiles.build_config("G", ROUTER, WORKERS[:1], reviewer_model="w1/qwen3.6")
    assert set(cfg["provider"]) == {"router", "w1"} and "qwen3.6" in cfg["provider"]["w1"]["models"]
    assert cfg["agent"]["reviewer"]["model"] == cfg["agent"]["reviewer-verdict"]["model"] == "w1/qwen3.6"
    assert cfg["agent"]["fixer"]["model"] == cfg["agent"]["impl-1"]["model"] == "router/qwen3.8-nothink"
    assert sorted(a for a in cfg["agent"] if a.startswith("impl-")) == ["impl-1"]
    assert profiles.build_config("G", ROUTER, [])["agent"]["reviewer"]["model"] == "router/qwen3.8-nothink"


def test_read_first_rule_is_added_to_the_reviewer_prompt_only_when_asked():
    # Trial 2: one-step verdicts hallucinated bugs; the rule forces at least one read of a changed function.
    plain = profiles.build_config("G", ROUTER, [])["agent"]
    ruled = profiles.build_config("G", ROUTER, [], read_first=True)["agent"]
    assert profiles.READ_FIRST_RULE not in plain["reviewer"]["prompt"]
    assert ruled["reviewer"]["prompt"].endswith(profiles.READ_FIRST_RULE)
    assert ruled["reviewer"]["prompt"].startswith(profiles.REVIEWER_PROMPT.split("Your reply must START")[0][:40])
    assert "Your reply must START" in ruled["reviewer"]["prompt"]           # the verdict format stays last-but-one
    assert ruled["reviewer-verdict"]["prompt"] == plain["reviewer-verdict"]["prompt"]   # the tools-off turn cannot read
    assert ruled["fixer"]["prompt"] == plain["fixer"]["prompt"]


def test_worker_alias_override_names_the_worker_model_and_the_trial_reviewer():
    # Trial 3: a worker serving Gemma 4 under alias "gemma4"; the reviewer model is w1/gemma4.
    cfg = profiles.build_config("G", ROUTER, WORKERS[:1], reviewer_model="w1/gemma4", worker_alias="gemma4")
    assert list(cfg["provider"]["w1"]["models"]) == ["gemma4"]
    assert cfg["agent"]["reviewer"]["model"] == "w1/gemma4"
    assert list(profiles.build_config("G", ROUTER, WORKERS[:1], reviewer_model="w1/qwen3.6")["provider"]["w1"]["models"]) == ["qwen3.6"]


def test_review_steps_override_changes_only_the_reviewer_cap():
    cfg = profiles.build_config("G", ROUTER, [], review_steps=30)["agent"]
    assert cfg["reviewer"]["steps"] == 30 and cfg["reviewer-verdict"]["steps"] == profiles.VERDICT_STEPS
    assert cfg["fixer"]["steps"] == profiles.FIX_STEPS and cfg["impl-1"]["steps"] == profiles.IMPL_STEPS
    assert profiles.build_config("G", ROUTER, [])["agent"]["reviewer"]["steps"] == profiles.REVIEW_STEPS == 20


GPU_LEAD = "http://192.168.6.151:8090/v1"


def test_lead_url_moves_the_whole_lead_to_its_own_provider():
    # Orchestration trial: reviewer, verdict turn and fixer on the GPU lead server; the workers unchanged.
    cfg = profiles.build_config("T", ROUTER, WORKERS, lead_url=GPU_LEAD)
    assert cfg["provider"]["lead"]["options"]["baseURL"] == GPU_LEAD
    assert cfg["provider"]["lead"]["options"]["apiKey"] == "{env:WF_WORKER_KEY}"
    a = cfg["agent"]
    assert a["reviewer"]["model"] == a["reviewer-verdict"]["model"] == a["fixer"]["model"] == "lead/qwen3.6"
    assert [a[f"impl-{i}"]["model"] for i in (1, 2, 3)] == ["w1/qwen3.6", "w2/qwen3.6", "w3/qwen3.6"]
    assert cfg["model"] == "lead/qwen3.6"
    g = profiles.build_config("S", ROUTER, [], lead_url=GPU_LEAD, lead_alias="other")["agent"]
    assert g["impl-1"]["model"] == "lead/other"
    assert profiles.keys_for("S", lead_url=GPU_LEAD) == profiles.KEY_ENV and profiles.keys_for("S") == ("WF_ROUTER_KEY",)


def test_reviewer_temperature_sets_only_the_reviewer_agents():
    a = profiles.build_config("G", ROUTER, [], reviewer_temperature=0.2)["agent"]
    assert a["reviewer"]["temperature"] == a["reviewer-verdict"]["temperature"] == 0.2
    assert "temperature" not in a["fixer"] and "temperature" not in a["impl-1"]
    assert "temperature" not in profiles.build_config("G", ROUTER, [])["agent"]["reviewer"]
