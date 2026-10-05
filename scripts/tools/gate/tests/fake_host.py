"""Fake pct / systemctl / curl for exercising gate_env.sh without a cluster.

World state lives in $FAKE_WORLD (a JSON file): unit states per container ("host" for the
Proxmox host itself), router.env text, chat capacity. Every invocation is appended to
$FAKE_WORLD.log so a failing test can show the order of operations.
"""
import json
import os
import re
import sys

W = os.environ["FAKE_WORLD"]


def load():
    with open(W) as f:
        return json.load(f)


def save(w):
    with open(W, "w") as f:
        json.dump(w, f)


def systemctl(w, ct, args):
    verb, units = args[0], args[1:]
    st = w["units"].setdefault(ct, {})
    if verb == "is-active":
        state = st.get(units[0], "inactive")
        print(state)
        return 0 if state == "active" else 3
    if verb in ("start", "stop", "restart"):
        for u in units:
            u = u if "." in u else u + ".service"
            st[u] = "inactive" if verb == "stop" else "active"
    return 0


def pct(w, args):
    assert args[0] == "exec" and args[2] == "--", args
    ct, cmd = args[1], args[3:]
    if cmd[0] == "systemctl":
        return systemctl(w, ct, cmd[1:])
    if cmd[0] == "grep":
        m = [ln for ln in w["router_env"].splitlines() if ln.startswith("RATE_LIMIT_CHAT=")]
        print(m[0] if m else "")
        return 0 if m else 1
    if cmd[0] == "sed":
        expr = cmd[2]
        if expr.startswith("s|"):
            _, pat, rep, _ = expr.split("|")
            w["router_env"] = re.sub("(?m)" + pat, rep, w["router_env"])
        elif expr.endswith("/d"):
            w["router_env"] = "".join(ln + "\n" for ln in w["router_env"].splitlines()
                                      if not ln.startswith("RATE_LIMIT_CHAT="))
        return 0
    if cmd[0] == "sh":
        w["router_env"] += cmd[2].split("echo ", 1)[1].split(" >>")[0] + "\n"
        return 0
    return 0


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    with open(W + ".log", "a") as f:
        f.write(" ".join([tool] + args) + "\n")
    w = load()
    if tool == "systemctl":
        rc = systemctl(w, "host", args)
    elif tool == "curl":
        print(json.dumps({"ok": True, "active_chat_profile": "qwen3.8",
                          "chat_admission": {"capacity": w["capacity"], "in_use": 0}}))
        rc = 0
    elif tool == "pct":
        rc = pct(w, args)
    elif tool == "setcap":                       # used by the fake redteam scripts
        w["capacity"] = int(args[0])
        rc = 0
    else:
        raise SystemExit(f"fake_host: unknown tool {tool}")
    save(w)
    return rc


if __name__ == "__main__":
    sys.exit(main())
