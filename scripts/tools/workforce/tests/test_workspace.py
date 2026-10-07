"""Task workspaces: snapshot in, changes and patch out, baseline git kept outside the workspace."""
import io
import os
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import workspace  # noqa: E402


def make_tar(path, files):
    with tarfile.open(path, "w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            raw = data.encode()
            info.size, info.mode = len(raw), 0o755 if name.endswith(".sh") else 0o644
            t.addfile(info, io.BytesIO(raw))


@pytest.fixture
def ws(tmp_path):
    tar = tmp_path / "snapshot.tar"
    make_tar(tar, {"pkg/mod.py": "x = 1\n", "run.sh": "echo hi\n", ".gitignore": "*.log\n"})
    return workspace.Workspace.materialize(str(tar), str(tmp_path / "ws"), str(tmp_path / "git"))


def test_materialize_has_no_git_dir_inside_the_workspace(ws):
    assert sorted(os.listdir(ws.root)) == [".gitignore", "pkg", "run.sh"]
    assert ws.changes() == []


def test_changes_report_status_mode_and_path_including_ignored_files(ws):
    with open(os.path.join(ws.root, "pkg", "mod.py"), "w") as f:
        f.write("x = 2\n")
    with open(os.path.join(ws.root, "new.log"), "w") as f:          # .gitignore cannot hide changes
        f.write("hidden?\n")
    os.remove(os.path.join(ws.root, "run.sh"))
    assert sorted(ws.changes()) == [("A", "100644", "new.log"), ("D", "000000", "run.sh"),
                                    ("M", "100644", "pkg/mod.py")]


def test_patch_contains_only_the_requested_paths_and_applies_to_a_fresh_copy(ws, tmp_path):
    with open(os.path.join(ws.root, "pkg", "mod.py"), "w") as f:
        f.write("x = 2\n")
    with open(os.path.join(ws.root, "other.txt"), "w") as f:
        f.write("not exported\n")
    patch = ws.patch(["pkg/mod.py"])
    assert b"pkg/mod.py" in patch and b"other.txt" not in patch
    fresh = workspace.Workspace.materialize(str(tmp_path / "snapshot.tar"), str(tmp_path / "ws2"),
                                            str(tmp_path / "git2"))
    workspace.apply_patch(fresh.root, patch)
    with open(os.path.join(fresh.root, "pkg", "mod.py")) as f:
        assert f.read() == "x = 2\n"


def test_empty_selection_gives_an_empty_patch(ws):
    assert ws.patch([]) == b""


def test_an_agent_made_git_repo_inside_the_workspace_does_not_confuse_the_baseline(ws):
    subprocess.run(["git", "init", "-q", ws.root], check=True)
    with open(os.path.join(ws.root, "pkg", "mod.py"), "w") as f:
        f.write("x = 3\n")
    assert ("M", "100644", "pkg/mod.py") in ws.changes()


def test_copy_to_makes_an_independent_copy(ws, tmp_path):
    dest = ws.copy_to(str(tmp_path / "review"))
    with open(os.path.join(dest, "pkg", "mod.py"), "w") as f:
        f.write("changed in review\n")
    assert ws.changes() == []


def test_apply_patch_failure_raises(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    bad = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-two\n+three\n"
    with pytest.raises(workspace.PatchError):
        workspace.apply_patch(str(tmp_path), bad)


def test_unsafe_tar_members_are_refused(tmp_path):
    tar = tmp_path / "evil.tar"
    make_tar(tar, {"../escape.txt": "x\n"})
    with pytest.raises(Exception):
        workspace.Workspace.materialize(str(tar), str(tmp_path / "ws"), str(tmp_path / "git"))
    assert not (tmp_path / "escape.txt").exists()


def test_git_commands_trust_a_workspace_owned_by_another_user():
    assert "safe.directory=*" in workspace._ID
