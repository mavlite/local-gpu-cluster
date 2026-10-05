"""One capacity run: workspace -> opencode coordinator fan-out -> grade (spec §5.6 capacity sub-gate).

Footguns, each found by running:
  * `opencode run` reads stdin when it is not a TTY and waits for EOF forever -> stdin=DEVNULL.
  * the npm shim (opencode.cmd) is a cmd.exe wrapper; a timeout kills the shim and leaves the real
    opencode.exe running -> launch the binary itself (resolve_opencode()).
"""
import json
import os
import shutil
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request

import profiles
import task_set

PROBE_PROMPT = "Reply with the single word: ready"


def resolve_opencode():
    """Path to the real opencode binary (not the npm .cmd shim)."""
    explicit = os.environ.get("GATE_OPENCODE")
    if explicit:
        return explicit
    appdata = os.environ.get("APPDATA")
    if appdata:
        exe = os.path.join(appdata, "npm", "node_modules", "opencode-ai", "bin", "opencode.exe")
        if os.path.isfile(exe):
            return exe
    found = shutil.which("opencode")
    if not found:
        raise FileNotFoundError("opencode not found; set GATE_OPENCODE")
    return found


def probe_once(router_url, alias, key, timeout=600):
    """(ok, seconds) for one tiny non-streamed completion on the coordinator alias."""
    body = json.dumps({"model": alias, "max_tokens": 8, "temperature": 0, "stream": False,
                       "messages": [{"role": "user", "content": PROBE_PROMPT}]}).encode()
    req = urllib.request.Request(router_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.load(r)
            return r.status == 200, time.monotonic() - t0
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False, time.monotonic() - t0


class Prober:
    """Background sidecar: one probe every `interval` seconds until stop()."""

    def __init__(self, router_url, alias, key, interval=20.0, probe=probe_once):
        self.args, self.interval, self.probe = (router_url, alias, key), interval, probe
        self.samples, self._stop = [], threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            ok, secs = self.probe(*self.args)
            self.samples.append({"t": time.time(), "ok": ok, "s": secs})
            self._stop.wait(self.interval)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        self._t.join()
        ok = [s["s"] for s in self.samples if s["ok"]]
        return {"n": len(self.samples), "failed": len(self.samples) - len(ok),
                "p50": statistics.median(ok) if ok else None}


def parse_events(text):
    """task tool calls from `opencode run --format json` output (one JSON object per line)."""
    calls = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        part = ev.get("part") or {}
        if ev.get("type") != "tool_use" or part.get("tool") != "task":
            continue
        st = part.get("state") or {}
        tm = st.get("time") or {}
        meta = st.get("metadata") or {}
        calls.append({"agent": (st.get("input") or {}).get("subagent_type"),
                      "provider": (meta.get("model") or {}).get("providerID"),
                      "status": st.get("status"),
                      "start_ms": tm.get("start"), "end_ms": tm.get("end")})
    return calls


def max_overlap(calls):
    """Largest number of task calls running at the same instant (1 means fully serial)."""
    edges = []
    for c in calls:
        if c["start_ms"] is not None and c["end_ms"] is not None:
            edges += [(c["start_ms"], 1), (c["end_ms"], -1)]
    best = cur = 0
    for _, d in sorted(edges, key=lambda e: (e[0], e[1])):
        cur += d
        best = max(best, cur)
    return best


KICKOFF = "Dispatch every task under tasks/ to the workers as your instructions describe."
ARM_CAPACITY = {"A": 3, "B": 1}          # router chat_admission.capacity each layout must show


def chat_capacity(router_url, timeout=10):
    """chat_admission.capacity from the router's unauthenticated /healthz."""
    base = router_url.rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    with urllib.request.urlopen(base + "/healthz", timeout=timeout) as r:
        return json.load(r)["chat_admission"]["capacity"]


def run_capacity(arm, out_dir, task_root, router_url, worker_urls=(), timeout_s=4 * 3600,
                 probe_interval=20.0, prober=None, env=None):
    """Run one fan-out; write out_dir/result.json and return the result dict."""
    base_env = dict(os.environ if env is None else env)
    if not base_env.get("GATE_ROUTER_KEY") or (arm == "B" and not base_env.get("GATE_WORKER_KEY")):
        raise RuntimeError("GATE_ROUTER_KEY (and GATE_WORKER_KEY for arm B) must be set")
    cap_before = chat_capacity(router_url)
    if cap_before != ARM_CAPACITY[arm]:
        raise RuntimeError(f"router chat capacity is {cap_before}; arm {arm} needs "
                           f"{ARM_CAPACITY[arm]} -- wrong layout, run gate_env.sh arm-{arm.lower()}")
    os.makedirs(out_dir, exist_ok=False)                  # never reuse a run directory
    ws, home = os.path.join(out_dir, "workspace"), os.path.join(out_dir, "home")
    os.makedirs(ws)
    os.makedirs(home)
    tasks = task_set.prepare_workspace(task_root, ws)
    profiles.install(arm, ws, router_url, worker_urls)
    if prober is None:
        prober = Prober(router_url, profiles.ROUTER_MODEL, base_env["GATE_ROUTER_KEY"], probe_interval)
    prober.start()
    t0 = time.time()
    timed_out = False
    with open(os.path.join(out_dir, "events.jsonl"), "w", encoding="utf-8") as ev, \
            open(os.path.join(out_dir, "opencode.stderr"), "w", encoding="utf-8") as er:
        try:
            rc = subprocess.run([resolve_opencode(), "run", "--pure", "--agent", "coordinator",
                                 "--dir", ws, "--format", "json", KICKOFF],
                                env=profiles.opencode_env(base_env, home), stdin=subprocess.DEVNULL,
                                stdout=ev, stderr=er, timeout=timeout_s).returncode
        except subprocess.TimeoutExpired:
            rc, timed_out = None, True
    t1 = time.time()
    probe = prober.stop()
    try:
        cap_after = chat_capacity(router_url)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        cap_after = None
    with open(os.path.join(out_dir, "events.jsonl"), encoding="utf-8") as f:
        calls = parse_events(f.read())
    grades = task_set.grade(task_root, ws)
    completed = sum(1 for c in calls if c["status"] == "completed")
    wall = t1 - t0
    result = {"arm": arm, "t_start": t0, "t_end": t1, "wall_s": wall, "rc": rc,
              "timed_out": timed_out, "n_tasks": len(tasks), "task_calls": len(calls),
              "completed": completed, "tasks_per_hour": completed / (wall / 3600.0),
              "passed": sum(1 for g in grades.values() if g["pass"]), "grades": grades,
              "max_overlap": max_overlap(calls),
              "providers": sorted({c["provider"] for c in calls if c["provider"]}),
              "probe": probe, "capacity_before": cap_before, "capacity_after": cap_after}
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    return result
