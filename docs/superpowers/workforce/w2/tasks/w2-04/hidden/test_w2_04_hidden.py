"""Hidden checks for w2-04: setdefault semantics end to end."""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alias_defaults as ad  # noqa: E402

NOTHINK = {"backend": "qwen3.8", "enable_thinking": False, "sampling": ad.QWEN38_NOTHINK_SAMPLING}


def test_an_explicit_null_from_the_client_wins_and_the_input_is_untouched():
    body = {"model": "qwen3.8-nothink", "temperature": None, "messages": []}
    before = copy.deepcopy(body)
    out = ad.apply_alias_defaults(body, NOTHINK)
    assert out["temperature"] is None and out["top_p"] == 0.8 and out["presence_penalty"] == 1.5
    assert out["model"] == "qwen3.8" and body == before


def test_existing_template_kwargs_are_kept_and_missing_ones_added():
    body = {"model": "qwen3.8-nothink", "chat_template_kwargs": {"enable_thinking": True, "x": 1}}
    out = ad.apply_alias_defaults(body, NOTHINK)
    assert out["chat_template_kwargs"] == {"enable_thinking": True, "x": 1}
    out = ad.apply_alias_defaults({"model": "qwen3.8-nothink"}, NOTHINK)
    assert out["chat_template_kwargs"] == {"enable_thinking": False}
