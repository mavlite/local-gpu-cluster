import json
import os
import shutil

_STRIP = ("opencode.json", "opencode.jsonc", "AGENTS.md", "CLAUDE.md")
_MODEL = "qwen3.8-think"


def _is_stripped_file(name: str) -> bool:
    # opencode.json* (json/jsonc/json5) plus AGENTS.md/CLAUDE.md, matched anywhere.
    return name in _STRIP or name.startswith("opencode.json")


def strip_project_config(dest: str) -> None:
    """Remove attacker-controlled project config anywhere under an exported job dir.

    Recursive: opencode loads nested AGENTS.md/opencode.json/.opencode, so a stray
    copy in a subdirectory would re-arm the exact config the overlay strips at root.
    """
    for root, dirs, files in os.walk(dest):
        for name in list(dirs):
            if name == ".opencode":
                p = os.path.join(root, name)
                if os.path.islink(p):
                    os.remove(p)
                else:
                    shutil.rmtree(p, ignore_errors=True)
                dirs.remove(name)  # don't descend into the removed tree
        for name in files:
            if _is_stripped_file(name):
                p = os.path.join(root, name)
                if os.path.lexists(p):
                    os.remove(p)


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
