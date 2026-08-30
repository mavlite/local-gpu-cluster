"""Agentic eval harness v3 — hidden-test grading, long horizons, multi-rep.

WHY v3. v2 graded on the same tests the agent could run, so an agent that fitted
the visible assertions scored the same as one that understood the problem. On
the hard set that produced 5/5 vs 5/5 for two very different models: the score
could not discriminate, only wall-clock could.

v3 adds:

  hidden_files / verify_hidden
      Files written ONLY after the agent stops, and a second verify command it
      never had access to. Visible tests cover the obvious cases; hidden tests
      cover what those cases IMPLY. Passing both means the intent was
      understood; passing only the visible ones means the assertions were fitted.

  REPS with variance
      Pass RATE across repetitions, not a single count. With 5 tasks one flake
      moves a bare score by 20%.

  usable MAXTURNS
      Long-horizon tasks need 25-40 turns. The cap must not be the binding
      constraint, or it measures the cap.

Env:
  TASKSET   std | hard | hidden | long | open
  MAXTURNS  turn cap (default 18)
  EVAL_URL  full chat-completions URL (default: the router)
  REPS      argv[3], repetitions per task
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, "/root")

TASKSET = os.environ.get("TASKSET", "std").lower()
if TASKSET == "hard":
    from agent_tasks_hard import HARD_TASKS as TASKS
elif TASKSET == "hidden":
    from agent_tasks_hidden import HIDDEN_TASKS as TASKS
elif TASKSET == "long":
    from agent_tasks_long import LONG_TASKS as TASKS
elif TASKSET == "open":
    from agent_tasks_open import OPEN_TASKS as TASKS
else:
    from agent_tasks import TASKS

MODEL = sys.argv[1]
KEY = sys.argv[2]
REPS = int(sys.argv[3]) if len(sys.argv) > 3 else 1
MAXTURNS = int(os.environ.get("MAXTURNS", "18"))
URL = os.environ.get("EVAL_URL",
                     "http://192.168.6.153:8000/v1/chat/completions")

OPTS = {
    "qwen3.8-think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
    "rag-qwen3.8": {"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                    "presence_penalty": 1.5},
    "qwen3-coder": {"temperature": 0.15, "top_p": 0.95},
}
opts = OPTS.get(MODEL, {})

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List files in the repo.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file's full contents.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write (overwrite) a file with new contents.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_tests",
        "description": "Run the repo's test suite and return output.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Call when the task is complete and tests pass.",
        "parameters": {"type": "object",
                       "properties": {"summary": {"type": "string"}},
                       "required": []}}},
]


def run(cmd, cwd, timeout=180):
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout + r.stderr)[-2500:], r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", 1


def exec_tool(name, args, repo, verify):
    try:
        if name == "list_files":
            return "\n".join(sorted(os.listdir(repo))) or "(empty)"
        if name == "read_file":
            p = os.path.join(repo, os.path.basename(args.get("path", "")))
            if not os.path.isfile(p):
                return "ERROR: no such file: %s" % args.get("path")
            return open(p, encoding="utf-8", errors="replace").read()
        if name == "write_file":
            p = os.path.join(repo, os.path.basename(args.get("path", "")))
            open(p, "w", encoding="utf-8").write(args.get("content", ""))
            return "wrote %s (%d bytes)" % (args.get("path"),
                                            len(args.get("content", "")))
        if name == "run_tests":
            if not verify:
                return ("This task has no runnable test suite. Reason from the "
                        "goal and the code, then call finish.")
            out, rc = run(verify, repo)
            return "exit=%d\n%s" % (rc, out)
        if name == "finish":
            return "acknowledged"
    except Exception as e:
        return "ERROR: %s: %s" % (type(e).__name__, e)
    return "ERROR: unknown tool"


def call(messages):
    body = {"model": MODEL, "messages": messages, "tools": TOOLS,
            "max_tokens": 3000, **opts}
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % KEY})
    return json.load(urllib.request.urlopen(req, timeout=900))


def play(task):
    repo = tempfile.mkdtemp(prefix="ag_%s_" % task["name"])
    for fn, body in task["files"].items():
        open(os.path.join(repo, fn), "w", encoding="utf-8").write(body)
    verify = task.get("verify")
    msgs = [
        {"role": "system", "content":
         "You are a coding agent working in a small repo. Use the provided "
         "tools to inspect and modify files, and run the tests. Keep going "
         "until the work is correct, then call finish. Prefer a general, "
         "correct solution over one that only satisfies the visible tests."},
        {"role": "user", "content": task["goal"]},
    ]
    turns = tool_calls = errs = 0
    t0 = time.time()
    while turns < MAXTURNS:
        turns += 1
        try:
            d = call(msgs)
        except Exception as e:
            shutil.rmtree(repo, ignore_errors=True)
            return dict(visible=False, hidden=False,
                        why="api_error:%s" % type(e).__name__, turns=turns,
                        tools=tool_calls, errs=errs, secs=time.time() - t0)
        m = d["choices"][0]["message"]
        tcs = m.get("tool_calls") or []
        am = {"role": "assistant", "content": m.get("content") or ""}
        if tcs:
            am["tool_calls"] = tcs
        msgs.append(am)
        if not tcs:
            msgs.append({"role": "user", "content":
                         "Continue using the tools, then call finish."})
            continue
        done = False
        for tc in tcs:
            fn = tc["function"]["name"]
            tool_calls += 1
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            res = exec_tool(fn, args, repo, verify)
            if isinstance(res, str) and res.startswith("ERROR"):
                errs += 1
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "name": fn, "content": str(res)[:4000]})
            if fn == "finish":
                done = True
        if done:
            break

    vis_ok = True
    why = "pass"
    if verify:
        out, rc = run(verify, repo)
        vis_ok = rc == 0
        if not vis_ok:
            why = (out.strip().splitlines() or ["fail"])[-1][:70]

    # Hidden grading: written only now, so the agent could never fit them.
    hid_ok = None
    if task.get("hidden_files"):
        for fn, body in task["hidden_files"].items():
            open(os.path.join(repo, fn), "w", encoding="utf-8").write(body)
        hout, hrc = run(task["verify_hidden"], repo)
        hid_ok = hrc == 0
        if vis_ok and not hid_ok:
            why = "visible-only: " + (hout.strip().splitlines() or ["fail"])[-1][:55]

    shutil.rmtree(repo, ignore_errors=True)
    return dict(visible=vis_ok, hidden=hid_ok, why=why, turns=turns,
                tools=tool_calls, errs=errs, secs=time.time() - t0)


print("===== AGENTIC EVAL: %s taskset=%s reps=%d (max %d turns) ====="
      % (MODEL, TASKSET, REPS, MAXTURNS), flush=True)
res = []
for rep in range(REPS):
    for t in TASKS:
        r = play(t)
        res.append((t["name"], r))
        h = "-" if r["hidden"] is None else ("HID-PASS" if r["hidden"] else "HID-FAIL")
        print("  %-22s rep%d: %-4s %-8s (%s) turns=%d tools=%d errs=%d %.0fs"
              % (t["name"], rep, "PASS" if r["visible"] else "FAIL", h,
                 r["why"], r["turns"], r["tools"], r["errs"], r["secs"]),
              flush=True)

n = len(res)
v = sum(1 for _, r in res if r["visible"])
hs = [r["hidden"] for _, r in res if r["hidden"] is not None]
hp = sum(1 for x in hs if x)
print("  --> visible %d/%d (%.0f%%)" % (v, n, 100.0 * v / n), flush=True)
if hs:
    print("  --> HIDDEN  %d/%d (%.0f%%)   <-- the discriminating score"
          % (hp, len(hs), 100.0 * hp / len(hs)), flush=True)
print("  --> mean turns %.1f  tools %.1f  %.0fs"
      % (sum(r["turns"] for _, r in res) / n,
         sum(r["tools"] for _, r in res) / n,
         sum(r["secs"] for _, r in res) / n), flush=True)
json.dump([(a, b) for a, b in res],
          open("/root/agent_%s_%s.json" % (MODEL, TASKSET), "w"))
