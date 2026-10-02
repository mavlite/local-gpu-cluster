import os
import re


def test_review_skill_has_required_sections():
    """Skill file must document review procedure, A/B coin-flip, result gate, and eligibility."""
    txt = open(".claude/skills/delegate-review/SKILL.md", encoding="utf-8").read()
    for needle in ("record_review", "coin", "result gate", "20K", "200-line"):
        assert needle.lower() in txt.lower(), f"Missing needle: {needle}"


def test_readme_documents_env_and_start():
    """README must document env vars and schtasks ONLOGON setup."""
    txt = open("scripts/delegate/README.md", encoding="utf-8").read()
    for needle in ("LOCAL_DELEGATE_BEARER_TOKEN", "LOCAL_DELEGATE_ROUTER_TOKEN", "schtasks"):
        assert needle in txt, f"Missing needle: {needle}"


def test_delegation_rule_exists():
    """CLAUDE-delegation-rule.md snippet must exist."""
    path = "clients/opencode-delegate/CLAUDE-delegation-rule.md"
    assert os.path.isfile(path), f"Missing: {path}"
