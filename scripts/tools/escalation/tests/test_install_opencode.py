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


# ---- fix round 1 ----
import io  # noqa: E402

import pytest  # noqa: E402

PROHIBITION = "NEVER call `claude` or `claude-hard` unless"
HEADINGS = ["## Goal", "## Context", "## Reproduce", "## Exact error", "## Code", "## What was tried", "## Question"]


def _home(tmp_path, cfg=BASE, rules="# Operating Rules\n"):
    oc = tmp_path / ".config" / "opencode"
    oc.mkdir(parents=True)
    (oc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if rules is not None:
        (oc / "RULES.md").write_text(rules, encoding="utf-8")
    return oc


def test_failed_merge_leaves_config_untouched_and_no_stray_files(tmp_path):
    oc = _home(tmp_path, cfg={"provider": []})
    before = (oc / "config.json").read_bytes()
    names = sorted(p.name for p in oc.iterdir())
    with pytest.raises(Exception):
        inst.main(str(tmp_path))
    assert (oc / "config.json").read_bytes() == before
    assert sorted(p.name for p in oc.iterdir()) == names


def test_skill_and_rules_forbid_unasked_claude_and_claude_hard():
    skill = open(os.path.join(inst.HERE, "skill", "SKILL.md"), encoding="utf-8").read()
    assert PROHIBITION in skill and PROHIBITION in inst.RULES_LINES


def test_ask_template_hard_is_exact_first_word():
    t = inst.ASK_TEMPLATE
    assert "first word" in t and "exactly" in t


def test_skill_template_headings_inside_fence():
    lines = open(os.path.join(inst.HERE, "skill", "SKILL.md"), encoding="utf-8").read().splitlines()
    fences = [i for i, ln in enumerate(lines) if ln.strip().startswith("```")]
    assert len(fences) >= 2
    inside = lines[fences[0] + 1:fences[1]]
    for h in HEADINGS:
        assert h in inside
    assert not any(ln in HEADINGS for ln in lines[fences[1]:])


def test_two_runs_same_second_keep_distinct_backups_and_first_holds_original(tmp_path):
    oc = _home(tmp_path)
    original = (oc / "config.json").read_bytes()
    inst.main(str(tmp_path))
    inst.main(str(tmp_path))
    baks = [p for p in oc.iterdir() if p.name.startswith("config.json.bak-")]
    assert len(baks) == 2 and any(b.read_bytes() == original for b in baks)


def test_main_reports_replaced_names_not_values(tmp_path, capsys):
    cfg = copy.deepcopy(BASE)
    cfg["agent"] = {"claude": {"model": "SECRETVALUE"}}
    _home(tmp_path, cfg)
    inst.main(str(tmp_path))
    out = capsys.readouterr().out
    assert "agent.claude" in out and "SECRETVALUE" not in out and "OWNERKEY" not in out


def test_non_ascii_survives_roundtrip_and_missing_rules_created(tmp_path):
    cfg = copy.deepcopy(BASE)
    cfg["provider"]["router"]["models"]["qwen3.8-think"] = {"name": "q \U0001F600 é"}
    oc = _home(tmp_path, cfg, rules=None)
    inst.main(str(tmp_path))
    got = json.loads((oc / "config.json").read_text(encoding="utf-8"))
    assert got["provider"]["router"]["models"]["qwen3.8-think"]["name"] == "q \U0001F600 é"
    assert inst.RULES_MARKER in (oc / "RULES.md").read_text(encoding="utf-8")


def _budget_home(tmp_path, monkeypatch, base_url):
    oc = tmp_path / ".config" / "opencode"
    oc.mkdir(parents=True)
    cfg = {"provider": {"router": {"options": {"baseURL": base_url, "apiKey": "TOPSECRETKEY"}}}}
    (oc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_budget_unreachable_prints_no_key_and_returns_1(tmp_path, monkeypatch, capsys):
    _budget_home(tmp_path, monkeypatch, "http://127.0.0.1:1/v1")
    assert claude_budget.main() == 1
    cap = capsys.readouterr()
    assert "TOPSECRETKEY" not in cap.out + cap.err


@pytest.mark.parametrize("base", ["http://h:8000/v1", "http://h:8000/v1/", "http://h:8000"])
def test_budget_url_normalised(tmp_path, monkeypatch, capsys, base):
    _budget_home(tmp_path, monkeypatch, base)
    seen = []
    rep = {"month": {"spent_usd": 1, "cap_usd": 2}, "day": {"spent_usd": 1, "cap_usd": 2},
           "calls_left": {"claude-ask": 1, "claude-auto": 1, "claude-hard": 1},
           "resets": {"day": "d", "month": "m"}, "recent": []}

    def fake(req):
        seen.append(req.full_url)
        return io.BytesIO(json.dumps(rep).encode())
    monkeypatch.setattr(claude_budget, "_open", fake)
    assert claude_budget.main() == 0
    assert seen == ["http://h:8000/escalation/budget"]
    assert "TOPSECRETKEY" not in capsys.readouterr().out


def test_budget_opener_does_not_follow_redirects():
    import urllib.request
    handler = claude_budget.NoRedirect()
    req = urllib.request.Request("http://h/x")
    assert handler.redirect_request(req, None, 302, "Found", {}, "http://evil/") is None
