"""live_dashboard.history -- SQLite request store, single writer, bands, prune, caps."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard.history import History, band_of  # noqa: E402

DAY = 86400.0


def row(key, finished, prompt, gen=40.0, pre=500.0, status="done"):
    return {"key": key, "server_run": 1.0, "slot": 0, "task": 1, "started": finished - 5, "finished": finished,
            "status": status, "prompt_tokens": prompt, "new_prompt_tokens": prompt, "cached_tokens": 0,
            "output_tokens": 100, "prefill_tok_s": pre, "gen_tok_s": gen, "duration_s": 5.0,
            "draft_accept": 0.8, "slow": False}


def test_band_edges():
    assert band_of(0) == "0-8K" and band_of(8191) == "0-8K" and band_of(8192) == "8-32K"
    assert band_of(65535) == "32-64K" and band_of(98303) == "64-96K" and band_of(131072) == "96-128K"
    assert band_of(None) is None


def test_add_recent_newest_first_and_upsert(tmp_path):
    h = History(str(tmp_path / "h.db"))
    h.add(row("a", 100.0, 1000))
    h.add(row("b", 200.0, 2000))
    h.add(row("a", 100.0, 1500))                     # same key replaces
    assert h.flush()
    keys = [r["key"] for r in h.recent(10)]
    assert keys == ["b", "a"] and h.recent(10)[1]["prompt_tokens"] == 1500
    assert h.recent(10)[0]["slow"] is False
    h.close()


def test_limit_and_days_are_clamped(tmp_path):
    h = History(str(tmp_path / "h.db"))
    for i in range(3):
        h.add(row(f"k{i}", 100.0 + i, 1000))
    h.flush()
    assert len(h.recent(0)) == 1 and len(h.recent(10_000)) == 3
    assert h.bands(10_000, now=200.0) == h.bands(30, now=200.0)
    h.close()


def test_bands_use_medians_and_skip_non_done(tmp_path):
    h = History(str(tmp_path / "h.db"))
    now = 10 * DAY
    h.add(row("a", now - 10, 1000, gen=40.0, pre=600.0))
    h.add(row("b", now - 20, 2000, gen=50.0, pre=500.0))
    h.add(row("c", now - 30, 3000, gen=60.0, pre=400.0))
    h.add(row("d", now - 40, 70000, gen=30.0, pre=300.0))
    h.add(row("e", now - 50, 1000, gen=1.0, pre=1.0, status="approx"))
    h.add(row("old", now - 8 * DAY, 1000, gen=99.0, pre=99.0))
    h.flush()
    b = {x["band"]: x for x in h.bands(7, now=now)}
    assert b["0-8K"]["n"] == 3 and b["0-8K"]["gen_tok_s"] == 50.0 and b["0-8K"]["prefill_tok_s"] == 500.0
    assert b["64-96K"]["n"] == 1 and "8-32K" not in b
    h.close()


def test_prune_and_survives_reopen(tmp_path):
    p = str(tmp_path / "h.db")
    h = History(p, retention_days=30)
    h.add(row("old", 1.0, 1000))
    h.add(row("new", 40 * DAY, 1000))
    h.flush()
    assert h.prune(now=40 * DAY + 1) == 1
    h.close()
    h2 = History(p)
    assert [r["key"] for r in h2.recent(10)] == ["new"]
    h2.close()


def test_unwritable_path_sets_ok_false_without_raising(tmp_path):
    bad = tmp_path / "dir_not_file"
    bad.mkdir()
    h = History(str(bad))                            # a directory cannot be opened as a database
    assert h.ok is False
    h.add(row("x", 1.0, 1))                          # never raises
    assert h.recent(5) == [] and h.bands(7, now=2.0) == []
    h.close()


def test_flush_false_when_writer_dead_and_after_close(tmp_path):
    h = History(str(tmp_path / "h.db"))
    h.close()
    assert h.flush(timeout=1.0) is False
    h.add(row("x", 1.0, 1))                          # after close: dropped, never raises
    assert h.prune(5.0) == 0


def test_writer_survives_bad_row_and_ok_stays_true(tmp_path):
    h = History(str(tmp_path / "h.db"))
    h.add({"key": "bad", "slow": object()})           # unbindable value -> not a database failure
    h.add({"slow": False})                            # missing key -> NOT NULL-less PK; must not kill writer
    h.add(row("good", 100.0, 1000))
    assert h.flush()
    assert h.ok is True
    assert "good" in [r["key"] for r in h.recent(10)]
    h.close()


def test_disabled_store_drops_writes_instead_of_queueing(tmp_path):
    bad = tmp_path / "d"
    bad.mkdir()
    h = History(str(bad))
    for i in range(50):
        h.add(row(f"k{i}", 1.0, 1))
    assert h._q.qsize() == 0
    assert h.flush(timeout=1.0) is False
    h.close()


def test_queue_is_bounded(tmp_path):
    h = History(str(tmp_path / "h.db"))
    assert h._q.maxsize > 0
    h.close()


def test_prune_called_positionally(tmp_path):
    h = History(str(tmp_path / "h.db"))
    h.add(row("old", 1.0, 1000))
    h.flush()
    assert h.prune(40 * DAY) == 1
    h.close()
