import sys

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
    assert checks.load_allowlist("clients/opencode-delegate/allowlist.toml") == [
        ["cat"], ["ls"], ["rg"], ["sed", "-n"], ["bash", "-n"], ["shellcheck"],
        ["python", "-m", "pytest"],
    ]
