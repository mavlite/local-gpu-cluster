"""report.py -- one page per background task (coding engine spec §3.3 B: "results land as branches plus a
one-page report per ticket"), plus an index. Reads a harvested run directory; writes markdown only.
Agent-written text (review feedback) is quoted line by line so it cannot add headings or close blocks."""
import json
import os


def _quote(text):
    return "\n".join("> " + line for line in (text or "").splitlines()) or "> (empty)"


def _page(rec, run_dir):
    tid = rec["id"]
    status = "ACCEPTED" if rec.get("accepted") else "NOT ACCEPTED"
    g = rec.get("grade")
    tests = (g.get("summary") or ("pass" if g.get("pass") else "fail")) if g else "not graded"
    lines = [f"# {tid} -- {status}", "",
             f"- Outcome: `{rec.get('outcome')}`",
             f"- Hidden + visible tests: {tests}",
             f"- Rework rounds: {rec.get('rework_rounds', 0)}",
             f"- Time: {((rec.get('t_end') or 0) - (rec.get('t_start') or 0)) / 60:.1f} min"]
    patch = os.path.join(run_dir, "patches", f"{tid}.patch")
    lines.append(f"- Patch: `patches/{tid}.patch`" if os.path.exists(patch) else "- Patch: none")
    if rec.get("dropped"):
        lines.append(f"- Dropped paths (out of scope): {', '.join(f'`{p}`' for p in rec['dropped'])}")
    lines += ["", "## Review", ""]
    reviews = rec.get("reviews") or []
    if not reviews:
        lines.append("No review.")
    for i, rv in enumerate(reviews, start=1):
        lines += [f"Review {i} on `{rv.get('lane', 'lead')}`: **{rv.get('verdict')}**", "", _quote(rv.get("feedback")), ""]
    if rec.get("review_infra"):
        lines += ["", "## Worker failures (retried, not review rounds)", ""]
        lines += [f"- `{x.get('lane')}` rc {x.get('rc')}" for x in rec["review_infra"]]
    return "\n".join(lines) + "\n"


def write(run_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tasks_dir = os.path.join(run_dir, "tasks")
    recs = []
    for tid in sorted(os.listdir(tasks_dir)):
        p = os.path.join(tasks_dir, tid, "record.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                recs.append(json.load(f))
    written = []
    for rec in recs:
        path = os.path.join(out_dir, f"{rec['id']}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_page(rec, run_dir))
        written.append(path)
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
        run = json.load(f)
    acc = sum(1 for r in recs if r.get("accepted"))
    idx = [f"# Run report ({run.get('arm')}, {'valid' if run.get('valid') else 'INVALID'})", "",
           f"{acc} of {len(recs)} accepted in {run.get('wall_s', 0) / 60:.0f} min; "
           f"reviews per lane: {json.dumps(run.get('review_lanes', {}))}", ""]
    idx += [f"- [{r['id']}]({r['id']}.md) -- {'accepted' if r.get('accepted') else 'not accepted'} "
            f"(`{r.get('outcome')}`)" for r in recs]
    path = os.path.join(out_dir, "index.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(idx) + "\n")
    written.append(path)
    return written
