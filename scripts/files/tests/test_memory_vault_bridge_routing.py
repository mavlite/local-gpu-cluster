import importlib.util
from pathlib import Path

from starlette.testclient import TestClient

_SPEC = importlib.util.spec_from_file_location(
    "memory_vault_bridge", Path(__file__).resolve().parents[1] / "memory-vault-bridge.py"
)
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

_HEADERS = {"Accept": "application/json, text/event-stream"}
_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_bare_mcp_path_with_space_is_served_without_redirect():
    # Clients are configured with .../mcp?space=<repo>; a Mount would 307 them to /mcp/.
    # The session manager can only run() once, so all requests share one client.
    with TestClient(bridge.app, follow_redirects=False) as client:
        r = client.post("/mcp?space=local-gpu-cluster", json=_LIST, headers=_HEADERS)
        legacy = client.post("/mcp/?space=x", json=_LIST, headers=_HEADERS)
    assert r.status_code == 200
    assert {t["name"] for t in r.json()["result"]["tools"]} >= {"remember", "recall"}
    # Clients still configured with the trailing slash are redirected, query intact.
    assert legacy.status_code == 307
    assert legacy.headers["location"].endswith("/mcp?space=x")
