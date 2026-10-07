"""SSRF-guarded GET for the router's web_fetch tool.

Every request target -- the original URL AND every redirect hop -- must pass the same checks:
http(s) only, not a denied host, and every address the name resolves to must be globally
routable (no private, loopback, link-local, CGNAT, reserved, multicast, ULA or IPv4-mapped
private). Redirects are followed manually so each hop is checked before it is requested; the
original guard checked only the first host and let httpx follow redirects unchecked.

The body is read as a stream and capped, so an oversized response is never fully downloaded.
A narrow DNS-rebinding window remains between resolution and connect (documented in the router).
"""
import asyncio
import ipaddress
import socket
import urllib.parse
from typing import Awaitable, Callable, Iterable, Optional

MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DENY_HOSTS = {
    # Cloud metadata endpoints -- block to prevent SSRF to instance creds.
    "169.254.169.254", "metadata.google.internal", "metadata", "169.254.170.2",
    # Loopback / unspecified: a fetch of the router itself would be a recursion vector.
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
}

Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


async def default_resolve(host: str, port: int) -> list:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_target(url: str, resolve: Resolver) -> Optional[dict]:
    """None if the URL may be requested, otherwise the error dict to return to the model."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:  # noqa: BLE001 -- any parse failure is an invalid URL
        return {"error": "invalid_url", "message": str(e)}
    if parsed.scheme not in ("http", "https"):
        return {"error": "scheme_not_allowed", "scheme": parsed.scheme}
    host = (parsed.hostname or "").lower()
    if not host:
        return {"error": "missing_host"}
    if host in DENY_HOSTS:
        return {"error": "host_denied", "host": host}
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as e:
        return {"error": "invalid_url", "message": str(e)}
    try:
        ipaddress.ip_address(host.strip("[]"))
        addrs = [host.strip("[]")]          # a literal IP is checked as-is, never resolved
    except ValueError:
        try:
            addrs = list(await resolve(host, port))
        except (OSError, socket.gaierror) as e:
            return {"error": "dns_resolution_failed", "message": str(e)}
    if not addrs:
        return {"error": "dns_resolution_failed", "message": "no addresses"}
    for addr_str in addrs:
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            return {"error": "host_denied_unparseable_ip", "host": host, "addr": addr_str}
        mapped = getattr(ip, "ipv4_mapped", None)
        if not ip.is_global or (mapped is not None and not mapped.is_global):
            return {"error": "host_denied_private_range", "host": host, "resolved": addr_str}
    return None


async def guarded_get(url: str, *, client, resolve: Resolver = default_resolve, headers: dict,
                      max_bytes: int, max_redirects: int = MAX_REDIRECTS) -> dict:
    """GET url with every hop checked. `client` must be created with follow_redirects=False."""
    if not isinstance(url, str) or not url.strip():
        return {"error": "missing 'url' field"}
    current = url
    for _ in range(max_redirects + 1):
        err = await check_target(current, resolve)
        if err is not None:
            return err
        async with client.stream("GET", current, headers=headers) as r:
            location = r.headers.get("location")
            if r.status_code in REDIRECT_STATUSES and location:
                current = urllib.parse.urljoin(current, location)
                continue
            body = bytearray()
            truncated = False
            async for chunk in r.aiter_bytes():
                room = max_bytes + 1 - len(body)
                body.extend(chunk[:room])
                if len(body) > max_bytes:
                    truncated = True
                    break
            body_bytes = bytes(body[:max_bytes])
            return {
                "status": r.status_code,
                "url": str(r.url),
                "content_type": r.headers.get("content-type", ""),
                "content_length": len(body_bytes),
                "truncated": truncated,
                "body": body_bytes.decode("utf-8", errors="replace"),
            }
    return {"error": "too_many_redirects", "max_redirects": max_redirects}
