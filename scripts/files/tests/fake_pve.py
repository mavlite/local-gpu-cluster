"""Fake pct / systemctl / redteam layout scripts for exercising wf-window.sh without a cluster.

World state is a JSON file at $FAKE_WORLD: unit states and invocation ids per place ("host", "151",
"153"), the chat capacity, the redteam state file path. Each call is appended to $FAKE_WORLD.log.
Invoked as `python fake_pve.py <tool> <args...>` by tiny shell wrappers named after each tool.
"""
import json
import os
import sys
import uuid

W = os.environ["FAKE_WORLD"]


def load():
    with open(W) as f:
        return json.load(f)


def save(w):
    with open(W, "w") as f:
        json.dump(w, f)


def unit_name(u):
    return u if "." in u else u + ".service"


def systemctl(w, place, args):
    verb = args[0]
    units = w["units"].setdefault(place, {})
    ids = w["invocation"].setdefault(place, {})
    if verb == "is-active":
        state = units.get(unit_name(args[1]), "inactive")
        print(state)
        return 0 if state == "active" else 3
    if verb == "show":                                   # show -p InvocationID --value <unit>
        print(ids.get(unit_name(args[-1]), ""))
        return 0
    if verb in ("start", "stop", "restart"):
        for u in (unit_name(a) for a in args[1:] if not a.startswith("-")):
            if w.get("fail_start") == u and verb != "stop":
                print(f"Job for {u} failed", file=sys.stderr)
                return 1
            was = units.get(u)
            units[u] = "inactive" if verb == "stop" else "active"
            if verb == "restart" or (verb == "start" and was != "active"):
                ids[u] = uuid.uuid4().hex
    return 0


def healthz(w):
    amd = w["units"].get("151", {})
    up = {"chat": "ok",
          "embed": "ok" if amd.get("llamacpp-embed.service") == "active" else "down",
          "rerank": "ok" if amd.get("llamacpp-rerank.service") == "active" else "down"}
    print(json.dumps({"ok": True, "upstream": up, "active_chat_profile": "qwen3.8",
                      "chat_admission": {"capacity": w["capacity"], "in_use": 0}}))
    return 0


def pct(w, args):
    assert args[0] == "exec" and args[2] == "--", args
    ct, cmd = args[1], args[3:]
    if cmd[0] == "systemctl":
        return systemctl(w, ct, cmd[1:])
    if cmd[0] == "curl":
        return healthz(w)
    if cmd[0] == "cat" and cmd[1].endswith("boot_id"):
        print(w["boot_id"])
        return 0
    return 0


def redteam(w, which):
    if w.get("fail_enter") and which == "enter":
        return 1
    amd = w["units"].setdefault("151", {})
    host = w["units"].setdefault("host", {})
    if which == "enter":                                 # the real enter: 3 x 128K, RAG unloaded
        w["capacity"] = 3
        amd["llamacpp-embed.service"] = amd["llamacpp-rerank.service"] = "inactive"
        host["redteam-mode-watch.service"] = host["redteam-mode-idle.timer"] = "active"
        amd["llamacpp-fast.service"] = "active"
        with open(w["rt_state"], "w") as f:
            f.write("active\n")
    else:                                                # the real exit: 1 slot, RAG and timer back
        w["capacity"] = 1
        amd["llamacpp-embed.service"] = amd["llamacpp-rerank.service"] = "active"
        amd["llamacpp-chat-restart.timer"] = "active"
        amd["llamacpp-fast.service"] = "inactive"
        if os.path.exists(w["rt_state"]):
            os.remove(w["rt_state"])
    w["invocation"].setdefault("151", {})["llamacpp-chat.service"] = uuid.uuid4().hex
    return 0


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    w = load()
    with open(W + ".log", "a") as f:
        f.write(" ".join([tool, *args]) + "\n")
    if tool == "pct":
        rc = pct(w, args)
    elif tool == "systemctl":
        rc = systemctl(w, "host", args)
    elif tool in ("redteam-mode-enter.sh", "redteam-mode-exit.sh"):
        rc = redteam(w, "enter" if "enter" in tool else "exit")
    elif tool == "cat":
        print(w["boot_id"])
        rc = 0
    else:
        rc = 0
    save(w)
    return rc


if __name__ == "__main__":
    sys.exit(main())
