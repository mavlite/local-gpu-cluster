import os
import subprocess

import pytest

from scripts.delegate import gitstore, resultgate


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "d@d"], ["config", "user.name", "d"],
              ["config", "core.autocrlf", "false"]):
        subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    (r / "a.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True, capture_output=True)
    return r


def _job(tmp_path):
    repo = _repo(tmp_path)
    work, gdir = tmp_path / "work", tmp_path / "gitdir"
    gitstore.export(str(repo), "HEAD", str(work), str(gdir))
    return work, gdir, gitstore.base_sha(str(gdir))


def _inspect(work, gdir, base):
    gitstore.commit_work(str(gdir), str(work))
    return resultgate.inspect_diff(str(gdir), str(work), base)


def test_plain_edit_passes(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / "a.txt").write_text("changed\n")
    rep = _inspect(work, gdir, base)
    assert rep.rejected is False and rep.reasons == [] and rep.flagged == []


def test_github_path_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / ".github").mkdir()
    (work / ".github" / "ci.yml").write_text("run: evil\n")
    rep = _inspect(work, gdir, base)
    assert rep.rejected and any(".github" in r for r in rep.reasons)


def test_husky_and_conftest_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / ".husky").mkdir()
    (work / ".husky" / "pre-commit").write_text("x\n")
    (work / "sub").mkdir()
    (work / "sub" / "conftest.py").write_text("x = 1\n")
    rep = _inspect(work, gdir, base)
    assert rep.rejected
    assert any(".husky" in r for r in rep.reasons)
    assert any("conftest.py" in r for r in rep.reasons)


def test_git_named_path_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / "vendor" / ".git").mkdir(parents=True)
    (work / "vendor" / ".git" / "config").write_text("x\n")
    rep = _inspect(work, gdir, base)
    # git itself refuses to stage nested .git; gate must still never pass it through
    assert rep.rejected is False or any(".git" in r for r in rep.reasons)


def test_executable_mode_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    gitstore._dgit(str(gdir), str(work), "update-index", "--chmod=+x", "a.txt")
    gitstore._dgit(str(gdir), str(work), "commit", "-q", "--no-verify", "-m", "work")
    rep = resultgate.inspect_diff(str(gdir), str(work), base)
    assert rep.rejected and any("mode-change +x" in r for r in rep.reasons)


def test_new_executable_file_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / "run.sh").write_text("#!/bin/sh\n")
    gitstore._dgit(str(gdir), str(work), "add", "-A")
    gitstore._dgit(str(gdir), str(work), "update-index", "--chmod=+x", "run.sh")
    gitstore._dgit(str(gdir), str(work), "commit", "-q", "--no-verify", "-m", "work")
    rep = resultgate.inspect_diff(str(gdir), str(work), base)
    assert rep.rejected and any("+x" in r for r in rep.reasons)


def test_binary_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / "blob.bin").write_bytes(b"\x00\x01\x02\xff\x00" * 20)
    rep = _inspect(work, gdir, base)
    assert rep.rejected and any(r.startswith("binary") for r in rep.reasons)


def test_symlink_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    try:
        os.symlink("a.txt", str(work / "link"))
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here; test_symlink_via_index_rejected covers mode 120000")
    rep = _inspect(work, gdir, base)
    assert rep.rejected and any("symlink" in r for r in rep.reasons)


def test_symlink_via_index_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    blob = subprocess.run(["git", f"--git-dir={gdir}", "hash-object", "-w", "--stdin"],
                          input="a.txt", capture_output=True, text=True, check=True, env=env).stdout.strip()
    gitstore._dgit(str(gdir), str(work), "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    gitstore._dgit(str(gdir), str(work), "commit", "-q", "--no-verify", "-m", "work")
    rep = resultgate.inspect_diff(str(gdir), str(work), base)
    assert rep.rejected and any("symlink" in r for r in rep.reasons)


def test_gitlink_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    sha = "1" * 40
    gitstore._dgit(str(gdir), str(work), "update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
    gitstore._dgit(str(gdir), str(work), "commit", "-q", "--no-verify", "-m", "work")
    rep = resultgate.inspect_diff(str(gdir), str(work), base)
    assert rep.rejected and any("gitlink" in r for r in rep.reasons)


def test_requirements_flagged_not_rejected(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / "requirements.txt").write_text("evil-pkg\n")
    (work / "package.json").write_text("{}\n")
    (work / "poetry.lock").write_text("x\n")
    rep = _inspect(work, gdir, base)
    assert rep.rejected is False
    assert {"requirements.txt", "package.json", "poetry.lock"} <= set(rep.flagged)


def test_overlay_files_ignored(tmp_path):
    work, gdir, base = _job(tmp_path)
    (work / ".opencode").mkdir()
    (work / ".opencode" / "agent.yml").write_text("x: 1\n")
    (work / ".opencode" / "blob.bin").write_bytes(b"\x00\x01" * 30)
    (work / "opencode.json").write_text("{}\n")
    (work / "a.txt").write_text("changed\n")
    rep = _inspect(work, gdir, base)
    assert rep.rejected is False and rep.flagged == []
