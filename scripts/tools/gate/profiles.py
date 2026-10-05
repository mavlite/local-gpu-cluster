"""Isolated opencode profiles for the gate arms (spec §5.0.2-5.0.3, §5.4-5.5).

Each run gets a throw-away workspace with its own opencode.json and agents, and runs
under an isolated HOME, so the user's global config (bash/edit/webfetch: allow) is
never loaded. Keys appear only as {env:...} references, never as literals.
"""
import json
import os

ROUTER_MODEL = "qwen3.8-nothink"          # coordinator, and the GPU workers in arm A (thinking pinned OFF)
WORKER_MODEL = "qwen3.6"                  # alias the CPU workers serve (--alias)
N_WORKERS = 3

COORDINATOR_PROMPT = """You coordinate workers. You never write code yourself.
The folder tasks/ contains one sub-folder per task. Assign the tasks round-robin to
worker-1, worker-2 and worker-3 using the task tool. Start one task on EACH worker in the
SAME message so they run at the same time; whenever a worker finishes, give it the next
unassigned task. Tell each worker the exact folder (for example tasks/T1-ttl-cache), that the
full contract is in that folder's TASK.md, and that it may edit only files in that folder.
Do not review or retry their work. When every task has been handed out once and every
worker has reported back, reply with the single word DONE."""

WORKER_PROMPT = """You implement exactly one task. Read TASK.md in the folder you are given,
then edit the Python module in that folder so it satisfies every rule in TASK.md.
Edit only files inside that folder. You cannot run commands. Finish with a line starting
with SUMMARY: that says what you implemented."""

_DENY_ALL = {"edit": "deny", "bash": {"*": "deny"}, "webfetch": "deny",
             "external_directory": "deny", "skill": {"*": "deny"}}
_WORKER_PERMS = {"edit": "allow", "bash": {"*": "deny"}, "webfetch": "deny",
                 "external_directory": "deny", "skill": {"*": "deny"}}


def _model_entry(name, context, output):
    return {name: {"name": name, "tools": True, "limit": {"context": context, "output": output}}}


def build_config(arm, router_url, worker_urls=()):
    if arm not in ("A", "B"):
        raise ValueError("arm must be A or B")
    # Arm A: 3 slots x 128K on the GPU. Arm B: 1 slot x 256K coordinator; CPU workers 64K.
    coord_ctx = 120000 if arm == "A" else 200000
    providers = {"router": {
        "npm": "@ai-sdk/openai-compatible", "name": "router",
        "options": {"baseURL": router_url, "apiKey": "{env:GATE_ROUTER_KEY}",
                    "timeout": 1800000, "chunkTimeout": 600000},
        "models": _model_entry(ROUTER_MODEL, coord_ctx, 8192)}}
    if arm == "B":
        if len(worker_urls) != N_WORKERS:
            raise ValueError(f"arm B needs {N_WORKERS} worker URLs")
        for i, url in enumerate(worker_urls, start=1):
            providers[f"w{i}"] = {
                "npm": "@ai-sdk/openai-compatible", "name": f"w{i}",
                "options": {"baseURL": url, "apiKey": "{env:GATE_WORKER_KEY}",
                            "timeout": 1800000, "chunkTimeout": 600000},
                "models": _model_entry(WORKER_MODEL, 65536 - 8192, 8192)}
    return {"$schema": "https://opencode.ai/config.json", "provider": providers,
            "mcp": {}, "permission": _DENY_ALL}


def _agent_md(description, mode, model, perms, prompt):
    lines = ["---", f"description: {description}", f"mode: {mode}", f"model: {model}",
             "permission:"]
    for k, v in perms.items():
        lines.append(f"  {k}: {json.dumps(v)}")
    return "\n".join(lines + ["---", prompt, ""])


def worker_model(arm, i):
    return f"router/{ROUTER_MODEL}" if arm == "A" else f"w{i}/{WORKER_MODEL}"


def install(arm, dest, router_url, worker_urls=()):
    """Write opencode.json and .opencode/agent/*.md into the run workspace."""
    with open(os.path.join(dest, "opencode.json"), "w", encoding="utf-8") as f:
        json.dump(build_config(arm, router_url, worker_urls), f, indent=1)
    adir = os.path.join(dest, ".opencode", "agent")
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "coordinator.md"), "w", encoding="utf-8") as f:
        f.write(_agent_md("Gate coordinator: dispatches tasks, never implements", "primary",
                          f"router/{ROUTER_MODEL}", _DENY_ALL, COORDINATOR_PROMPT))
    for i in range(1, N_WORKERS + 1):
        with open(os.path.join(adir, f"worker-{i}.md"), "w", encoding="utf-8") as f:
            f.write(_agent_md(f"Gate worker {i}: implements one task", "subagent",
                              worker_model(arm, i), _WORKER_PERMS, WORKER_PROMPT))


def opencode_env(base_env, iso_home):
    """Child env: isolated HOME/XDG so no global config loads; only the gate keys pass through."""
    env = {k: v for k, v in base_env.items()
           if k.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
                            "SYSTEMDRIVE", "APPDATA", "LOCALAPPDATA", "LANG"}}
    for k in ("GATE_ROUTER_KEY", "GATE_WORKER_KEY"):
        if k in base_env:
            env[k] = base_env[k]
    env.update({"HOME": iso_home, "USERPROFILE": iso_home,
                "XDG_CONFIG_HOME": os.path.join(iso_home, ".config"),
                "XDG_DATA_HOME": os.path.join(iso_home, ".local", "share")})
    return env
