"""live_dashboard.tracker -- request lifecycle from journal events + /slots snapshots."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard.tracker import Tracker  # noqa: E402


def ev(kind, t, **kw):
    return dict(kind=kind, since_start_s=t, **kw)


def slots(per=None):
    per = per or {}
    out = []
    for i in range(3):
        s = {"id": i, "n_ctx": 131072, "is_processing": False, "id_task": None, "n_prompt_tokens": 0,
             "processed": 0, "cache": 0, "n_decoded": 0}
        s.update(per.get(i, {}))
        out.append(s)
    return out


def full_request(tr, t0=100.0, slot=1, task=4772):
    tr.on_journal(ev("start", 10.0, slot=slot, task=task), t0)
    tr.on_journal(ev("prefill", 13.0, slot=slot, task=task, n=2048, progress=0.55, tok_s=665.88), t0 + 3)
    tr.on_journal(ev("prefill", 16.0, slot=slot, task=task, n=3723, progress=1.0, tok_s=624.75), t0 + 6)
    tr.on_slots(slots({slot: {"is_processing": True, "id_task": task, "n_prompt_tokens": 3735, "n_decoded": 8}}), t0 + 7)
    tr.on_journal(ev("prompt_eval", 16.6, slot=slot, task=task, ms=6154.43, n=3727), t0 + 7.4)
    tr.on_journal(ev("eval", 16.6, slot=slot, task=task, ms=459.73, n=23), t0 + 7.4)
    tr.on_journal(ev("total", 16.6, slot=slot, task=task, ms=6614.16, n=3750), t0 + 7.4)
    tr.on_journal(ev("draft", 16.6, slot=slot, task=task, rate=0.7619, accepted=16, generated=21), t0 + 7.4)
    tr.on_journal(ev("release", 16.6, slot=slot, task=task, n=3750), t0 + 7.4)


def test_reading_progress_from_journal():
    tr = Tracker()
    tr.on_journal(ev("start", 10.0, slot=1, task=4772), 100.0)
    tr.on_journal(ev("prefill", 13.0, slot=1, task=4772, n=2048, progress=0.55, tok_s=665.88), 103.0)
    v = tr.slot_views(103.0)[1]
    assert v["state"] == "reading" and v["progress"] == 0.55 and v["processed_tokens"] == 2048
    assert v["tok_s"] == 665.88 and v["task"] == 4772
    assert tr.slot_views(103.0)[0]["state"] == "idle"


def test_generating_from_slots_then_done_row_from_journal():
    tr = Tracker()
    tr.on_journal(ev("start", 10.0, slot=1, task=4772), 100.0)
    tr.on_slots(slots({1: {"is_processing": True, "id_task": 4772, "n_prompt_tokens": 3735, "n_decoded": 8}}), 107.0)
    tr.on_slots(slots({1: {"is_processing": True, "id_task": 4772, "n_prompt_tokens": 3745, "n_decoded": 18}}), 108.0)
    v = tr.slot_views(108.0)[1]
    assert v["state"] == "generating" and v["output_tokens"] == 18 and v["tok_s"] == 10.0 and v["ctx_used"] == 3745
    tr2 = Tracker()
    full_request(tr2)
    rows = tr2.pop_finished()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "done" and r["slot"] == 1 and r["task"] == 4772
    assert r["prompt_tokens"] == 3727 and r["new_prompt_tokens"] == 3727 and r["cached_tokens"] == 0
    assert r["output_tokens"] == 23 and abs(r["prefill_tok_s"] - 3727 / 6.15443) < 1e-6
    assert abs(r["gen_tok_s"] - 23 / 0.45973) < 1e-6 and abs(r["duration_s"] - 6.61416) < 1e-9
    assert abs(r["draft_accept"] - 16 / 21) < 1e-9 and r["slow"] is False
    assert tr2.pop_finished() == [] and tr2.slot_views(108.0)[1]["state"] == "idle"
    assert tr2.slot_views(108.0)[1]["last_done"] == 107.4


def test_cached_prompt_and_slow_flag():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=9), 10.0)
    tr.on_journal(ev("prompt_eval", 2.0, slot=0, task=9, ms=10000.0, n=6000), 20.0)
    tr.on_journal(ev("eval", 2.0, slot=0, task=9, ms=1000.0, n=50), 20.0)
    tr.on_journal(ev("total", 2.0, slot=0, task=9, ms=11000.0, n=40050), 20.0)
    tr.on_journal(ev("release", 2.0, slot=0, task=9, n=40050), 20.0)
    r = tr.pop_finished()[0]
    assert r["prompt_tokens"] == 40000 and r["new_prompt_tokens"] == 6000 and r["cached_tokens"] == 34000
    assert r["slow"] is True and r["draft_accept"] is None


def test_cancelled_request():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=2, task=5), 10.0)
    tr.on_journal(ev("cancel", 3.0, task=5), 12.0)
    tr.on_journal(ev("release", 3.0, slot=2, task=5, n=100), 12.0)
    r = tr.pop_finished()[0]
    assert r["status"] == "cancelled" and r["output_tokens"] is None


def test_restart_interrupts_open_rows_and_keys_do_not_collide():
    tr = Tracker()
    tr.on_journal(ev("start", 500.0, slot=0, task=16), 100.0)
    tr.on_journal(ev("server_start", 0.01), 110.0)
    rows = tr.pop_finished()
    assert len(rows) == 1 and rows[0]["status"] == "interrupted" and rows[0]["finished"] == 110.0
    assert all(v["state"] == "restarting" for v in tr.slot_views(111.0))
    tr.on_journal(ev("server_ready", 22.0), 132.0)
    assert all(v["state"] == "idle" for v in tr.slot_views(133.0))
    tr.on_journal(ev("start", 30.0, slot=0, task=16), 140.0)
    assert tr.open_rows()[0]["key"] != rows[0]["key"]


def test_prefix_going_backwards_is_a_restart():
    tr = Tracker()
    tr.on_journal(ev("start", 600.0, slot=0, task=1), 100.0)
    tr.on_journal(ev("start", 5.0, slot=1, task=1), 200.0)
    rows = tr.pop_finished()
    assert len(rows) == 1 and rows[0]["status"] == "interrupted"
    assert tr.open_rows()[0]["slot"] == 1


def test_journal_down_gives_approx_rows_from_slots():
    tr = Tracker(approx_grace_s=0.0)
    tr.set_journal_up(False, 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 77, "n_prompt_tokens": 900, "n_decoded": 0}}), 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 77, "n_prompt_tokens": 950, "n_decoded": 50}}), 102.0)
    tr.on_slots(slots(), 103.0)
    r = tr.pop_finished()[0]
    assert r["status"] == "approx" and r["output_tokens"] == 50 and r["task"] == 77 and r["started"] == 100.0


def test_slots_unreachable_interrupts_after_threshold():
    tr = Tracker(unreachable_s=10.0)
    tr.on_journal(ev("start", 1.0, slot=0, task=3), 100.0)
    tr.set_journal_up(False, 100.0)                  # fix round: interrupt-on-unreachable applies only if journal is down
    tr.on_slots(None, 105.0)
    tr.on_slots(None, 114.0)                         # 9 s unreachable: still open
    assert tr.pop_finished() == []
    tr.on_slots(None, 115.5)                         # 10.5 s: interrupted
    assert tr.pop_finished()[0]["status"] == "interrupted"


def test_release_without_timings_and_no_cancel_is_approx():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=3), 100.0)
    tr.on_journal(ev("release", 2.0, slot=0, task=3, n=10), 101.0)
    assert tr.pop_finished()[0]["status"] == "approx"


def test_server_start_while_stuck_restarting_still_interrupts_and_rekeys():
    tr = Tracker()
    tr.on_journal(ev("start", 600.0, slot=0, task=1), 100.0)
    tr.on_journal(ev("server_start", 0.01), 110.0)
    first = tr.pop_finished()
    assert len(first) == 1
    tr.on_slots(None, 111.0)
    # server_ready was missed: two more server_start lines arrive while still restarting
    tr.on_journal(ev("server_start", 0.02), 112.0)
    tr.on_journal(ev("server_start", 0.03), 113.0)
    assert tr.server_run == 113.0
    tr.on_journal(ev("start", 5.0, slot=0, task=1), 120.0)
    assert tr.open_rows()[0]["key"] != first[0]["key"]
    tr.on_journal(ev("server_start", 0.01), 130.0)
    rows = tr.pop_finished()
    assert len(rows) == 1 and rows[0]["status"] == "interrupted" and rows[0]["finished"] == 130.0
    tr.on_journal(ev("start", 5.0, slot=0, task=1), 140.0)
    assert tr.open_rows()[0]["key"] != rows[0]["key"]


def test_missed_server_ready_cleared_by_serving_event_or_slots():
    tr = Tracker()
    tr.on_journal(ev("server_start", 0.01), 110.0)
    assert tr.slot_views(111.0)[0]["state"] == "restarting"
    tr.on_journal(ev("start", 30.0, slot=0, task=1), 140.0)
    assert tr.slot_views(141.0)[0]["state"] == "reading"
    tr2 = Tracker()
    tr2.on_journal(ev("server_start", 0.01), 110.0)
    tr2.on_slots(slots({2: {"is_processing": True, "id_task": 9, "n_prompt_tokens": 5}}), 112.0)
    assert tr2.restarting is False


def test_task_mismatch_journal_up_ignores_entry():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=10), 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 10, "n_prompt_tokens": 100, "n_decoded": 50}}), 101.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 11, "n_prompt_tokens": 100, "n_decoded": 3}}), 102.0)
    v = tr.slot_views(102.0)[0]
    assert v["task"] == 10 and v["output_tokens"] == 50
    assert v["tok_s"] is None or v["tok_s"] >= 0
    assert tr.pop_finished() == []


def test_task_mismatch_journal_down_splits_rows():
    tr = Tracker(approx_grace_s=0.0)
    tr.set_journal_up(False, 100.0)
    for t, task, dec in ((100.0, 77, 40), (101.0, 77, 90), (102.0, 78, 3)):
        tr.on_slots(slots({0: {"is_processing": True, "id_task": task, "n_prompt_tokens": 9, "n_decoded": dec}}), t)
    assert tr.slot_views(102.0)[0]["tok_s"] is None or tr.slot_views(102.0)[0]["tok_s"] >= 0
    tr.on_slots(slots(), 103.0)
    rows = tr.pop_finished()
    assert [(r["task"], r["output_tokens"], r["status"]) for r in rows] == [(77, 90, "approx"), (78, 3, "approx")]


def test_prefill_rate_never_shown_as_gen_speed():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=1, task=4), 100.0)
    tr.on_journal(ev("prefill", 2.0, slot=1, task=4, n=2000, progress=1.0, tok_s=600.0), 102.0)
    assert tr.slot_views(102.0)[1]["tok_s"] == 600.0
    tr.on_slots(slots({1: {"is_processing": True, "id_task": 4, "n_prompt_tokens": 2005, "n_decoded": 5}}), 103.0)
    assert tr.slot_views(103.0)[1]["tok_s"] is None
    tr.on_journal(ev("prefill", 3.0, slot=1, task=4, n=2000, progress=1.0, tok_s=600.0), 103.5)
    assert tr.slot_views(103.5)[1]["tok_s"] is None


def test_journal_gen_between_polls_does_not_distort_delta():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=1, task=4), 100.0)
    tr.on_slots(slots({1: {"is_processing": True, "id_task": 4, "n_prompt_tokens": 10, "n_decoded": 8}}), 107.0)
    tr.on_journal(ev("gen", 2.0, slot=1, task=4, n=15, tok_s=70.0), 107.5)
    tr.on_slots(slots({1: {"is_processing": True, "id_task": 4, "n_prompt_tokens": 20, "n_decoded": 18}}), 108.0)
    v = tr.slot_views(108.0)[1]
    assert v["tok_s"] == 10.0 and v["output_tokens"] == 18


def test_unreachable_with_journal_up_keeps_rows_until_journal_finishes():
    tr = Tracker(unreachable_s=10.0)
    tr.on_journal(ev("start", 1.0, slot=0, task=3), 100.0)
    tr.on_slots(None, 105.0)
    tr.on_slots(None, 135.0)
    assert tr.pop_finished() == []
    tr.on_journal(ev("eval", 2.0, slot=0, task=3, ms=1000.0, n=10), 136.0)
    tr.on_journal(ev("total", 2.0, slot=0, task=3, ms=2000.0, n=60), 136.0)
    tr.on_journal(ev("release", 2.0, slot=0, task=3, n=60), 136.0)
    assert tr.pop_finished()[0]["status"] == "done"


def test_unreachable_journal_down_interrupts_and_rekeys():
    tr = Tracker(unreachable_s=10.0)
    tr.set_journal_up(False, 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 5, "n_prompt_tokens": 9}}), 100.0)
    tr.on_slots(None, 101.0)
    tr.on_slots(None, 112.0)
    rows = tr.pop_finished()
    assert len(rows) == 1 and rows[0]["status"] == "interrupted"
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 5, "n_prompt_tokens": 9}}), 120.0)
    assert tr.open_rows()[0]["key"] != rows[0]["key"]


def test_journal_start_adopts_row_opened_from_slots():
    tr = Tracker()
    tr.set_journal_up(False, 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 5, "n_prompt_tokens": 9}}), 100.0)
    key = tr.open_rows()[0]["key"]
    tr.set_journal_up(True, 101.0)
    tr.on_journal(ev("start", 1.0, slot=0, task=5), 101.0)
    assert tr.pop_finished() == []
    assert len(tr.open_rows()) == 1 and tr.open_rows()[0]["key"] == key


def test_reading_view_derives_prompt_and_ctx_from_journal():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=5), 100.0)
    tr.on_journal(ev("prefill", 2.0, slot=0, task=5, n=2048, progress=0.5, tok_s=600.0), 102.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 5, "n_prompt_tokens": 7, "n_decoded": 0}}), 102.5)
    v = tr.slot_views(102.5)[0]
    assert v["prompt_tokens"] == 4096 and v["ctx_used"] == 2048


def test_cancelled_row_has_duration_and_last_output():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=2, task=5), 10.0)
    tr.on_slots(slots({2: {"is_processing": True, "id_task": 5, "n_prompt_tokens": 9, "n_decoded": 30}}), 11.0)
    tr.on_journal(ev("cancel", 3.0, task=5), 12.0)
    tr.on_journal(ev("release", 3.0, slot=2, task=5, n=100), 12.0)
    r = tr.pop_finished()[0]
    assert r["status"] == "cancelled" and r["duration_s"] == 2.0 and r["output_tokens"] == 30


def test_approx_release_uses_release_n_fallback():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=3), 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 3, "n_prompt_tokens": 9, "n_decoded": 20}}), 101.0)
    tr.on_journal(ev("release", 2.0, slot=0, task=3, n=120), 102.0)
    r = tr.pop_finished()[0]
    assert r["status"] == "approx" and r["prompt_tokens"] == 100 and r["output_tokens"] == 20


def test_finish_clears_idle_since_so_next_row_gets_full_grace():
    tr = Tracker(approx_grace_s=10.0)
    tr.on_journal(ev("start", 1.0, slot=0, task=1), 100.0)
    tr.on_slots(slots(), 101.0)                          # idle, row open: idle_since[0] = 101
    tr.on_journal(ev("release", 2.0, slot=0, task=1, n=1), 102.0)
    assert 0 not in tr.idle_since
    tr.on_journal(ev("start", 3.0, slot=0, task=2), 150.0)
    tr.on_slots(slots(), 155.0)                          # only 0 s idle for the new row
    assert tr.open_rows()[0]["task"] == 2 and tr.pop_finished()[-1]["task"] == 1


def test_journal_gap_does_not_compare_prefixes_across_runs():
    tr = Tracker()
    tr.on_journal(ev("start", 9000.0, slot=0, task=0), 50.0)       # old run, stale last_since
    tr.on_journal(ev("release", 9001.0, slot=0, task=0, n=1), 51.0)
    tr.pop_finished()
    tr.set_journal_up(False, 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 0, "n_prompt_tokens": 9, "n_decoded": 0}}), 100.0)
    tr.set_journal_up(True, 101.0)
    tr.on_journal(ev("start", 3.0, slot=0, task=0), 101.0)
    assert tr.slot_views(101.5)[0]["state"] != "idle"
    tr.on_journal(ev("prompt_eval", 4.0, slot=0, task=0, ms=1000.0, n=100), 102.0)
    tr.on_journal(ev("eval", 5.0, slot=0, task=0, ms=1000.0, n=10), 102.0)
    tr.on_journal(ev("total", 5.0, slot=0, task=0, ms=2000.0, n=110), 102.0)
    tr.on_journal(ev("release", 5.0, slot=0, task=0, n=110), 102.0)
    rows = tr.pop_finished()
    assert [r["status"] for r in rows] == ["done"]


def test_idle_unreachable_episode_rekeys_once():
    tr = Tracker(approx_grace_s=0.0, unreachable_s=10.0)
    tr.set_journal_up(False, 100.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 0, "n_prompt_tokens": 9}}), 100.0)
    tr.on_slots(slots(), 101.0)
    first = tr.pop_finished()[0]
    tr.on_slots(None, 102.0)
    tr.on_slots(None, 113.0)
    run = tr.server_run
    tr.on_slots(None, 120.0)
    assert tr.server_run == run                      # once per episode
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 0, "n_prompt_tokens": 9}}), 130.0)
    assert tr.open_rows()[0]["key"] != first["key"]


def test_progress_is_complete_once_generating():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=5), 100.0)
    tr.on_journal(ev("prefill", 2.0, slot=0, task=5, n=2048, progress=0.55, tok_s=600.0), 102.0)
    tr.on_slots(slots({0: {"is_processing": True, "id_task": 5, "n_prompt_tokens": 9, "n_decoded": 4}}), 103.0)
    assert tr.slot_views(103.0)[0]["progress"] == 1.0
