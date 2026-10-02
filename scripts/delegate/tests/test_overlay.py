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


def test_agent_def_is_locked_down():
    txt = open(os.path.join(CFG.overlay_dir, "agent", "delegate.md"), encoding="utf-8").read()
    assert "external_directory: deny" in txt
    assert "webfetch: deny" in txt
    assert '"*": deny' in txt
    assert "ask" not in txt.split("---")[1]


def test_allowlist_is_argv_prefixes():
    import tomllib
    with open(os.path.join(CFG.overlay_dir, "allowlist.toml"), "rb") as f:
        allow = tomllib.load(f)["allow"]
    assert "python -m pytest" in allow and "git" not in allow


def test_opencode_env_is_isolated(tmp_path):
    cfg = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r",
                       "LOCAL_DELEGATE_JOBS_DIR": str(tmp_path / "jobs")})
    env = overlay.opencode_env(cfg)
    home = env["HOME"]
    assert os.path.isdir(home) and home.startswith(str(tmp_path))
    assert env["USERPROFILE"] == home
    assert env["XDG_CONFIG_HOME"].startswith(home)
