"""Hidden checks for w2-02: rag-refresh.timer is stopped for the window and restored to its prior state."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_gate_env import world  # noqa: E402,F401  (pytest fixture)


def test_an_active_refresh_timer_is_stopped_then_restored(world):
    run, state, _ = world
    run("pin")
    assert state()["units"]["host"]["rag-refresh.timer"] == "inactive"
    run("restore")
    assert state()["units"]["host"]["rag-refresh.timer"] == "active"


def test_an_inactive_refresh_timer_stays_inactive_after_restore(world):
    run, state, _ = world
    w = state()
    w["units"]["host"]["rag-refresh.timer"] = "inactive"
    state.set(w)
    run("pin")
    run("restore")
    assert state()["units"]["host"]["rag-refresh.timer"] == "inactive"
