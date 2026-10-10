"""Workforce harness CLI (workforce spec §5-§9). Every subcommand prints JSON.

Workstation:  bundle-build, bundle-validate, manifest, import, w1, w3, run-meta, w3-schedule, scan,
              recover-patches
Sandbox VM:   run, replay-reviews
Keys come from the environment only (WF_ROUTER_KEY: the per-run scoped router key; WF_WORKER_KEY:
the throwaway worker key) and are never printed.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import analysis
import bundle
import grade
import oc
import paths
import pipeline
import profiles
import replay
import runmeta
import scan


def resolve_opencode(env):
    """The real opencode binary (not the npm shim, which survives a timeout kill), or None."""
    if env.get("WF_OPENCODE"):
        return env["WF_OPENCODE"]
    if env.get("APPDATA"):
        exe = os.path.join(env["APPDATA"], "npm", "node_modules", "opencode-ai", "bin", "opencode.exe")
        if os.path.isfile(exe):
            return exe
    return shutil.which("opencode")


def build_opencode(arm, oc_dir, router_url, worker_urls, oc_cmd, base_env, launcher=None):
    """Write the read-only config under oc_dir/cfg and return an Opencode bound to the locked env.
    Every run passes its own per-task, per-role home; oc_dir/home is only the fallback."""
    cfg = profiles.install(os.path.join(oc_dir, "cfg"), profiles.build_config(arm, router_url, worker_urls))
    home = os.path.join(oc_dir, "home")
    os.makedirs(home, exist_ok=True)
    return oc.Opencode(oc_cmd, profiles.opencode_env(base_env, home, cfg, keys=profiles.keys_for(arm)),
                       launcher=launcher)


def assert_private(path):
    """Refuse to run agents as another user if they could read `path` (bundles hold hidden tests)."""
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"{path} is mode {oct(mode)}; bundles must be 0700 to the harness user")


def role_user(prefix, role):
    """OS user for a role: one per implementer slot and one for the lead; None is the harness (root)."""
    return "root" if role is None else f"{prefix}-{role}"


def chown_tree(prefix):
    """own(path, role): hand a file or tree to the role's user, private to it (POSIX, harness as root).
    Symlinks are never followed, so an agent cannot aim a chown at a file outside its tree."""
    import pwd

    def own(path, role):
        pw = pwd.getpwnam(role_user(prefix, role))
        os.chown(path, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
        for dirpath, dirnames, files in os.walk(path):
            for name in dirnames + files:
                os.chown(os.path.join(dirpath, name), pw.pw_uid, pw.pw_gid, follow_symlinks=False)
        if not os.path.islink(path):
            os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)
    return own


_PATCH_PATH_RES = (re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M), re.compile(r"^--- a/(.+)$", re.M),
                   re.compile(r"^\+\+\+ b/(.+)$", re.M), re.compile(r"^(?:rename|copy) (?:from|to) (.+)$", re.M))


def patch_paths(patch_text):
    """Every path a patch names: `diff --git` headers AND the `---`/`+++`/rename/copy lines that
    `git apply` actually follows (security review LOW: they can disagree)."""
    names = set()
    for rx in _PATCH_PATH_RES:
        for m in rx.finditer(patch_text):
            names.update(g for g in m.groups() if g)
    return sorted(names)


def make_grader(spec):
    """'local' or 'docker:<image>'."""
    if spec.startswith("docker:"):
        return grade.DockerRunner(spec.split(":", 1)[1])
    if spec == "local":
        return grade.LocalRunner()
    raise SystemExit(f"--grader must be 'local' or 'docker:<image>', not {spec!r}")


def import_patch(repo, patch_file, bundle_dir, run_id):
    """Check the patch against the task's declared files, apply it to the task's parent commit in a
    throwaway worktree, and create branch workforce/<run>/<task>. The user merges."""
    task = grade.load_task(bundle_dir)
    with open(patch_file, encoding="utf-8") as f:
        text = f.read()
    names = patch_paths(text)
    allowed, dropped = paths.classify([("M", "100644", p) for p in names], task["files"])
    if dropped or not allowed:
        raise ValueError(f"refusing {patch_file}: paths not allowed: {dropped or 'empty patch'}")
    branch = f"workforce/{run_id}/{task['id']}"
    tmp = tempfile.mkdtemp(prefix="wf-import-")
    wt = os.path.join(tmp, "wt")

    def git(*args, cwd=repo):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    git("worktree", "add", "--detach", wt, task["parent"])
    try:
        subprocess.run(["git", "apply", "--check", "--binary", patch_file], cwd=wt, check=True,
                       capture_output=True, text=True)
        git("apply", "--binary", patch_file, cwd=wt)
        git("add", "--", *allowed, cwd=wt)
        git("commit", "-q", "-m", f"workforce({run_id}): {task['id']}", cwd=wt)
        git("branch", branch, git("rev-parse", "HEAD", cwd=wt).strip())
    finally:
        git("worktree", "remove", "--force", wt)
        shutil.rmtree(tmp, ignore_errors=True)
    return {"branch": branch, "paths": allowed}


def _records(run_dir):
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
        return json.load(f)


def w1_stats(run_dirs):
    """Aggregate W1 run directories of one configuration. Invalid runs are excluded, and only real
    attempts count (outcome `implemented`; harness- and infra-errors are not loop-free attempts)."""
    out = {"attempts": 0, "looped": 0, "passed": 0, "wall_s": 0.0, "invalid_runs": 0}
    for d in run_dirs:
        s = _records(d)
        if not s["valid"]:
            out["invalid_runs"] += 1
            continue
        out["attempts"] += s["outcomes"].get("implemented", 0)
        out["looped"] += s["looped"]
        out["passed"] += s["accepted"]
        out["wall_s"] += s["wall_s"]
    return out


def _task_seconds(run_dir, task_ids):
    """{task: wall seconds} from the per-task records that have both timestamps."""
    out = {}
    for tid in task_ids:
        path = os.path.join(run_dir, "tasks", tid, "record.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("t_start") is not None and rec.get("t_end") is not None:
                out[tid] = rec["t_end"] - rec["t_start"]
    return out


def round2_runs(schedule, tasks=None, void=()):
    """w3_runs plus the round-2 fields (spec §5.3): wall_s and review_capped from run.json, gpu_ms
    (and worker_s when a window recorded it) from meta.json, per-task wall seconds from the records."""
    runs = w3_runs(schedule, tasks, void=void)
    for item, r in zip(schedule, runs):
        s = _records(item["run_dir"])
        meta = {}
        path = os.path.join(item["run_dir"], "meta.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                meta = json.load(f)
        r.update({"wall_s": s["wall_s"], "review_capped": s.get("review_capped", 0),
                  "review_timed_out": s.get("review_timed_out", 0),
                  "review_none_after_turn": s.get("review_none_after_turn", 0),
                  "gpu_ms": meta.get("gpu_ms"), "worker_s": meta.get("worker_s"),
                  "task_s": _task_seconds(item["run_dir"], r["accepted"])})
    return runs


def _implementer_accepted(run_dir, task_ids, review=True):
    """Accepted tasks the run's rate counts: implementer-accepted with a review, any accepted without
    one (arm S / --w1 record outcome `implemented`)."""
    n = 0
    for tid in task_ids:
        path = os.path.join(run_dir, "tasks", tid, "record.json")
        if not os.path.isfile(path):
            raise SystemExit(f"{path} missing: cannot recompute throughput without the voided task")
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        n += bool(rec["accepted"] and (rec["outcome"] == "implementer-accepted" or not review))
    return n


def w3_runs(schedule, tasks=None, void=()):
    """schedule: [{"arm", "run_dir", "valid", "probe_p50", "baseline_p50"}] -> analysis input. With
    `tasks`, every run must cover exactly that task set (a mismatch would score every task as a tie).
    `void`: tasks the transcript scan found tainted (spec §8). They leave every run of both arms, and
    accepted-per-hour is recomputed without them from the per-task records."""
    void = set(void)
    if void and (tasks is None or not void <= set(tasks)):
        raise SystemExit(f"cannot void {sorted(void - set(tasks or ()))}: not in the task set")
    runs = []
    for item in schedule:
        s = _records(item["run_dir"])
        if tasks is not None and set(s["accepted_by_task"]) != set(tasks):
            raise SystemExit(f"{item['run_dir']}: its tasks {sorted(s['accepted_by_task'])} do not match "
                             f"the bundle set {sorted(tasks)}")
        accepted = {t: v for t, v in s["accepted_by_task"].items() if t not in void}
        per_hour = s["accepted_per_hour"]
        if void:
            per_hour = _implementer_accepted(item["run_dir"], accepted, s.get("review", True)) / (s["wall_s"] / 3600)
        runs.append({"arm": item["arm"], "valid": bool(item["valid"] and s["valid"]),
                     "accepted": accepted, "accepted_per_hour": per_hour,
                     "probe_p50": item["probe_p50"], "baseline_p50": item["baseline_p50"]})
    return runs


def main(argv=None, env=None):
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(prog="workforce")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bundle-build")
    p.add_argument("--repo", required=True)
    p.add_argument("--taskdef", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--refs", required=True)
    p = sub.add_parser("bundle-validate")
    p.add_argument("--bundles", required=True)
    p.add_argument("--refs", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--grader", default="local", help="'local' or 'docker:<image>' (the VM's grader)")
    p = sub.add_parser("manifest")
    p.add_argument("--bundles", required=True)
    p = sub.add_parser("run")
    p.add_argument("--arm", choices=("T", "G", "S"), required=True,
                   help="T: lead + 3 CPU workers; G: lead + GPU implementer; S: GPU implementer alone, no review")
    p.add_argument("--bundles", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--router", required=True)
    p.add_argument("--worker", action="append", default=[])
    p.add_argument("--w1", action="store_true", help="W1 loop check: implement and grade, no review")
    p.add_argument("--grader", default="local", help="'local' or 'docker:<image>'")
    p.add_argument("--agent-user-prefix",
                   help="run each role as OS user <prefix>-<role> in systemd scopes (the VM; harness as root)")
    p = sub.add_parser("recover-patches", help="write tasks/<id>/impl-r{k}.patch from a round-1 run's review packets")
    p.add_argument("--run", required=True, help="harvested run directory (before 76 push-run)")
    p = sub.add_parser("replay-reviews", help="re-run only the reviewer on a harvested run (round 2 §5.1)")
    p.add_argument("--run", required=True, help="harvested run directory (tasks/<id>/record.json, patches/)")
    p.add_argument("--bundles", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--router", required=True)
    p.add_argument("--grader", default="local", help="'local' or 'docker:<image>' (runs the visible tests)")
    p.add_argument("--agent-user-prefix", help="run the reviewer as <prefix>-lead (needs the docker grader)")
    p.add_argument("--journal", help="llama-server journal captured over the replay: prompt-token totals")
    p = sub.add_parser("import")
    p.add_argument("--repo", required=True)
    p.add_argument("--patch", required=True)
    p.add_argument("--bundle", required=True)
    p.add_argument("--run-id", required=True)
    p = sub.add_parser("w1")
    p.add_argument("--config", action="append", required=True, help="ID=run_dir[,run_dir...]")
    p = sub.add_parser("w3")
    p.add_argument("--schedule", required=True)
    p.add_argument("--tasks", required=True, help="bundle root (defines the task list)")
    p.add_argument("--void", action="append", default=[], help="task the scan found tainted (repeatable)")
    p.add_argument("--round2", action="store_true", help="round-2 three-band decision over arms S, G and T")
    p = sub.add_parser("scan", help="transcript scan of a harvested run: tainted (leak) and suspect tasks")
    p.add_argument("--run", required=True)
    p.add_argument("--bundles", required=True)
    p.add_argument("--refs", required=True)
    p = sub.add_parser("run-meta", help="judge one harvested W3 run; writes <run-dir>/meta.json")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--stamps", required=True, help="wf-window.sh stamp file (before-/after-<label>)")
    p.add_argument("--label", required=True)
    p.add_argument("--probe", required=True, help="wf-user-probe.py output")
    p.add_argument("--journal", required=True, help="llamacpp-chat journal over the run")
    p.add_argument("--fail-s", type=float, default=120.0, help="latency a failed probe counts as")
    p = sub.add_parser("w3-schedule", help="assemble the schedule `w3` reads, in run order")
    p.add_argument("--baseline-probe", required=True, help="probe output from the idle baseline")
    p.add_argument("--run", action="append", required=True,
                   help="ARM=run_dir[@baseline_probe_file], in schedule order (@: that run's own window baseline)")
    p.add_argument("--out", required=True)
    p.add_argument("--fail-s", type=float, default=120.0)
    a = ap.parse_args(argv)

    if a.cmd == "bundle-build":
        out = {"bundle": bundle.build(a.repo, a.taskdef, a.out, a.refs)}
    elif a.cmd == "bundle-validate":
        problems, runner = [], make_grader(a.grader)
        for name in sorted(os.listdir(a.bundles)):
            problems += bundle.validate(os.path.join(a.bundles, name), os.path.join(a.refs, f"{name}.patch"),
                                        a.work, runner)
        out = {"problems": problems, "manifest": bundle.manifest(a.bundles), "grader": a.grader}
    elif a.cmd == "manifest":
        out = {"manifest": bundle.manifest(a.bundles)}
    elif a.cmd == "run":
        if not env.get("WF_ROUTER_KEY") or (a.arm == "T" and not env.get("WF_WORKER_KEY")):
            raise SystemExit("WF_ROUTER_KEY (and WF_WORKER_KEY for arm T) must be set")
        if a.agent_user_prefix and not a.grader.startswith("docker:"):
            raise SystemExit("--agent-user-prefix needs --grader docker:<image>: the local grader would run "
                             "agent code as the harness user (root on the VM)")
        oc_bin = resolve_opencode(env)
        if not oc_bin:
            raise SystemExit("opencode not found; set WF_OPENCODE")
        grader = make_grader(a.grader)
        launcher = own = None
        if a.agent_user_prefix:
            assert_private(a.bundles)
            launcher, own = oc.SystemdScopeLauncher(a.agent_user_prefix), chown_tree(a.agent_user_prefix)
        opencode = build_opencode(a.arm, a.out + ".opencode", a.router, a.worker, [oc_bin], env,
                                  launcher=launcher)
        bundles = [os.path.join(a.bundles, n) for n in sorted(os.listdir(a.bundles))]
        monitor = pipeline.HealthMonitor(a.worker) if a.arm == "T" else None
        out = pipeline.Pipeline(a.arm, bundles, opencode, a.out, grader, review=not a.w1 and a.arm != "S",
                                monitor=monitor, own=own).run()
    elif a.cmd == "recover-patches":
        out = replay.recover_patches(a.run)
    elif a.cmd == "replay-reviews":
        if not env.get("WF_ROUTER_KEY"):
            raise SystemExit("WF_ROUTER_KEY must be set")
        if a.agent_user_prefix and not a.grader.startswith("docker:"):
            raise SystemExit("--agent-user-prefix needs --grader docker:<image>: the local grader would run "
                             "agent code as the harness user (root on the VM)")
        oc_bin = resolve_opencode(env)
        if not oc_bin:
            raise SystemExit("opencode not found; set WF_OPENCODE")
        launcher = own = None
        if a.agent_user_prefix:
            assert_private(a.bundles)
            launcher, own = oc.SystemdScopeLauncher(a.agent_user_prefix), chown_tree(a.agent_user_prefix)
        opencode = build_opencode("G", a.out + ".opencode", a.router, [], [oc_bin], env, launcher=launcher)
        journal = None
        if a.journal:
            with open(a.journal, encoding="utf-8", errors="replace") as f:
                journal = f.read()
        out = replay.replay(a.run, a.bundles, opencode, a.out, make_grader(a.grader), journal_text=journal,
                            own=own)
    elif a.cmd == "import":
        out = import_patch(a.repo, a.patch, a.bundle, a.run_id)
    elif a.cmd == "scan":
        out = scan.scan_run(a.run, a.bundles, a.refs)
    elif a.cmd == "run-meta":
        out = runmeta.run_meta(a.run_dir, a.stamps, a.label, a.probe, a.journal, a.fail_s)
    elif a.cmd == "w3-schedule":
        runs = []
        for spec in a.run:
            arm, rest = spec.split("=", 1)
            if arm not in ("G", "T", "S"):
                raise SystemExit(f"--run {spec!r}: arm must be G, T or S")
            d, _, own = rest.partition("@")
            runs.append((arm, d, own or None))
        out = runmeta.w3_schedule(a.baseline_probe, runs, a.fail_s)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    elif a.cmd == "w1":
        results = {}
        for spec in a.config:
            cid, dirs = spec.split("=", 1)
            results[cid] = w1_stats(dirs.split(","))
        out = analysis.w1_choose(results)
    else:
        with open(a.schedule, encoding="utf-8") as f:
            schedule = json.load(f)
        tasks = sorted(os.listdir(a.tasks))
        kept = [t for t in tasks if t not in set(a.void)]
        if a.round2:
            out = analysis.round2_decide(round2_runs(schedule, tasks, void=a.void), kept)
        else:
            out = analysis.w3_decide(w3_runs(schedule, tasks, void=a.void), kept)
        out["voided"] = sorted(set(a.void))
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
