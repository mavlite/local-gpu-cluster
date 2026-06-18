import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOOKS_DIR)

import mv_common as mv


class TestMvCommon(unittest.TestCase):
    def test_load_env_parses_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "hooks.env")
            with open(p, "w", encoding="utf-8") as f:
                f.write("# comment\nMEMVAULT_API_URL=http://x:8000\n\nMEMVAULT_HOOKS_TOKEN=abc \n")
            env = mv.load_env(p)
        self.assertEqual(env["MEMVAULT_API_URL"], "http://x:8000")
        self.assertEqual(env["MEMVAULT_HOOKS_TOKEN"], "abc")

    def test_load_env_missing_file_returns_empty(self):
        self.assertEqual(mv.load_env("/no/such/file.env"), {})

    def test_resolve_space_env_override_wins(self):
        self.assertEqual(mv.resolve_space("/whatever", {"MEMVAULT_SPACE": "custom"}), "custom")

    def test_resolve_space_falls_back_to_basename(self):
        # A path that is not a git repo resolves to its basename.
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "my-repo")
            os.makedirs(sub)
            self.assertEqual(mv.resolve_space(sub, {}), "my-repo")

    def test_build_session_context_with_memories(self):
        ctx = mv.build_session_context(["decided X", "use Y"], "proj")
        self.assertIn("space `proj`", ctx)
        self.assertIn("- decided X", ctx)
        self.assertIn("- use Y", ctx)
        self.assertIn(mv.STANDING_INSTRUCTION, ctx)

    def test_build_session_context_empty_is_instruction_only(self):
        ctx = mv.build_session_context([], "proj")
        self.assertEqual(ctx.strip(), mv.STANDING_INSTRUCTION)

    def test_recent_memories_parses_chunks(self):
        fake = {"chunks": [{"content": "alpha\nbeta"}, {"content": " "}, {"content": "gamma"}]}
        with mock.patch.object(mv, "http_get_json", return_value=fake):
            out = mv.recent_memories("http://x:8000", "tok", "sp", 6)
        self.assertEqual(out, ["alpha beta", "gamma"])

    def test_recent_memories_handles_none(self):
        with mock.patch.object(mv, "http_get_json", return_value=None):
            self.assertEqual(mv.recent_memories("http://x:8000", "tok", "sp", 6), [])

    def test_watchdog_nudges_once_over_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            transcript = os.path.join(d, "t.jsonl")
            with open(transcript, "wb") as f:
                f.write(b"x" * 1000)
            sentinel = os.path.join(d, "sent")
            self.assertTrue(mv.watchdog_should_nudge(transcript, 500, sentinel))   # over -> nudge
            self.assertTrue(os.path.exists(sentinel))
            self.assertFalse(mv.watchdog_should_nudge(transcript, 500, sentinel))  # sentinel -> no repeat

    def test_watchdog_no_nudge_under_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            transcript = os.path.join(d, "t.jsonl")
            with open(transcript, "wb") as f:
                f.write(b"x" * 100)
            self.assertFalse(mv.watchdog_should_nudge(transcript, 500, os.path.join(d, "sent")))

    def test_watchdog_missing_transcript_no_nudge(self):
        self.assertFalse(mv.watchdog_should_nudge("/no/file", 1, "/tmp/sent-x"))

    def test_watchdog_bare_filename_sentinel_no_crash(self):
        # A sentinel path with no directory component must not raise (Windows hazard).
        import os as _os
        cwd = _os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "t.jsonl")
            with open(t, "wb") as f:
                f.write(b"x" * 1000)
            _os.chdir(d)
            try:
                self.assertTrue(mv.watchdog_should_nudge(t, 500, "bare-sentinel"))
                self.assertTrue(os.path.exists("bare-sentinel"))
            finally:
                _os.chdir(cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
