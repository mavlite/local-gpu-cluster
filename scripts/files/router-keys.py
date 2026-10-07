#!/usr/bin/env python3
"""Issue, list and revoke scoped router keys (workforce design rev 2, §5.3). Stdlib only.

Runs inside the router LXC as root, installed as /usr/local/sbin/router-keys:
    router-keys add --name wf-run-1 --aliases qwen3.8-nothink --ttl-hours 72 --out /root/wf-run-1.key
    router-keys list
    router-keys revoke --name wf-run-1        |  router-keys revoke --all

The plaintext key goes ONLY to the --out file (created 0600, must not exist); stdout never shows it.
The keys file stores SHA-256 hashes and is replaced atomically; the router re-reads it on change.
add and revoke hold an exclusive lock (<keys file>.lock) across load-modify-save, so overlapping
invocations serialise instead of one silently undoing the other's change.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time

DEFAULT_KEYS_FILE = os.environ.get("ROUTER_KEYS_FILE", "/etc/router-keys.json")
MAX_TTL_HOURS = 24 * 14
# Same rule as access_keys.NAME_RE: names are logged as the request's principal.
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RESERVED_NAMES = {"owner"}
LOCK_TIMEOUT_S = 30.0


@contextlib.contextmanager
def _locked(path: str):
    """Exclusive lock on <path>.lock for one load-modify-save (flock on Linux, msvcrt elsewhere)."""
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:                 # Windows (off-box tests)
            import msvcrt
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise SystemExit(f"{path}.lock: timed out waiting for the lock")
                    time.sleep(0.05)
        yield
    finally:
        os.close(fd)                        # closing releases flock and msvcrt locks


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"keys": []}
    if not isinstance(data.get("keys"), list):
        raise SystemExit(f"{path}: malformed (no 'keys' list)")
    return data


def _save(path: str, data: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".router-keys.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o640)
        try:
            import grp
            os.chown(tmp, 0, grp.getgrnam("router").gr_gid)
        except (ImportError, KeyError, PermissionError, AttributeError):
            pass                      # off-box tests / no router group: perms still 0640
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def cmd_add(a) -> int:
    if not NAME_RE.fullmatch(a.name) or a.name in RESERVED_NAMES:
        print("refusing: --name must match [a-z0-9][a-z0-9._-]{0,63} and not be 'owner'", file=sys.stderr)
        return 2
    aliases = [x.strip() for x in a.aliases.split(",") if x.strip()]
    if not aliases:
        print("refusing: --aliases is empty", file=sys.stderr)
        return 2
    if not (0 < a.ttl_hours <= MAX_TTL_HOURS):
        print(f"refusing: --ttl-hours must be in (0, {MAX_TTL_HOURS}]", file=sys.stderr)
        return 2
    with _locked(a.keys_file):
        return _add_locked(a, aliases)


def _add_locked(a, aliases) -> int:
    data = _load(a.keys_file)
    if any(e.get("name") == a.name for e in data["keys"]):
        print(f"refusing: a key named {a.name!r} already exists", file=sys.stderr)
        return 2
    key = "wf_" + secrets.token_urlsafe(32)
    try:
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"refusing: {a.out} already exists", file=sys.stderr)
        return 2
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    expires = time.time() + a.ttl_hours * 3600
    data["keys"].append({"name": a.name, "sha256": hashlib.sha256(key.encode()).hexdigest(),
                         "aliases": aliases, "expires": expires})
    _save(a.keys_file, data)
    print(f"added {a.name}: aliases={','.join(aliases)} expires={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(expires))}"
          f" key-> {a.out}")
    return 0


def cmd_list(a) -> int:
    for e in _load(a.keys_file)["keys"]:
        exp = e.get("expires")
        when = "never" if exp is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))
        state = "EXPIRED" if exp is not None and time.time() >= exp else "active"
        print(f"{e.get('name')}\t{state}\texpires={when}\taliases={','.join(e.get('aliases', []))}")
    return 0


def cmd_revoke(a) -> int:
    with _locked(a.keys_file):
        return _revoke_locked(a)


def _revoke_locked(a) -> int:
    data = _load(a.keys_file)
    if a.all:
        n = len(data["keys"])
        data["keys"] = []
    else:
        before = len(data["keys"])
        data["keys"] = [e for e in data["keys"] if e.get("name") != a.name]
        n = before - len(data["keys"])
        if n == 0:
            print(f"no key named {a.name!r}", file=sys.stderr)
            return 1
    _save(a.keys_file, data)
    print(f"revoked {n} key(s)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="router-keys")
    ap.add_argument("--keys-file", default=DEFAULT_KEYS_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--aliases", required=True)
    p.add_argument("--ttl-hours", type=float, required=True)
    p.add_argument("--out", required=True)
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--name")
    g.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    return {"add": cmd_add, "list": cmd_list, "revoke": cmd_revoke}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
