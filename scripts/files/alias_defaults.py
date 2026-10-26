"""Per-alias request defaults the router injects before forwarding upstream.

Stdlib only so the unit tests can import it without FastAPI; app.py imports it
as a sibling module (WorkingDirectory=/opt/llm-router).

Every default is applied with setdefault semantics: a value the client sent,
including an explicit null, always wins.
"""
import copy

# Qwen/Qwen3.8-27B model card, unchanged since 2026-08-14 (re-checked
# 2026-09-17). Without these, clients that send no sampling get llama-server's
# generic defaults (temp 0.8, top_k 40, min_p 0.05, no presence penalty).
QWEN38_THINK_SAMPLING = {
    "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
    "presence_penalty": 0.0,
}
QWEN38_NOTHINK_SAMPLING = {
    "temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
    "presence_penalty": 1.5,
}

# Qwen/Qwen3.6-35B-A3B model card (fetched 2026-10-26). The card lists a
# third recipe for precise coding (temp 0.6, presence_penalty 0.0); coding
# clients such as opencode send that themselves, so the router injects the
# general thinking recipe. Applies to the -fast (Q4) profile too: same model.
QWEN36_THINK_SAMPLING = {
    "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
    "presence_penalty": 1.5,
}
QWEN36_NOTHINK_SAMPLING = {
    "temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
    "presence_penalty": 1.5,
}

# OBLITERATUS/Qwen3.8-27B-OBLITERATED V3 model card (fetched 2026-10-17):
# repetition_penalty 1.15 is "Essential", agentic temperature 0.1-0.3
# (0.2 = mid-range), top_p/top_k/min_p "Not needed" — greedy + rep-pen is
# the card's recipe, so no other sampling keys are injected. The card gives
# no per-mode split, so both thinking modes share this dict.
OBLITERATED_SAMPLING = {
    "temperature": 0.2,
    "repetition_penalty": 1.15,
}


def apply_alias_defaults(body: dict, alias_info: dict) -> dict:
    """Return a copy of body with the alias's backend, template kwargs and
    sampling defaults applied. The input body is not modified."""
    out = copy.deepcopy(body)
    if "model" in out and alias_info["backend"] != out["model"]:
        out["model"] = alias_info["backend"]

    template_defaults = {}
    if alias_info.get("enable_thinking") is not None:
        template_defaults["enable_thinking"] = alias_info["enable_thinking"]
    if alias_info.get("reasoning_effort"):
        template_defaults["reasoning_effort"] = alias_info["reasoning_effort"]
    if template_defaults:
        kwargs = out.setdefault("chat_template_kwargs", {})
        for key, value in template_defaults.items():
            kwargs.setdefault(key, value)

    for key, value in (alias_info.get("sampling") or {}).items():
        out.setdefault(key, value)
    return out
