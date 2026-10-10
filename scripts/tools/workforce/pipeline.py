"""The workforce harness pipeline (workforce spec §6). Runs on the sandbox VM.

Per task: an implementer works in the task's workspace; the lead reviews a disposable copy and
answers ACCEPT or REVISE; a REVISE resumes the SAME implementer session (at most `max_rounds`
times); after that the lead fixes it in a fresh session (`lead-fixed`). The finished workspace is
filtered to the declared paths, exported as a patch, graded from the pristine snapshot, and the
implementer's session is scored for loops.

Who owns what (on the VM each role is its own OS user; `own(path, role)` hands paths over, role None
meaning the harness): a task's workspace belongs to its implementer only while that implementer's
turn runs, to the lead only during the fix, and to the harness otherwise. Reviews get their own copy
owned by the lead. No agent code ever runs as the harness: the test runs shown to the lead go
through the grader (the network-less container on the VM), on the pristine snapshot plus the
filtered patch, exactly like the final grade.

Layout: <run>/agent/<id>/ holds what agents may touch (workspace, review copies, packets, opencode
homes); <run>/tasks/<id>/ (0700) holds the baseline git, events, check and grading trees and records.

Scheduling: one thread per implementer, one lead thread. Rework goes back to the implementer that
did the task, ahead of new tasks; while a task waits for review its implementer takes the next new
task. The lead reviews in arrival order. With review=False (W1) implementers' work is graded
directly. An implementer run that fails outright (non-zero exit, not a timeout -- e.g. its worker is
down) is retried and never counts as a round; after `retries` failures the task is an infrastructure
error and the run is invalid.
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

import context
import grade
import loops
import paths
import profiles
import review
import workspace

UNREACHABLE_LIMIT_S = 300            # spec §9: worker unreachable for 5 minutes -> run invalid
_NOISE = ("__pycache__/", ".pytest_cache/")
LEAD = "lead"


@dataclass
class Limits:
    impl_s: int = 1800
    review_s: int = 900
    verdict_s: int = 300             # the resumed tools-off verdict turn (round 2 §3.1)
    fix_s: int = 1800
    tests_s: int = 600
    max_rounds: int = 2
    retries: int = 3                 # failed (non-timeout) implementer runs retried per turn
    retry_wait_s: float = 60.0


@dataclass
class _Task:
    id: str
    bundle: str
    task: dict
    request: str
    dir: str                                         # harness-private
    adir: str                                        # agent area
    ws: object = None
    homes: dict = None                               # opencode home per role (agent area)
    agent: str = None
    session: str = None
    rounds: list = field(default_factory=list)       # implementer turns
    impl_failures: list = field(default_factory=list)
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
                 monitor=None, own=None):
        loaded = []
        for b in bundle_dirs:
            t = grade.load_task(b)
            with open(os.path.join(b, t["request"]), encoding="utf-8") as f:
                loaded.append((b, t, f.read()))
        ids = [t["id"] for _, t, _ in loaded]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids: {dupes}")
        os.makedirs(run_dir)                               # never reuse a run directory
        for sub_dir, mode in (("agent", 0o711), ("tasks", 0o700)):
            os.makedirs(os.path.join(run_dir, sub_dir))
            os.chmod(os.path.join(run_dir, sub_dir), mode)
        os.chmod(run_dir, 0o711)
        self.own = own or (lambda path, role: None)
        self.arm, self.oc, self.run_dir, self.grader = arm, opencode, run_dir, grader
        self.review, self.limits, self.monitor = review, limits or Limits(), monitor
        self.impls = profiles.implementers(arm)
        self.tasks = [_Task(t["id"], b, t, req, os.path.join(run_dir, "tasks", t["id"]),
                            os.path.join(run_dir, "agent", t["id"])) for b, t, req in loaded]
        self.cond = threading.Condition()
        self.pending = list(self.tasks)
        self.rework = {a: [] for a in self.impls}
        self.lead_q = []
        self.done = 0
        self.records, self.errors, self.infra = {}, [], []

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
            with self.cond:
                self.errors.append(f"{t.id}: {type(e).__name__}: {e}")
            try:
                os.makedirs(t.dir, exist_ok=True)
                with open(os.path.join(t.dir, "harness-error.txt"), "w", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
                self._write_record(t, "harness-error", None, {}, None)
            except Exception as e2:                        # never let error handling kill the thread
                with self.cond:
                    self.errors.append(f"{t.id}: recording the error failed: {type(e2).__name__}: {e2}")
            finally:
                self._complete(t, self._record(t, "harness-error", None, {}, None))   # no-op if done

    # ---- helpers ------------------------------------------------------------------------------
    def _run_as(self, role, workdir, fn):
        """Hand `workdir` to `role` for the duration of fn(), then take it back."""
        self.own(workdir, role)
        try:
            return fn()
        finally:
            self.own(workdir, None)

    def _filtered_patch(self, t):
        allowed, dropped = paths.classify(t.ws.changes(), t.task["files"])
        dropped = {p: why for p, why in dropped.items() if not any(x in p for x in _NOISE)}
        return t.ws.patch(allowed), dropped

    def _diff_for_reader(self, t):
        """(diff with whole-function context over ALLOWED paths only, dropped paths by name). Junk
        outside the scope never reaches the packet as text (round 2 §3.2)."""
        allowed, dropped = paths.classify(t.ws.changes(), t.task["files"])
        dropped = {p: why for p, why in dropped.items() if not any(x in p for x in _NOISE)}
        return t.ws.reader_diff(allowed), dropped

    def _visible_tests(self, t, name):
        """The visible tests on pristine snapshot + filtered patch, run by the grader (never as the
        harness, never with the agent's conftest), for the lead to read."""
        patch, _ = self._filtered_patch(t)
        return grade.check_visible(t.bundle, patch, os.path.join(t.dir, name), self.grader,
                                   self.limits.tests_s)

    # ---- steps --------------------------------------------------------------------------------
    def _implement(self, t, agent, feedback):
        if t.ws is None:
            os.makedirs(t.dir, mode=0o700, exist_ok=True)
            os.makedirs(t.adir, exist_ok=True)
            t.ws = workspace.Workspace.materialize(os.path.join(t.bundle, "snapshot.tar"),
                                                   os.path.join(t.adir, "ws"), os.path.join(t.dir, "git"))
            self.own(t.ws.root, None)
            t.homes = {}
            for role in (agent, LEAD):
                t.homes[role] = os.path.join(t.adir, f"home-{role}")
                os.makedirs(t.homes[role])
                self.own(t.homes[role], role)
            t.agent, t.t_start = agent, time.time()
        n = len(t.rounds)
        msg = review.implement_message(t.task, t.request) if feedback is None else review.revise_message(feedback)
        events = os.path.join(t.dir, f"impl-r{n}.jsonl")
        for attempt in range(self.limits.retries + 1):
            t0 = time.time()
            r = self._run_as(agent, t.ws.root, lambda: self.oc.run(
                agent, t.ws.root, msg, self.limits.impl_s, events, session=t.session,
                home=t.homes[agent], user=agent))
            if r.rc == 0 or r.timed_out:
                break
            failed = f"{events[:-6]}-fail{attempt}.jsonl"
            if os.path.exists(events):
                os.replace(events, failed)
            t.impl_failures.append({"round": n, "attempt": attempt, "rc": r.rc, "t_start": t0,
                                    "t_end": time.time(), "events": os.path.basename(failed)})
            if attempt < self.limits.retries:
                time.sleep(self.limits.retry_wait_s)
        else:
            with self.cond:
                self.infra.append(f"{t.id}: implementer runs failed {len(t.impl_failures)} times "
                                  f"(last rc {r.rc}) on {agent}")
            self._write_record(t, "infra-error", None, {}, None)
            return
        t.session = t.session or r.session_id
        t.rounds.append({"round": n, "agent": agent, "session": r.session_id, "rc": r.rc,
                         "timed_out": r.timed_out, "t_start": t0, "t_end": time.time(), "summary": r.text[-500:]})
        with open(os.path.join(t.dir, f"impl-r{n}.patch"), "wb") as f:   # the state this review sees (replay)
            f.write(self._filtered_patch(t)[0])
        if not self.review:
            self._finalize(t, "implemented")
            return
        with self.cond:
            self.lead_q.append(t)
            self.cond.notify_all()

    def _packet_for(self, t, test_out, header="", history=()):
        """(text, truncated, diff): the review/fix packet with everything the reviewer used to spend
        steps re-reading (round 2 §3.2) -- function context, call sites of changed names, the tests
        that touch the changed files, the implementer's own summary. Context comes from git blobs and
        the pristine snapshot only."""
        diff, dropped = self._diff_for_reader(t)
        allowed, _ = paths.classify(t.ws.changes(), t.task["files"])
        snapshot = os.path.join(t.bundle, "snapshot.tar")
        patched = {p: t.ws.read(p) for p in allowed}
        sites = context.call_sites(context.changed_names(diff), snapshot, patched)
        tests = context.touching_tests(list(patched), snapshot)
        summary = t.rounds[-1].get("summary", "") if t.rounds else ""
        text, truncated, _ = review.packet(t.task, t.request, diff, test_out, history=history, dropped=dropped,
                                           call_sites=sites, tests=tests, summary=summary, header=header)
        return text, truncated, diff

    def _review(self, t):
        n = len(t.reviews)
        tree = t.ws.copy_to(os.path.join(t.adir, f"review-r{n}"))
        test_out, header = self._visible_tests(t, f"review-r{n}-check")
        text, truncated, diff = self._packet_for(t, test_out, header)
        if truncated:
            os.makedirs(os.path.join(tree, os.path.dirname(review.FULL_PACKET)), exist_ok=True)
            with open(os.path.join(tree, review.FULL_PACKET), "w", encoding="utf-8") as f:
                f.write("\n".join(["# Request", t.request, "# Test output", test_out, "# Diff", diff]))
        pkt = os.path.join(t.adir, f"review-r{n}.packet.md")
        with open(pkt, "w", encoding="utf-8") as f:
            f.write(text)
        self.own(pkt, LEAD)
        t0 = time.time()
        r = self._run_as(LEAD, tree, lambda: self.oc.run(
            "reviewer", tree, "REVIEW the change described in the attached packet.", self.limits.review_s,
            os.path.join(t.dir, f"review-r{n}.jsonl"), attach=pkt, home=t.homes[LEAD], user=LEAD))
        with open(os.path.join(t.dir, f"review-r{n}.jsonl"), encoding="utf-8", errors="replace") as f:
            steps = loops.steps_from_events(f.read())
        verdict, feedback = review.parse_verdict(r.text)
        entry = {"round": n, "verdict": verdict, "feedback": feedback[:2000], "session": r.session_id,
                 "timed_out": r.timed_out, "steps": steps, "capped": steps >= profiles.REVIEW_STEPS,
                 "t_start": t0, "t_end": time.time(), "verdict_turn": None}
        if verdict == "NONE" and r.session_id:
            # Round 2 §3.1: opencode's last-step banner tells the model to summarize instead of decide.
            # Resume the SAME session for one tools-off turn that may only answer; a second no-verdict
            # falls through to rework / the lead fix exactly as before. Never graded unreviewed.
            t1 = time.time()
            v = self._run_as(LEAD, tree, lambda: self.oc.run(
                "reviewer-verdict", tree, profiles.VERDICT_MESSAGE, self.limits.verdict_s,
                os.path.join(t.dir, f"review-r{n}-verdict.jsonl"), session=r.session_id,
                home=t.homes[LEAD], user=LEAD))
            verdict, feedback = review.parse_verdict(v.text)
            entry["verdict_turn"] = {"verdict": verdict, "rc": v.rc, "timed_out": v.timed_out,
                                     "t_start": t1, "t_end": time.time()}
            entry["verdict"], entry["feedback"] = verdict, feedback[:2000]
        shutil.rmtree(tree, ignore_errors=True)            # whatever the reviewer did there is discarded
        t.reviews.append(entry)
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
        test_out, header = self._visible_tests(t, "fix-check")
        text, _, _ = self._packet_for(t, test_out, header, history=t.history)
        pkt = os.path.join(t.adir, "fix.packet.md")
        with open(pkt, "w", encoding="utf-8") as f:
            f.write(text)
        self.own(pkt, LEAD)
        t0 = time.time()
        r = self._run_as(LEAD, t.ws.root, lambda: self.oc.run(
            "fixer", t.ws.root, "FIX: finish the request; the attached packet has the details.",
            self.limits.fix_s, os.path.join(t.dir, "fix.jsonl"), attach=pkt, home=t.homes[LEAD], user=LEAD))
        t.fix = {"session": r.session_id, "rc": r.rc, "timed_out": r.timed_out, "t_start": t0,
                 "t_end": time.time()}
        self._finalize(t, "lead-fixed")

    def _finalize(self, t, outcome):
        patch, dropped = self._filtered_patch(t)
        os.makedirs(os.path.join(self.run_dir, "patches"), exist_ok=True)
        with open(os.path.join(self.run_dir, "patches", f"{t.id}.patch"), "wb") as f:
            f.write(patch)
        g = grade.grade(t.bundle, patch, os.path.join(t.dir, "grade"), self.grader)
        loop = self._score_loops(t)
        for name, session, role in (("impl", t.session, t.agent), ("fix", (t.fix or {}).get("session"), LEAD)):
            self._archive_export(t, name, session, role)
        self._write_record(t, outcome, g, dropped, loop)

    def _score_loops(self, t):
        """Loops over all implementer rounds, from the events this harness captured itself."""
        calls, steps = [], []
        for n in range(len(t.rounds)):
            with open(os.path.join(t.dir, f"impl-r{n}.jsonl"), encoding="utf-8", errors="replace") as f:
                text = f.read()
            calls += loops.tool_calls_from_events(text)
            steps.append(loops.steps_from_events(text))
        s = loops.summarize_calls(calls)
        s["steps_per_turn"] = steps
        return s

    def _archive_export(self, t, name, session, role):
        """Keep `opencode export` for reading later; it is agent-writable, so nothing is scored from it."""
        if not session:
            return
        try:
            doc = self.oc.export(session, home=t.homes[role])
        except Exception as e:                             # an export failure is recorded, not fatal
            doc = {"export_error": f"{type(e).__name__}: {e}"}
        with open(os.path.join(t.dir, f"export-{name}.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def _record(self, t, outcome, g, dropped, loop):
        accepted = bool(g and g["pass"] and outcome in ("implementer-accepted", "lead-fixed", "implemented"))
        return {"id": t.id, "arm": self.arm, "implementer": t.agent, "outcome": outcome, "accepted": accepted,
                "grade": g, "dropped": dropped, "loops": loop, "rounds": t.rounds, "reviews": t.reviews,
                "impl_failures": t.impl_failures, "rework_rounds": max(0, len(t.rounds) - 1), "fix": t.fix,
                "time_cap_hits": sum(1 for r in t.rounds if r["timed_out"]) + int(bool(t.fix and t.fix["timed_out"])),
                "t_start": t.t_start, "t_end": time.time()}

    def _write_record(self, t, outcome, g, dropped, loop):
        rec = self._record(t, outcome, g, dropped, loop)
        os.makedirs(t.dir, exist_ok=True)
        with open(os.path.join(t.dir, "record.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        self._complete(t, rec)

    def _complete(self, t, rec):
        """Count a task as finished exactly once; the run ends when every task is finished."""
        with self.cond:
            if t.id in self.records:
                return
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
        invalid += [f"infrastructure: {e}" for e in self.infra]
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
                   "review_capped": sum(1 for r in recs for rv in r["reviews"] if rv.get("capped")),
                   "review_timed_out": sum(1 for r in recs for rv in r["reviews"] if rv.get("timed_out")),
                   "verdict_turns": sum(1 for r in recs for rv in r["reviews"] if rv.get("verdict_turn")),
                   "review_none_after_turn": sum(1 for r in recs for rv in r["reviews"]
                                                 if rv.get("verdict_turn") and rv.get("verdict") == "NONE"),
                   "health": health, "valid": not invalid, "invalid_reasons": invalid}
        with open(os.path.join(self.run_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
        return summary
