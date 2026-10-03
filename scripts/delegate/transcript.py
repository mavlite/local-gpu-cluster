"""Compute a task's Claude-token cost from a Claude Code session transcript.

The Phase-1 A/B experiment compares Claude's own tokens for delegated vs. self-done
tasks (design section 6). This reads the session JSONL and sums `usage` over exactly
the task's turns, so the number is measured rather than hand-entered:

- Delegated: the submit_task turn, plus the result()..record_review turns. The turns
  in between (background work while the job runs) are excluded, so the delegated arm
  is not inflated by unrelated work.
- Self-done: the mark_start turn through the record_direct turn, contiguous.

All four usage components are returned so the headline metric can be reweighted later
without re-running the experiment.
"""
import glob
import json
import os

# (our key, Claude Code transcript key)
_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("cache_creation", "cache_creation_input_tokens"),
)


def find_transcript(session_id=None, projects_dir=None, repo=None):
    """Locate a session transcript. Prefer an exact session_id match across all
    project dirs (ids are unique); else the newest .jsonl, optionally biased to a
    dir whose name mentions the repo. Returns a path or None."""
    base = projects_dir or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if session_id:
        hits = glob.glob(os.path.join(base, "*", f"{session_id}.jsonl"))
        if hits:
            return hits[0]
    cands = glob.glob(os.path.join(base, "*", "*.jsonl"))
    if repo:
        tag = os.path.basename(os.path.normpath(repo)).lower()
        tagged = [c for c in cands if tag in c.lower()]
        cands = tagged or cands
    return max(cands, key=os.path.getmtime) if cands else None


def _load(path):
    """Parse the transcript into ordered assistant turns + a tool_use_id -> result map.

    turns: [{"index": i, "usage": {...}, "tool_uses": [{"name","id","input"}]}]
    results: {tool_use_id: parsed-json-result-or-{}}
    """
    turns, results = [], {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue  # Claude Code writes non-message bookkeeping lines too
            msg = o.get("message") or {}
            content = msg.get("content") or []
            if o.get("type") == "assistant":
                tus = [{"name": b.get("name"), "id": b.get("id"), "input": b.get("input") or {}}
                       for b in content
                       if isinstance(b, dict) and b.get("type") == "tool_use"]
                turns.append({"index": len(turns), "usage": msg.get("usage") or {}, "tool_uses": tus})
            else:
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = _result_json(b.get("content"))
    return turns, results


def _result_json(content):
    """A tool_result's content is a JSON string, or a list of text blocks, or already-parsed."""
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
    return content or {}


def _calls(turn, tool):
    """tool_use blocks in this turn for the named tool, bare or MCP-prefixed."""
    return [tu for tu in turn["tool_uses"]
            if tu["name"] == tool or (tu["name"] or "").endswith("__" + tool)]


def _sum(turns):
    out = {k: 0 for k, _ in _TOKEN_KEYS}
    for t in turns:
        u = t["usage"]
        for k, src in _TOKEN_KEYS:
            out[k] += int(u.get(src) or 0)
    out["total"] = sum(out[k] for k, _ in _TOKEN_KEYS)
    return out


def _by_index(turns, keep):
    return [t for t in turns if t["index"] in keep]


def delegated_turns(turns, results, job_id):
    submit_i = result_i = review_i = None
    for t in turns:
        for tu in _calls(t, "submit_task"):
            if results.get(tu["id"], {}).get("job_id") == job_id:
                submit_i = t["index"]
        for tu in _calls(t, "result"):
            if tu["input"].get("job_id") == job_id and result_i is None and submit_i is not None:
                result_i = t["index"]
        for tu in _calls(t, "record_review"):
            if tu["input"].get("job_id") == job_id:
                review_i = t["index"]
    if submit_i is None:
        raise ValueError(f"no submit_task turn found for job {job_id}")
    end = review_i if review_i is not None else turns[-1]["index"]
    start2 = result_i if result_i is not None else end
    keep = {submit_i} | set(range(start2, end + 1))
    return _by_index(turns, keep)


def selfdone_turns(turns, results, marker_id=None):
    start_i = end_i = None
    for t in turns:
        for tu in _calls(t, "mark_start"):
            mid = results.get(tu["id"], {}).get("marker_id")
            if marker_id is None or mid == marker_id:
                start_i = t["index"]  # marker_id None -> nearest preceding (last) mark_start
        for tu in _calls(t, "record_direct"):
            if marker_id is not None and tu["input"].get("marker_id") == marker_id:
                end_i = t["index"]
    if start_i is None:
        raise ValueError("no mark_start turn found")
    if end_i is None:
        end_i = turns[-1]["index"]
    return _by_index(turns, set(range(start_i, end_i + 1)))


def measure_delegated(path, job_id):
    turns, results = _load(path)
    return _sum(delegated_turns(turns, results, job_id))


def measure_selfdone(path, marker_id=None):
    turns, results = _load(path)
    return _sum(selfdone_turns(turns, results, marker_id))
