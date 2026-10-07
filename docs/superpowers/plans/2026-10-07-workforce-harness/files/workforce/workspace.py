"""One task's workspace (workforce spec §5.1, §5.5).

The snapshot is unpacked into `root`; the baseline is committed to a git directory OUTSIDE the
workspace, so nothing an agent does inside the workspace (its own `.git`, a `.gitignore`, hooks)
can change what the harness sees as the agent's changes.
"""
import os
import shutil
import subprocess
import tarfile

_ID = ["-c", "user.name=workforce", "-c", "user.email=workforce@localhost", "-c", "commit.gpgsign=false",
       "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotepath=false",
       "-c", "safe.directory=*"]          # on the VM the workspace belongs to the agent user


class PatchError(RuntimeError):
    """A patch does not apply."""


def _git_env(ceiling=None):
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    if ceiling:
        env["GIT_CEILING_DIRECTORIES"] = ceiling       # never discover an enclosing repository
    return env


def unpack(tar_path, dest):
    """Extract a snapshot tar with the stdlib 'data' filter: no absolute paths, no `..`, no links
    leaving the tree, no device files."""
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(tar_path) as t:
        t.extractall(dest, filter="data")


def apply_patch(root, patch_text):
    """Apply a `git diff --binary` patch to a plain directory tree."""
    if not patch_text:
        return
    r = subprocess.run(["git", *_ID, "apply", "--binary", "--whitespace=nowarn", "-"], cwd=root,
                       input=patch_text.encode(), capture_output=True,
                       env=_git_env(os.path.dirname(os.path.abspath(root))))
    if r.returncode != 0:
        raise PatchError(r.stderr.decode(errors="replace").strip())


class Workspace:
    def __init__(self, root, git_dir, baseline):
        self.root, self.git_dir, self.baseline = root, git_dir, baseline

    def _git(self, *args, input_bytes=None):
        r = subprocess.run(["git", *_ID, f"--git-dir={self.git_dir}", f"--work-tree={self.root}", *args],
                           input=input_bytes, capture_output=True, env=_git_env(), check=True)
        return r.stdout

    @classmethod
    def materialize(cls, tar_path, root, git_dir):
        unpack(tar_path, root)
        subprocess.run(["git", "init", "-q", "--bare", git_dir], check=True, capture_output=True,
                       env=_git_env())
        ws = cls(root, git_dir, None)
        ws._git("add", "-A", "-f")
        ws._git("commit", "-q", "--allow-empty", "--no-verify", "-m", "baseline")
        ws.baseline = ws._git("rev-parse", "HEAD").decode().strip()
        return ws

    def changes(self):
        """[(status, new_mode, path)] relative to the baseline, ignored files included."""
        self._git("add", "-A", "-f")
        raw = self._git("diff", "--cached", "--raw", "-z", "--no-renames", self.baseline).decode()
        fields = [f for f in raw.split("\0") if f != ""]
        out = []
        for meta, path in zip(fields[0::2], fields[1::2]):
            _old_mode, new_mode, _a, _b, status = meta.lstrip(":").split()
            out.append((status[0], new_mode, path))
        return out

    def patch(self, paths):
        """Binary-safe patch of `paths` against the baseline ("" if there is nothing to export)."""
        if not paths:
            return ""
        self._git("add", "-A", "-f")
        return self._git("diff", "--cached", "--binary", "--no-renames", self.baseline, "--",
                         *paths).decode()

    def copy_to(self, dest):
        shutil.copytree(self.root, dest, symlinks=True)
        return dest
