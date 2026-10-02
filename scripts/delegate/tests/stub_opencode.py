# Stand-in for `opencode run --format json`, driven by the scenario in argv[-1].
import sys, time, json

scenario = sys.argv[-1]


def emit(o):
    print(json.dumps(o), flush=True)


if scenario == "ok":
    emit({"type": "step_finish", "part": {"tokens": {"input": 20, "output": 4, "reasoning": 0, "cache": 0}}})
    emit({"type": "text", "text": "hello "})
    emit({"type": "step_finish", "part": {"tokens": {"input": 10, "output": 3, "reasoning": 0, "cache": 0}}})
    emit({"type": "text", "part": {"text": "world"}})
    sys.exit(0)
if scenario == "rejected":
    sys.stderr.write("permission requested: external_directory (...); auto-rejecting\n")
    sys.exit(0)
if scenario == "rejected_event":
    emit({"type": "rejected"})
    sys.exit(0)
if scenario == "hang":
    print("started", flush=True)
    time.sleep(60)
    sys.exit(0)
if scenario in ("write", "exit1"):
    # End-to-end scenarios: act on the --dir the runner passes, like the real agent.
    dest = sys.argv[sys.argv.index("--dir") + 1]
    if scenario == "write":
        with open(f"{dest}/newfile.txt", "w", encoding="utf-8") as f:
            f.write("made by stub\n")
        emit({"type": "step_finish", "part": {"tokens": {"input": 7, "output": 2, "reasoning": 0, "cache": 0}}})
        emit({"type": "text", "text": "SUMMARY: added newfile.txt"})
        sys.exit(0)
    sys.stderr.write("stub failure\n")
    sys.exit(3)
