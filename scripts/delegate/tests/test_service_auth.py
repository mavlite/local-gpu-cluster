from starlette.testclient import TestClient
from scripts.delegate.config import load_config
from scripts.delegate.service import build_app

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "secret", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"})


class Deps:  # minimal stand-ins; /mcp handshake not exercised here
    http = None
    ledger = None
    lease = None


def test_missing_bearer_is_401():
    client = TestClient(build_app(CFG, Deps()))
    r = client.post("/mcp", json={})
    assert r.status_code == 401


def test_wrong_bearer_is_401():
    client = TestClient(build_app(CFG, Deps()))
    r = client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_correct_bearer_passes_auth_gate():
    # Context manager runs the lifespan so the session manager is started.
    with TestClient(build_app(CFG, Deps())) as client:
        r = client.post("/mcp", json={}, headers={"Authorization": "Bearer secret"})
    assert r.status_code != 401


def test_bare_mcp_path_is_served_without_redirect():
    # Clients are configured with http://.../mcp; a Mount would 307 them to /mcp/.
    h = {"Authorization": "Bearer secret", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(build_app(CFG, Deps()), follow_redirects=False) as client:
        r = client.post("/mcp", json=body, headers=h)
    assert r.status_code == 200
    assert any(t["name"] == "ask_local" for t in r.json()["result"]["tools"])


class BusyLease:
    def acquire(self, timeout_s=0):
        return False

    def release(self):  # pragma: no cover - must not be called when not acquired
        raise AssertionError("release without acquire")


class BusyDeps:
    http = None
    ledger = None
    lease = BusyLease()


def test_ask_local_reports_gpu_busy_when_lease_held():
    h = {"Authorization": "Bearer secret", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_local", "arguments": {"prompt": "x"}}}
    with TestClient(build_app(CFG, BusyDeps())) as client:
        r = client.post("/mcp", json=body, headers=h)
    assert r.status_code == 200
    assert "GPU busy" in r.json()["result"]["content"][0]["text"]
