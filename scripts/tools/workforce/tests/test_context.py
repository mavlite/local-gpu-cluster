"""Packet context (round 2 §3.2): changed names, call sites, touching tests, hunk shrinking. Everything
comes from the pristine snapshot plus git blobs of in-scope files, never from the agent's tree."""
import io
import os
import sys
import tarfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import context  # noqa: E402

DIFF = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,4 @@\n"
        "-def should_cache(code):\n+def should_cache(code, body=None):\n     return code == 200\n"
        "+class Cache:\n+    pass\n")


def snap(path, files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    path.write_bytes(buf.getvalue())
    return str(path)


def test_changed_names_are_the_added_or_changed_top_level_defs():
    assert context.changed_names(DIFF) == ["should_cache", "Cache"]


def test_call_sites_come_from_the_snapshot_with_patched_in_scope_files(tmp_path):
    tar = snap(tmp_path / "s.tar", {
        "m.py": "def should_cache(code):\n    return 1\n",
        "app.py": "import m\nif m.should_cache(r.status_code, result):\n    pass\n",
        "notes.txt": "should_cache is called here too\n"})
    sites = context.call_sites(["should_cache"], tar, {"m.py": b"def should_cache(code, body=None):\n    return 1\n"})
    assert sites == ["app.py:2: if m.should_cache(r.status_code, result):"]     # not the def, not .txt


def test_call_sites_see_the_patched_copy_of_an_in_scope_file(tmp_path):
    tar = snap(tmp_path / "s.tar", {"m.py": "def f():\n    pass\n"})
    sites = context.call_sites(["f"], tar, {"m.py": b"def f():\n    pass\n\n\ndef g():\n    return f()\n"})
    assert sites == ["m.py:6: return f()"]


def test_call_sites_are_capped_per_name(tmp_path):
    tar = snap(tmp_path / "s.tar", {"a.py": "\n".join(f"x{i} = f()" for i in range(100)) + "\n"})
    assert len(context.call_sites(["f"], tar, {})) == context.MAX_SITES == 40


def test_touching_tests_match_stem_or_basename(tmp_path):
    tar = snap(tmp_path / "s.tar", {
        "scripts/files/router-app.py": "",
        "scripts/files/tests/test_keys.py": 'spec_from_file_location("router_app", os.path.join(HERE, "..", "router-app.py"))\n',
        "scripts/files/tests/test_other.py": "import tavily_cache\n",
        "scripts/files/helper.py": "router-app.py is mentioned here but this is not a test\n"})
    assert context.touching_tests(["scripts/files/router-app.py"], tar) == ["scripts/files/tests/test_keys.py"]


def test_shrink_blocks_trims_oversized_context_and_says_so():
    body = "\n".join(f" line{i}" for i in range(200))
    diff = f"diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1,200 +1,201 @@\n{body}\n+new\n"
    out = context.shrink_blocks(diff, max_lines=60)
    assert out.count("\n line") <= 61 and "+new" in out
    assert "[context trimmed: big.py:" in out


def test_shrink_blocks_leaves_a_small_hunk_alone():
    diff = "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n"
    assert context.shrink_blocks(diff) == diff


def test_changed_names_include_the_enclosing_def_of_a_body_only_change():
    # Review I6: the commonest edit changes a body, not a def line; with -W the hunk opens on the def.
    diff = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,5 +1,5 @@\n def handle(x):\n     if x:\n"
            "-        return 1\n+        return 2\n     return 0\n@@ -20,3 +20,4 @@\n async def go():\n     pass\n"
            "+    await x\n+async def later():\n+    pass\n")
    assert context.changed_names(diff) == ["handle", "go", "later"]


def test_changed_names_ignore_nested_defs_in_context():
    diff = ("diff --git a/m.py b/m.py\n@@ -1,4 +1,4 @@\n def outer():\n     def inner():\n-        return 1\n"
            "+        return 2\n")
    assert context.changed_names(diff) == ["outer"]


def test_shrink_blocks_splits_on_newlines_only_and_ignores_empty_lines():
    # Review M3: splitlines() also breaks on \x0c and friends, which could fake hunk headers.
    diff = "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -1,3 +1,3 @@\n a\x0cb\n-b\n+c\n\n d\n"
    assert context.shrink_blocks(diff) == diff
    body = "\n".join(f" line{i}" for i in range(200))
    diff = f"diff --git a/big.py b/big.py\n@@ -1,200 +1,201 @@\n{body}\n\n+new\n"
    out = context.shrink_blocks(diff, max_lines=60)
    assert "+new" in out and out.count("\n line") <= 61
