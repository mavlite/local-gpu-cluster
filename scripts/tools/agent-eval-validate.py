"""Validate every task set before any model is graded on it.

Three failure modes this catches, all of which look like a working eval:

  passes-unmodified   the task is already solved; it measures nothing
  unsolvable          no solution exists; every model loses equally and it
                      looks like difficulty
  decorative-hidden   a naive solution passes the hidden tests too, so the
                      hidden grading adds no signal over the visible one

The third is the important one for the hidden sets. Each has a CHEAT solution
written to satisfy only the visible examples. If that cheat also passes hidden,
the task is rejected.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from agent_tasks_hard import HARD_TASKS
from agent_tasks_hidden import HIDDEN_TASKS
from agent_tasks_long import LONG_TASKS
from agent_tasks_open import OPEN_TASKS

# Deliberately naive: satisfies the visible examples and nothing more.
CHEATS = {
    "slugify_intent": {"slug.py":
        "def slugify(t):\n    return t.lower().replace(' ', '-')\n"},
    "chunk_intent": {"chunker.py":
        "def chunk(seq, size):\n"
        "    return [seq[i:i+size] for i in range(0, len(seq), size)]\n"},
    "merge_intent": {"merger.py":
        "def deep_merge(a, b):\n    out = dict(a)\n    out.update(b)\n    return out\n"},
    "retry_intent": {"retrier.py":
        "def retry(fn, attempts):\n    return fn()\n"},
    "parse_duration_intent": {"dur.py":
        "def parse_duration(s):\n    return int(s[:-1])\n"},
    "threadsafe_reserve": {},          # unchanged source is the cheat
    "safe_path_join": {},
}

SOLUTIONS = {
    "slugify_intent": {"slug.py":
        "import re\n\n"
        "def slugify(t):\n"
        "    s = re.sub(r'[^a-z0-9]+', '-', t.lower())\n"
        "    return s.strip('-')\n"},
    "chunk_intent": {"chunker.py":
        "def chunk(seq, size):\n"
        "    if size <= 0:\n        raise ValueError('size must be positive')\n"
        "    items = list(seq)\n"
        "    return [items[i:i+size] for i in range(0, len(items), size)]\n"},
    "merge_intent": {"merger.py":
        "import copy\n\n"
        "def deep_merge(a, b):\n"
        "    out = copy.deepcopy(a)\n"
        "    for k, v in b.items():\n"
        "        if isinstance(v, dict) and isinstance(out.get(k), dict):\n"
        "            out[k] = deep_merge(out[k], v)\n"
        "        else:\n"
        "            out[k] = copy.deepcopy(v)\n"
        "    return out\n"},
    "retry_intent": {"retrier.py":
        "def retry(fn, attempts):\n"
        "    last = None\n"
        "    for _ in range(attempts):\n"
        "        try:\n            return fn()\n"
        "        except Exception as e:\n            last = e\n"
        "    raise last\n"},
    "parse_duration_intent": {"dur.py":
        "import re\n\n"
        "UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}\n\n"
        "def parse_duration(s):\n"
        "    m = re.fullmatch(r'(\\d+)([smhd]?)', (s or '').strip())\n"
        "    if not m:\n        raise ValueError('bad duration: %r' % s)\n"
        "    return int(m.group(1)) * UNITS[m.group(2) or 's']\n"},
    "threadsafe_reserve": {"reserver.py":
        "import threading, time\n\n"
        "class Reserver:\n"
        "    def __init__(self):\n"
        "        self._taken = set()\n"
        "        self._granted = []\n"
        "        self._lock = threading.Lock()\n\n"
        "    def reserve(self, key):\n"
        "        with self._lock:\n"
        "            if key in self._taken:\n"
        "                return False\n"
        "            self._taken.add(key)\n"
        "            self._granted.append(key)\n"
        "        time.sleep(0.002)\n"
        "        return True\n\n"
        "    def granted(self):\n"
        "        with self._lock:\n"
        "            return list(self._granted)\n"},
    "safe_path_join": {"paths.py":
        "import os\n\n"
        "def safe_join(root, user_path):\n"
        "    root_abs = os.path.abspath(root)\n"
        "    target = os.path.abspath(os.path.join(root_abs, user_path))\n"
        "    if target != root_abs and not target.startswith(root_abs + os.sep):\n"
        "        raise ValueError('path escapes root: %r' % user_path)\n"
        "    return target\n"},
    "shared_state_leak": {"registry.py":
        "class Registry:\n"
        "    def __init__(self, items=None):\n"
        "        self.items = dict(items) if items is not None else {}\n\n"
        "    def add(self, k, v):\n        self.items[k] = v\n        return self\n\n"
        "    def snapshot(self):\n        return dict(self.items)\n"},
    "search_range_edges": {"ranges.py":
        "import bisect\n\n"
        "def search_range(arr, target):\n"
        "    lo = bisect.bisect_left(arr, target)\n"
        "    if lo == len(arr) or arr[lo] != target:\n        return (-1, -1)\n"
        "    return (lo, bisect.bisect_right(arr, target) - 1)\n"},
    "quadratic_dedupe": {"uniq.py":
        "def dedupe(items):\n"
        "    out, seen, un = [], set(), []\n"
        "    for it in items:\n"
        "        try:\n"
        "            if it in seen:\n                continue\n"
        "            seen.add(it)\n"
        "        except TypeError:\n"
        "            if it in un:\n                continue\n"
        "            un.append(it)\n"
        "        out.append(it)\n"
        "    return out\n"},
    "config_precedence": {"conf.py":
        "def resolve(explicit, env, filecfg, defaults):\n"
        "    out = dict(defaults)\n"
        "    for layer in (filecfg, env, explicit):\n"
        "        for k, v in layer.items():\n"
        "            if v is not None:\n                out[k] = v\n"
        "    return out\n"},
    "indirect_root_cause": {"parse.py":
        "def parse_row(line):\n"
        "    name, qty = line.split(',')\n"
        "    return name.strip(), int(qty.strip())\n"},
    "wide_rename": None,            # mechanical; checked as fails-initially only
    "consistent_validation": None,
}


def build(task, root, extra=None):
    for n, c in task["files"].items():
        open(os.path.join(root, n), "w", encoding="utf-8").write(c)
    for n, c in (extra or {}).items():
        open(os.path.join(root, n), "w", encoding="utf-8").write(c)


def rc_of(cmd, cwd):
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=240)
        return r.returncode
    except subprocess.TimeoutExpired:
        return 1


def check(task, label):
    name = task["name"]
    problems = []

    # 1) must not already pass
    root = tempfile.mkdtemp(prefix="v_")
    try:
        build(task, root)
        if task.get("verify") and rc_of(task["verify"], root) == 0:
            problems.append("passes-unmodified")
        if task.get("hidden_files"):
            for n, c in task["hidden_files"].items():
                open(os.path.join(root, n), "w", encoding="utf-8").write(c)
            if rc_of(task["verify_hidden"], root) == 0:
                problems.append("hidden-passes-unmodified")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 2) hidden grading must add signal: cheat passes visible, fails hidden
    if task.get("hidden_files") and name in CHEATS:
        root = tempfile.mkdtemp(prefix="v_")
        try:
            build(task, root, CHEATS[name])
            vis = rc_of(task.get("verify"), root)
            for n, c in task["hidden_files"].items():
                open(os.path.join(root, n), "w", encoding="utf-8").write(c)
            hid = rc_of(task["verify_hidden"], root)
            if task.get("verify") and vis != 0:
                problems.append("cheat-fails-visible(weak-check)")
            if hid == 0:
                problems.append("DECORATIVE-hidden(cheat passes hidden)")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # 3) must be solvable
    sol = SOLUTIONS.get(name)
    if sol is not None:
        root = tempfile.mkdtemp(prefix="v_")
        try:
            build(task, root, sol)
            if task.get("verify") and rc_of(task["verify"], root) != 0:
                problems.append("reference-fails-visible")
            if task.get("hidden_files"):
                for n, c in task["hidden_files"].items():
                    open(os.path.join(root, n), "w", encoding="utf-8").write(c)
                if rc_of(task["verify_hidden"], root) != 0:
                    problems.append("reference-fails-hidden")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    ok = not problems
    print("  %-8s %-24s %s" % (label, name, "OK" if ok else "; ".join(problems)))
    return 0 if ok else 1


def main():
    bad = 0
    for t in HARD_TASKS:
        bad += check(t, "hard")
    for t in HIDDEN_TASKS:
        bad += check(t, "hidden")
    for t in LONG_TASKS:
        bad += check(t, "long")
    for t in OPEN_TASKS:
        bad += check(t, "open")
    print("\n  %d task(s) need attention" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
