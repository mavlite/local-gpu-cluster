"""Stand-in for /usr/local/bin/agent-run: `fake_agent_run.py DIR MESSAGE`.

Behaviour from $FAKE_AGENT_MODE: pass | fail | noreport | exit75 | hang | crash-after-pass.
It reports through the fake gh ($FAKE_GH_CMD), exactly as the real agent would with `gh issue comment`.
"""
import json
import os
import shlex
import subprocess
import sys
import time


def main(argv):
    work_dir, message = argv[0], argv[1]
    mode = os.environ.get("FAKE_AGENT_MODE", "pass")
    print(json.dumps({"type": "step_start", "dir": work_dir}), flush=True)
    print(json.dumps({"type": "text", "text": "leaked ghp_" + "A" * 36 + " and Bearer abc.def.ghi"}), flush=True)
    if mode == "exit75":
        print("agent-run: lane closed", file=sys.stderr)
        return 75
    if mode == "hang":
        time.sleep(120)
        return 0
    with open(os.path.join(work_dir, "probe.txt"), "w") as f:
        f.write(message)
    if mode == "noreport":
        print(json.dumps({"type": "text", "text": "I forgot the report"}), flush=True)
        return 0
    sha = subprocess.run(["git", "-C", work_dir, "log", "-1", "--format=%H"],
                         capture_output=True, text=True, check=True).stdout.strip()
    verdict = "FAIL" if mode == "fail" else "PASS"
    body = ("### runner report\n```yaml\n"
            f"task: {os.environ['FAKE_TASK']}\nverdict: {verdict}\nref: main@{sha}\n"
            "started: x\nfinished: y\nmodel: qwen3.6\nresults:\n  - check: git log -1\n"
            f"    value: {sha}\n    pass: {'true' if verdict == 'PASS' else 'false'}\n"
            "artifacts: []\ndeviations: none\nstate_left: main\n```\n")
    gh = shlex.split(os.environ["FAKE_GH_CMD"])
    subprocess.run(gh + ["issue", "comment", os.environ["FAKE_TASK"], "-R", os.environ["FAKE_REPO"],
                         "--body", body], check=True)
    print(json.dumps({"type": "step_finish"}), flush=True)
    return 3 if mode == "crash-after-pass" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
