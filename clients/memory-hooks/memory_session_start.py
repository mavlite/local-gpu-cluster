#!/usr/bin/env python3
"""Claude Code SessionStart hook: inject a recent-memory primer + standing instruction.

Reads the hook JSON on stdin, resolves the repo's memory space, fetches recent
memories from Memory Vault, and prints hookSpecificOutput.additionalContext.
Always exits 0; on any failure it injects only the standing instruction.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mv_common as mv


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    cwd = payload.get("cwd") or os.getcwd()

    env_path = os.path.expanduser(os.path.join("~", ".config", "memory-vault", "hooks.env"))
    env = mv.load_env(env_path)
    api_url = env.get("MEMVAULT_API_URL", "")
    token = env.get("MEMVAULT_HOOKS_TOKEN", "")
    space = mv.resolve_space(cwd, env)

    memories = []
    if api_url and token:
        try:
            count = int(env.get("MEMVAULT_PRIMER_COUNT", mv.DEFAULT_PRIMER_COUNT))
        except (TypeError, ValueError):
            count = mv.DEFAULT_PRIMER_COUNT
        memories = mv.recent_memories(api_url, token, space, count)

    context = mv.build_session_context(memories, space)
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
