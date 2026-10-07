"""Loop metric over an `opencode export` session (workforce spec §7).

A loop episode is either
  * a repeat: 3 or more consecutive identical tool calls (same tool, same arguments), or
  * a cycle: a block of 2-4 calls repeated 3 or more times in a row.

Only identical calls form an episode, so test runs separated by real edits never do: an edit with
new arguments breaks the run. Re-running the same tests with nothing changed in between, or
re-applying the same edit, is a loop. `invalid` calls (a tool the agent was not offered) count.
"""
import json

MIN_REPS = 3
MAX_PERIOD = 4


def tool_calls(export):
    """[{"tool", "input", "status"}] in session order."""
    out = []
    for msg in export.get("messages", []):
        for part in msg.get("parts", []):
            if part.get("type") == "tool":
                st = part.get("state") or {}
                out.append({"tool": part.get("tool"), "input": st.get("input") or {},
                            "status": st.get("status")})
    return out


def key(call):
    return call["tool"], json.dumps(call["input"], sort_keys=True, separators=(",", ":"))


def _reps(keys, i, p):
    block, n = keys[i:i + p], 1
    while keys[i + n * p:i + (n + 1) * p] == block:
        n += 1
    return n


def episodes(calls):
    keys = [key(c) for c in calls]
    out, i = [], 0
    while i < len(keys):
        n = _reps(keys, i, 1)
        if n >= MIN_REPS:
            out.append({"kind": "repeat", "start": i, "length": n, "period": 1})
            i += n
            continue
        for p in range(2, MAX_PERIOD + 1):
            if i + p > len(keys):          # an all-identical block was already caught as a repeat
                continue
            n = _reps(keys, i, p)
            if n >= MIN_REPS:
                out.append({"kind": "cycle", "start": i, "length": n * p, "period": p})
                i += n * p
                break
        else:
            i += 1
    return out


def steps_per_turn(export):
    """Assistant messages after each user message: one opencode step each (spec §9 step-limit hits)."""
    out = []
    for msg in export.get("messages", []):
        role = (msg.get("info") or {}).get("role")
        if role == "user":
            out.append(0)
        elif role == "assistant" and out:
            out[-1] += 1
    return out


def summarize(export):
    calls = tool_calls(export)
    eps = episodes(calls)
    return {"n_calls": len(calls), "episodes": len(eps), "looped": bool(eps), "detail": eps}
