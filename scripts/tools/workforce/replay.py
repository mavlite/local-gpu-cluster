"""Offline reviewer replay (round 2 §5.1): re-run ONLY the reviewer (and its verdict turn) on the
review packets of a harvested run, rebuilt with the current packet builder from the bundle snapshot
plus the patch the reviewer saw. Nothing is implemented, fixed or graded; the output is a gate for
the reviewer changes before a measurement window costs a day of cluster time.

A round is replayable when its reviewed state is on disk: `tasks/<id>/impl-r{k}.patch` (the round-2
harness stores one per implementer round) or, for the LAST review of a task the implementer got
accepted on, `patches/<id>.patch`. A lead-fixed task's final patch is the fixer's state, not the
reviewed one, so its rounds are skipped unless stored per round.

Prompt tokens come from a llama-server journal over the replay, when given. The journal clock is
relative to the server start and the harness never learns llama's task ids, so tokens are reported as
run totals (every request in a replay journal is a reviewer request, bar the user probe's tiny
prompts), never attributed to a round: each round's `prompt_tokens` is null.
"""
import json
import os
import statistics
import sys
import time

import grade
import pipeline
import workspace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gate"))
import llama_log  # noqa: E402

MIN_PROMPT_N = 64                # below this a journal request is the user probe, not the reviewer
_OLD_DIFF_HEAD = "\n# Diff\n"      # the round-1 packet's last section
_OLD_TRUNCATED = "[packet truncated at "


def recover_patches(run_dir):
    """Round-1 runs stored no per-round patch, but the lead's packet for each review survives in
    agent/<id>/review-r{k}.packet.md with the exact diff it reviewed as its last section. Write that
    diff to tasks/<id>/impl-r{k}.patch (never over an existing one) so every round replays; a cut
    packet (its diff ends in the pointer line) is skipped. {"recovered", "kept", "skipped"}."""
    out = {"recovered": [], "kept": [], "skipped": []}
    for tid, rec in _records(run_dir).items():
        for k in range(len(rec.get("reviews", []))):
            dest = os.path.join(run_dir, "tasks", tid, f"impl-r{k}.patch")
            if os.path.isfile(dest):
                out["kept"].append({"task": tid, "round": k})
                continue
            src = os.path.join(run_dir, "agent", tid, f"review-r{k}.packet.md")
            if not os.path.isfile(src):
                out["skipped"].append({"task": tid, "round": k, "why": "no packet"})
                continue
            with open(src, encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
            i = text.find(_OLD_DIFF_HEAD)
            if i < 0:
                out["skipped"].append({"task": tid, "round": k, "why": "no diff section"})
                continue
            diff = text[i + len(_OLD_DIFF_HEAD):].rstrip("\n")
            if diff.rsplit("\n", 1)[-1].startswith(_OLD_TRUNCATED):
                out["skipped"].append({"task": tid, "round": k, "why": "packet truncated"})
                continue
            with open(dest, "wb") as f:
                f.write((diff + "\n").encode("utf-8"))
            out["recovered"].append({"task": tid, "round": k})
    return out


class _ReplayPipeline(pipeline.Pipeline):
    """A Pipeline whose review rounds end in a result instead of a grade or a lead fix."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.outcomes = {}

    def _finalize(self, t, outcome):
        self.outcomes[t.id] = outcome

    def _lead_fix(self, t):
        self.outcomes[t.id] = "not-accepted"


def _records(run_dir):
    tasks = os.path.join(run_dir, "tasks")
    out = {}
    for tid in sorted(os.listdir(tasks)):
        path = os.path.join(tasks, tid, "record.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                out[tid] = json.load(f)
    return out


def _bundles(bundles_root, ids):
    found = {}
    for name in sorted(os.listdir(bundles_root)):
        b = os.path.join(bundles_root, name)
        if os.path.isfile(os.path.join(b, "task.json")):
            tid = grade.load_task(b)["id"]
            if tid in ids:
                found[tid] = b
    return found


def _stored_patch(run_dir, rec, k):
    """Bytes of the state review k saw, or None when the run did not keep it."""
    per_round = os.path.join(run_dir, "tasks", rec["id"], f"impl-r{k}.patch")
    if os.path.isfile(per_round):
        with open(per_round, "rb") as f:
            return f.read()
    final = os.path.join(run_dir, "patches", f"{rec['id']}.patch")
    if (k == len(rec["reviews"]) - 1 and rec.get("outcome") == "implementer-accepted"
            and os.path.isfile(final)):
        with open(final, "rb") as f:
            return f.read()
    return None


def _journal_totals(journal_text):
    if journal_text is None:
        return {"prompt_tokens_total": None, "requests": None, "median_prompt_tokens": None}
    recs = llama_log.parse(journal_text)
    big = [r["prompt_n"] for r in recs if r["prompt_n"] >= MIN_PROMPT_N]
    return {"prompt_tokens_total": sum(r["prompt_n"] for r in recs), "requests": len(big),
            "median_prompt_tokens": statistics.median(big) if big else None}


def _plan(run_dir, bundles_root):
    """(rounds to replay as (rec, k, bundle, patch), skipped rounds, bundle dirs in use)."""
    records = _records(run_dir)
    bundles = _bundles(bundles_root, set(records))
    todo, skipped = [], []
    for tid, rec in records.items():
        for k in range(len(rec.get("reviews", []))):
            patch = _stored_patch(run_dir, rec, k) if tid in bundles else None
            if patch is None:
                skipped.append({"task": tid, "round": k})
            else:
                todo.append((rec, k, bundles[tid], patch))
    return todo, skipped, sorted({b for _, _, b, _ in todo})


def _round_task(p, rec, k, bundle, patch, out_dir):
    """A _Task positioned exactly where review k of the harvested task happened."""
    base = next(t for t in p.tasks if t.id == rec["id"])
    t = pipeline._Task(rec["id"], bundle, base.task, base.request,
                       os.path.join(out_dir, "tasks", rec["id"], f"r{k}"),
                       os.path.join(out_dir, "agent", rec["id"], f"r{k}"))
    os.makedirs(t.dir, mode=0o700)
    os.makedirs(t.adir)
    t.ws = workspace.Workspace.materialize(os.path.join(bundle, "snapshot.tar"), os.path.join(t.adir, "ws"),
                                           os.path.join(t.dir, "git"))
    if patch.strip():                               # an empty diff is a round where nothing was changed
        workspace.apply_patch(t.ws.root, patch)
    p.own(t.ws.root, None)
    t.homes = {pipeline.LEAD: os.path.join(t.adir, f"home-{pipeline.LEAD}")}
    os.makedirs(t.homes[pipeline.LEAD])
    p.own(t.homes[pipeline.LEAD], pipeline.LEAD)
    t.agent, t.t_start = rec.get("implementer"), time.time()
    rounds = rec.get("rounds") or []
    t.rounds = [{"summary": (rounds[k] if k < len(rounds) else {}).get("summary", "")}]
    return t


def replay(run_dir, bundles_root, opencode, out_dir, grader, limits=None, journal_text=None, own=None):
    todo, skipped, bundle_dirs = _plan(run_dir, bundles_root)
    lim = limits or pipeline.Limits()
    lim.max_rounds = 0                                 # every non-ACCEPT ends the round: no rework here
    p = _ReplayPipeline("G", bundle_dirs, opencode, out_dir, grader, review=True, limits=lim, own=own)
    rounds = []
    for rec, k, bundle, patch in todo:
        t = _round_task(p, rec, k, bundle, patch, out_dir)
        p._review(t)
        e = t.reviews[0]
        rounds.append({"task": rec["id"], "round": k, "old_verdict": rec["reviews"][k].get("verdict"),
                       "new_verdict": e["verdict"], "capped": e["capped"], "steps": e["steps"],
                       "timed_out": e["timed_out"], "verdict_turn": e["verdict_turn"],
                       "feedback": e["feedback"][:500], "prompt_tokens": None,
                       "t_start": e["t_start"], "t_end": time.time()})
    judged = [r for r in rounds if r["old_verdict"] != "NONE"]
    totals = {"n_rounds": len(rounds), "n_skipped": len(skipped),
              "old_none": sum(1 for r in rounds if r["old_verdict"] == "NONE"),
              "new_none_after_turn": sum(1 for r in rounds if r["new_verdict"] == "NONE"),
              "capped": sum(1 for r in rounds if r["capped"]),
              "verdict_turns": sum(1 for r in rounds if r["verdict_turn"]),
              "agreement": (sum(1 for r in judged if r["new_verdict"] == r["old_verdict"]) / len(judged)
                            if judged else None),
              "median_steps": statistics.median([r["steps"] for r in rounds]) if rounds else None,
              **_journal_totals(journal_text)}
    out = {"run": os.path.abspath(run_dir), "rounds": rounds, "skipped": skipped, "totals": totals}
    with open(os.path.join(out_dir, "replay.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    return out
