"""Stand-in for `docker` in unit tests: `run` sleeps (a grade that hangs), `kill <name>` records the name."""
import os
import sys
import time

args = sys.argv[1:]
if args and args[0] == "kill":
    with open(os.environ["FAKE_DOCKER_KILLED"], "a") as f:
        f.write(args[1] + "\n")
    sys.exit(0)
if args and args[0] == "run":
    time.sleep(30)
sys.exit(0)
