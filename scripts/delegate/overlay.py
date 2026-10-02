import json
import os
import shutil

_STRIP = ("opencode.json", "opencode.jsonc", "AGENTS.md", "CLAUDE.md")
_MODEL = "qwen3.8-think"


def strip_project_config(dest: str) -> None:
    """Remove attacker-controlled project config from an exported job dir."""
    for name in _STRIP:
        p = os.path.join(dest, name)
        if os.path.lexists(p):
            os.remove(p)
    d = os.path.join(dest, ".opencode")
    if os.path.islink(d):
        os.remove(d)
    elif os.path.isdir(d):
        shutil.rmtree(d)


def _provider(cfg) -> dict:
    # The isolated home has no global provider, so define the router here.
    # The key is read from the environment, never written to disk.
    return {"router": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "router",
        "options": {"baseURL": cfg.router_url,
                    "apiKey": "{env:LOCAL_DELEGATE_ROUTER_TOKEN}",
                    "timeout": 1200000, "chunkTimeout": 180000},
        "models": {_MODEL: {"name": _MODEL, "tools": True,
                            "limit": {"context": 200000, "output": 32768}}},
    }}


def install_overlay(cfg, dest: str, allow_web: bool) -> None:
    agent_dst = os.path.join(dest, ".opencode", "agent")
    os.makedirs(agent_dst, exist_ok=True)
    shutil.copyfile(os.path.join(cfg.overlay_dir, "agent", "delegate.md"),
                    os.path.join(agent_dst, "delegate.md"))
    conf = {"$schema": "https://opencode.ai/config.json",
            "provider": _provider(cfg), "mcp": {}}
    if allow_web:
        conf["mcp"]["searxng"] = {"type": "local",
                                  "command": ["npx", "-y", "mcp-searxng"], "enabled": True}
    with open(os.path.join(dest, "opencode.json"), "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)


def opencode_env(cfg) -> dict:
    # Isolated config home so global RULES/TASK-LOOP/MCPs/skills are NOT inherited.
    # PROVISIONAL: see task-7-report.md for the live-verification status.
    iso = os.path.join(cfg.jobs_dir, "_opencode_home")
    os.makedirs(iso, exist_ok=True)
    return {"HOME": iso, "USERPROFILE": iso,
            "XDG_CONFIG_HOME": os.path.join(iso, ".config"),
            "LOCAL_DELEGATE_ROUTER_TOKEN": cfg.router_token}
