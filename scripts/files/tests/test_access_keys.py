"""Scoped router keys (workforce design rev 2, §5.3).

The owner key (ROUTER_API_KEY) keeps full access. Scoped keys live in a JSON file as SHA-256 hashes,
expire, may call only chat completions and the model list, only their allowlisted aliases, never
server-side tools, and never trigger a profile swap.
"""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import access_keys as ak  # noqa: E402

OWNER = "owner-key-0123456789"
WF = "wf_scoped-key-abcdef"


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def write(path, entries):
    path.write_text(json.dumps({"keys": entries}))


@pytest.fixture
def store(tmp_path):
    p = tmp_path / "router-keys.json"
    write(p, [{"name": "wf-run-1", "sha256": h(WF), "aliases": ["qwen3.8-nothink"], "expires": 2000.0}])
    clock = {"now": 1000.0}
    s = ak.KeyStore(str(p), OWNER, clock=lambda: clock["now"])
    return s, p, clock


def test_owner_key_is_owner(store):
    s, _, _ = store
    p = s.authenticate(OWNER)
    assert p is not None and p.is_owner and p.name == "owner"


def test_scoped_key_matches_by_hash_and_carries_its_scope(store):
    s, _, _ = store
    p = s.authenticate(WF)
    assert p is not None and not p.is_owner
    assert p.name == "wf-run-1" and p.aliases == frozenset({"qwen3.8-nothink"})


def test_unknown_and_empty_keys_rejected(store):
    s, _, _ = store
    assert s.authenticate("nope") is None and s.authenticate("") is None


def test_expired_key_rejected(store):
    s, _, clock = store
    clock["now"] = 2000.0
    assert s.authenticate(WF) is None


def test_file_is_reloaded_when_it_changes_so_revocation_is_immediate(store):
    s, p, _ = store
    assert s.authenticate(WF) is not None
    write(p, [])
    os.utime(p, (os.path.getmtime(p) + 5, os.path.getmtime(p) + 5))
    assert s.authenticate(WF) is None


def test_malformed_or_missing_file_fails_closed_for_scoped_keys_only(store, tmp_path):
    s, p, _ = store
    p.write_text("{not json")
    os.utime(p, (os.path.getmtime(p) + 5, os.path.getmtime(p) + 5))
    assert s.authenticate(WF) is None and s.authenticate(OWNER).is_owner
    missing = ak.KeyStore(str(tmp_path / "absent.json"), OWNER)
    assert missing.authenticate(WF) is None and missing.authenticate(OWNER).is_owner


def test_entry_missing_fields_is_ignored_not_crashing(tmp_path):
    p = tmp_path / "k.json"
    write(p, [{"name": "bad"}, {"name": "ok", "sha256": h(WF), "aliases": ["a"], "expires": 20.0}])
    s = ak.KeyStore(str(p), OWNER, clock=lambda: 10.0)
    assert s.authenticate(WF).name == "ok"


def test_endpoint_allowlist():
    scoped = ak.Principal("wf", "scoped", frozenset({"a"}), None)
    assert ak.endpoint_allowed(scoped, "POST", "/v1/chat/completions")
    assert ak.endpoint_allowed(scoped, "GET", "/v1/models")
    for method, path in [("POST", "/v1/embeddings"), ("POST", "/v1/messages"), ("POST", "/v1/completions"),
                         ("POST", "/v1/tavily/search"), ("GET", "/v1/tavily/usage"), ("POST", "/v1/rerank"),
                         ("POST", "/v1/messages/count_tokens")]:
        assert not ak.endpoint_allowed(scoped, method, path), path
    assert ak.endpoint_allowed(ak.OWNER, "POST", "/v1/embeddings")


def test_chat_policy_alias_and_server_tools():
    scoped = ak.Principal("wf", "scoped", frozenset({"qwen3.8-nothink"}), None)
    assert ak.chat_violation(scoped, {"model": "qwen3.8-nothink"}) is None
    assert ak.chat_violation(scoped, {"model": "qwen3.8-think"}) == "model_not_allowed"
    assert ak.chat_violation(scoped, {"model": "qwen3.8-nothink", "tool_execution": "server"}) == \
        "server_tools_not_allowed"
    assert ak.chat_violation(ak.OWNER, {"model": "anything", "tool_execution": "server"}) is None


def test_scoped_requests_are_forced_to_client_tools_without_mutating_input():
    scoped = ak.Principal("wf", "scoped", frozenset({"m"}), None)
    body = {"model": "m"}
    out = ak.apply_chat_policy(scoped, body)
    assert out["tool_execution"] == "client" and "tool_execution" not in body
    assert ak.apply_chat_policy(ak.OWNER, body) is body


def test_only_the_owner_may_swap_profiles():
    assert ak.may_swap(ak.OWNER)
    assert not ak.may_swap(ak.Principal("wf", "scoped", frozenset(), None))


def test_deploy_script_ships_modules_cli_and_an_empty_keys_file():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    s = open(os.path.join(root, "53-lxc-router.sh"), encoding="utf-8").read()
    for f in ("access_keys.py", "workforce_lane.py"):
        assert f"files/{f}" in s and f"/opt/llm-router/{f}" in s
    assert "files/router-keys.py" in s and "/usr/local/sbin/router-keys" in s
    assert "/etc/router-keys.json" in s and "0640" in s
    assert '{"keys": []}' in s                       # created empty, never overwritten if present


def test_deploy_block_creates_valid_empty_keys_file_and_never_overwrites(tmp_path):
    # The first version of this block (nested quoting through pct exec + sh -c) wrote
    # '{keys: []}', which is not JSON -- and a malformed keys file disables scoped keys.
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    src = open(os.path.join(root, "53-lxc-router.sh"), encoding="utf-8").read()
    start = src.index("  # No nested quoting through pct/sh")
    end = src.index("  pct exec \"$ROUTER_VMID\" -- chmod 0640 /etc/router-keys.json\n") + len(
        "  pct exec \"$ROUTER_VMID\" -- chmod 0640 /etc/router-keys.json\n")
    block = tmp_path / "block.sh"
    block.write_text(src[start:end], newline="\n")
    ct = tmp_path / "ct"
    (ct / "etc").mkdir(parents=True)
    runner = tmp_path / "run.sh"
    runner.write_text(
        f'ROOT="{ct.as_posix()}"\n'
        'pct() { case "$1" in\n'
        '  exec) shift 3; [ "$1" = test ] && { test "$2" "$ROOT$3"; return; }; return 0;;\n'
        '  push) cp "$3" "$ROOT$4";; esac; }\n'
        f'ROUTER_VMID=153\n. "{block.as_posix()}"\n', newline="\n")
    keys = ct / "etc" / "router-keys.json"
    subprocess.run([bash, str(runner)], check=True)
    assert json.loads(keys.read_text()) == {"keys": []}
    keys.write_text('{"keys": [{"name": "issued"}]}\n')
    subprocess.run([bash, str(runner)], check=True)
    assert json.loads(keys.read_text()) == {"keys": [{"name": "issued"}]}


def test_invalid_entries_are_skipped_and_never_break_valid_keys(tmp_path):
    # Security review L3: a hand-edited entry must not 500 every scoped request, widen an
    # allowlist (a string is not a list of aliases) or remove expiry (nan/inf/null/bool).
    good = {"name": "ok", "sha256": h(WF), "aliases": ["m"], "expires": 20.0}
    bad = [
        {"name": "nonhex", "sha256": "é" * 64, "aliases": ["m"], "expires": 20.0},
        {"name": "short", "sha256": "ab", "aliases": ["m"], "expires": 20.0},
        {"name": "stralias", "sha256": h("wf_stralias"), "aliases": "m", "expires": 20.0},
        {"name": "noalias", "sha256": h("wf_noalias"), "aliases": [], "expires": 20.0},
        {"name": "nan", "sha256": h("wf_nan"), "aliases": ["m"], "expires": "nan"},
        {"name": "inf", "sha256": h("wf_inf"), "aliases": ["m"], "expires": float("inf")},
        {"name": "never", "sha256": h("wf_never"), "aliases": ["m"], "expires": None},
        {"name": "bool", "sha256": h("wf_bool"), "aliases": ["m"], "expires": True},
        {"name": "owner", "sha256": h("wf_owner"), "aliases": ["m"], "expires": 20.0},
    ]
    p = tmp_path / "k.json"
    write(p, bad + [good])
    s = ak.KeyStore(str(p), OWNER, clock=lambda: 10.0)
    assert s.authenticate(WF).name == "ok"
    for k in ("wf_stralias", "wf_noalias", "wf_nan", "wf_inf", "wf_never", "wf_bool", "wf_owner", "junk"):
        assert s.authenticate(k) is None, k


def test_revocation_is_seen_even_when_mtime_and_size_are_unchanged(tmp_path):
    # Security review L4: an atomic replace that keeps mtime and size must still reload.
    p = tmp_path / "k.json"
    write(p, [{"name": "wf-run-1", "sha256": h(WF), "aliases": ["m"], "expires": 2000.0}])
    st = os.stat(p)
    s = ak.KeyStore(str(p), OWNER, clock=lambda: 1000.0)
    assert s.authenticate(WF) is not None
    tmp = tmp_path / "k.tmp"
    write(tmp, [{"name": "wf-run-2", "sha256": h(WF + "X"), "aliases": ["m"], "expires": 2000.0}])
    assert os.path.getsize(tmp) == st.st_size
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, p)
    assert os.stat(p).st_mtime_ns == st.st_mtime_ns
    assert s.authenticate(WF) is None


def test_non_string_model_is_refused_without_crashing():
    # Security review L8: an unhashable model raised TypeError (HTTP 500).
    scoped = ak.Principal("wf", "scoped", frozenset({"m"}), None)
    for model in (["m"], {"m": 1}, None, 3):
        assert ak.chat_violation(scoped, {"model": model}) == "model_not_allowed"


def test_deploy_block_leaves_issued_keys_alone_when_the_existence_check_errors(tmp_path):
    # Security review L9: only "does not exist" (exit 1) may create the file; any other failure
    # of `pct exec ... test -e` (container not running, attach error) must abort, not overwrite.
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    src = open(os.path.join(root, "53-lxc-router.sh"), encoding="utf-8").read()
    tail = "  pct exec \"$ROUTER_VMID\" -- chmod 0640 /etc/router-keys.json\n"
    block = tmp_path / "block.sh"
    block.write_text(src[src.index("  # No nested quoting through pct/sh"):src.index(tail) + len(tail)], newline="\n")
    ct = tmp_path / "ct"
    (ct / "etc").mkdir(parents=True)
    runner = tmp_path / "run.sh"
    runner.write_text(
        f'ROOT="{ct.as_posix()}"\n'
        'die() { echo "$*" >&2; exit 1; }\n'
        'pct() { case "$1" in\n'
        '  exec) shift 3; [ "$1" = test ] && return 255; return 0;;\n'
        '  push) cp "$3" "$ROOT$4";; esac; }\n'
        f'ROUTER_VMID=153\n. "{block.as_posix()}"\n', newline="\n")
    keys = ct / "etc" / "router-keys.json"
    keys.write_text('{"keys": [{"name": "issued"}]}\n')
    r = subprocess.run([bash, str(runner)], capture_output=True, text=True)
    assert r.returncode != 0
    assert json.loads(keys.read_text()) == {"keys": [{"name": "issued"}]}
