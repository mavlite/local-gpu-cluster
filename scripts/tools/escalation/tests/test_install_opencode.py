"""Installer for the opencode side of the Claude escalation broker, and the budget printer."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import claude_budget  # noqa: E402
import install_opencode as inst  # noqa: E402

BASE = {"model": "router/qwen3.8-think", "instructions": ["~/.config/opencode/RULES.md"],
        "provider": {"router": {"options": {"baseURL": "http://192.168.6.153:8000/v1", "apiKey": "OWNERKEY"},
                                "models": {"qwen3.8-think": {"name": "q"}}}}}


def test_merge_adds_models_agents_commands_without_mutating_input():
    before = copy.deepcopy(BASE)
    out = inst.merge_config(BASE)
    assert BASE == before
    models = out["provider"]["router"]["models"]
    for alias, cap in (("claude-ask", 2048), ("claude-auto", 2048), ("claude-hard", 4096)):
        assert models[alias]["tools"] is False and models[alias]["limit"] == {"context": 16384, "output": cap}
    assert models["qwen3.8-think"] == {"name": "q"}
    agents = out["agent"]
    assert agents["claude"]["model"] == "router/claude-ask" and agents["claude"]["mode"] == "subagent"
    assert agents["claude-hard"]["model"] == "router/claude-hard"
    assert agents["claude-auto"]["model"] == "router/claude-auto"
    assert all(v is False for a in agents.values() for v in a["tools"].values())
    assert "ask-claude" in out["command"] and "claude-budget" in out["command"]
    assert "$ARGUMENTS" in out["command"]["ask-claude"]["template"]
    assert out["model"] == "router/qwen3.8-think"


def test_merge_is_idempotent():
    once = inst.merge_config(BASE)
    assert inst.merge_config(once) == once


def test_rules_lines_added_once():
    t = inst.ensure_rules("# Operating Rules\n")
    assert t.count(inst.RULES_MARKER) == 1 and inst.ensure_rules(t) == t


def test_main_backs_up_and_installs(tmp_path):
    oc = tmp_path / ".config" / "opencode"
    oc.mkdir(parents=True)
    (oc / "config.json").write_text(json.dumps(BASE))
    (oc / "RULES.md").write_text("# Operating Rules\n")
    assert inst.main(str(tmp_path)) == 0
    assert any(p.name.startswith("config.json.bak-") for p in oc.iterdir())
    assert "claude-ask" in json.loads((oc / "config.json").read_text())["provider"]["router"]["models"]
    assert (oc / "skills" / "escalate" / "SKILL.md").exists()
    assert (oc / "bin" / "claude_budget.py").exists()
    assert inst.RULES_MARKER in (oc / "RULES.md").read_text()


def test_budget_report_format_has_no_secrets():
    rep = {"month": {"spent_usd": 23.1, "cap_usd": 150.0}, "day": {"spent_usd": 1.84, "cap_usd": 7.0},
           "calls_left": {"claude-ask": 11, "claude-auto": 3, "claude-hard": 3},
           "resets": {"day": "2026-10-16 00:00 UTC", "month": "2026-11-01 00:00 UTC"},
           "recent": [{"ts": 1792000000, "alias": "claude-ask", "model": "claude-sonnet-5-5", "cost_usd": 0.05,
                       "input_tokens": 900, "output_tokens": 120, "outcome": "ok"}]}
    s = claude_budget.format_report(rep)
    assert "$23.10 of $150.00 this month" in s and "$1.84 of $7.00 today" in s
    assert "ask 11 · auto 3 · hard 3 calls left today" in s and "claude-ask" in s
