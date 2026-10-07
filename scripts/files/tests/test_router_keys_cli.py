"""router-keys CLI: issue, list and revoke scoped router keys (workforce design rev 2, §5.3).

Runs inside LXC 153 as root. The plaintext key is written ONLY to a 0600 file named by --out; stdout
never carries it, and the keys file stores only its SHA-256.
"""
import hashlib
import importlib.util
import json
import os
import stat
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("router_keys", os.path.join(HERE, "..", "router-keys.py"))
rk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rk)

sys.path.insert(0, os.path.join(HERE, ".."))
import access_keys as ak  # noqa: E402


def run(argv, capsys):
    rc = rk.main(argv)
    return rc, capsys.readouterr()


def test_add_writes_key_only_to_out_file_and_hash_only_to_keys_file(tmp_path, capsys):
    keys, out = tmp_path / "router-keys.json", tmp_path / "wf.key"
    rc, io = run(["--keys-file", str(keys), "add", "--name", "wf-run-1", "--aliases", "qwen3.8-nothink,qwen3.8",
                  "--ttl-hours", "72", "--out", str(out)], capsys)
    assert rc == 0
    key = out.read_text().strip()
    assert key.startswith("wf_") and len(key) > 40
    assert key not in io.out and key not in io.err
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    data = json.loads(keys.read_text())
    entry = data["keys"][0]
    assert entry["name"] == "wf-run-1" and entry["sha256"] == hashlib.sha256(key.encode()).hexdigest()
    assert entry["aliases"] == ["qwen3.8-nothink", "qwen3.8"] and entry["expires"] > 0
    assert key not in keys.read_text()
    p = ak.KeyStore(str(keys), "owner").authenticate(key)
    assert p is not None and p.name == "wf-run-1"


def test_add_refuses_duplicate_names_and_existing_out_file(tmp_path, capsys):
    keys, out = tmp_path / "k.json", tmp_path / "a.key"
    assert run(["--keys-file", str(keys), "add", "--name", "a", "--aliases", "m", "--ttl-hours", "1",
                "--out", str(out)], capsys)[0] == 0
    assert run(["--keys-file", str(keys), "add", "--name", "a", "--aliases", "m", "--ttl-hours", "1",
                "--out", str(tmp_path / "b.key")], capsys)[0] != 0
    assert run(["--keys-file", str(keys), "add", "--name", "b", "--aliases", "m", "--ttl-hours", "1",
                "--out", str(out)], capsys)[0] != 0


def test_add_requires_aliases_and_bounded_ttl(tmp_path, capsys):
    keys = tmp_path / "k.json"
    assert run(["--keys-file", str(keys), "add", "--name", "a", "--aliases", "", "--ttl-hours", "1",
                "--out", str(tmp_path / "x.key")], capsys)[0] != 0
    assert run(["--keys-file", str(keys), "add", "--name", "a", "--aliases", "m", "--ttl-hours", "0",
                "--out", str(tmp_path / "y.key")], capsys)[0] != 0
    assert run(["--keys-file", str(keys), "add", "--name", "a", "--aliases", "m", "--ttl-hours", "1000",
                "--out", str(tmp_path / "z.key")], capsys)[0] != 0


def test_list_shows_no_secrets_and_revoke_removes(tmp_path, capsys):
    keys, out = tmp_path / "k.json", tmp_path / "a.key"
    run(["--keys-file", str(keys), "add", "--name", "wf-a", "--aliases", "m", "--ttl-hours", "2",
         "--out", str(out)], capsys)
    rc, io = run(["--keys-file", str(keys), "list"], capsys)
    assert rc == 0 and "wf-a" in io.out and "sha256" not in io.out and out.read_text().strip() not in io.out
    assert run(["--keys-file", str(keys), "revoke", "--name", "wf-a"], capsys)[0] == 0
    assert json.loads(keys.read_text())["keys"] == []
    assert run(["--keys-file", str(keys), "revoke", "--name", "wf-a"], capsys)[0] != 0


def test_revoke_all_clears_everything(tmp_path, capsys):
    keys = tmp_path / "k.json"
    for n in ("a", "b"):
        run(["--keys-file", str(keys), "add", "--name", n, "--aliases", "m", "--ttl-hours", "1",
             "--out", str(tmp_path / f"{n}.key")], capsys)
    assert run(["--keys-file", str(keys), "revoke", "--all"], capsys)[0] == 0
    assert json.loads(keys.read_text())["keys"] == []


def test_concurrent_add_cannot_resurrect_a_revoked_key(tmp_path, capsys, monkeypatch):
    # Security review M1: `add` loaded the file, a `revoke` completed, then `add` saved its stale
    # copy -- the revoked key came back while the operator had been told "revoked 1 key(s)".
    keys = tmp_path / "k.json"
    assert rk.main(["--keys-file", str(keys), "add", "--name", "compromised", "--aliases", "m",
                    "--ttl-hours", "1", "--out", str(tmp_path / "c.key")]) == 0
    real_save, state = rk._save, {"first": True, "rc": []}

    def save_with_a_revoke_in_between(path, data):
        if state["first"]:
            state["first"] = False
            t = threading.Thread(target=lambda: state["rc"].append(
                rk.main(["--keys-file", str(keys), "revoke", "--name", "compromised"])))
            t.start()
            t.join(0.5)                  # without a lock the revoke finishes here, before add saves
            state["t"] = t
        real_save(path, data)

    monkeypatch.setattr(rk, "_save", save_with_a_revoke_in_between)
    assert rk.main(["--keys-file", str(keys), "add", "--name", "wf-run-2", "--aliases", "m",
                    "--ttl-hours", "1", "--out", str(tmp_path / "w.key")]) == 0
    state["t"].join(10)
    assert state["rc"] == [0]
    assert [e["name"] for e in json.loads(keys.read_text())["keys"]] == ["wf-run-2"]


@pytest.mark.parametrize("name", ["owner", "Owner", "bad\tname", "a b", "", "-x", "x" * 65, "../x"])
def test_add_refuses_unsafe_or_reserved_names(tmp_path, capsys, name):
    # Security review L5: a scoped key named "owner" was indistinguishable from the owner in the
    # access log; tabs/newlines broke `list`.
    out = tmp_path / "x.key"
    rc, _ = run(["--keys-file", str(tmp_path / "k.json"), "add", f"--name={name}", "--aliases", "m",
                 "--ttl-hours", "1", "--out", str(out)], capsys)
    assert rc != 0 and not out.exists()
