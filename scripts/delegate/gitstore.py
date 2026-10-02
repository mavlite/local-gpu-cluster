import io
import os
import subprocess
import tarfile
import tempfile


class RepoNotAllowed(Exception):
    pass


class BadRef(Exception):
    pass


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True, env=env).stdout


def validate_repo(cfg, repo: str) -> str:
    real = os.path.realpath(repo)
    roots = [os.path.realpath(r) for r in cfg.allowed_repo_roots]
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
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


def _worktree_tree(repo: str) -> str:
    """Write a tree of HEAD + tracked edits + untracked files via a temp index."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(td, "index")}
        head = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "read-tree", head, env=env)
        _git(repo, "add", "-A", env=env)
        return _git(repo, "write-tree", env=env).strip()


def _archive_tree(repo: str, treeish: str, dest: str) -> None:
    tar = subprocess.run(["git", "-C", repo, "archive", "--format=tar", treeish],
                         check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(dest, filter="data")


def _init_fresh_git(dest: str) -> None:
    def g(*a):
        subprocess.run(["git", "-C", dest, *a], check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "delegate@local")
    g("config", "user.name", "delegate")
    g("add", "-A")
    g("commit", "-qm", "base")


def export(repo: str, ref: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    treeish = _worktree_tree(repo) if ref == "WORKTREE" else ref
    _archive_tree(repo, treeish, dest)
    _init_fresh_git(dest)


def base_sha(dest: str) -> str:
    return _git(dest, "rev-parse", "HEAD").strip()


def commit_work(dest: str) -> None:
    """Commit the agent's edits atop the base commit; no-op if nothing changed."""
    if not _git(dest, "status", "--porcelain").strip():
        return
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "work")


def extract_patch(dest: str, base: str) -> str:
    return _git(dest, "diff", f"{base}..HEAD")
