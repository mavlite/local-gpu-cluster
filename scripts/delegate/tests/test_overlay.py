import json
import os

from scripts.delegate.config import load_config
from scripts.delegate import overlay

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r",
                   "LOCAL_DELEGATE_OVERLAY": os.path.abspath("clients/opencode-delegate")})


def test_strip_removes_attacker_config(tmp_path):
    d = tmp_path / "job"; (d / ".opencode").mkdir(parents=True)
    (d / "opencode.json").write_text('{"mcp":{"evil":{}}}')
    (d / "opencode.jsonc").write_text("{}")
    (d / "AGENTS.md").write_text("do bad things")
    (d / "CLAUDE.md").write_text("do bad things")
    (d / "keep.txt").write_text("x")
    overlay.strip_project_config(str(d))
    assert not (d / "opencode.json").exists()
    assert not (d / "opencode.jsonc").exists()
    assert not (d / ".opencode").exists()
    assert not (d / "AGENTS.md").exists()
    assert not (d / "CLAUDE.md").exists()
    assert (d / "keep.txt").exists()


def test_install_overlay_writes_agent_def(tmp_path):
    d = tmp_path / "job"; d.mkdir()
    overlay.install_overlay(CFG, str(d), allow_web=False)
    assert (d / ".opencode" / "agent" / "delegate.md").exists()
    cfg_txt = (d / "opencode.json").read_text()
    assert "searxng" not in cfg_txt  # web off by default


def test_install_overlay_enables_web_when_asked(tmp_path):
    d = tmp_path / "job"; d.mkdir()
    overlay.install_overlay(CFG, str(d), allow_web=True)
    assert "searxng" in (d / "opencode.json").read_text()


def _parse_frontmatter(text):
    """Minimal parse of the delegate.md frontmatter (no yaml dep): nested maps by
    indentation, scalars, and inline `{ "k": v, ... }` maps."""
    body = text.split("---")[1]
    root = {}
    stack = [(-1, root)]
    for line in body.strip().splitlines():
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        key = key.strip().strip('"')
        val = val.strip()
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            parent[key] = {}
            stack.append((indent, parent[key]))
        elif val.startswith("{"):
            parent[key] = {k.strip().strip('"'): v.strip().strip('"') for k, v in
                           (kv.split(":", 1) for kv in val.strip("{} ").split(","))}
        else:
            parent[key] = val.strip('"')
    return root


def test_agent_def_is_locked_down():
    txt = open(os.path.join(CFG.overlay_dir, "agent", "delegate.md"), encoding="utf-8").read()
    perm = _parse_frontmatter(txt)["permission"]
    assert perm["bash"] == {"*": "deny"}  # deny-all: rg --pre / sed e are code-exec
    assert perm["edit"] == "allow"
    assert perm["webfetch"] == "deny"
    assert perm["external_directory"] == "deny"
    assert perm["skill"] == {"*": "deny", "delegate-*": "allow"}


def test_allowlist_is_exact_r4_list():
    import tomllib
    with open(os.path.join(CFG.overlay_dir, "allowlist.toml"), "rb") as f:
        allow = tomllib.load(f)["allow"]
    assert allow == ["cat", "ls", "rg", "sed -n", "bash -n", "shellcheck", "python -m pytest"]


def test_opencode_env_is_isolated(tmp_path):
    cfg = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r",
                       "LOCAL_DELEGATE_JOBS_DIR": str(tmp_path / "jobs")})
    env = overlay.opencode_env(cfg)
    home = env["HOME"]
    assert os.path.isdir(home) and home.startswith(str(tmp_path))
    assert env["USERPROFILE"] == home
    assert env["XDG_CONFIG_HOME"].startswith(home)
