"""report.py -- one page per background task (coding engine spec §3.3 B: "results land as branches plus a
one-page report per ticket"), plus an index. Reads a harvested run directory; writes markdown only.

The test result shown is the harness's own: PASS/FAIL from grade["pass"], the JUnit count grade["tests"]
(passed/expected) and grade["stage"]. The pytest stdout tail (grade["summary"]) can be forged by agent code
(see grade.py check_visible), so it is shown only as untrusted text.

Agent-sourced text -- review feedback, the grade summary and dropped path names -- is written only inside
top-level backtick-fenced code blocks whose fence is longer than any backtick run in the text, with CR / CRLF
normalised to LF first; dropped names are written as Python repr(), one per line. So under CommonMark that text
cannot close its block or render as a heading, link, image or HTML. Nothing broader is claimed (a renderer that
ignores fences is out of scope). Pages are written with errors="backslashreplace", so a non-UTF-8 name
(a lone surrogate) cannot stop the run."""
import json
import os
import re

NO_RECORD = "no record (run died before recording)"


def _fenced(text):
    """`text` as a fenced code block it cannot break out of."""
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence, *body.split("\n"), fence]


def _result(g):
    if not g:
        return "not graded"
    out = "PASS" if g.get("pass") else "FAIL"
    t = g.get("tests")
    if isinstance(t, dict) and "expected" in t and "passed" in t:
        out += f", {t['passed']}/{t['expected']} expected tests passed (harness JUnit count)"
    if g.get("stage"):
        out += f", stage `{g['stage']}`"
    return out


def _minutes(rec):
    t0, t1 = rec.get("t_start"), rec.get("t_end")
    if not isinstance(t0, (int, float)) or not isinstance(t1, (int, float)):
        return "n/a"
    return f"{(t1 - t0) / 60:.1f} min"


def _page(rec, run_dir):
    tid = rec["id"]
    status = "ACCEPTED" if rec.get("accepted") else "NOT ACCEPTED"
    g = rec.get("grade")
    lines = [f"# {tid} -- {status}", "",
             f"- Outcome: `{rec.get('outcome')}`",
             f"- Result: {_result(g)}",
             f"- Rework rounds: {rec.get('rework_rounds', 0)}",
             f"- Time: {_minutes(rec)}"]
    patch = os.path.join(run_dir, "patches", f"{tid}.patch")
    lines.append(f"- Patch: `patches/{tid}.patch`" if os.path.exists(patch) else "- Patch: none")
    if g and g.get("summary"):
        lines += ["", "Grade output tail (untrusted -- agent code can forge it; the Result line above is the "
                  "harness's own count):", "", *_fenced(g["summary"])]
    if rec.get("dropped"):
        lines += ["", "Dropped paths (out of scope; agent-chosen names, shown as Python repr):", "",
                  *_fenced("\n".join(repr(p) for p in rec["dropped"]))]
    lines += ["", "## Review", ""]
    reviews = rec.get("reviews") or []
    if not reviews:
        lines.append("No review.")
    for i, rv in enumerate(reviews, start=1):
        lines += [f"Review {i} on `{rv.get('lane', 'lead')}`: **{rv.get('verdict')}** (feedback below is "
                  "untrusted agent text)", "", *_fenced(rv.get("feedback") or "(empty)"), ""]
    if rec.get("review_infra"):
        lines += ["", "## Worker failures (retried, not review rounds)", ""]
        lines += [f"- `{x.get('lane')}` rc {x.get('rc')}" for x in rec["review_infra"]]
    return "\n".join(lines) + "\n"


def _write_text(path, text):
    with open(path, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as f:
        f.write(text)


def write(run_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tasks_dir = os.path.join(run_dir, "tasks")
    recs, missing = [], []
    for tid in sorted(os.listdir(tasks_dir)):
        if not os.path.isdir(os.path.join(tasks_dir, tid)):
            continue
        p = os.path.join(tasks_dir, tid, "record.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                recs.append(json.load(f))
        else:
            missing.append(tid)
    written = []
    for rec in recs:
        path = os.path.join(out_dir, f"{rec['id']}.md")
        _write_text(path, _page(rec, run_dir))
        written.append(path)
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
        run = json.load(f)
    acc = sum(1 for r in recs if r.get("accepted"))
    n = run.get("n_tasks")
    n = n if isinstance(n, int) and not isinstance(n, bool) else len(recs)
    idx = [f"# Run report ({run.get('arm')}, {'valid' if run.get('valid') else 'INVALID'})", "",
           f"{acc} of {n} accepted in {run.get('wall_s', 0) / 60:.0f} min; "
           f"reviews per lane: {json.dumps(run.get('review_lanes', {}))}", ""]
    idx += [f"- [{r['id']}]({r['id']}.md) -- {'accepted' if r.get('accepted') else 'not accepted'} "
            f"(`{r.get('outcome')}`)" for r in recs]
    idx += [f"- `{tid}` -- {NO_RECORD}" for tid in missing]
    path = os.path.join(out_dir, "index.md")
    _write_text(path, "\n".join(idx) + "\n")
    written.append(path)
    return written
