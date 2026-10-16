"""End to end: the real Hub + server against the fake cluster World from test_ld_collectors, over HTTP; plus the
deploy script's shape. Nothing here runs the deploy script."""
import http.client
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard import collectors as C  # noqa: E402
from live_dashboard import main as M  # noqa: E402
from live_dashboard import server as S  # noqa: E402
from test_ld_collectors import World, fx  # noqa: E402

ROOT = os.path.join(HERE, "..", "..")


def _script():
    with open(os.path.join(ROOT, "64-live-dashboard.sh"), encoding="utf-8") as f:
        return f.read()


def test_stream_over_http_with_journal_request(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(C, "find_llama_key", lambda *a, **k: "CHATKEY")
    monkeypatch.setattr(C, "find_router_key", lambda *a, **k: "ROUTERKEY")
    cfg = dict(C.DEFAULTS, history_path=str(tmp_path / "h.db"), drm_root=str(tmp_path / "x"),
               hwmon_root=str(tmp_path / "y"), bind_host="127.0.0.1", bind_port=0, stream_interval_s=0.05)
    hub = C.Hub(cfg, w.http_get, w.read_text, w.run_cmd)
    for i, line in enumerate(fx("journal_chat_last10min_raw.txt").splitlines()):
        hub.on_journal_line(line, 1000.0 + i)
    hub.poll_fast(1100.0)
    hub.poll_services(1100.0)
    srv = S.make_server(hub, cfg, os.path.join(ROOT, "files", "live_dashboard", "static"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        buf = b""
        deadline = time.monotonic() + 10
        # the stream opens with a "retry: 3000" preamble event; the snapshot is the first "data:" event
        while b"data: " not in buf or buf.count(b"\n\n") < 2:
            assert time.monotonic() < deadline, "no data event within 10 s"
            chunk = r.read1(65536)
            assert chunk, "stream closed (EOF) before a data event"
            buf += chunk
        event = next(e for e in buf.split(b"\n\n") if e.startswith(b"data: "))
        snap = json.loads(event[6:])
        assert snap["recent"][0]["task"] == 4772 and snap["sources"]["slots"]["ok"] is True
        assert "CHATKEY" not in buf.decode() and "ROUTERKEY" not in buf.decode()
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/")
        assert c.getresponse().status == 200
    finally:
        srv.shutdown()
        hub.history.close()


def test_deploy_script_shape():
    s = _script()
    for needle in ("install)", "side-by-side)", "cutover)", "rollback)", "status)",
                   "/opt/live-dashboard/live_dashboard", "/etc/live-dashboard.json", "/var/lib/live-dashboard",
                   "/root/live-dashboard-rollback.sh", "systemctl disable --now cluster-monitor",
                   "systemctl enable --now cluster-monitor", '"bind_port": 8889', '"bind_port": 8888',
                   "set -euo pipefail", "EUID", "live_dashboard.prev", "/healthz",
                   "import live_dashboard.main", "sys.version_info >= (3, 11)"):
        assert needle in s, needle
    # cutover must roll back itself when the dashboard does not answer on :8888
    cut = s.split("cutover)", 1)[1].split("rollback)", 1)[0]
    gate = cut.index("if ! wait_health 8888")
    assert "check_sources 8888" in cut[gate:cut.index("then", gate)]
    assert gate < cut.index('bash "$ROLLBACK"') < cut.index("exit 1", gate)
    assert "ROLLBACK FAILED" in cut
    # side-by-side must refuse unless cluster-monitor is still active, before touching anything
    sbs = s.split("side-by-side)", 1)[1].split("cutover)", 1)[0]
    assert sbs.index("is-active --quiet cluster-monitor") < sbs.index("install_files")
    u = open(os.path.join(ROOT, "files", "live-dashboard.service"), encoding="utf-8").read()
    assert "python3 -m live_dashboard.main --config /etc/live-dashboard.json" in u
    assert "WorkingDirectory=/opt/live-dashboard" in u and "Restart=always" in u
    for hardening in ("ProtectHome=true", "PrivateTmp=true"):
        assert hardening in u, hardening
    unit_lines = [ln.strip() for ln in u.splitlines() if not ln.lstrip().startswith("#")]
    assert "NoNewPrivileges=true" not in unit_lines
    assert not any(ln.startswith("User=") for ln in unit_lines)


def test_deploy_config_template_passes_validate_config():
    m = re.search(r"^CFG_TEMPLATE='(.*)'$", _script(), re.M)
    assert m, "CFG_TEMPLATE not found"
    for port in (8889, 8888):
        loaded = json.loads(m.group(1).replace("@PORT@", str(port)))
        assert loaded["bind_port"] == port
        cfg = dict(C.DEFAULTS)
        cfg.update(loaded)
        M.validate_config(cfg)


def _run_source_gate(stdin_text):
    """Run the script's own SOURCES_CHECK_PY snippet (as check_sources does) and return its stdout."""
    import subprocess
    m = re.search(r"^SOURCES_CHECK_PY='(.*?)'$", _script(), re.M | re.S)
    assert m, "SOURCES_CHECK_PY not found"
    r = subprocess.run([sys.executable, "-c", m.group(1), "slots journal gpu host"], input=stdin_text,
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def _snap(**oks):
    return json.dumps({"sources": {n: {"ok": v} for n, v in oks.items()}})


def test_source_gate_passes_only_on_explicit_success():
    assert _run_source_gate(_snap(slots=True, journal=True, gpu=True, host=True, extra=False)) == "SOURCES_OK"


def test_source_gate_names_failing_sources():
    out = _run_source_gate(_snap(slots=True, journal=False, gpu=True, host=True))
    assert out != "SOURCES_OK" and out == "journal"
    assert _run_source_gate(_snap(slots=True, journal=True, gpu=True)) == "host"      # missing source
    assert _run_source_gate(_snap(slots=True, journal=True, gpu=True, host="yes")) == "host"   # not literally true


def test_source_gate_fails_closed_on_unexpected_snapshots():
    for bad in ("{}", "[]", '{"sources": null}', '{"sources": []}', '{"sources": {"slots": null}}', "null", "3",
                '"x"', "{not json", ""):
        assert _run_source_gate(bad) != "SOURCES_OK", bad
    # bash side: only the explicit marker passes
    assert '[[ "$bad" != "SOURCES_OK" ]]' in _script()


# ---- final fix wave: check_sources retries, cut-over enables the unit, unit restart limits
def _bash():
    import shutil
    import subprocess
    b = shutil.which("bash")
    if not b:
        return None
    try:
        r = subprocess.run([b, "-c", "echo ok"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return b if r.stdout.strip() == "ok" else None


def _run_check_sources(responses):
    """Source the script's own check_sources (and its python gate) with curl/sleep stubbed; `responses` are the
    successive snapshots the stream returns. Returns (rc, curl calls, simulated seconds, stderr)."""
    import subprocess
    import pytest
    b = _bash()
    if not b:
        pytest.skip("no working bash")
    s = _script()
    fn = re.search(r"^check_sources\(\) \{.*?^\}$", s, re.M | re.S)
    gate = re.search(r"^SOURCES_CHECK_PY='.*?'$", s, re.M | re.S)
    req = re.search(r"^REQUIRED_SOURCES=.*$", s, re.M)
    assert fn and gate and req
    lines = [f"RESP{i}={json.dumps('data: ' + r)!s}" for i, r in enumerate(responses)]
    # curl runs in a command-substitution subshell, so its call counter lives in a file; sleep runs in the
    # function's own shell and advances bash's SECONDS clock instead of waiting
    harness = "\n".join([
        "set -euo pipefail", "HOST_IP=127.0.0.1", f"PY='{sys.executable.replace(chr(92), '/')}'",
        req.group(0), gate.group(0), *lines, f"NRESP={len(responses)}",
        'CF="$(mktemp)"; echo 0 > "$CF"',
        'curl() { local n; n=$(cat "$CF"); echo $((n + 1)) > "$CF"; '
        'local v="RESP$(( n < NRESP ? n : NRESP - 1 ))"; printf "%s\\n" "${!v}"; }',
        'sleep() { SECONDS=$((SECONDS + ${1%.*})); }',
        fn.group(0),
        "SECONDS=0", "rc=0; check_sources 8888 > /dev/null || rc=$?", "T=$SECONDS",
        'echo "RC=$rc CALLS=$(cat "$CF") T=$T"; rm -f "$CF"',
    ])
    r = subprocess.run([b, "-c", harness], capture_output=True, text=True, timeout=60)
    m = re.search(r"RC=(\d+) CALLS=(\d+) T=(\d+)", r.stdout)
    assert m, r.stdout + r.stderr
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), r.stderr


def _src(journal):
    return json.dumps({"sources": {"slots": {"ok": True}, "journal": {"ok": journal}, "gpu": {"ok": True},
                                   "host": {"ok": True}}})


def test_check_sources_retries_until_sources_come_up():
    rc, calls, t, _ = _run_check_sources([_src(False), _src(False), _src(True)])
    assert rc == 0 and calls == 3 and t < 15


def test_check_sources_gives_up_after_about_15_seconds():
    rc, calls, t, err = _run_check_sources([_src(False)])
    assert rc == 1 and calls >= 3 and 14 <= t <= 17
    assert "sources not ok: journal" in err


def test_cutover_enables_the_unit_so_8888_survives_a_reboot():
    cut = _script().split("cutover)", 1)[1].split("rollback)", 1)[0]
    assert "systemctl enable live-dashboard" in cut
    assert cut.index("systemctl enable live-dashboard") < cut.index("systemctl restart live-dashboard")


def test_unit_stops_on_bad_config_and_limits_restarts():
    u = open(os.path.join(ROOT, "files", "live-dashboard.service"), encoding="utf-8").read()
    unit, service = u.split("[Service]", 1)
    assert "StartLimitIntervalSec=300" in unit and "StartLimitBurst=10" in unit      # [Unit] keys
    assert "RestartPreventExitStatus=2" in service.split("[Install]", 1)[0]


def test_deploy_script_is_executable_in_the_index():
    import subprocess
    r = subprocess.run(["git", "ls-files", "-s", "scripts/64-live-dashboard.sh"], cwd=os.path.join(ROOT, ".."),
                       capture_output=True, text=True, timeout=30)
    assert r.stdout.startswith("100755"), r.stdout
