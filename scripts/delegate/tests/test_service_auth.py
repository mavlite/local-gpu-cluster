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
