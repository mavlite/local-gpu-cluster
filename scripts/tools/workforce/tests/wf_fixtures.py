"""Shared test fixtures: tiny task bundles and a scripted stand-in for opencode."""
import io
import json
import os
import sys
import tarfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import oc  # noqa: E402


def stub_src():
    return "def f(x):\n    return 0\n"


def solution_src():
    return "def f(x):\n    return x * 2\n"


def test_src(tid):
    return (f"import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
            f"from {tid} import f\n\ndef test_f():\n    assert f(2) == 4\n")


def hidden_src(tid):
    return (f"import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
            f"from {tid} import f\n\ndef test_f_hidden():\n    assert f(-3) == -6\n")


def make_bundle(root, tid, extra=None):
    """A bundle whose task is: make pkg/<tid>.py's f() double its argument. `extra`: more snapshot
    files, {path: text}."""
    d = os.path.join(root, tid)
    os.makedirs(os.path.join(d, "hidden"))
    files = {f"pkg/{tid}.py": stub_src(), f"pkg/tests/test_{tid}.py": test_src(tid), "README.md": "repo\n",
             **(extra or {})}
    with tarfile.open(os.path.join(d, "snapshot.tar"), "w") as t:
        for name, data in sorted(files.items()):
            raw = data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            t.addfile(info, io.BytesIO(raw))
    with open(os.path.join(d, "hidden", f"test_{tid}_hidden.py"), "w") as f:
        f.write(hidden_src(tid))
    with open(os.path.join(d, "request.md"), "w") as f:
        f.write(f"f() in pkg/{tid}.py should double its argument.\n")
    task = {"id": tid, "commit": "c" * 40, "parent": "p" * 40, "request": "request.md",
            "files": [f"pkg/{tid}.py"], "tests": [f"pkg/tests/test_{tid}.py"],
            "hidden": {f"test_{tid}_hidden.py": f"pkg/tests/test_{tid}_hidden.py"},
            "needs_conftest": False, "timeout_s": 120}
    with open(os.path.join(d, "task.json"), "w") as f:
        json.dump(task, f)
    return d


def _task_of(workdir):
    parts = os.path.normpath(workdir).split(os.sep)
    return parts[parts.index("agent") + 1]


class FakeOpencode:
    """Scripted agents. script[task_id] = {
         "impl": [ {path: content, ...} per implementer turn ],
         "review": ["ACCEPT" | "REVISE: ..." | other text | {"text", "steps", "timed_out"}, ... per review],
         "verdict": [text, ... per verdict turn]   (the resumed reviewer-verdict agent),
         "fix": {path: content},
         "raise_on": "impl"|"review" (optional), "tool_calls": [(tool, input), ...] (export)}"""

    def __init__(self, script):
        self.script, self.calls, self.lock = script, [], threading.Lock()
        self.turns, self.sessions = {}, {}
        self.owners = None                         # set to a dict to record path ownership at call time

    def run(self, agent, workdir, message, timeout_s, events_path, session=None, attach=None, home=None,
            user=None):
        tid = _task_of(workdir)
        s = self.script[tid]
        role = {"reviewer": "review", "reviewer-verdict": "verdict", "fixer": "fix"}.get(agent, "impl")
        with self.lock:
            n = self.turns.get((tid, role), 0)
            self.turns[(tid, role)] = n + 1
            sid = session or f"ses_{tid}_{agent}_{n}"
            self.sessions[sid] = tid
            self.calls.append({"task": tid, "agent": agent, "role": role, "session": sid,
                               "resumed": session is not None, "message": message, "attach": attach,
                               "workdir": workdir, "home": home, "user": user,
                               "workdir_existed": os.path.isdir(workdir),
                               "owner": self.owners.get(workdir) if self.owners is not None else None})
        if s.get("raise_on") == role:
            raise RuntimeError(f"scripted failure in {role}")
        with open(events_path, "w") as f:                   # the harness-captured --format json stream
            if role == "impl" and n == 0:
                for tool, inp in s.get("tool_calls", []):
                    f.write(json.dumps({"type": "step_start", "sessionID": sid}) + "\n")
                    f.write(json.dumps({"type": "tool_use", "sessionID": sid, "part": {
                        "type": "tool", "tool": tool, "state": {"status": "completed", "input": inp}}}) + "\n")
        if role == "impl" and n < s.get("fail_impl_turns", 0):
            return oc.RunResult(1, False, None, "", 0.01)          # opencode died: no session, rc 1
        if role == "verdict":
            return oc.RunResult(0, False, sid, s["verdict"][n], 0.01)
        if role == "review":
            entry = s["review"][n]
            if isinstance(entry, dict):                      # a scripted step count / timeout
                with open(events_path, "w") as f:
                    for _ in range(entry.get("steps", 1)):
                        f.write(json.dumps({"type": "step_start", "sessionID": sid}) + "\n")
                return oc.RunResult(0, entry.get("timed_out", False), sid, entry["text"], 0.01)
            text = entry
            if s.get("expect_full_packet"):
                fp = os.path.join(workdir, ".workforce_review", "packet.md")
                s.setdefault("full_packet_seen", []).append(os.path.isfile(fp))
                s.setdefault("full_packet_text", []).append(open(fp, encoding="utf-8").read() if os.path.isfile(fp) else None)
            with open(os.path.join(workdir, "reviewer_scribble.txt"), "w") as f:
                f.write("reviewers can run bash; this must not reach the workspace\n")
            return oc.RunResult(0, False, sid, text, 0.01)
        if role == "fix":
            fp = os.path.join(workdir, ".workforce_review", "packet.md")
            self.calls[-1]["full_packet_present"] = os.path.isfile(fp)
            self.calls[-1]["full_packet_text"] = open(fp, encoding="utf-8").read() if os.path.isfile(fp) else ""
        files = s["fix"] if role == "fix" else s["impl"][n]
        for path, raw in (s.get("binary", {}) if role == "impl" else {}).items():
            with open(os.path.join(workdir, path), "wb") as f:
                f.write(raw)
        for path, content in files.items():
            p = os.path.join(workdir, path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
        return oc.RunResult(0, False, sid, "SUMMARY: done", 0.01)

    def export(self, session_id, timeout=120, home=None):
        # The session DB is agent-writable on the VM: model one the agent has scrubbed clean.
        return {"info": {"id": session_id}, "messages": [{"info": {"role": "user"}, "parts": []}]}
