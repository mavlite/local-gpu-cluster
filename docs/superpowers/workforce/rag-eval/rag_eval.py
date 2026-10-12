"""RAG synthesis eval: the same frozen retrieved context, answered by different chat models.

  retrieve  <questions.json> <out.json>      on the Proxmox host (uses scripts/rag/lib/allm.py; the
            AnythingLLM key is read from scripts/config.env, never printed): top-N chunks per question
            from the vcf-reference workspace plus the workspace's own system prompt; marks each question
            `answerable` when every key fact's pattern matches the retrieved context.
  answer    <frozen.json> <base_url> <model> <key_file> <out.json> [--kwargs JSON] [--temperature T ...]
            each answerable question once, non-streaming; records answer, latency, tokens.
  grade     <frozen.json> <answers.json> [<answers.json> ...]
            per model: key facts found / total, questions fully answered, median latency.
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.request

WORKSPACE = "vcf-reference"
ALLM = "http://192.168.6.154:3001/api/v1"
TOP_N = 6


def _facts_in(text, facts):
    return [bool(re.search(f["pattern"], text, re.I | re.S)) for f in facts]


def retrieve(qpath, out):
    sys.path.insert(0, "/root/local-gpu-cluster/scripts/rag")
    from lib import allm  # noqa: E402
    client = allm.AnythingLLMClient(base_url=ALLM, api_key=allm.load_api_key(), timeout=120)
    ws = client.session.get(f"{ALLM}/workspace/{WORKSPACE}", timeout=60).json()
    ws = ws.get("workspace") or ws
    ws = ws[0] if isinstance(ws, list) else ws
    system_prompt = ws.get("openAiPrompt") or ""
    qs = json.load(open(qpath, encoding="utf-8"))
    for q in qs:
        res = client.vector_search(WORKSPACE, q["question"], top_n=TOP_N)
        q["chunks"] = [{"text": r.get("text", ""), "source": (r.get("metadata") or {}).get("title")
                        or (r.get("metadata") or {}).get("url") or "", "score": r.get("score")} for r in res]
        ctx = "\n".join(c["text"] for c in q["chunks"])
        q["context_has"] = _facts_in(ctx, q["facts"])
        q["answerable"] = all(q["context_has"])
    json.dump({"system_prompt": system_prompt, "top_n": TOP_N, "questions": qs}, open(out, "w", encoding="utf-8"), indent=1)
    print(f"{sum(q['answerable'] for q in qs)} of {len(qs)} questions answerable from the retrieved context; "
          f"system prompt {len(system_prompt)} chars")
    for q in qs:
        print(f"  {q['id']:6} answerable={q['answerable']} facts_in_context={q['context_has']}")


def _context_block(chunks):
    return "\n\n".join(f"[Source: {c['source']}]\n{c['text']}" for c in chunks)


def answer(frozen, base, model, key_file, out, extra):
    d = json.load(open(frozen, encoding="utf-8"))
    key = open(key_file).read().strip()
    results = []
    for q in [q for q in d["questions"] if q["answerable"]]:
        msgs = [{"role": "system", "content": d["system_prompt"] + "\n\nContext:\n" + _context_block(q["chunks"])},
                {"role": "user", "content": q["question"]}]
        body = {"model": model, "messages": msgs, "max_tokens": 1500, "stream": False, **extra}
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        t0 = time.time()
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=900).read())
            text = r["choices"][0]["message"].get("content") or ""
            usage = r.get("usage") or {}
        except Exception as e:  # noqa: BLE001
            text, usage = f"ERROR {type(e).__name__}: {e}", {}
        results.append({"id": q["id"], "answer": text, "seconds": round(time.time() - t0, 1),
                        "completion_tokens": usage.get("completion_tokens"), "prompt_tokens": usage.get("prompt_tokens")})
        print(f"  {q['id']:6} {results[-1]['seconds']:6.1f}s {len(text):5d} chars")
    json.dump({"model": model, "base": base, "extra": extra, "results": results}, open(out, "w", encoding="utf-8"), indent=1)


def grade(frozen, answer_files):
    d = json.load(open(frozen, encoding="utf-8"))
    qs = {q["id"]: q for q in d["questions"] if q["answerable"]}
    for af in answer_files:
        a = json.load(open(af, encoding="utf-8"))
        found = total = full = 0
        misses = []
        for r in a["results"]:
            hits = _facts_in(r["answer"], qs[r["id"]]["facts"])
            found += sum(hits); total += len(hits); full += all(hits)
            if not all(hits):
                misses.append(f"{r['id']}:" + ",".join(f["name"] for f, h in zip(qs[r["id"]]["facts"], hits) if not h))
        secs = [r["seconds"] for r in a["results"]]
        print(f"{a['model']:28} facts {found}/{total} ({100*found/max(total,1):.0f}%), questions fully answered "
              f"{full}/{len(a['results'])}, median {statistics.median(secs):.1f}s, missed: {'; '.join(misses) or '-'}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "retrieve":
        retrieve(sys.argv[2], sys.argv[3])
    elif cmd == "answer":
        extra = {}
        args = sys.argv[7:]
        while args:
            flag, val = args[0], args[1]; args = args[2:]
            if flag == "--kwargs":
                extra["chat_template_kwargs"] = json.loads(val)
            else:
                extra[flag.lstrip("-").replace("-", "_")] = float(val) if re.fullmatch(r"[\d.]+", val) else val
        answer(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], extra)
    elif cmd == "grade":
        grade(sys.argv[2], sys.argv[3:])
