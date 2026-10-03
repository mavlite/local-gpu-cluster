# scripts/redteam/provision_user.py
"""Provision one orchestrator user via the app's own db + security modules.
Run INSIDE LXC 157 from the backend dir with the orchestrator venv:
  REDTEAM_ORCH_USER=… REDTEAM_ORCH_PASSWORD=… \
    /root/RedteamAgent/orchestrator/backend/.venv/bin/python provision_user.py
Idempotent: an existing username is treated as success (create_user raises
UsernameAlreadyExistsError, it does not skip — spec §4.3)."""
import os
import sys

from app import db, security  # resolved from backend/ cwd inside 157


def main() -> int:
    user = os.environ.get("REDTEAM_ORCH_USER")
    pw = os.environ.get("REDTEAM_ORCH_PASSWORD")
    if not user or not pw:
        print("REDTEAM_ORCH_USER and REDTEAM_ORCH_PASSWORD are required", file=sys.stderr)
        return 2
    salt, password_hash = security.hash_password(pw)  # salt FIRST (spec §4.3)
    try:
        db.create_user(user, password_hash, salt)     # (username, hash, salt)
        print("created", user)
    except db.UsernameAlreadyExistsError:
        print("exists", user)
    return 0


if __name__ == "__main__":
    sys.exit(main())
