import sys

from scripts.delegate.service import redirect_output


def test_no_log_var_leaves_streams_alone(monkeypatch):
    before_out, before_err = sys.stdout, sys.stderr
    redirect_output({})
    assert (sys.stdout, sys.stderr) == (before_out, before_err)


def test_log_var_appends_stdout_and_stderr_to_file(tmp_path, monkeypatch):
    # Under pythonw (the logon task) both streams are None; without this the
    # uvicorn log is silently lost.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log = tmp_path / "sub" / "service.log"
    log.parent.mkdir()
    log.write_text("earlier run\n", encoding="utf-8")

    redirect_output({"LOCAL_DELEGATE_LOG": str(log)})
    print("to stdout")
    sys.stderr.write("to stderr\n")
    sys.stdout.flush()

    assert log.read_text(encoding="utf-8") == "earlier run\nto stdout\nto stderr\n"


def test_no_console_defaults_to_localappdata_log(tmp_path, monkeypatch):
    # The logon task cannot set env vars, so pythonw (streams None) logs to the default path.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    redirect_output({"LOCALAPPDATA": str(tmp_path)})
    print("from pythonw")
    sys.stdout.flush()
    log = tmp_path / "local-delegate" / "service.log"
    assert log.read_text(encoding="utf-8") == "from pythonw\n"


def test_log_var_creates_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    log = tmp_path / "new-dir" / "service.log"
    redirect_output({"LOCAL_DELEGATE_LOG": str(log)})
    print("hello")
    sys.stdout.flush()
    assert log.read_text(encoding="utf-8") == "hello\n"
