"""Hidden checks for w2-13: a look-alike 'used' label ahead of the total does not become the total."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm  # noqa: E402


def test_a_look_alike_used_label_is_not_taken_as_the_total():
    text = json.dumps({
        "card0": {"VRAM Total Memory Used (B)": "2147483648",
                  "VRAM Total Used Memory (B)": "2147483648",
                  "VRAM Total Memory (B)": "34359738368"},
        "card1": {"VRAM Total Memory (B)": "34359738368", "VRAM Total Used Memory (B)": "0"},
    })
    rows = cm.parse_rocm_vram_json(text)
    assert [(i, round(u), round(t)) for i, u, t in rows] == [(0, 2048, 32768), (1, 0, 32768)]
