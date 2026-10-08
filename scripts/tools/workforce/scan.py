"""Transcript scan of a harvested run (spec §8: "transcripts are scanned for leaked solutions; a tainted
task is voided and reported"; Plan C hand-off: grade forgery). Runs on the workstation.

tainted  a distinctive line of the task's reference answer appeared in something an agent READ (any
         tool's output) before any agent had written that line. Lines already in the task's own
         snapshot or request are not evidence: the agent was given them. A tainted task is voided in
         every run of both arms (`cli.py w3 --void`), so the pairing stays intact.
suspect  an agent wrote something that could forge or short-circuit a grade: JUnit reports, pytest
         hooks, conftest, os._exit. The grader already defends against these (JUnit must list every
         expected test; --noconftest; harness-owned config); a suspect task is reported for a human
         to read, not voided automatically.
Transcripts are the events this harness captured itself (never the agent-writable session DB), read
in the order they happened: failed attempts, then implement/review rounds, then the lead's fix.
"""
import json
import os
import re
import tarfile

MIN_LEN = 40
WRITE_TOOLS = {"write", "edit", "multiedit", "patch"}
FORGERY = re.compile(r"junit|pytest_sessionfinish|pytest_runtest|pytest_collect|pytest_terminal_summary|"
                     r"pytest_configure|conftest|os\._exit", re.I)
_ORDER = re.compile(r"^(impl|review)-r(\d+)(?:-fail(\d+))?\.jsonl$")


def answer_lines(patch_text):
    """Distinctive added lines of a reference patch: at least MIN_LEN characters stripped, and not a
    line the patch also removes (a moved line is not new information)."""
    added, removed = set(), set()
    for line in patch_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.add(line[1:].strip())
        elif line.startswith("-") and not line.startswith("---"):
            removed.add(line[1:].strip())
    return {s for s in added - removed if len(s) >= MIN_LEN}


def given_text(bundle_dir):
    """Everything the agent was handed: the snapshot's files and the request."""
    parts = []
    with tarfile.open(os.path.join(bundle_dir, "snapshot.tar")) as t:
        for m in t.getmembers():
            if m.isfile():
                parts.append(t.extractfile(m).read().decode("utf-8", errors="replace"))
    req = os.path.join(bundle_dir, "request.md")
    if os.path.isfile(req):
        with open(req, encoding="utf-8", errors="replace") as f:
            parts.append(f.read())
    return "\n".join(parts)


def transcript_order(names):
    def key(n):
        if n == "fix.jsonl":
            return (10 ** 6, 0, 0)
        m = _ORDER.match(n)
        rnd, kind, fail = int(m.group(2)), m.group(1), m.group(3)
        return (rnd, 0 if kind == "impl" else 1, -1 if fail is None else -10 ** 3 + int(fail))
    return sorted((n for n in names if n == "fix.jsonl" or _ORDER.match(n)), key=key)


def _calls(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "tool_use":
                part = ev.get("part") or {}
                st = part.get("state") or {}
                yield part.get("tool"), st.get("input") or {}, str(st.get("output") or "")


def _written(tool, inp):
    if tool in WRITE_TOOLS:
        return "\n".join(str(inp.get(k, "")) for k in ("content", "newString", "patch", "edits"))
    if tool == "bash":
        return str(inp.get("command", ""))
    return ""


def scan_task(task_dir, bundle_dir, ref_patch):
    with open(ref_patch, encoding="utf-8", errors="replace") as f:
        lines = answer_lines(f.read())
    given = given_text(bundle_dir)
    lines = {s for s in lines if s not in given}
    written, tainted, suspect = set(), [], []
    for name in transcript_order(os.listdir(task_dir)):
        for tool, inp, out in _calls(os.path.join(task_dir, name)):
            for s in lines - written:
                if s in out:
                    tainted.append(f"{name}: {tool} output held answer line {s[:60]!r} before any agent wrote it")
            text = _written(tool, inp)
            if text:
                written.update(s for s in lines if s in text)
                m = FORGERY.search(text)
                if m:
                    suspect.append(f"{name}: {tool} wrote {m.group(0)!r}")
    return tainted, suspect


def scan_run(run_dir, bundles, refs):
    out = {"tainted": {}, "suspect": {}}
    tasks_dir = os.path.join(run_dir, "tasks")
    for tid in sorted(os.listdir(tasks_dir)):
        tainted, suspect = scan_task(os.path.join(tasks_dir, tid), os.path.join(bundles, tid),
                                     os.path.join(refs, f"{tid}.patch"))
        if tainted:
            out["tainted"][tid] = tainted
        if suspect:
            out["suspect"][tid] = suspect
    return out
