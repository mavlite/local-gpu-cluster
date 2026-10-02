import json
from scripts.delegate.ledger import Ledger

def test_append_then_read_roundtrip(tmp_path):
    led = Ledger(str(tmp_path / "l.jsonl"))
    led.append({"ulid": "01", "delegated": True, "claude_tokens": 12})
    led.append({"ulid": "02", "delegated": False, "claude_tokens": 40})
    rows = led.read_all()
    assert [r["ulid"] for r in rows] == ["01", "02"]
    assert all("ts" in r for r in rows)

def test_append_is_one_line_per_record(tmp_path):
    p = tmp_path / "l.jsonl"
    led = Ledger(str(p))
    led.append({"ulid": "01"})
    led.append({"ulid": "02"})
    assert p.read_text(encoding="utf-8").count("\n") == 2
    # each line is valid json
    for line in p.read_text(encoding="utf-8").splitlines():
        json.loads(line)
