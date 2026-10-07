"""The workforce harness pipeline (workforce spec §6). Runs on the sandbox VM.

Per task: an implementer works in the task's workspace; the lead reviews a disposable copy and
answers ACCEPT or REVISE; a REVISE resumes the SAME implementer session (at most `max_rounds`
times); after that the lead fixes it in a fresh session (`lead-fixed`). The finished workspace is
filtered to the declared paths, exported as a patch, graded from the pristine snapshot, and the
implementer's session is scored for loops.

Layout: <run>/agent/<id>/ holds what agents may touch (workspace, review copies, packets) and is
handed to the agent user with `give`; <run>/tasks/<id>/ (0700) holds the baseline git, events,
grading tree and records, which the agent user cannot read.

Scheduling: one thread per implementer, one lead thread. Rework goes back to the implementer that
did the task, ahead of new tasks; while a task waits for review its implementer takes the next new
task. The lead reviews in arrival order. With review=False (W1) implementers' work is graded
directly.
"""
import json
import os
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import grade
import loops
import paths
import profiles
import review
import workspace

UNREACHABLE_LIMIT_S = 300            # spec §9: worker unreachable for 5 minutes -> run invalid
_NOISE = ("__pycache__/", ".pytest_cache/")


@dataclass
class Limits:
    impl_s: int = 1800
    review_s: int = 900
    fix_s: int = 1800
    tests_s: int = 600
    max_rounds: int = 2


@dataclass
class _Task:
    id: str
    bundle: str
    task: dict
    request: str
    dir: str                                         # harness-private
    adir: str                                        # agent area
    ws: object = None
    agent: str = None
    session: str = None
    rounds: list = field(default_factory=list)       # implementer turns
    reviews: list = field(default_factory=list)
    history: list = field(default_factory=list)      # feedback lines for the fixer
    fix: dict = None
    t_start: float = None


class HealthMonitor:
    """Polls each worker's /health; reports the longest unreachable stretch per URL."""

    def __init__(self, urls, interval=30.0, timeout=5.0, probe=None, clock=time.monotonic):
        self.urls, self.interval, self.clock = list(urls), interval, clock
        self.probe = probe or self._probe
        self.timeout = timeout
        self._stop = threading.Event()
        self._down_since, self.max_down = {}, {u: 0.0 for u in self.urls}
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _probe(self, url):
        try:
            with urllib.request.urlopen(url.rstrip("/").removesuffix("/v1") + "/health", timeout=self.timeout):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def poll_once(self):
        now = self.clock()
        for u in self.urls:
            if self.probe(u):
                self._down_since.pop(u, None)
            else:
                since = self._down_since.setdefault(u, now)
                self.max_down[u] = max(self.max_down[u], now - since)

    def _loop(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        self._t.join()
        self.poll_once()
        return {"max_unreachable_s": dict(self.max_down)}


class Pipeline:
    def __init__(self, arm, bundle_dirs, opencode, run_dir, grader, review=True, limits=None,
                 monitor=None, check_runner=None, give=None):
        os.makedirs(run_dir)                               # never reuse a run directory
        for sub_dir, mode in (("agent", 0o711), ("tasks", 0o700)):
            os.makedirs(os.path.join(run_dir, sub_dir))
            os.chmod(os.path.join(run_dir, sub_dir), mode)
        os.chmod(run_dir, 0o711)
        self.give = give or (lambda path: None)
        self.arm, self.oc, self.run_dir, self.grader = arm, opencode, run_dir, grader
        self.review, self.limits, self.monitor = review, limits or Limits(), monitor
        self.check = check_runner or grade.LocalRunner()
        self.impls = profiles.implementers(arm)
        self.tasks = []
        for b in bundle_dirs:
            t = grade.load_task(b)
            with open(os.path.join(b, t["request"]), encoding="utf-8") as f:
                req = f.read()
            self.tasks.append(_Task(t["id"], b, t, req, os.path.join(run_dir, "tasks", t["id"]),
                                    os.path.join(run_dir, "agent", t["id"])))
        self.cond = threading.Condition()
        self.pending = list(self.tasks)
        self.rework = {a: [] for a in self.impls}
        self.lead_q = []
        self.done = 0
        self.records, self.errors = {}, []

    # ---- scheduling ---------------------------------------------------------------------------
    def _finished(self):
        return self.done == len(self.tasks)

    def _impl_loop(self, agent):
        while True:
            with self.cond:
                while not (self.rework[agent] or self.pending or self._finished()):
                    self.cond.wait()
                if self.rework[agent]:
                    t, feedback = self.rework[agent].pop(0)
                elif self.pending:
                    t, feedback = self.pending.pop(0), None
                else:
                    return
            self._guarded(t, self._implement, t, agent, feedback)

    def _lead_loop(self):
        while True:
            with self.cond:
                while not (self.lead_q or self._finished()):
                    self.cond.wait()
                if not self.lead_q:
                    return
                t = self.lead_q.pop(0)
            self._guarded(t, self._review, t)

    def _guarded(self, t, fn, *args):
        try:
            fn(*args)
        except Exception as e:                             # spec §9: a harness exception invalidates
            self.errors.append(f"{t.id}: {type(e).__name__}: {e}")
            os.makedirs(t.dir, exist_ok=True)
            with open(os.path.join(t.dir, "harness-error.txt"), "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self._write_record(t, "harness-error", None, {}, None)

    # ---- steps --------------------------------------------------------------------------------
    def _implement(self, t, agent, feedback):
        if t.ws is None:
            os.makedirs(t.dir, mode=0o700, exist_ok=True)
            os.makedirs(t.adir, exist_ok=True)
            t.ws = workspace.Workspace.materialize(os.path.join(t.bundle, "snapshot.tar"),
                                                   os.path.join(t.adir, "ws"), os.path.join(t.dir, "git"))
            self.give(t.ws.root)
            t.agent, t.t_start = agent, time.time()
        n = len(t.rounds)
        msg = review.implement_message(t.task, t.request) if feedback is None else review.revise_message(feedback)
        t0 = time.time()
        r = self.oc.run(agent, t.ws.root, msg, self.limits.impl_s, os.path.join(t.dir, f"impl-r{n}.jsonl"),
                        session=t.session)
        t.session = t.session or r.session_id
        t.rounds.append({"round": n, "agent": agent, "session": r.session_id, "rc": r.rc,
                         "timed_out": r.timed_out, "t_start": t0, "t_end": time.time(), "summary": r.text[-500:]})
        if not self.review:
            self._finalize(t, "implemented")
            return
        with self.cond:
            self.lead_q.append(t)
            self.cond.notify_all()

    def _diff_for_reader(self, t):
        changed = [c[2] for c in t.ws.changes() if not any(x in c[2] for x in _NOISE)]
        return t.ws.patch(changed)

    def _visible_tests(self, t, tree):
        """The task's visible tests (pristine copies) run against the current change, for the lead."""
        grade.restore_visible_tests(t.bundle, t.task, tree)
        _rc, out, timed_out = self.check.run(tree, ["-m", "pytest", "-q", "-p", "no:cacheprovider",
                                                    *t.task["tests"]], self.limits.tests_s)
        if timed_out:
            return f"(tests did not finish within {self.limits.tests_s} s)"
        return "\n".join(out.strip().splitlines()[-200:])

    def _review(self, t):
        n = len(t.reviews)
        tree = t.ws.copy_to(os.path.join(t.adir, f"review-r{n}"))
        test_out = self._visible_tests(t, tree)
        diff = self._diff_for_reader(t)
        text, truncated = review.packet(t.task, t.request, diff, test_out)
        if truncated:
            os.makedirs(os.path.join(tree, os.path.dirname(review.FULL_PACKET)), exist_ok=True)
            with open(os.path.join(tree, review.FULL_PACKET), "w", encoding="utf-8") as f:
                f.write("\n".join(["# Request", t.request, "# Test output", test_out, "# Diff", diff]))
        pkt = os.path.join(t.adir, f"review-r{n}.packet.md")
        with open(pkt, "w", encoding="utf-8") as f:
            f.write(text)
        self.give(tree)
        self.give(pkt)
        t0 = time.time()
        r = self.oc.run("reviewer", tree, "REVIEW the change described in the attached packet.",
                        self.limits.review_s, os.path.join(t.dir, f"review-r{n}.jsonl"), attach=pkt)
        shutil.rmtree(tree, ignore_errors=True)            # whatever the reviewer did there is discarded
        verdict, feedback = review.parse_verdict(r.text)
        t.reviews.append({"round": n, "verdict": verdict, "feedback": feedback[:2000], "session": r.session_id,
                          "timed_out": r.timed_out, "t_start": t0, "t_end": time.time()})
        if verdict == "ACCEPT":
            self._finalize(t, "implementer-accepted")
            return
        t.history.append(f"Review {n + 1}: " + (f"REVISE: {feedback}" if verdict == "REVISE"
                                                 else f"(no verdict) {feedback[:500]}"))
        if len(t.rounds) <= self.limits.max_rounds:
            fb = feedback if verdict == "REVISE" else "The reviewer gave no clear verdict; re-check the request."
            with self.cond:
                self.rework[t.agent].append((t, fb))
                self.cond.notify_all()
            return
        self._lead_fix(t)

    def _lead_fix(self, t):
        tree = t.ws.copy_to(os.path.join(t.dir, "fix-probe"))      # harness-only: runs the tests
        test_out = self._visible_tests(t, tree)
        shutil.rmtree(tree, ignore_errors=True)
        text, _ = review.packet(t.task, t.request, self._diff_for_reader(t), test_out, history=t.history)
        pkt = os.path.join(t.adir, "fix.packet.md")
        with open(pkt, "w", encoding="utf-8") as f:
            f.write(text)
        self.give(pkt)
        t0 = time.time()
        r = self.oc.run("fixer", t.ws.root, "FIX: finish the request; the attached packet has the details.",
                        self.limits.fix_s, os.path.join(t.dir, "fix.jsonl"), attach=pkt)
        t.fix = {"session": r.session_id, "timed_out": r.timed_out, "t_start": t0, "t_end": time.time()}
        self._finalize(t, "lead-fixed")

    def _finalize(self, t, outcome):
        changes = t.ws.changes()
        allowed, dropped = paths.classify(changes, t.task["files"])
        dropped = {p: why for p, why in dropped.items() if not any(x in p for x in _NOISE)}
        patch = t.ws.patch(allowed)
        os.makedirs(os.path.join(self.run_dir, "patches"), exist_ok=True)
        with open(os.path.join(self.run_dir, "patches", f"{t.id}.patch"), "w", encoding="utf-8") as f:
            f.write(patch)
        g = grade.grade(t.bundle, patch, os.path.join(t.dir, "grade"), self.grader)
        loop = self._loops(t, t.session, "impl")
        if t.fix and t.fix.get("session"):
            self._loops(t, t.fix["session"], "fix")
        self._write_record(t, outcome, g, dropped, loop)

    def _loops(self, t, session, name):
        if not session:
            return None
        try:
            doc = self.oc.export(session)
        except Exception as e:                             # an export failure is recorded, not fatal
            return {"error": f"{type(e).__name__}: {e}"}
        with open(os.path.join(t.dir, f"export-{name}.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        s = loops.summarize(doc)
        s["steps_per_turn"] = loops.steps_per_turn(doc)
        return s

    def _write_record(self, t, outcome, g, dropped, loop):
        accepted = bool(g and g["pass"] and outcome in ("implementer-accepted", "lead-fixed", "implemented"))
        rec = {"id": t.id, "arm": self.arm, "implementer": t.agent, "outcome": outcome, "accepted": accepted,
               "grade": g, "dropped": dropped, "loops": loop, "rounds": t.rounds, "reviews": t.reviews,
               "rework_rounds": max(0, len(t.rounds) - 1), "fix": t.fix,
               "time_cap_hits": sum(1 for r in t.rounds if r["timed_out"]) + int(bool(t.fix and t.fix["timed_out"])),
               "t_start": t.t_start, "t_end": time.time()}
        os.makedirs(t.dir, exist_ok=True)
        with open(os.path.join(t.dir, "record.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        with self.cond:
            self.records[t.id] = rec
            self.done += 1
            self.cond.notify_all()

    # ---- run ----------------------------------------------------------------------------------
    def run(self):
        t0 = time.time()
        if self.monitor:
            self.monitor.start()
        threads = [threading.Thread(target=self._impl_loop, args=(a,), daemon=True) for a in self.impls]
        if self.review:
            threads.append(threading.Thread(target=self._lead_loop, daemon=True))
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        t1 = time.time()
        health = self.monitor.stop() if self.monitor else {"max_unreachable_s": {}}
        recs = [self.records[t.id] for t in self.tasks]
        invalid = [f"harness exception: {e}" for e in self.errors]
        invalid += [f"worker {u} unreachable for {s:.0f} s" for u, s in health["max_unreachable_s"].items()
                    if s >= UNREACHABLE_LIMIT_S]
        impl_acc = sum(1 for r in recs if r["accepted"] and r["outcome"] == "implementer-accepted")
        hours = max(t1 - t0, 1e-9) / 3600
        summary = {"arm": self.arm, "review": self.review, "n_tasks": len(recs), "t_start": t0, "t_end": t1,
                   "wall_s": t1 - t0, "accepted": sum(1 for r in recs if r["accepted"]),
                   "implementer_accepted": impl_acc,
                   "lead_fixed": sum(1 for r in recs if r["accepted"] and r["outcome"] == "lead-fixed"),
                   "accepted_per_hour": (impl_acc if self.review else
                                         sum(1 for r in recs if r["accepted"])) / hours,
                   "accepted_by_task": {r["id"]: r["accepted"] for r in recs},
                   "outcomes": {o: sum(1 for r in recs if r["outcome"] == o) for o in {r["outcome"] for r in recs}},
                   "rework_rounds": sum(r["rework_rounds"] for r in recs),
                   "looped": sum(1 for r in recs if (r["loops"] or {}).get("looped")),
                   "time_cap_hits": sum(r["time_cap_hits"] for r in recs),
                   "health": health, "valid": not invalid, "invalid_reasons": invalid}
        with open(os.path.join(self.run_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
        return summary
