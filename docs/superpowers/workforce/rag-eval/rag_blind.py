"""Build blind grading batches for the RAG comparison, and score the graders' output.

  build <frozen.json> <answersA.json> <answersB.json> <outdir> [batches]
        writes outdir/batch-<k>.md (question, frozen context, two answers labelled X / Y in a per-question
        random order) and outdir/key.json (which label is which model) -- the key never goes to a grader.
  score <outdir>   reads outdir/grades-<k>.json from the graders, un-blinds with key.json, prints totals.
"""
import glob
import json
import os
import random
import sys

RUBRIC = """You grade answers from a retrieval-augmented assistant. For each question below you get the exact
context the assistant was given and two answers, X and Y, from two different systems (order is random).
Judge each answer ONLY against the context shown (not your own knowledge of VMware):

score 2 = answers the question correctly; every substantive claim is supported by the context.
score 1 = partly answers, or mixes correct supported content with at least one unsupported or wrong claim.
score 0 = wrong, mostly unsupported, or off-topic; OR the refusal "Not in the provided VCF documents."
          when the context does contain an answer.
A refusal scores 2 when the context genuinely does not contain the answer.
Ignore length, formatting and the mandatory "Currency:" line. Do not reward verbosity; do penalise
claims (numbers, steps, requirements) that are not in the context.

Return ONLY a JSON object: {"<question id>": {"X": {"score": 0|1|2, "why": "<one sentence>"},
"Y": {"score": 0|1|2, "why": "<one sentence>"}}, ...} covering every question in this batch.
"""


def build(frozen, fa, fb, outdir, batches=4):
    d = json.load(open(frozen, encoding="utf-8"))
    qs = {q["id"]: q for q in d["questions"] if q["answerable"]}
    a = {r["id"]: r for r in json.load(open(fa, encoding="utf-8"))["results"]}
    b = {r["id"]: r for r in json.load(open(fb, encoding="utf-8"))["results"]}
    ma, mb = json.load(open(fa))["model"], json.load(open(fb))["model"]
    ids = sorted(set(a) & set(b) & set(qs))
    rng = random.Random(20261012)
    key = {}
    os.makedirs(outdir, exist_ok=True)
    chunks = [ids[i::batches] for i in range(batches)]
    for k, part in enumerate(chunks):
        out = [RUBRIC]
        for qid in part:
            q = qs[qid]
            flip = rng.random() < 0.5
            x, y = (b, a) if flip else (a, b)
            key[qid] = {"X": mb if flip else ma, "Y": ma if flip else mb}
            ctx = "\n\n".join(f"[Source: {c['source']}]\n{c['text']}" for c in q["chunks"])
            out.append(f"\n\n======== QUESTION {qid} ========\n{q['question']}\n\n---- CONTEXT ----\n{ctx}\n\n"
                       f"---- ANSWER X ----\n{x[qid]['answer']}\n\n---- ANSWER Y ----\n{y[qid]['answer']}\n")
        open(os.path.join(outdir, f"batch-{k}.md"), "w", encoding="utf-8").write("".join(out))
    json.dump(key, open(os.path.join(outdir, "key.json"), "w"), indent=1)
    print(f"{len(ids)} questions in {batches} batches; models {ma} / {mb}")


def score(outdir):
    key = json.load(open(os.path.join(outdir, "key.json")))
    tot, per = {}, {}
    for f in sorted(glob.glob(os.path.join(outdir, "grades-*.json"))):
        g = json.load(open(f, encoding="utf-8"))
        for qid, xy in g.items():
            for lab in ("X", "Y"):
                m = key[qid][lab]
                s = int(xy[lab]["score"])
                tot.setdefault(m, [0, 0, 0])[s] += 1
                per.setdefault(qid, {})[m] = (s, xy[lab]["why"])
    missing = sorted(set(key) - set(per))
    for m, c in tot.items():
        print(f"{m:20} points {c[1] + 2 * c[2]}/{2 * sum(c)}  score2={c[2]} score1={c[1]} score0={c[0]}")
    print("missing:", missing or "-")
    for qid in sorted(per):
        print(qid, "  ".join(f"{m}={s}" for m, (s, _) in sorted(per[qid].items())))
    return per


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6]) if len(sys.argv) > 6 else 4)
    else:
        score(sys.argv[2])
