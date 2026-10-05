"""§5.2 concurrency probe: does opencode run `task` subagents at the same time?

Drives the real opencode binary against stub_llm: the coordinator issues three task calls in one
message and each worker's first model call sleeps `worker_sleep` seconds server-side. Parallel
execution finishes in ~1x the sleep; serial in ~3x. The verdict comes from the stub's own request
timestamps, not from opencode's report.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import fanout
import profiles
import stub_llm


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def mechanism_probe(worker_sleep=4.0, task_ids=("T1", "T2", "T3"), timeout_s=300):
    root = tempfile.mkdtemp(prefix="gate-probe-")
    ws, home, log = (os.path.join(root, d) for d in ("ws", "home", "requests.jsonl"))
    for tid in task_ids:
        os.makedirs(os.path.join(ws, "tasks", tid))
        with open(os.path.join(ws, "tasks", tid, "TASK.md"), "w") as f:
            f.write("probe\n")
    os.makedirs(home)
    ports = free_ports(4)
    servers = stub_llm.serve(ports, stub_llm.Script(worker_sleep, task_ids), log)
    try:
        profiles.install("B", ws, f"http://127.0.0.1:{ports[0]}/v1",
                         [f"http://127.0.0.1:{p}/v1" for p in ports[1:]])
        env = profiles.opencode_env(dict(os.environ, GATE_ROUTER_KEY="probe", GATE_WORKER_KEY="probe"), home)
        r = subprocess.run([fanout.resolve_opencode(), "run", "--pure", "--agent", "coordinator",
                            "--dir", ws, "--format", "json", "Dispatch every task under tasks/."],
                           env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout_s)
    finally:
        for s in servers:
            s.shutdown()
    with open(log, encoding="utf-8") as f:
        reqs = [json.loads(line) for line in f]
    sleeps = [q for q in reqs if q["role"] != 0 and q["t1"] - q["t0"] >= worker_sleep * 0.9]
    span = (max(q["t1"] for q in sleeps) - min(q["t0"] for q in sleeps)) if sleeps else None
    serial = len(task_ids) * worker_sleep
    verdict = ("parallel" if len(sleeps) == len(task_ids) and span is not None and span < serial * 0.6
               else "serial" if len(sleeps) == len(task_ids) else "inconclusive")
    return {"rc": r.returncode, "worker_calls": len(sleeps), "span_s": span,
            "serial_s": serial, "verdict": verdict,
            "worker_tools": sorted({t for q in reqs if q["role"] != 0 for t in q["tools"]}),
            "coordinator_tools": sorted({t for q in reqs if q["role"] == 0 for t in q["tools"]}),
            "escaped": os.path.exists(os.path.join(root, "escaped.txt")),
            "events": fanout.parse_events(r.stdout), "dir": root}


if __name__ == "__main__":
    out = mechanism_probe()
    out.pop("events")
    print(json.dumps(out, indent=1))
    sys.exit(0 if out["verdict"] == "parallel" and not out["escaped"] else 1)
