import subprocess, os, pytest
from scripts.delegate.config import load_config
from scripts.delegate import gitstore

def _run(*a, cwd): subprocess.run(a, cwd=cwd, check=True, capture_output=True)

def _repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    _run("git","init","-q", cwd=r); _run("git","config","user.email","t@t", cwd=r)
    _run("git","config","user.name","t", cwd=r)
    (r/"a.txt").write_text("base\n")
    _run("git","add","-A", cwd=r); _run("git","commit","-qm","init", cwd=r)
    return r

def _cfg(root): return load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r",
                                    "LOCAL_DELEGATE_ALLOWED_ROOTS":str(root)})

def test_validate_rejects_repo_outside_roots(tmp_path):
    r = _repo(tmp_path)
    with pytest.raises(gitstore.RepoNotAllowed):
        gitstore.validate_repo(_cfg(tmp_path/"other"), str(r))

def test_validate_accepts_repo_inside_roots_and_rejects_non_git(tmp_path):
    r = _repo(tmp_path)
    assert gitstore.validate_repo(_cfg(tmp_path), str(r)) == os.path.realpath(str(r))
    plain = tmp_path / "plain"; plain.mkdir()
    with pytest.raises(gitstore.RepoNotAllowed):
        gitstore.validate_repo(_cfg(tmp_path), str(plain))

def test_resolve_ref_rejects_dangerous(tmp_path):
    r = _repo(tmp_path)
    for bad in ("-x", "a..b"):
        with pytest.raises(Exception):
            gitstore.resolve_ref(str(r), bad)

def test_resolve_ref_returns_sha_and_passes_worktree(tmp_path):
    r = _repo(tmp_path)
    sha = gitstore.resolve_ref(str(r), "HEAD")
    assert len(sha) == 40
    assert gitstore.resolve_ref(str(r), "WORKTREE") == "WORKTREE"

def test_export_head_is_independent_git(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    sha = gitstore.resolve_ref(str(r), "HEAD")
    gitstore.export(str(r), sha, str(dest))
    assert (dest/"a.txt").read_text() == "base\n"
    # dest/.git is a real dir, not a pointer into repo/.git
    assert (dest/".git").is_dir()
    gitdir = subprocess.run(["git","-C",str(dest),"rev-parse","--git-dir"],
                            capture_output=True,text=True).stdout.strip()
    assert os.path.realpath(os.path.join(str(dest),gitdir)) != os.path.realpath(str(r/".git"))

def test_worktree_snapshot_includes_uncommitted_and_untracked(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    (r/"a.txt").write_text("edited\n")        # tracked, uncommitted
    (r/"new.txt").write_text("fresh\n")       # untracked
    gitstore.export(str(r), "WORKTREE", str(dest))
    assert (dest/"a.txt").read_text() == "edited\n"
    assert (dest/"new.txt").read_text() == "fresh\n"

def test_worktree_does_not_touch_source_index(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    (r/"new.txt").write_text("fresh\n")
    gitstore.export(str(r), "WORKTREE", str(dest))
    status = subprocess.run(["git","-C",str(r),"status","--porcelain"],
                            capture_output=True,text=True).stdout
    assert "?? new.txt" in status

def test_worktree_on_clean_tree_does_not_crash(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    gitstore.export(str(r), "WORKTREE", str(dest))
    assert (dest/"a.txt").read_text() == "base\n"

def test_commit_work_then_extract_patch_shows_new_file(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    gitstore.export(str(r), "HEAD", str(dest))
    base = gitstore.base_sha(str(dest))
    (dest/"added.txt").write_text("hello\n")
    gitstore.commit_work(str(dest))
    patch = gitstore.extract_patch(str(dest), base)
    assert "added.txt" in patch and "+hello" in patch
    assert gitstore.base_sha(str(dest)) != base

def test_commit_work_with_no_changes_keeps_head(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    gitstore.export(str(r), "HEAD", str(dest))
    base = gitstore.base_sha(str(dest))
    gitstore.commit_work(str(dest))
    assert gitstore.base_sha(str(dest)) == base
    assert gitstore.extract_patch(str(dest), base) == ""

def test_export_keeps_export_ignored_files(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    (r/".gitattributes").write_text("secret.txt export-ignore\n")
    (r/"secret.txt").write_text("keep me\n")
    _run("git","add","-A", cwd=r); _run("git","commit","-qm","attrs", cwd=r)
    gitstore.export(str(r), "HEAD", str(dest))
    assert (dest/"secret.txt").read_text() == "keep me\n"

def test_planted_hook_is_not_executed(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    gitstore.export(str(r), "HEAD", str(dest))
    sentinel = tmp_path/"hook-ran"
    for name in ("pre-commit", "post-commit"):
        h = dest/".git"/"hooks"/name
        h.parent.mkdir(exist_ok=True)
        h.write_text(f"#!/bin/sh\necho x > '{sentinel.as_posix()}'\n")
        h.chmod(0o755)
    (dest/"w.txt").write_text("w\n")
    gitstore.commit_work(str(dest))
    assert not sentinel.exists()

def test_validate_is_case_insensitive_on_windows_and_rejects_sibling_prefix(tmp_path):
    r = _repo(tmp_path)
    sib = tmp_path/"repo2"; sib.mkdir(); (sib/".git").mkdir()
    cfg = _cfg(r)
    with pytest.raises(gitstore.RepoNotAllowed):
        gitstore.validate_repo(cfg, str(sib))
