import dataclasses
import json
import os
import subprocess
import threading

from scripts.delegate import overlay

_TOKEN_KEYS = ("input", "output", "reasoning", "cache")


@dataclasses.dataclass
class RunResult:
    exit_code: int
    text: str
    tokens: dict
    rejected: bool
    stderr_tail: str


def _opencode_cmd(cfg, dest, prompt):
    return [cfg.opencode_exe, "run", "--agent", "delegate", "--dir", dest,
            "--format", "json", prompt]


def _child_env(cfg):
    # Functional (PATH/SystemRoot kept so node/opencode start) but scrubbed of
    # ambient git/ssh credentials; overlay supplies isolation home + router token.
    env = dict(os.environ)
    for k in list(env):
        if k in ("GH_TOKEN", "GITHUB_TOKEN") or k.startswith("SSH"):
            env.pop(k, None)
    env.update(overlay.opencode_env(cfg))
    return env


def _kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def _apply_event(evt, texts, tok):
    kind = evt.get("type")
    part = evt.get("part") or {}
    if kind == "text":
        texts.append(evt.get("text") or part.get("text") or "")
    elif kind == "step_finish":
        for k in _TOKEN_KEYS:
            tok[k] += int((part.get("tokens") or {}).get(k, 0) or 0)
    return kind == "rejected"


def _drain(stream, sink):
    for chunk in stream:
        sink.append(chunk)


def run_opencode(cfg, dest, prompt, *, spawn=subprocess.Popen, timeout_s=None) -> RunResult:
    proc = spawn(_opencode_cmd(cfg, dest, prompt),
                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, env=_child_env(cfg))
    texts, tok, err_chunks = [], dict.fromkeys(_TOKEN_KEYS, 0), []
    timed_out = threading.Event()

    def _reap_timeout():
        timed_out.set()
        _kill_tree(proc.pid)

    err_thread = threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True)
    err_thread.start()
    timer = threading.Timer(timeout_s, _reap_timeout) if timeout_s else None
    if timer:
        timer.start()
    rejected = False
    try:
        for line in proc.stdout:
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict):
                rejected = _apply_event(evt, texts, tok) or rejected
        proc.wait()
    finally:
        if timer:
            timer.cancel()
        if proc.poll() is None:  # an exception left it running: don't leak a GPU holder
            _kill_tree(proc.pid)
    err_thread.join(timeout=5)
    stderr = "".join(err_chunks)
    code = proc.returncode
    if timed_out.is_set() and code == 0:
        code = -1
    return RunResult(exit_code=code, text="".join(texts), tokens=tok,
                     rejected=rejected or "auto-rejecting" in stderr,
                     stderr_tail=stderr[-2000:])
