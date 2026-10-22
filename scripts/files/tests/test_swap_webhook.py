"""swap-webhook.py: the profile allowlist must track swap-chat-model.sh's PROFILE_NAMES
WITHOUT a service restart.

Freezing the allowlist at import (``VALID_PROFILES = _discover_profiles(...)`` once) meant a
profile added by a later git-pull returned 400 "unknown_profile" until the webhook was
restarted -- hemmingway hit exactly this on 2026-10-19 (the webhook had been up since before
the profile was merged). ``current_valid_profiles()`` re-reads when swap-chat-model.sh changes
on disk, mtime-gated so the common path is a single stat()."""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
WEBHOOK = os.path.join(HERE, "..", "swap-webhook.py")


def load():
    # swap-webhook.py is hyphenated (a CLI name, not an importable module) and only
    # starts the server under __main__, so importing it for its functions is safe.
    spec = importlib.util.spec_from_file_location("swap_webhook_under_test", WEBHOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_script(path, names):
    path.write_text("#!/usr/bin/env bash\n# stub\nPROFILE_NAMES=(%s)\n" % " ".join(names), encoding="utf-8")


def set_mtime(path, when):
    os.utime(path, (when, when))


def test_discover_profiles_parses_profile_names(tmp_path):
    mod = load()
    s = tmp_path / "swap-chat-model.sh"
    write_script(s, ["qwen3.6", "hemmingway", "qwen3.8-redteam"])
    assert mod._discover_profiles(str(s)) == {"qwen3.6", "hemmingway", "qwen3.8-redteam"}


def test_newly_added_profile_is_seen_without_restart(tmp_path):
    # THE regression: add a profile to the script AFTER the module is imported.
    # The allowlist must pick it up (the file's mtime changed) -- no re-import,
    # no systemctl restart.
    mod = load()
    s = tmp_path / "swap-chat-model.sh"
    write_script(s, ["qwen3.6", "coder"])
    set_mtime(s, 1_000_000)
    first = mod.current_valid_profiles(str(s))
    assert "newmodel" not in first
    write_script(s, ["qwen3.6", "coder", "newmodel"])
    set_mtime(s, 1_000_010)        # distinct, newer mtime so the cache refreshes
    second = mod.current_valid_profiles(str(s))
    assert "newmodel" in second


def test_unreadable_script_returns_fallback(tmp_path):
    mod = load()
    missing = tmp_path / "does-not-exist.sh"
    assert mod.current_valid_profiles(str(missing)) == set(mod._FALLBACK_PROFILES)


def test_fallback_profiles_are_always_included(tmp_path):
    mod = load()
    s = tmp_path / "swap-chat-model.sh"
    write_script(s, ["only-this-one"])
    got = mod.current_valid_profiles(str(s))
    assert "only-this-one" in got
    assert set(mod._FALLBACK_PROFILES) <= got


def test_returned_set_is_a_copy_not_the_live_cache(tmp_path):
    # Callers sort/send the set in error responses; mutating it must not poison the cache.
    mod = load()
    s = tmp_path / "swap-chat-model.sh"
    write_script(s, ["qwen3.6"])
    got = mod.current_valid_profiles(str(s))
    got.add("mutant")
    assert "mutant" not in mod.current_valid_profiles(str(s))
