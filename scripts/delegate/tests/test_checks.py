import os
import sys
from pathlib import Path

import pytest

from scripts.delegate import checks

ALLOW = [["python"], ["bash", "-n"]]


def test_non_allowlisted_command_rejected(tmp_path):
    with pytest.raises(checks.CheckNotAllowed):
        checks.run_checks(str(tmp_path), [["rm", "-rf", "x"]], allowlist=ALLOW)


def test_prefix_must_match_whole_leading_tokens(tmp_path):
    with pytest.raises(checks.CheckNotAllowed):
        checks.run_checks(str(tmp_path), [["bash", "-c", "id"]], allowlist=ALLOW)


def test_empty_argv_rejected(tmp_path):
    with pytest.raises(checks.CheckNotAllowed):
        checks.run_checks(str(tmp_path), [[]], allowlist=ALLOW)


def test_scrubbed_env_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DELEGATE_ROUTER_TOKEN", "SECRET")
    probe = [sys.executable, "-c", "import os;print('TOK' if os.environ.get('LOCAL_DELEGATE_ROUTER_TOKEN') else 'NONE')"]
    res = checks.run_checks(str(tmp_path), [probe], allowlist=[[sys.executable]])
    assert res[0].output.strip().endswith("NONE")


def test_scrubbed_env_contents():
    import os
    os.environ.update({"GH_TOKEN": "x", "GITHUB_TOKEN": "x", "SSH_AUTH_SOCK": "x",
                       "LOCAL_DELEGATE_X": "x"})
    try:
        env = checks._scrubbed_env()
    finally:
        for k in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "LOCAL_DELEGATE_X"):
            del os.environ[k]
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "LOCAL_DELEGATE_X"} & set(env)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "PATH" in env


def test_passing_check_reports_exit_zero(tmp_path):
    res = checks.run_checks(str(tmp_path), [[sys.executable, "-c", "print(1)"]], allowlist=[[sys.executable]])
    assert res[0].exit_code == 0 and "1" in res[0].output


def test_failing_check_reports_nonzero(tmp_path):
    res = checks.run_checks(str(tmp_path), [[sys.executable, "-c", "raise SystemExit(3)"]],
                            allowlist=[[sys.executable]])
    assert res[0].exit_code == 3


def test_python_gets_isolation_flag_and_runs_argv_without_shell(tmp_path):
    calls = []

    class P:
        returncode, stdout, stderr = 0, "", ""

    def fake(argv, **kw):
        calls.append((argv, kw))
        return P()

    checks.run_checks(str(tmp_path), [["python", "-m", "pytest", "t.py"], ["bash", "-n", "a.sh"]],
                      allowlist=[["python", "-m", "pytest"], ["bash", "-n"]], runner=fake)
    assert calls[0][0] == ["python", "-I", "-m", "pytest", "-p", "no:cacheprovider", "t.py"]
    assert calls[1][0] == ["bash", "-n", "a.sh"]
    for _, kw in calls:
        assert kw["cwd"] == str(tmp_path) and not kw.get("shell")
        assert kw["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_planted_sitecustomize_does_not_run(tmp_path):
    (tmp_path / "sitecustomize.py").write_text("print('PWNED')\n")
    res = checks.run_checks(str(tmp_path), [[sys.executable, "-c", "print('ok')"]],
                            allowlist=[[sys.executable]])
    assert "PWNED" not in res[0].output and "ok" in res[0].output


def test_load_allowlist_splits_prefixes():
    assert checks.load_allowlist(str(Path(__file__).resolve().parents[3] / "clients/opencode-delegate/allowlist.toml")) == [
        ["cat"], ["ls"], ["rg"], ["sed", "-n"], ["bash", "-n"], ["shellcheck"],
        ["python", "-m", "pytest"],
    ]


def test_trailing_dash_I_does_not_suppress_isolation(tmp_path):
    assert checks._isolate(["python", "-c", "print(1)", "-I"])[:2] == ["python", "-I"]
    probe = "import sys;print(sys.flags.isolated)"
    res = checks.run_checks(str(tmp_path), [[sys.executable, "-c", probe, "-I"]],
                            allowlist=[[sys.executable]])
    assert res[0].output.strip() == "1"


def test_broad_secret_names_scrubbed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("DB_PASSWORD", "x")
    env = checks._scrubbed_env()
    assert not {"OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD"} & set(env)
    assert "PATH" in env


def test_allowlist_excludes_secret_vars_without_telltale_substrings(monkeypatch):
    """Allow-list, not deny-list: vars with no TOKEN/SECRET/KEY/PASSWORD substring
    (AWS_ACCESS_KEY_ID, GOOGLE_APPLICATION_CREDENTIALS) must still not reach checks."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAnotasecret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", r"C:\creds.json")
    monkeypatch.setenv("AZURE_CLIENT_ID", "abc")
    env = checks._scrubbed_env()
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "AZURE_CLIENT_ID" not in env
    assert "PATH" in env  # the functional minimum is still passed through


def test_scrubbed_env_is_allowlist_only(monkeypatch):
    monkeypatch.setenv("TOTALLY_ARBITRARY_VAR", "leak-me")
    env = checks._scrubbed_env()
    assert "TOTALLY_ARBITRARY_VAR" not in env
    assert set(env) - {"PYTHONDONTWRITEBYTECODE"} <= {k for k in os.environ}
    assert all(k.upper() in checks._ENV_ALLOW or k == "PYTHONDONTWRITEBYTECODE" for k in env)


def test_nothing_runs_if_any_entry_disallowed(tmp_path):
    calls = []
    with pytest.raises(checks.CheckNotAllowed):
        checks.run_checks(str(tmp_path), [["python", "-c", "1"], ["rm", "x"]],
                          allowlist=[["python"]], runner=lambda *a, **k: calls.append(a))
    assert calls == []


def test_missing_tool_becomes_result_and_keeps_earlier_results(tmp_path):
    res = checks.run_checks(str(tmp_path),
                            [[sys.executable, "-c", "print(1)"], ["no-such-tool-xyz"]],
                            allowlist=[[sys.executable], ["no-such-tool-xyz"]])
    assert res[0].exit_code == 0
    assert res[1].exit_code != 0 and res[1].output


def test_timeout_passed_to_runner(tmp_path):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, "", ""

    def fake(argv, **kw):
        seen.update(kw)
        return P()

    checks.run_checks(str(tmp_path), [["ls"]], allowlist=[["ls"]], runner=fake, timeout=7)
    assert seen["timeout"] == 7
