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
