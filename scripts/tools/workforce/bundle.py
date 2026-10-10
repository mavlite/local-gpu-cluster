"""Build task bundles from repository history (workforce spec §5.1, §8). Runs on the workstation.

A task definition directory holds:
    taskdef.json   {"id", "commit", "files", "tests", "hidden", "needs_conftest", "timeout_s",
                    optional "pytest_args"}  -- files: the declared (non-test) files the commit changed;
                    tests: the commit's test files, given to the implementer as visible tests
    request.md     the issue-style request
    hidden/        hidden tests, never placed in an agent workspace

`build` writes the bundle (see grade.py for its layout) and, separately, the reference patch
(`<ref_root>/<id>.patch`, the commit's change to the declared files). The reference patch never
goes to the sandbox; it is only used by `validate`.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile

import grade

STRIP_PREFIXES = (".superpowers/", "docs/superpowers/")
_TASKDEF_KEYS = {"id": str, "commit": str, "files": list, "tests": list, "hidden": dict,
                 "needs_conftest": bool, "timeout_s": int}


class LeakError(RuntimeError):
    """The snapshot contains a byte-identical copy of the answer."""


def _git(repo, *args, text=True):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", *args], cwd=repo, capture_output=True,
                       check=True, env=env)
    return r.stdout.decode().strip() if text else r.stdout


def _blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _object(repo, commit, path):
    """(mode, blob_id) of path at commit, or None if absent."""
    out = _git(repo, "ls-tree", commit, "--", path)
    if not out:
        return None
    mode, _type, rest = out.split(" ", 2)
    return mode, rest.split("\t", 1)[0]


def load_taskdef(taskdef_dir):
    with open(os.path.join(taskdef_dir, "taskdef.json"), encoding="utf-8") as f:
        td = json.load(f)
    for key, typ in _TASKDEF_KEYS.items():
        if not isinstance(td.get(key), typ):
            raise ValueError(f"{taskdef_dir}: taskdef.json '{key}' missing or not {typ.__name__}")
    return td


def _snapshot_members(repo, parent, commit, tests):
    """{name: (TarInfo, bytes|None)} -- the parent tree minus STRIP_PREFIXES, with the commit's
    visible tests overlaid."""
    members = {}
    with tarfile.open(fileobj=io.BytesIO(_git(repo, "archive", "--format=tar", parent, text=False))) as t:
        for m in t.getmembers():
            if m.type in (tarfile.XGLTYPE, tarfile.XHDTYPE) or m.name.startswith(STRIP_PREFIXES):
                continue
            name = m.name.rstrip("/")
            if not name or (name + "/").startswith(STRIP_PREFIXES):
                continue
            data = t.extractfile(m).read() if m.isfile() else None
            members[name] = (m, data)
    for path in tests:
        obj = _object(repo, commit, path)
        if obj is None:
            raise ValueError(f"visible test {path} does not exist at {commit}")
        info = tarfile.TarInfo(path)
        info.mode = 0o755 if obj[0] == "100755" else 0o644
        members[path] = (info, _git(repo, "show", f"{commit}:{path}", text=False))
    return members


def _write_tar(path, members):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as out:
        for name in sorted(members):
            src, data = members[name]
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.linkname = src.type, src.mode, src.linkname
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            if data is not None:
                info.size = len(data)
                out.addfile(info, io.BytesIO(data))
            else:
                out.addfile(info)


def build(repo, taskdef_dir, out_root, ref_root):
    """Write <out_root>/<id>/ and <ref_root>/<id>.patch; return the bundle path."""
    td = load_taskdef(taskdef_dir)
    commit = _git(repo, "rev-parse", td["commit"])
    parents = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    if len(parents) != 1:
        raise ValueError(f"{td['id']}: {commit} must have exactly one parent (has {len(parents)})")
    parent = parents[0]
    members = _snapshot_members(repo, parent, commit, td["tests"])

    answers = {}
    for path in td["files"]:
        new, old = _object(repo, commit, path), _object(repo, parent, path)
        if new is not None and new != old:
            answers[new[1]] = path
    leaks = sorted(name for name, (_m, data) in members.items()
                   if data is not None and _blob_id(data) in answers)
    if leaks:
        raise LeakError(f"{td['id']}: snapshot holds the answer for "
                        f"{sorted(set(answers.values()))} at {leaks}")

    dest = os.path.join(out_root, td["id"])
    os.makedirs(dest)                                   # never overwrite an existing bundle
    _write_tar(os.path.join(dest, "snapshot.tar"), members)
    shutil.copyfile(os.path.join(taskdef_dir, "request.md"), os.path.join(dest, "request.md"))
    os.makedirs(os.path.join(dest, "hidden"))
    for name in td["hidden"]:
        shutil.copyfile(os.path.join(taskdef_dir, "hidden", name), os.path.join(dest, "hidden", name))
    task = dict(td, commit=commit, parent=parent, request="request.md")
    with open(os.path.join(dest, "task.json"), "w", encoding="utf-8") as f:
        json.dump(task, f, indent=1, sort_keys=True)
    os.makedirs(ref_root, exist_ok=True)
    with open(os.path.join(ref_root, f"{td['id']}.patch"), "wb") as f:
        f.write(_git(repo, "diff", "--binary", parent, commit, "--", *td["files"], text=False))
    grade.load_task(dest)                               # the bundle must satisfy the grader's checks
    return dest


def validate(bundle_dir, reference_patch, workdir, runner):
    """Problems with one bundle: its tests must fail on the snapshot and pass with the reference."""
    problems = []
    try:
        task = grade.load_task(bundle_dir)
    except ValueError as e:
        return [str(e)]
    pristine = grade.grade(bundle_dir, "", os.path.join(workdir, task["id"], "pristine"), runner)
    if pristine["pass"]:
        problems.append(f"{task['id']}: tests pass on the unchanged snapshot (vacuous)")
    with open(reference_patch, encoding="utf-8") as f:
        ref = grade.grade(bundle_dir, f.read(), os.path.join(workdir, task["id"], "reference"), runner)
    if not ref["pass"]:
        problems.append(f"{task['id']}: tests fail with the reference patch ({ref['stage']}: {ref['summary']})")
    return problems


def manifest(root):
    """sha256 over every bundle file (relative path + bytes), stable order. Frozen before W1/W3."""
    h = hashlib.sha256()
    for dirpath, dirnames, files in os.walk(root):
        dirnames.sort()
        for name in sorted(files):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).replace(os.sep, "/").encode() + b"\0")
            with open(p, "rb") as f:
                h.update(f.read())
    return h.hexdigest()
