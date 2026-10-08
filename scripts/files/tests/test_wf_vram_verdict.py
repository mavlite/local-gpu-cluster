"""77's verdict: GPU readings from LXC 151 are data, never code (security review HIGH)."""
import json
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wf-vram-verdict.py")


def verdict(tmp_path, fits, units, concurrent, before, after):
    b, a = tmp_path / "before.json", tmp_path / "after.json"
    b.write_text(before)
    a.write_text(after)
    r = subprocess.run([sys.executable, SCRIPT, fits, units, concurrent, str(b), str(a)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_valid_readings_are_parsed(tmp_path):
    v = verdict(tmp_path, "true", "active active active", "ok",
                '{"card0": {"VRAM Total Used Memory (B)": "1"}}', '{"card0": {"x": false}}')
    assert v["fits"] is True and v["units_with_rag"] == ["active", "active", "active"]
    assert v["vram_3slot_with_rag"] == {"card0": {"x": False}}


def test_an_injection_attempt_is_kept_as_text_and_never_executed(tmp_path):
    marker = tmp_path / "pwned"
    evil = f'{{}}; open(r"{marker}", "w").write("x"); {{}}'
    v = verdict(tmp_path, "false", "active failed", "skipped", evil, "WARNING: not json")
    assert not marker.exists()
    assert v["fits"] is False and v["vram_3slot_no_rag"] == {"raw": evil}
    assert v["vram_3slot_with_rag"] == {"raw": "WARNING: not json"}
