"""Shared helpers for the Memory Vault Claude Code hooks (phase 2a).

Stdlib-only, cross-platform. Every network call is best-effort with a short
timeout and swallows errors so a hook never blocks or slows a session.
"""
import json
import os
import subprocess
import urllib.error
import urllib.request
from urllib.parse import quote

DEFAULT_PRIMER_COUNT = 6
DEFAULT_WATCHDOG_BYTES = 300000

STANDING_INSTRUCTION = (
    "You have a persistent project memory (MCP `memory` tools: recall, remember). "
    "Call `recall` when starting a task or when you need prior decisions or context. "
    "Proactively call `remember` to checkpoint durable decisions, the reasons behind "
    "them, and current working state — especially as the conversation grows — so "
    "the context survives compaction and future sessions."
)


def load_env(path):
    """Parse a KEY=VALUE env file into a dict. Returns {} if absent/unreadable."""
    env = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        return {}
    return env


def resolve_space(cwd, env):
    """Memory space: MEMVAULT_SPACE override, else git top-level basename, else cwd basename."""
    override = (env.get("MEMVAULT_SPACE") or os.environ.get("MEMVAULT_SPACE") or "").strip()
    if override:
        return override
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return os.path.basename(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.basename(os.path.normpath(cwd)) if cwd else "default"


def http_get_json(url, token, timeout=3):
    """GET `url` with bearer auth; return parsed JSON dict/list or None on any failure."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def recent_memories(api_url, token, space, count):
    """Up to `count` recent memory contents for `space`, most-recent first; [] on failure."""
    url = "{}/api/chunks?space={}&limit={}&sort=recent".format(
        api_url.rstrip("/"), quote(space), int(count))
    data = http_get_json(url, token)
    if not isinstance(data, dict):
        return []
    out = []
    for c in data.get("chunks", []):
        text = (c.get("content") or "").strip().replace("\n", " ")
        if text:
            out.append(text[:240])
    return out


def build_session_context(memories, space):
    """Compose additionalContext: recent-memory primer (if any) + standing instruction."""
    lines = []
    if memories:
        lines.append("Recent project memory (space `{}`):".format(space))
        lines.extend("- " + m for m in memories)
        lines.append("")
    lines.append(STANDING_INSTRUCTION)
    return "\n".join(lines)


def watchdog_should_nudge(transcript_path, threshold_bytes, sentinel_path):
    """True if transcript >= threshold AND not already nudged this session.
    Creates the sentinel as a side effect when returning True."""
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return False
    if size < threshold_bytes or os.path.exists(sentinel_path):
        return False
    try:
        d = os.path.dirname(sentinel_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(sentinel_path, "w", encoding="utf-8") as f:
            f.write("nudged")
    except OSError:
        pass
    return True
