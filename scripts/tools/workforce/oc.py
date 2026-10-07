"""Run `opencode run` headless and read what it did (workforce spec §6).

Footguns, each found by running (opencode 1.18.34):
  * a non-TTY stdin makes `opencode run` wait for EOF forever -> stdin=DEVNULL;
  * `--file` is an array option: anything after it is taken as another file, so the message goes
    BEFORE `--file`;
  * an attached file reaches the model through the read tool, capped at 2000 lines (review.py
    keeps packets under that);
  * a timeout must kill the whole process tree (opencode spawns bash, test runs, servers). On the
    VM, SystemdScopeLauncher runs each opencode as the unprivileged agent user in its own transient
    scope, so the kill reaches processes an agent detached with setsid/nohup too, and the agent user
    cannot read the harness's bundles (hidden tests) or baseline git directories.
"""
import json
import os
import signal
import subprocess
import time
import uuid

import profiles
from dataclasses import dataclass
from typing import Optional


@dataclass
class RunResult:
    rc: Optional[int]
    timed_out: bool
    session_id: Optional[str]
    text: str
    wall_s: float


def parse_events(stdout):
    """(session_id, final_text) from `--format json` output. final_text is the text emitted after the
    last tool call -- the agent's closing answer."""
    session, texts = None, []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        session = session or ev.get("sessionID")
        if ev.get("type") == "tool_use":
            texts = []
        elif ev.get("type") == "text":
            texts.append((ev.get("part") or {}).get("text") or "")
    return session, "\n".join(t for t in texts if t)


def pid_alive(pid):
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


class DirectLauncher:
    """Runs opencode as the harness's own user in a new process group (tests, workstation). The
    role is ignored: there is only one user."""

    def argv(self, argv, name, role=None):
        return argv

    def popen_kwargs(self):
        return {"start_new_session": os.name != "nt"}

    def kill(self, name, proc):
        if proc.poll() is None:
            _kill_tree(proc)
        elif os.name != "nt":                              # reap anything left in the group
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class SystemdScopeLauncher:
    """Runs opencode as the OS user `<prefix>-<role>` (one user per implementer slot and one for the
    lead) in a transient systemd scope named `name`; kill = the whole cgroup."""

    def __init__(self, prefix, memory_max="8G", tasks_max=512, run=None):
        self.prefix, self.memory_max, self.tasks_max = prefix, memory_max, tasks_max
        self.run = run or (lambda argv: subprocess.run(argv, capture_output=True))

    def user(self, role):
        if not role:
            raise ValueError("every agent run needs a role")
        return f"{self.prefix}-{role}"

    def argv(self, argv, name, role=None):
        user = self.user(role)
        return ["systemd-run", "--scope", "--quiet", f"--unit={name}", f"--uid={user}",
                f"--gid={user}", "-p", f"MemoryMax={self.memory_max}", "-p", f"TasksMax={self.tasks_max}",
                "--", *argv]

    def popen_kwargs(self):
        return {}

    def kill(self, name, proc):
        self.run(["systemctl", "kill", "--signal=SIGKILL", f"{name}.scope"])
        self.run(["systemctl", "stop", f"{name}.scope"])
        if proc is not None:
            proc.wait()


class Opencode:
    def __init__(self, cmd, env, launcher=None):
        self.cmd, self.env = list(cmd), env
        self.launcher = launcher or DirectLauncher()

    def run(self, agent, workdir, message, timeout_s, events_path, session=None, attach=None, home=None,
            user=None):
        """home: this task's opencode home for the role (sessions resume from it); user: the role whose
        OS user the launcher runs opencode as."""
        env = profiles.with_home(self.env, home) if home else self.env
        argv = [*self.cmd, "run", "--pure", "--agent", agent, "--dir", workdir, "--format", "json"]
        if session:
            argv += ["--session", session]
        argv.append(message)
        if attach:
            argv += ["--file", attach]
        name = f"wf-{uuid.uuid4().hex[:12]}"
        t0 = time.monotonic()
        with open(events_path, "w", encoding="utf-8") as out, \
                open(events_path + ".stderr", "w", encoding="utf-8") as err:
            proc = subprocess.Popen(self.launcher.argv(argv, name, user), env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, **self.launcher.popen_kwargs())
            try:
                rc, timed_out = proc.wait(timeout=timeout_s), False
            except subprocess.TimeoutExpired:
                rc, timed_out = None, True
            self.launcher.kill(name, proc)                 # timeout, or leftovers after a normal exit
        with open(events_path, encoding="utf-8", errors="replace") as f:
            sid, text = parse_events(f.read())
        return RunResult(rc, timed_out, sid, text, time.monotonic() - t0)

    def export(self, session_id, timeout=120, home=None):
        env = profiles.with_home(self.env, home) if home else self.env
        r = subprocess.run([*self.cmd, "export", session_id], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout, check=True)
        return json.loads(r.stdout[r.stdout.index("{"):])
