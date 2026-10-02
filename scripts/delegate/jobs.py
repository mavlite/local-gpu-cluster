"""Single-worker job queue: one GPU lease held for the whole job, JSON state per job."""
import ctypes
import json
import os
import queue
import threading
import time
from types import SimpleNamespace

from scripts.delegate import checks, gitstore, ids, overlay, resultgate, runner
from scripts.delegate.lease import GpuLease
from scripts.delegate.ledger import Ledger

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_START_TOLERANCE_S = 2.0


# --- process liveness ------------------------------------------------------

def _open_process(pid: int):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    return k32, k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))


def proc_start(pid: int):
    """Process create time (epoch seconds) or None if unobtainable/dead.

    Windows only (GetProcessTimes). Elsewhere None, and liveness falls back to
    "a process with this PID exists" -- which cannot detect PID reuse.
    """
    if os.name != "nt":
        return None
    k32, h = _open_process(pid)
    if not h:
        return None
    try:
        c, e, k, u = (ctypes.c_ulonglong() for _ in range(4))
        ok = k32.GetProcessTimes(ctypes.c_void_p(h), ctypes.byref(c), ctypes.byref(e),
                                 ctypes.byref(k), ctypes.byref(u))
        if not ok:
            return None
        return c.value / 1e7 - 11644473600  # FILETIME (1601, 100ns) -> epoch s
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        k32, h = _open_process(pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
                return True  # exists but unqueryable: treat as alive (never abandon wrongly)
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def owner_alive(pid, started) -> bool:
    if not isinstance(pid, int) or pid <= 0 or not _pid_exists(pid):
        return False
    now = proc_start(pid)
    if started and now is not None:
        return abs(now - started) <= _START_TOLERANCE_S  # mismatch => PID reused
    return True  # start time unobtainable: PID-exists fallback (PID-reuse caveat)


# --- pipeline --------------------------------------------------------------

def render_prompt(spec: dict) -> str:
    parts = [spec["task"].strip(), ""]
    if spec.get("checks"):
        parts.append("These checks must pass: "
                     + "; ".join(" ".join(c) for c in spec["checks"]))
    parts.append("Work only in this directory.")
    parts.append("Finish with a SUMMARY section describing what you changed.")
    return "\n".join(parts)


def real_deps(cfg):
    return SimpleNamespace(lease=GpuLease(cfg.lease_path), gitstore=gitstore,
                           overlay=overlay, runner=runner, checks=checks,
                           resultgate=resultgate, ledger=Ledger(cfg.ledger_path))


def _prepare(cfg, d, spec, job_dir):
    repo = d.gitstore.validate_repo(cfg, spec["repo"])
    ref = d.gitstore.resolve_ref(repo, spec.get("base_ref", "HEAD"))
    work, gitdir = os.path.join(job_dir, "work"), os.path.join(job_dir, "gitdir")
    os.makedirs(work, exist_ok=True)
    os.makedirs(gitdir, exist_ok=True)
    d.gitstore.export(repo, ref, work, gitdir)
    d.overlay.strip_project_config(work)
    d.overlay.install_overlay(cfg, work, spec.get("allow_web", False))
    d.gitstore.commit_work(gitdir, work)  # base includes strip+overlay
    return work, gitdir, d.gitstore.base_sha(gitdir)


def _execute(cfg, d, spec, job_dir) -> dict:
    work, gitdir, base = _prepare(cfg, d, spec, job_dir)
    res = d.runner.run_opencode(cfg, work, render_prompt(spec),
                                timeout_s=spec.get("timeout_s", cfg.job_timeout_s))
    d.gitstore.commit_work(gitdir, work)
    gate = d.resultgate.inspect_diff(gitdir, work, base)
    diff = d.gitstore.extract_patch(gitdir, base, work)
    results, skipped = [], None
    if spec.get("checks"):
        if gate.rejected:
            skipped = "skipped: gate rejected"
        elif res.rejected:
            skipped = "skipped: run rejected"
        elif res.exit_code != 0:
            skipped = "skipped: run failed"
        else:
            allow = d.checks.load_allowlist(os.path.join(cfg.overlay_dir, "allowlist.toml"))
            results = d.checks.run_checks(work, spec["checks"], allowlist=allow)
    bad = res.exit_code != 0 or res.rejected or gate.rejected
    return {"status": "failed" if bad else "done", "base": base, "diff": diff,
            "exit_code": res.exit_code, "text": res.text, "tokens": res.tokens,
            "rejected": res.rejected, "stderr_tail": res.stderr_tail,
            "gate": {"rejected": gate.rejected, "reasons": gate.reasons,
                     "flagged": gate.flagged},
            "checks": [{"argv": c.argv, "exit_code": c.exit_code, "output": c.output}
                       for c in results],
            "checks_skipped": skipped}


# --- store -----------------------------------------------------------------

class JobStore:
    def __init__(self, cfg, deps):
        self.cfg, self.deps = cfg, deps
        self._q = queue.Queue()
        self._io = threading.Lock()
        self._worker = None
        self._worker_lock = threading.Lock()
        self._worker_starts = 0  # observable for the single-worker test
        os.makedirs(cfg.jobs_dir, exist_ok=True)

    def _path(self, jid):
        return os.path.join(self.cfg.jobs_dir, f"{jid}.json")

    def _save(self, state):
        with self._io:
            tmp = self._path(state["id"]) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self._path(state["id"]))

    def get(self, jid) -> dict:
        with self._io, open(self._path(jid), encoding="utf-8") as f:
            return json.load(f)

    def list(self, status=None) -> list:
        out = []
        for name in sorted(os.listdir(self.cfg.jobs_dir)):
            if name.endswith(".json"):
                try:
                    st = self.get(name[:-5])
                except ValueError:
                    continue  # corrupt job file: skip, don't break listing
                if status is None or st.get("status") == status:
                    out.append(st)
        return out

    def submit(self, spec: dict) -> str:
        jid = ids.new_id()
        self._save({"id": jid, "status": "queued", "spec": dict(spec),
                    "submitted": time.time()})
        self._q.put(jid)
        self._ensure_worker()
        return jid

    def join(self):
        self._q.join()

    def _ensure_worker(self):
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True)
                self._worker_starts += 1
                self._worker.start()

    def _loop(self):
        while True:
            jid = self._q.get()
            try:
                self._run_job(jid)
            except Exception as e:  # keep the worker alive; record on the job
                self._fail(jid, e)
            finally:
                self._q.task_done()

    def _fail(self, jid, exc):
        try:
            st = self.get(jid)
            self._save({**st, "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}", "finished": time.time()})
        except Exception:  # state itself unwritable; nothing more can be persisted
            pass

    def _run_job(self, jid):
        st = self.get(jid)
        if st.get("status") != "queued":
            return  # already run/finished (e.g. reconcile re-enqueue)
        if not self.deps.lease.acquire(timeout_s=self.cfg.job_timeout_s):
            self._save({**st, "status": "failed", "error": "GPU lease busy"})
            return
        try:
            st = {**st, "status": "running", "owner_pid": os.getpid(),
                  "owner_started": proc_start(os.getpid()), "started": time.time()}
            self._save(st)
            try:
                out = _execute(self.cfg, self.deps, st["spec"],
                               os.path.join(self.cfg.jobs_dir, jid))
            except Exception as e:  # recorded on the job, never swallowed silently
                out = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
            final = {**st, **out, "finished": time.time()}
            self._save(final)
            try:
                self._ledger(final)
            except Exception:  # ledger failure must not lose the saved result
                pass
        finally:
            self.deps.lease.release()

    def _ledger(self, st):
        led = getattr(self.deps, "ledger", None)
        if led is not None:
            led.append({"job": st["id"], "status": st["status"],
                        "tokens": st.get("tokens")})

    def reconcile(self) -> None:
        # NOTE: past-deadline "interrupted" status is deferred.
        for st in self.list("queued"):  # leftovers from a previous process
            self._q.put(st["id"])
            self._ensure_worker()
        for st in self.list("running"):
            if not owner_alive(st.get("owner_pid"), st.get("owner_started")):
                self._save({**st, "status": "abandoned"})


# --- test helpers (also imported by Task 12 tests) ---------------------------

class MemLedger:
    def __init__(self):
        self.records = []

    @property
    def rows(self) -> list:
        return self.records

    def append(self, record: dict) -> None:
        self.records.append(dict(record))

    def read_all(self) -> list:
        return list(self.records)


class _TestDeps:
    """Fakes for lease + pipeline collaborators; records calls, tracks lease overlap."""

    def __init__(self, sleep=0, exit_code=0, gate_rejected=False, raise_in=None):
        self.sleep, self.raise_in = sleep, raise_in
        self.lease_held = self.max_concurrent = self.acquired = 0
        self.calls = []
        self.lease = self
        self.ledger = MemLedger()
        rec = self._rec
        self.gitstore = SimpleNamespace(
            validate_repo=lambda cfg, repo: rec("validate_repo") or repo,
            resolve_ref=lambda repo, ref: rec("resolve_ref") or ref,
            export=lambda *a: rec("export", *a),
            base_sha=lambda g: rec("base_sha", g) or "BASE",
            commit_work=lambda g, w: rec("commit_work", g, w),
            extract_patch=lambda g, b, w: rec("extract_patch", g, b, w) or "DIFF")
        self.overlay = SimpleNamespace(
            strip_project_config=lambda w: rec("strip", w),
            install_overlay=lambda cfg, w, web: rec("overlay", w))
        self.runner = SimpleNamespace(
            run_opencode=lambda cfg, w, p, **kw: self._run(exit_code, w))
        self.resultgate = SimpleNamespace(
            inspect_diff=lambda g, w, b: rec("inspect_diff", g, w, b) or SimpleNamespace(
                rejected=gate_rejected, reasons=["r"] if gate_rejected else [], flagged=[]))
        self.checks = SimpleNamespace(
            load_allowlist=lambda p: [["pytest"]],
            run_checks=lambda w, c, **kw: rec("run_checks", w) or [
                SimpleNamespace(argv=x, exit_code=0, output="ok") for x in c])

    def _rec(self, name, *args):
        if self.raise_in == name:
            raise RuntimeError("boom")
        self.calls.append((name,) + args)

    def _run(self, exit_code, work):
        self._rec("run_opencode", work)
        time.sleep(self.sleep)  # hold the lease long enough to expose overlap
        return SimpleNamespace(exit_code=exit_code, text="SUMMARY", tokens={"output": 5},
                               rejected=False, stderr_tail="")

    def acquire(self, timeout_s=0):
        self.lease_held += 1
        self.acquired += 1
        self.max_concurrent = max(self.max_concurrent, self.lease_held)
        return True

    def release(self):
        self.lease_held -= 1


def make_test_deps(sleep=0, **kw):
    return _TestDeps(sleep=sleep, **kw)
