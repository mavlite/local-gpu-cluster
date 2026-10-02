import json
import threading
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


def test_concurrent_appends_produce_n_valid_lines(tmp_path):
    """A single lock-guarded writer: N threads appending yield N intact JSONL lines."""
    p = tmp_path / "l.jsonl"
    led = Ledger(str(p))
    n = 200
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()  # maximize contention
        led.append({"i": i, "pad": "x" * 500})  # long records make interleaving visible

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == n
    seen = {json.loads(ln)["i"] for ln in lines}  # every line parses; none torn
    assert seen == set(range(n))
