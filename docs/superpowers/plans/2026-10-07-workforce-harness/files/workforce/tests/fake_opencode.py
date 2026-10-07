"""Stand-in for `opencode` in unit tests. Behaviour comes from FAKE_OC_MODE:
  ok      emit a session, a tool call and final text; write argv to FAKE_OC_ARGV
  sleep   sleep (and spawn a sleeping child) so the caller's timeout must kill the tree
  export  `export <id>` prints a status line then the JSON document
"""
import json
import os
import subprocess
import sys
import time

mode = os.environ.get("FAKE_OC_MODE", "ok")
argv = sys.argv[1:]
if os.environ.get("FAKE_OC_ARGV"):
    with open(os.environ["FAKE_OC_ARGV"], "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\n")

if argv and argv[0] == "export":
    print("Exporting session: " + argv[1])
    print(json.dumps({"info": {"id": argv[1]}, "messages": []}))
    sys.exit(0)

if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_OC_CHILD"], "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
    sys.exit(0)

sid = os.environ.get("FAKE_OC_SESSION", "ses_fake1")
for ev in ({"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}},
           {"type": "text", "sessionID": sid, "part": {"type": "text", "text": "thinking about it"}},
           {"type": "tool_use", "sessionID": sid, "part": {"type": "tool", "tool": "bash"}},
           {"type": "text", "sessionID": sid, "part": {"type": "text", "text": "REVISE: add a test"}},
           {"type": "text", "sessionID": sid, "part": {"type": "text", "text": "for negatives"}},
           {"type": "step_finish", "sessionID": sid, "part": {"type": "step-finish"}}):
    print(json.dumps(ev))
sys.exit(0)
