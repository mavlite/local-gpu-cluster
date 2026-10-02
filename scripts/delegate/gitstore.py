import atexit
import os
import shutil
import subprocess
import tempfile


class RepoNotAllowed(Exception):
    pass


class BadRef(Exception):
    pass


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True, env=env).stdout


def _within(path: str, root: str) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    r = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


def validate_repo(cfg, repo: str) -> str:
    real = os.path.realpath(repo)
    if not any(_within(real, r) for r in cfg.allowed_repo_roots):
        raise RepoNotAllowed(repo)
    if not os.path.isdir(os.path.join(real, ".git")):
        raise RepoNotAllowed(f"{repo} is not a git repo")
    return real


def resolve_ref(repo: str, base_ref: str) -> str:
    if base_ref == "WORKTREE":
        return "WORKTREE"
    if base_ref.startswith("-") or ".." in base_ref:
        raise BadRef(base_ref)
    return _git(repo, "rev-parse", "--verify", "--end-of-options",
                f"{base_ref}^{{commit}}").strip()


_EMPTY_HOOKS = tempfile.mkdtemp(prefix="delegate-nohooks-")
atexit.register(shutil.rmtree, _EMPTY_HOOKS, ignore_errors=True)


def _dgit(git_dir, work_dir, *args):
    """Run git with metadata in git_dir (never inside the agent-writable work_dir),
    and hooks/fsmonitor/signing/global config neutralized as defense in depth."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    cmd = ["git", "-c", f"core.hooksPath={_EMPTY_HOOKS}", "-c", "core.fsmonitor=false",
           "-c", "commit.gpgsign=false", f"--git-dir={git_dir}", f"--work-tree={work_dir}", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env).stdout


def _populate(repo: str, ref: str, dest: str) -> None:
    """Fill dest with tracked files via a temp index + checkout-index (no git archive,
    so .gitattributes export-ignore/export-subst cannot drop or rewrite files)."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(td, "index")}
        if ref == "WORKTREE":
            _git(repo, "read-tree", "--end-of-options", _git(repo, "rev-parse", "HEAD").strip(), env=env)
            _git(repo, "add", "-A", env=env)
        else:
            _git(repo, "read-tree", "--end-of-options", ref, env=env)
        _git(repo, f"--work-tree={os.path.abspath(dest)}", "checkout-index", "-a", "-f", env=env)


def _init_fresh_git(git_dir: str, work_dir: str) -> None:
    _dgit(git_dir, work_dir, "init", "-q")
    _dgit(git_dir, work_dir, "config", "core.bare", "false")
    _dgit(git_dir, work_dir, "config", "user.email", "delegate@local")
    _dgit(git_dir, work_dir, "config", "user.name", "delegate")
    _dgit(git_dir, work_dir, "add", "-A")
    _dgit(git_dir, work_dir, "commit", "-q", "--no-verify", "-m", "base")


def export(repo: str, ref: str, work_dir: str, git_dir: str) -> None:
    """work_dir gets tracked files only (no .git); git metadata lives in git_dir."""
    work_dir, git_dir = os.path.abspath(work_dir), os.path.abspath(git_dir)
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(git_dir, exist_ok=True)
    _populate(repo, ref, work_dir)
    _init_fresh_git(git_dir, work_dir)


def base_sha(git_dir: str) -> str:
    return _dgit(git_dir, os.devnull, "rev-parse", "HEAD").strip()


def commit_work(git_dir: str, work_dir: str) -> None:
    """Commit the agent's edits atop the base commit; no-op if nothing changed."""
    if not _dgit(git_dir, work_dir, "status", "--porcelain").strip():
        return
    _dgit(git_dir, work_dir, "add", "-A")
    _dgit(git_dir, work_dir, "commit", "-q", "--no-verify", "-m", "work")


def extract_patch(git_dir: str, base: str, work_dir: str) -> str:
    return _dgit(git_dir, work_dir, "diff", "--no-ext-diff", "--no-textconv", f"{base}..HEAD")
