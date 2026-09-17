"""Unit tests for the router's per-alias request defaults.

Stdlib only, like test_tavily_cache.py: router-app.py pulls in FastAPI, so the
ALIAS_MAP checks read it with `ast` instead of importing it.
"""
import ast
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

import alias_defaults as ad

ROUTER_SRC = os.path.join(HERE, "..", "router-app.py")


def _alias_map_nodes():
    """Return {alias: {field: ast node}} for ALIAS_MAP in router-app.py."""
    tree = ast.parse(open(ROUTER_SRC, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "ALIAS_MAP":
            return {
                k.value: {fk.value: fv for fk, fv in zip(v.keys, v.values)}
                for k, v in zip(node.value.keys, node.value.values)
            }
    raise AssertionError("ALIAS_MAP not found in router-app.py")


NOTHINK = {"backend": "qwen3.8", "enable_thinking": False, "strip_thinking": True,
           "sampling": ad.QWEN38_NOTHINK_SAMPLING}
THINK = {"backend": "qwen3.8", "enable_thinking": True, "strip_thinking": False,
         "reasoning_effort": "medium", "sampling": ad.QWEN38_THINK_SAMPLING}
PASSTHROUGH = {"backend": "devstral", "enable_thinking": None, "strip_thinking": False}


class TestSamplingConstants(unittest.TestCase):
    def test_nothink_matches_qwen_model_card(self):
        self.assertEqual(ad.QWEN38_NOTHINK_SAMPLING, {
            "temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 1.5})

    def test_think_matches_qwen_model_card(self):
        self.assertEqual(ad.QWEN38_THINK_SAMPLING, {
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 0.0})


class TestApplyAliasDefaults(unittest.TestCase):
    def test_rewrites_model_to_backend(self):
        out = ad.apply_alias_defaults({"model": "qwen3.8-nothink"}, NOTHINK)
        self.assertEqual(out["model"], "qwen3.8")

    def test_injects_thinking_off_and_sampling(self):
        out = ad.apply_alias_defaults({"model": "qwen3.8-nothink"}, NOTHINK)
        self.assertIs(out["chat_template_kwargs"]["enable_thinking"], False)
        self.assertNotIn("reasoning_effort", out["chat_template_kwargs"])
        self.assertEqual(out["presence_penalty"], 1.5)
        self.assertEqual(out["temperature"], 0.7)

    def test_injects_reasoning_effort_for_think(self):
        out = ad.apply_alias_defaults({"model": "qwen3.8-think"}, THINK)
        self.assertEqual(out["chat_template_kwargs"],
                         {"enable_thinking": True, "reasoning_effort": "medium"})
        self.assertEqual(out["temperature"], 1.0)

    def test_client_values_win(self):
        body = {"model": "qwen3.8-nothink", "temperature": 0.2, "presence_penalty": 0.0,
                "chat_template_kwargs": {"enable_thinking": True}}
        out = ad.apply_alias_defaults(body, NOTHINK)
        self.assertEqual(out["temperature"], 0.2)
        self.assertEqual(out["presence_penalty"], 0.0)
        self.assertIs(out["chat_template_kwargs"]["enable_thinking"], True)
        self.assertEqual(out["top_k"], 20)

    def test_explicit_null_is_left_alone(self):
        out = ad.apply_alias_defaults({"model": "qwen3.8-nothink", "top_p": None}, NOTHINK)
        self.assertIsNone(out["top_p"])

    def test_does_not_mutate_input(self):
        body = {"model": "qwen3.8-think", "chat_template_kwargs": {"x": 1}}
        ad.apply_alias_defaults(body, THINK)
        self.assertEqual(body, {"model": "qwen3.8-think", "chat_template_kwargs": {"x": 1}})

    def test_missing_model_is_not_added(self):
        unknown = {"backend": "?", "enable_thinking": None, "strip_thinking": False}
        self.assertEqual(ad.apply_alias_defaults({"messages": []}, unknown), {"messages": []})

    def test_passthrough_alias_injects_nothing(self):
        body = {"model": "devstral", "messages": []}
        self.assertEqual(ad.apply_alias_defaults(body, PASSTHROUGH), body)


class TestAliasMap(unittest.TestCase):
    """Regression: qwen3.8-nothink was lost in the 2026-09-08 router deploy and
    silently fell through to thinking ON at the template's xhigh default."""

    def setUp(self):
        self.aliases = _alias_map_nodes()

    def test_nothink_alias_exists_with_thinking_off(self):
        entry = self.aliases["qwen3.8-nothink"]
        self.assertEqual(ast.literal_eval(entry["backend"]), "qwen3.8")
        self.assertIs(ast.literal_eval(entry["enable_thinking"]), False)

    def test_every_qwen38_alias_sets_mode_matched_sampling(self):
        for alias, entry in self.aliases.items():
            if ast.literal_eval(entry["backend"]) != "qwen3.8":
                continue
            with self.subTest(alias=alias):
                want = ("QWEN38_THINK_SAMPLING" if ast.literal_eval(entry["enable_thinking"])
                        else "QWEN38_NOTHINK_SAMPLING")
                self.assertIn("sampling", entry)
                self.assertEqual(ast.unparse(entry["sampling"]), f"alias_defaults.{want}")


if __name__ == "__main__":
    unittest.main()
