"""Hidden checks for w2-06: deep hops are checked; metadata is refused; public 303s still work."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_web_fetch_guard import chain, redirect, run  # noqa: E402


def test_a_private_host_on_the_second_hop_is_blocked_before_it_is_requested():
    res, seen = run("https://public.example/a", chain({
        "public.example": (302, "https://other.example/b"),
        "other.example": (302, "http://inward.example/c"),
        "inward.example": b"secret"}))
    assert res["error"] == "host_denied_private_range"
    assert seen == ["https://public.example/a", "https://other.example/b"]


def test_a_redirect_to_the_metadata_address_is_refused():
    res, seen = run("https://public.example/r", redirect("http://169.254.169.254/latest/meta-data/"))
    assert "error" in res and len(seen) == 1


def test_a_public_303_is_still_followed():
    res, _ = run("https://public.example/a", chain({
        "public.example": (303, "https://other.example/b"), "other.example": b"fine"}))
    assert res["status"] == 200 and res["body"] == "fine"
