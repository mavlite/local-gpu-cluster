"""Turn a Phase-1 gate worker task into a commit-based task definition (workforce spec §7, Plan D).

W1 uses the gate's T1, T2 and T4 (docs/superpowers/gate/worker-tasks/). Those are not commits: each
is seed/ (the stub the worker starts from), reference/ (a solution), hidden/ (the gate's tests) and
TASK.md. `bundle-build` builds from a commit, so `synth` writes a two-commit repository -- `seed`,
then `reference + visible tests` -- and the task definition for its second commit. The gate never
gave workers runnable tests (that is why they looped); W1 does, so each task gets authored visible
tests: a small subset of the contract, with the edge cases left to the hidden tests.

    python gate_tasks.py <gate-task-dir> <visible-tests-dir> <task-id> <repo-out> <taskdef-out>
"""
import json
import os
import shutil
import subprocess
import sys

# Fixed identity and dates: the commit ids, hence the bundles and their manifest, are reproducible.
_GIT_ENV = {"GIT_AUTHOR_NAME": "workforce", "GIT_AUTHOR_EMAIL": "workforce@localhost",
            "GIT_COMMITTER_NAME": "workforce", "GIT_COMMITTER_EMAIL": "workforce@localhost",
            "GIT_AUTHOR_DATE": "2026-10-08T00:00:00Z", "GIT_COMMITTER_DATE": "2026-10-08T00:00:00Z",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo, *args):
    env = dict(os.environ, **_GIT_ENV)
    r = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo, check=True, capture_output=True,
                       text=True, env=env)
    return r.stdout.strip()


def _files(d):
    return sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)))


def _copy(src_dir, names, dest_dir):
    for n in names:
        shutil.copyfile(os.path.join(src_dir, n), os.path.join(dest_dir, n))


def synth(task_dir, visible_dir, task_id, repo_out, def_out):
    """Write the repository and the task definition; return the taskdef dict."""
    for p in (repo_out, def_out):
        if os.path.exists(p):
            raise FileExistsError(p)
    seed, ref, hidden = (os.path.join(task_dir, d) for d in ("seed", "reference", "hidden"))
    visible, hidden_names = _files(visible_dir), _files(hidden)
    clash = set(visible) & set(hidden_names)
    if clash:
        raise ValueError(f"visible test {sorted(clash)[0]} shadows the hidden test of the same name")
    os.makedirs(repo_out)
    _git(repo_out, "init", "-q", "-b", "main")
    _copy(seed, _files(seed), repo_out)
    _git(repo_out, "add", "-A")
    _git(repo_out, "commit", "-q", "-m", "seed")
    files = _files(ref)
    _copy(ref, files, repo_out)
    _copy(visible_dir, visible, repo_out)
    _git(repo_out, "add", "-A")
    _git(repo_out, "commit", "-q", "-m", "reference + visible tests")
    stem = task_id.replace("-", "_").replace(".", "_")
    td = {"id": task_id, "commit": _git(repo_out, "rev-parse", "HEAD"), "files": files, "tests": visible,
          "hidden": {h: f"test_{stem}_{h[5:] if h.startswith('test_') else h}" for h in hidden_names},
          "needs_conftest": False, "timeout_s": 120}
    os.makedirs(os.path.join(def_out, "hidden"))
    _copy(hidden, hidden_names, os.path.join(def_out, "hidden"))
    shutil.copyfile(os.path.join(task_dir, "TASK.md"), os.path.join(def_out, "request.md"))
    with open(os.path.join(def_out, "taskdef.json"), "w", encoding="utf-8") as f:
        json.dump(td, f, indent=1)
    return td


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    print(json.dumps(synth(*sys.argv[1:]), indent=1))
