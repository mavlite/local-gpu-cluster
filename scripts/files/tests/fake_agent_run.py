"""Stand-in for /usr/local/bin/agent-run: `fake_agent_run.py DIR MESSAGE` or `fake_agent_run.py --preflight DIR`.

The sandboxed agent has no GitHub credentials: it PRINTS its report as a text event of the opencode JSON
stream and the dispatcher posts it. Behaviour from $FAKE_AGENT_MODE:
  pass | fail | headerless | noreport | exit75 | exit76 | hang | crash-after-pass | leak
It writes one artifact into $AGENT_RESULTS_DIR, as a real task would into /results.
--preflight exits 76 when $FAKE_PREFLIGHT_FAIL is "all" or names the checkout's directory (comma list), else 0.
"""
import json
import os
import subprocess
import sys
import time


def emit(kind, **part):
    print(json.dumps({"type": kind, "part": dict(part, type=kind)}), flush=True)


def preflight(work_dir):
    failing = [n.strip() for n in os.environ.get("FAKE_PREFLIGHT_FAIL", "").split(",") if n.strip()]
    if "all" in failing or os.path.basename(os.path.normpath(work_dir)) in failing:
        print("agent-run: nftables table inet agent_egress is not loaded", file=sys.stderr)
        return 76
    print("agent-run: preflight ok")
    return 0


def main(argv):
    if argv[0] == "--preflight":
        return preflight(argv[1])
    work_dir = argv[0]
    mode = os.environ.get("FAKE_AGENT_MODE", "pass")
    emit("step_start")
    emit("text", text="leaked ghp_" + "A" * 36 + " and Bearer abc.def.ghi")
    if mode == "exit75":
        print("agent-run: lane closed", file=sys.stderr)
        return 75
    if mode == "exit76":
        print("agent-run: nftables table inet agent_egress is not loaded", file=sys.stderr)
        return 76
    if mode == "hang":
        time.sleep(120)
        return 0
    results = os.environ["AGENT_RESULTS_DIR"]
    os.makedirs(results, exist_ok=True)
    with open(os.path.join(results, "probe.txt"), "w") as f:
        f.write("artifact\n")
    if mode == "noreport":
        emit("text", text="I forgot the report")
        return 0
    sha = subprocess.run(["git", "-C", work_dir, "log", "-1", "--format=%H"],
                         capture_output=True, text=True, check=True).stdout.strip()
    verdict = "FAIL" if mode == "fail" else "PASS"
    extra = ""
    if mode == "leak":
        leaked = os.environ.get("FAKE_LEAK_VALUE")
        if not leaked:
            with open(os.environ["FAKE_SECRET_FILE"]) as f:
                leaked = f.read().strip()
        extra = "    note: " + leaked + "\n"
    yaml = (f"task: {os.environ['FAKE_TASK']}\nverdict: {verdict}\nref: main@{sha}\n"
            "started: x\nfinished: y\nmodel: qwen3.6\nresults:\n  - check: git log -1\n"
            f"    value: {sha}\n    pass: {'true' if verdict == 'PASS' else 'false'}\n{extra}"
            "artifacts:\n  - /results/probe.txt\ndeviations: none\nstate_left: main\n")
    if mode == "headerless":
        text = "Done.\n\n---\n" + yaml
    else:
        text = "Done.\n\n### runner report\n```yaml\n" + yaml + "```\n"
    emit("text", text=text)
    emit("step_finish")
    return 3 if mode == "crash-after-pass" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
