"""SSRF guard for the router's web_fetch tool.

The original guard validated only the FIRST host and then let httpx follow redirects, so a public
URL answering 302 -> http://192.168.6.x/ was fetched unchecked (adversarial review, 2026-10-07).
Every hop must now pass the same checks. All tests are offline: DNS and HTTP are injected.
"""
import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import web_fetch_guard as g  # noqa: E402

DNS = {
    "public.example": ["93.184.216.34"],
    "other.example": ["151.101.1.69"],
    "inward.example": ["192.168.6.153"],
    "mixed.example": ["93.184.216.34", "10.0.0.5"],
    "v6inward.example": ["::ffff:192.168.6.175"],
}


async def resolve(host, port):
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def run(url, handler, **kw):
    seen = []

    def wrapped(request):
        seen.append(str(request.url))
        return handler(request)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=False) as c:
            return await g.guarded_get(url, client=c, resolve=resolve, headers={}, max_bytes=kw.get("max_bytes", 1024),
                                       max_redirects=kw.get("max_redirects", 5))
    return asyncio.run(go()), seen


def redirect(to, status=302):
    return lambda request: httpx.Response(status, headers={"location": to})


def ok(body=b"hello"):
    return lambda request: httpx.Response(200, content=body, headers={"content-type": "text/plain"})


def chain(mapping):
    """Answer by host: a (status, location) redirect or a 200 body."""
    def handler(request):
        v = mapping[request.url.host]
        if isinstance(v, tuple):
            return httpx.Response(v[0], headers={"location": v[1]})
        return httpx.Response(200, content=v)
    return handler


def test_public_fetch_still_works():
    res, seen = run("https://public.example/page", ok())
    assert res["status"] == 200 and res["body"] == "hello" and seen == ["https://public.example/page"]


def test_initial_private_host_blocked_before_any_request():
    res, seen = run("http://inward.example/admin", ok())
    assert res["error"] == "host_denied_private_range" and seen == []


def test_redirect_to_private_literal_ip_is_blocked():
    res, seen = run("https://public.example/r", redirect("http://192.168.6.175:8006/"))
    assert res["error"] == "host_denied_private_range"
    assert seen == ["https://public.example/r"]          # the inward hop was never requested


def test_redirect_to_hostname_resolving_private_is_blocked():
    res, seen = run("https://public.example/r", redirect("http://inward.example/"))
    assert res["error"] == "host_denied_private_range" and len(seen) == 1


def test_redirect_to_mixed_resolution_is_blocked():
    res, _ = run("https://public.example/r", redirect("https://mixed.example/"))
    assert res["error"] == "host_denied_private_range"


def test_redirect_to_ipv4_mapped_ipv6_private_is_blocked():
    res, _ = run("https://public.example/r", redirect("https://v6inward.example/"))
    assert res["error"] == "host_denied_private_range"


def test_redirect_to_denied_host_and_bad_scheme_blocked():
    res, _ = run("https://public.example/r", redirect("http://localhost:8000/"))
    assert res["error"] == "host_denied"
    res, _ = run("https://public.example/r", redirect("file:///etc/passwd"))
    assert res["error"] == "scheme_not_allowed"


def test_relative_redirect_followed_and_public_chain_ok():
    res, seen = run("https://public.example/a", chain({
        "public.example": (301, "https://other.example/b"), "other.example": b"done"}))
    assert res["status"] == 200 and res["body"] == "done" and res["url"] == "https://other.example/b"
    res, seen = run("https://public.example/a", lambda r: (
        httpx.Response(302, headers={"location": "/b"}) if r.url.path == "/a" else httpx.Response(200, content=b"rel")))
    assert res["body"] == "rel" and seen == ["https://public.example/a", "https://public.example/b"]


def test_redirect_limit_enforced():
    res, seen = run("https://public.example/loop", redirect("https://public.example/loop"), max_redirects=3)
    assert res["error"] == "too_many_redirects" and len(seen) == 4


def test_body_read_is_capped_while_streaming():
    res, _ = run("https://public.example/big", ok(b"x" * 5000), max_bytes=100)
    assert res["truncated"] is True and len(res["body"]) == 100


def test_unresolvable_host_reports_dns_error():
    res, seen = run("https://nowhere.example/", ok())
    assert res["error"] == "dns_resolution_failed" and seen == []


ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _src(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_router_web_fetch_delegates_to_the_guard_and_never_auto_follows():
    s = _src("files", "router-app.py")
    body = s[s.index("async def _tool_web_fetch"):s.index("TOOLS: dict[str, dict] = {")]
    assert "web_fetch_guard.guarded_get(" in body
    assert "follow_redirects=False" in body and "follow_redirects=True" not in body


def test_deploy_script_ships_the_guard_module():
    s = _src("53-lxc-router.sh")
    assert 'files/web_fetch_guard.py' in s and "/opt/llm-router/web_fetch_guard.py" in s
