# Memory Vault Phase 2a — Claude Code Auto-Memory Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Claude Code automatically rehydrate project memory at session start (incl. post-compaction) and nudge the model to checkpoint state before compaction, via user-global hooks talking to the deployed Memory Vault REST API.

**Architecture:** Two Python hook scripts (cross-platform, stdlib-only) plus a shared helper module, installed to `~/.claude/hooks/` and registered in `~/.claude/settings.json`. `SessionStart` injects a recent-memory primer + a standing instruction; `UserPromptSubmit` injects a one-time "checkpoint now" nudge when the transcript grows large. All REST calls are best-effort (short timeout, errors swallowed, always `exit 0`).

**Tech Stack:** Python 3 (stdlib `urllib`/`json`/`subprocess`, no third-party deps), `unittest` for tests, Claude Code 2.x hooks, Memory Vault REST (`/api/chunks`).

## Global Constraints

- Hooks are **Python 3, stdlib-only** (no pip installs). Tests use `unittest`, run with `python clients/memory-hooks/test_hooks.py`.
- Every hook **always exits 0** and never blocks a session; all network calls use a **3s timeout** and swallow errors.
- Memory Vault REST: base `http://192.168.6.223:8000` (DHCP), bearer auth. Recent memories: `GET /api/chunks?space=<space>&limit=<n>&sort=recent` → `{"chunks":[{"chunk_id","content","space",...}], "total","limit","offset"}`.
- Memory **space = `MEMVAULT_SPACE` override, else git top-level basename, else cwd basename**.
- Secrets: a **dedicated hooks token** lives only in `~/.config/memory-vault/hooks.env` (KEY=VALUE), **never committed**. Only `hooks.env.example` (no secret) is versioned.
- MCP tool permission ids: `mcp__memory__recall`, `mcp__memory__remember`, `mcp__memory__forget`, `mcp__memory__memory_status`.
- Claude Code `SessionStart`/`UserPromptSubmit` inject context via stdout JSON `{"hookSpecificOutput":{"hookEventName":"<event>","additionalContext":"..."}}`. `SessionStart` matcher `startup|resume|compact`.
- No GPU/fan impact (Memory Vault endpoints are CPU-only).

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `clients/memory-hooks/mv_common.py` (create) | Shared helpers: env load, space resolution, HTTP GET, recent-memory fetch, primer formatting, watchdog decision |
| `clients/memory-hooks/memory_session_start.py` (create) | `SessionStart` hook entry — emits primer + standing instruction |
| `clients/memory-hooks/memory_context_watchdog.py` (create) | `UserPromptSubmit` hook entry — one-time save nudge |
| `clients/memory-hooks/test_hooks.py` (create) | `unittest` tests for all of the above |
| `clients/memory-hooks/settings.snippet.json` (create) | Hooks + permissions block to merge into `~/.claude/settings.json` |
| `clients/memory-hooks/hooks.env.example` (create) | Env template (URL + token placeholder + optional tunables) |
| `clients/memory-hooks/install.py` (create) | Installer: copy hooks, merge settings (idempotent), scaffold `hooks.env` |
| `docs/memory-vault-phase2-hooks.md` (create) | Install + verification doc; host token-mint step |

---

## Task 1: Shared helpers `mv_common.py`

**Files:**
- Create: `clients/memory-hooks/mv_common.py`
- Create/Test: `clients/memory-hooks/test_hooks.py`

**Interfaces — Produces:**
- `load_env(path) -> dict`
- `resolve_space(cwd, env) -> str`
- `http_get_json(url, token, timeout=3) -> dict | None`
- `recent_memories(api_url, token, space, count) -> list[str]`
- `build_session_context(memories, space) -> str`
- `watchdog_should_nudge(transcript_path, threshold_bytes, sentinel_path) -> bool`
- Constants `DEFAULT_PRIMER_COUNT = 6`, `DEFAULT_WATCHDOG_BYTES = 300000`, `STANDING_INSTRUCTION` (str)

- [ ] **Step 1: Write the failing tests**

Create `clients/memory-hooks/test_hooks.py`:

```python
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOOKS_DIR)

import mv_common as mv


class TestMvCommon(unittest.TestCase):
    def test_load_env_parses_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "hooks.env")
            with open(p, "w", encoding="utf-8") as f:
                f.write("# comment\nMEMVAULT_API_URL=http://x:8000\n\nMEMVAULT_HOOKS_TOKEN=abc \n")
            env = mv.load_env(p)
        self.assertEqual(env["MEMVAULT_API_URL"], "http://x:8000")
        self.assertEqual(env["MEMVAULT_HOOKS_TOKEN"], "abc")

    def test_load_env_missing_file_returns_empty(self):
        self.assertEqual(mv.load_env("/no/such/file.env"), {})

    def test_resolve_space_env_override_wins(self):
        self.assertEqual(mv.resolve_space("/whatever", {"MEMVAULT_SPACE": "custom"}), "custom")

    def test_resolve_space_falls_back_to_basename(self):
        # A path that is not a git repo resolves to its basename.
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "my-repo")
            os.makedirs(sub)
            self.assertEqual(mv.resolve_space(sub, {}), "my-repo")

    def test_build_session_context_with_memories(self):
        ctx = mv.build_session_context(["decided X", "use Y"], "proj")
        self.assertIn("space `proj`", ctx)
        self.assertIn("- decided X", ctx)
        self.assertIn("- use Y", ctx)
        self.assertIn(mv.STANDING_INSTRUCTION, ctx)

    def test_build_session_context_empty_is_instruction_only(self):
        ctx = mv.build_session_context([], "proj")
        self.assertEqual(ctx.strip(), mv.STANDING_INSTRUCTION)

    def test_recent_memories_parses_chunks(self):
        fake = {"chunks": [{"content": "alpha\nbeta"}, {"content": " "}, {"content": "gamma"}]}
        with mock.patch.object(mv, "http_get_json", return_value=fake):
            out = mv.recent_memories("http://x:8000", "tok", "sp", 6)
        self.assertEqual(out, ["alpha beta", "gamma"])

    def test_recent_memories_handles_none(self):
        with mock.patch.object(mv, "http_get_json", return_value=None):
            self.assertEqual(mv.recent_memories("http://x:8000", "tok", "sp", 6), [])

    def test_watchdog_nudges_once_over_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            transcript = os.path.join(d, "t.jsonl")
            with open(transcript, "wb") as f:
                f.write(b"x" * 1000)
            sentinel = os.path.join(d, "sent")
            self.assertTrue(mv.watchdog_should_nudge(transcript, 500, sentinel))   # over -> nudge
            self.assertTrue(os.path.exists(sentinel))
            self.assertFalse(mv.watchdog_should_nudge(transcript, 500, sentinel))  # sentinel -> no repeat

    def test_watchdog_no_nudge_under_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            transcript = os.path.join(d, "t.jsonl")
            with open(transcript, "wb") as f:
                f.write(b"x" * 100)
            self.assertFalse(mv.watchdog_should_nudge(transcript, 500, os.path.join(d, "sent")))

    def test_watchdog_missing_transcript_no_nudge(self):
        self.assertFalse(mv.watchdog_should_nudge("/no/file", 1, "/tmp/sent-x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'mv_common'` (file not created yet).

- [ ] **Step 3: Implement `mv_common.py`**

Create `clients/memory-hooks/mv_common.py`:

```python
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
        os.makedirs(os.path.dirname(sentinel_path), exist_ok=True)
        with open(sentinel_path, "w", encoding="utf-8") as f:
            f.write("nudged")
    except OSError:
        pass
    return True
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: PASS — all tests OK.

- [ ] **Step 5: Commit**

```bash
git add clients/memory-hooks/mv_common.py clients/memory-hooks/test_hooks.py
git commit -m "feat(memvault-hooks): shared helpers for Claude Code auto-memory hooks"
```

---

## Task 2: SessionStart hook `memory_session_start.py`

**Files:**
- Create: `clients/memory-hooks/memory_session_start.py`
- Modify: `clients/memory-hooks/test_hooks.py`

**Interfaces:**
- Consumes: `mv_common` (Task 1).
- Produces: a runnable hook script with `main()` reading stdin JSON and printing the SessionStart `additionalContext` JSON.

- [ ] **Step 1: Write the failing test** — append to `test_hooks.py` (before the `if __name__` line):

```python
class TestSessionStartHook(unittest.TestCase):
    def _run_main(self, payload, recent):
        import importlib
        mod = importlib.import_module("memory_session_start")
        importlib.reload(mod)
        out = []
        with mock.patch.object(mod.mv, "load_env", return_value={"MEMVAULT_API_URL": "http://x:8000", "MEMVAULT_HOOKS_TOKEN": "tok"}), \
             mock.patch.object(mod.mv, "recent_memories", return_value=recent), \
             mock.patch.object(mod.mv, "resolve_space", return_value="proj"), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             mock.patch("sys.stdout", io.StringIO()) as fake_out, \
             self.assertRaises(SystemExit) as cm:
            mod.main()
        out = fake_out.getvalue()
        return cm.exception.code, json.loads(out)

    def test_emits_primer_and_instruction(self):
        code, obj = self._run_main({"cwd": "/p", "source": "startup"}, ["decided X"])
        self.assertEqual(code, 0)
        self.assertEqual(obj["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ctx = obj["hookSpecificOutput"]["additionalContext"]
        self.assertIn("decided X", ctx)
        self.assertIn("recall", ctx)

    def test_no_memories_still_valid_json_exit0(self):
        code, obj = self._run_main({"cwd": "/p"}, [])
        self.assertEqual(code, 0)
        self.assertIn("additionalContext", obj["hookSpecificOutput"])
```

Add `import io` to the imports at the top of `test_hooks.py`.

- [ ] **Step 2: Run, verify fail**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory_session_start'`.

- [ ] **Step 3: Implement the hook**

Create `clients/memory-hooks/memory_session_start.py`:

```python
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
```

- [ ] **Step 4: Run, verify pass**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: PASS (all tests, including the two new ones).

- [ ] **Step 5: Commit**

```bash
git add clients/memory-hooks/memory_session_start.py clients/memory-hooks/test_hooks.py
git commit -m "feat(memvault-hooks): SessionStart recall primer + standing instruction"
```

---

## Task 3: UserPromptSubmit watchdog `memory_context_watchdog.py`

**Files:**
- Create: `clients/memory-hooks/memory_context_watchdog.py`
- Modify: `clients/memory-hooks/test_hooks.py`

**Interfaces:**
- Consumes: `mv_common` (Task 1).
- Produces: a runnable hook script printing a one-time nudge `additionalContext` when the transcript is large.

- [ ] **Step 1: Write the failing test** — append to `test_hooks.py` (before `if __name__`):

```python
class TestWatchdogHook(unittest.TestCase):
    def _run_main(self, payload, threshold):
        import importlib
        mod = importlib.import_module("memory_context_watchdog")
        importlib.reload(mod)
        with mock.patch.object(mod.mv, "load_env", return_value={"MEMVAULT_WATCHDOG_BYTES": str(threshold)}), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             mock.patch("sys.stdout", io.StringIO()) as fake_out, \
             self.assertRaises(SystemExit) as cm:
            mod.main()
        return cm.exception.code, fake_out.getvalue()

    def test_nudges_when_large_then_silent(self):
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "t.jsonl")
            with open(t, "wb") as f:
                f.write(b"x" * 2000)
            sid = "sess-" + os.path.basename(d)
            code1, out1 = self._run_main({"transcript_path": t, "session_id": sid}, 500)
            self.assertEqual(code1, 0)
            self.assertIn("remember", out1)
            code2, out2 = self._run_main({"transcript_path": t, "session_id": sid}, 500)
            self.assertEqual(code2, 0)
            self.assertEqual(out2.strip(), "")  # sentinel suppresses the repeat
            # cleanup sentinel
            import tempfile as _t
            try:
                os.remove(os.path.join(_t.gettempdir(), "mv-watchdog-" + sid))
            except OSError:
                pass

    def test_silent_when_small(self):
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "t.jsonl")
            with open(t, "wb") as f:
                f.write(b"x" * 50)
            code, out = self._run_main({"transcript_path": t, "session_id": "small"}, 500)
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")
```

- [ ] **Step 2: Run, verify fail**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory_context_watchdog'`.

- [ ] **Step 3: Implement the watchdog**

Create `clients/memory-hooks/memory_context_watchdog.py`:

```python
#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: once per session, when the transcript grows past
a byte threshold, nudge the model to checkpoint state via the memory `remember` tool."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mv_common as mv

NUDGE = (
    "Context is getting large and may compact soon. If there are durable decisions, "
    "reasons, or current working state not yet saved, call the memory `remember` tool "
    "now so they survive compaction."
)


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    transcript = payload.get("transcript_path") or ""
    session_id = payload.get("session_id") or "unknown"

    env_path = os.path.expanduser(os.path.join("~", ".config", "memory-vault", "hooks.env"))
    env = mv.load_env(env_path)
    try:
        threshold = int(env.get("MEMVAULT_WATCHDOG_BYTES", mv.DEFAULT_WATCHDOG_BYTES))
    except (TypeError, ValueError):
        threshold = mv.DEFAULT_WATCHDOG_BYTES
    sentinel = os.path.join(tempfile.gettempdir(), "mv-watchdog-" + session_id)

    if mv.watchdog_should_nudge(transcript, threshold, sentinel):
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": NUDGE,
            }
        }))
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, verify pass**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add clients/memory-hooks/memory_context_watchdog.py clients/memory-hooks/test_hooks.py
git commit -m "feat(memvault-hooks): UserPromptSubmit context watchdog (one-time save nudge)"
```

---

## Task 4: Config artifacts (settings snippet + env example)

**Files:**
- Create: `clients/memory-hooks/settings.snippet.json`
- Create: `clients/memory-hooks/hooks.env.example`

- [ ] **Step 1: Create `settings.snippet.json`** (the installer substitutes `<HOOKS_DIR>`):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|compact",
        "hooks": [
          { "type": "command", "command": "python \"<HOOKS_DIR>/memory_session_start.py\"" }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "python \"<HOOKS_DIR>/memory_context_watchdog.py\"" }
        ]
      }
    ]
  },
  "permissions": {
    "allow": [
      "mcp__memory__recall",
      "mcp__memory__remember",
      "mcp__memory__forget",
      "mcp__memory__memory_status"
    ]
  }
}
```

- [ ] **Step 2: Create `hooks.env.example`:**

```bash
# Memory Vault hooks config. Copy to ~/.config/memory-vault/hooks.env and chmod 600.
# Mint a DEDICATED token on the host (do not reuse the bridge token):
#   pct exec 156 -- bash -lc 'cd /opt/memory-vault && docker compose exec -T app memory-vault token create workstation-hooks'
MEMVAULT_API_URL=http://192.168.6.223:8000
MEMVAULT_HOOKS_TOKEN=
# Optional tunables:
# MEMVAULT_SPACE=             # force a space (default: git repo basename)
# MEMVAULT_PRIMER_COUNT=6     # recent memories injected at session start
# MEMVAULT_WATCHDOG_BYTES=300000   # transcript size that triggers the one-time save nudge
```

- [ ] **Step 3: Validate the snippet is valid JSON**

Run: `python -c "import json; json.load(open('clients/memory-hooks/settings.snippet.json')); print('valid')"`
Expected: `valid`.

- [ ] **Step 4: Commit**

```bash
git add clients/memory-hooks/settings.snippet.json clients/memory-hooks/hooks.env.example
git commit -m "feat(memvault-hooks): settings snippet + hooks.env template"
```

---

## Task 5: Installer `install.py`

**Files:**
- Create: `clients/memory-hooks/install.py`
- Modify: `clients/memory-hooks/test_hooks.py`

**Interfaces:**
- Produces: `merge_settings(existing: dict, snippet: dict) -> dict` (pure, idempotent deep-merge of `hooks` event lists by command + union of `permissions.allow`) and an `install(home, source_dir) -> None` driver.

- [ ] **Step 1: Write the failing test** — append to `test_hooks.py` (before `if __name__`):

```python
class TestInstaller(unittest.TestCase):
    def test_merge_settings_unions_and_dedupes(self):
        import importlib
        inst = importlib.import_module("install")
        importlib.reload(inst)
        existing = {
            "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "other"}]}]},
            "permissions": {"allow": ["mcp__memory__recall", "Bash(ls)"]},
        }
        snippet = {
            "hooks": {"SessionStart": [{"matcher": "startup|resume|compact", "hooks": [{"type": "command", "command": "python X"}]}]},
            "permissions": {"allow": ["mcp__memory__recall", "mcp__memory__remember"]},
        }
        merged = inst.merge_settings(existing, snippet)
        cmds = [h["command"] for e in merged["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertIn("other", cmds)
        self.assertIn("python X", cmds)
        self.assertEqual(sorted(merged["permissions"]["allow"]),
                         sorted(["mcp__memory__recall", "mcp__memory__remember", "Bash(ls)"]))
        # idempotent: merging again adds nothing
        merged2 = inst.merge_settings(merged, snippet)
        self.assertEqual(merged, merged2)

    def test_install_copies_and_scaffolds(self):
        import importlib
        inst = importlib.import_module("install")
        importlib.reload(inst)
        with tempfile.TemporaryDirectory() as home:
            inst.install(home, HOOKS_DIR)
            self.assertTrue(os.path.exists(os.path.join(home, ".claude", "hooks", "memory_session_start.py")))
            self.assertTrue(os.path.exists(os.path.join(home, ".claude", "hooks", "mv_common.py")))
            settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
            cmds = [h["command"] for e in settings["hooks"]["SessionStart"] for h in e["hooks"]]
            self.assertTrue(any("memory_session_start.py" in c for c in cmds))
            self.assertTrue(os.path.exists(os.path.join(home, ".config", "memory-vault", "hooks.env")))
            # idempotent second run
            inst.install(home, HOOKS_DIR)
            settings2 = json.load(open(os.path.join(home, ".claude", "settings.json")))
            self.assertEqual(settings, settings2)
```

- [ ] **Step 2: Run, verify fail**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'install'`.

- [ ] **Step 3: Implement `install.py`**

Create `clients/memory-hooks/install.py`:

```python
#!/usr/bin/env python3
"""Install the Memory Vault Claude Code hooks for the current user.

- Copies hook scripts to ~/.claude/hooks/
- Merges settings.snippet.json into ~/.claude/settings.json (idempotent; with
  <HOOKS_DIR> resolved to the absolute install path)
- Scaffolds ~/.config/memory-vault/hooks.env from the example if absent

Run: python clients/memory-hooks/install.py
"""
import json
import os
import shutil
import sys

HOOK_SCRIPTS = ("mv_common.py", "memory_session_start.py", "memory_context_watchdog.py")


def merge_settings(existing, snippet):
    """Deep-merge snippet into existing: hooks event-lists merged by command (no dup
    commands), permissions.allow unioned. Returns a new dict; inputs untouched."""
    out = json.loads(json.dumps(existing)) if existing else {}
    # hooks
    out_hooks = out.setdefault("hooks", {})
    for event, entries in snippet.get("hooks", {}).items():
        existing_entries = out_hooks.setdefault(event, [])
        existing_cmds = {
            h.get("command")
            for e in existing_entries for h in e.get("hooks", [])
        }
        for entry in entries:
            new_cmds = [h.get("command") for h in entry.get("hooks", [])]
            if any(c in existing_cmds for c in new_cmds):
                continue  # already present
            existing_entries.append(entry)
    # permissions.allow
    snip_allow = snippet.get("permissions", {}).get("allow")
    if snip_allow:
        perms = out.setdefault("permissions", {})
        allow = perms.setdefault("allow", [])
        for item in snip_allow:
            if item not in allow:
                allow.append(item)
    return out


def install(home, source_dir):
    claude_hooks = os.path.join(home, ".claude", "hooks")
    os.makedirs(claude_hooks, exist_ok=True)
    for name in HOOK_SCRIPTS:
        shutil.copyfile(os.path.join(source_dir, name), os.path.join(claude_hooks, name))

    # settings merge
    snippet_raw = open(os.path.join(source_dir, "settings.snippet.json"), encoding="utf-8").read()
    snippet_raw = snippet_raw.replace("<HOOKS_DIR>", claude_hooks.replace("\\", "/"))
    snippet = json.loads(snippet_raw)
    settings_path = os.path.join(home, ".claude", "settings.json")
    existing = {}
    if os.path.exists(settings_path):
        try:
            existing = json.load(open(settings_path, encoding="utf-8"))
        except ValueError:
            existing = {}
    merged = merge_settings(existing, snippet)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

    # hooks.env scaffold
    env_dir = os.path.join(home, ".config", "memory-vault")
    os.makedirs(env_dir, exist_ok=True)
    env_path = os.path.join(env_dir, "hooks.env")
    if not os.path.exists(env_path):
        shutil.copyfile(os.path.join(source_dir, "hooks.env.example"), env_path)
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass


def main():
    home = os.path.expanduser("~")
    source_dir = os.path.dirname(os.path.abspath(__file__))
    install(home, source_dir)
    print("Installed hooks to", os.path.join(home, ".claude", "hooks"))
    print("Edit", os.path.join(home, ".config", "memory-vault", "hooks.env"), "and set MEMVAULT_HOOKS_TOKEN.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, verify pass**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add clients/memory-hooks/install.py clients/memory-hooks/test_hooks.py
git commit -m "feat(memvault-hooks): idempotent installer (copy + settings merge + env scaffold)"
```

---

## Task 6: Operator/user documentation

**Files:**
- Create: `docs/memory-vault-phase2-hooks.md`

- [ ] **Step 1: Write the doc**

Create `docs/memory-vault-phase2-hooks.md`:

````markdown
# Memory Vault Phase 2a — Claude Code auto-memory hooks

User-global hooks that (1) inject a recent-memory primer + a standing instruction at
session start (incl. after compaction), and (2) nudge a `remember` checkpoint when the
transcript grows large. Stdlib Python; no GPU impact; fails silent if the cluster is
unreachable. Design: `docs/superpowers/specs/2026-06-18-memory-vault-phase2-hooks-design.md`.

## Install (workstation)

1. Mint a dedicated hooks token on the host:
   ```bash
   pct exec 156 -- bash -lc 'cd /opt/memory-vault && docker compose exec -T app memory-vault token create workstation-hooks'
   ```
2. Run the installer:
   ```bash
   python clients/memory-hooks/install.py
   ```
3. Put the token in `~/.config/memory-vault/hooks.env` (`MEMVAULT_HOOKS_TOKEN=...`),
   confirm `MEMVAULT_API_URL=http://192.168.6.223:8000`.
4. Restart Claude Code. On the first launch it will also prompt to approve the `memory`
   MCP server (one time).

## Verify

- **Primer:** in a repo with seeded memories, start `claude` and confirm a system
  reminder lists recent memories for the repo's space + the memory instruction.
- **Resilience:** stop the bridge/app, start a session — it must start cleanly (no primer, no error).
- **Watchdog:** set `MEMVAULT_WATCHDOG_BYTES=2000` in `hooks.env`, drive a long session,
  confirm exactly one "checkpoint now" nudge.
- **Round-trip:** have the model `remember` a decision; `/clear`; confirm the next
  session's primer surfaces it.

## Tunables (`~/.config/memory-vault/hooks.env`)
`MEMVAULT_SPACE` (force space), `MEMVAULT_PRIMER_COUNT` (default 6),
`MEMVAULT_WATCHDOG_BYTES` (default 300000).

## Notes
- Space = git repo basename by default; matches the per-repo `?space=` MCP convention.
- Token is never committed; only `hooks.env.example` is in git.
- Phase 2b (OpenCode plugin) is a separate plan, built after this is validated.
````

- [ ] **Step 2: Commit**

```bash
git add docs/memory-vault-phase2-hooks.md
git commit -m "docs(memvault-hooks): phase 2a install + verification guide"
```

---

## Task 7: Live verification on the workstation (operator)

**Files:** none (runtime validation).

> This task runs on the workstation against the live cluster. It is not unit-testable; it confirms the hooks work in real Claude Code.

- [ ] **Step 1: Mint token + install**

Run the host token-mint (Task 6 Step 1), `python clients/memory-hooks/install.py`, set the token in `hooks.env`.

- [ ] **Step 2: Seed a memory and verify the primer**

In this repo, have Claude Code `remember` a known fact (space `local-gpu-cluster`), then start a fresh `claude` session and confirm the SessionStart system-reminder includes it. Expected: the seeded fact appears in the injected context.

- [ ] **Step 3: Resilience check**

Temporarily set a bad `MEMVAULT_API_URL`, start a session, confirm it starts cleanly with only the standing instruction (no hang/error). Restore the URL.

- [ ] **Step 4: Watchdog check**

Set `MEMVAULT_WATCHDOG_BYTES=2000`, run a session that grows the transcript past it, confirm one nudge. Restore the default.

- [ ] **Step 5: Confirm tests still green**

Run: `python clients/memory-hooks/test_hooks.py`
Expected: PASS.

---

## Self-Review (completed during authoring)

**Spec coverage:** §4.1 helpers → Task 1; §4.2 SessionStart → Task 2; §4.2 watchdog → Task 3; §4.2 permissions + §7 settings/env artifacts → Task 4; install + settings merge (§7) → Task 5; §6 token handling → Tasks 4/6; §8 verification → Tasks 6/7; §10 `/api/chunks` params pinned (confirmed live: `space,limit,sort=recent`). OpenCode (§4.3) is explicitly deferred to its own plan (phase 2b). Refinement: hooks implemented in **Python** (not bash/lib.sh) for Windows + JSON robustness — noted in Global Constraints.

**Placeholder scan:** `<HOOKS_DIR>` is a real template token the installer substitutes (Task 5 Step 3), not an unfinished placeholder. No TBD/TODO. The dedicated token value is an operator secret (Task 6/7), correctly not in git.

**Type/name consistency:** `mv_common` function names (`load_env`, `resolve_space`, `http_get_json`, `recent_memories`, `build_session_context`, `watchdog_should_nudge`) are identical across Tasks 1–5 and the tests. Hook scripts both `import mv_common as mv` and reference `mv.<fn>`. Installer `HOOK_SCRIPTS` matches the three created files. Settings command paths match the script filenames.
