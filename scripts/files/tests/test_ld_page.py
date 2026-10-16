"""The page: self-contained (no external URLs), bound to the snapshot schema, renders stale sources."""
import os
import re

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "live_dashboard", "static", "index.html")


def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def test_self_contained_no_external_urls():
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", page())
    assert "cdn" not in page().lower()


def test_uses_the_api_and_schema_fields():
    p = page()
    for needle in ('new EventSource("/api/stream")', '"/api/series"', '"/api/requests?limit=20"',
                   '"/api/history?days=7"', ".slots", ".speed", ".gpus", ".host", ".services", ".escalation",
                   ".recent", ".sources", "stale_since", "history_ok", "prefill_tok_s", "gen_tok_s",
                   "cached_tokens", "progress"):
        assert needle in p, needle


def test_no_text_fields_rendered():
    p = page()
    assert "prompt_text" not in p and "content" not in re.findall(r"\.(\w+)", p)


def test_reconnect_and_escaping_present():
    p = page()
    assert "function esc(" in p and "es.close()" in p and "30000" in p
    assert "(stale)" in p
    assert "Think" not in p


def test_page_renders_hostile_and_null_snapshots():
    import shutil
    import subprocess

    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    harness = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ld_page_harness.js")
    r = subprocess.run([node, harness, PAGE], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    assert r.stdout.strip() == "ok"
