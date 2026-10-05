import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import llama_log  # noqa: E402

# Verbatim from LXC 151 llamacpp-chat (b11026), 2026-10-05.
MAINLINE = """\
125.08.644.985 I slot launch_slot_: id  2 | task 45174 | processing task, is_child = 0
125.14.711.139 I slot print_timing: id  2 | task 45174 | n_gen =    145, tg =  46.87 t/s, tg_3s =  47.19 t/s
125.17.008.216 I slot print_timing: id  2 | task 45174 | prompt eval time =    2989.10 ms /   818 tokens (    3.65 ms per token,   273.66 tokens per second)
125.17.008.219 I slot print_timing: id  2 | task 45174 |        eval time =    5369.39 ms /   251 tokens (   21.48 ms per token,    46.56 tokens per second)
125.17.008.219 I slot print_timing: id  2 | task 45174 |       total time =    8358.49 ms /  1069 tokens
125.17.008.222 I slot print_timing: id  2 | task 45174 | draft acceptance = 1.00000 (  189 accepted /   189 generated), mean len =  4.00
125.17.010.875 I slot      release: id  2 | task 45174 | stop processing: n_tokens = 72817, truncated = 0
125.03.842.776 I slot print_timing: id  1 | task 45111 | prompt processing, n_tokens =  71139, progress = 0.99, t = 164.31 s / 432.95 tokens per second
125.08.417.107 I slot print_timing: id  1 | task 45111 | prompt eval time =  166998.69 ms / 71655 tokens (    2.33 ms per token,   429.08 tokens per second)
125.08.417.110 I slot print_timing: id  1 | task 45111 |        eval time =    2095.34 ms /    91 tokens (   23.28 ms per token,    42.95 tokens per second)
125.08.417.111 I slot print_timing: id  1 | task 45111 |       total time =  169094.03 ms / 71746 tokens
"""

# ik_llama.cpp server-context.cpp print_timings(): SLT_INF prefix, then "\n" + unprefixed block.
IK = """\
INFO [print_timings] slot print_timing: id  0 | task 7 | 
prompt eval time =    5123.40 ms /  12001 tokens (    0.43 ms per token,  2342.37 tokens per second)
       eval time =   20011.00 ms /   600 tokens (   33.35 ms per token,    29.98 tokens per second)
      total time =   25134.40 ms / 12601 tokens
INFO [print_timings] slot print_timing: id  0 | task 9 | 
prompt eval time =     310.00 ms /    70 tokens (    4.43 ms per token,   225.81 tokens per second)
       eval time =    3000.00 ms /    90 tokens (   33.33 ms per token,    30.00 tokens per second)
      total time =    3310.00 ms /   160 tokens
"""


def test_mainline_record_with_cache_from_release_line():
    recs = llama_log.parse(MAINLINE)
    first = recs[0]
    assert (first["slot"], first["task"], first["prompt_n"], first["gen_n"]) == (2, 45174, 818, 251)
    assert first["cache_n"] == 72817 - 818 - 251
    assert recs[1]["task"] == 45111 and recs[1]["prompt_n"] == 71655 and recs[1]["cache_n"] is None


def test_progress_lines_are_not_records():
    assert len(llama_log.parse(MAINLINE)) == 2


def test_ik_unprefixed_block_takes_task_from_previous_line():
    recs = llama_log.parse(IK)
    assert [(r["task"], r["prompt_n"], r["gen_n"]) for r in recs] == [(7, 12001, 600), (9, 70, 90)]
    assert all(r["cache_n"] is None for r in recs)


def test_summary_counts_large_prefills():
    s = llama_log.summarize(llama_log.parse(MAINLINE + IK))
    assert s["requests"] == 4
    assert s["large_prefills"] == 2                 # 71655 and 12001
    assert s["gen_tokens"] == 251 + 91 + 600 + 90
    assert s["cache_n_total"] == 72817 - 818 - 251


def test_summary_of_empty_log():
    assert llama_log.summarize([]) == {"requests": 0, "prompt_tokens": 0, "gen_tokens": 0,
                                       "large_prefills": 0, "cache_n_total": None}
