"""SSRF-guarded GET for the router's web_fetch tool.

Every request target -- the original URL AND every redirect hop -- must pass the same checks:
http(s) only, not a denied host, and every address the name resolves to must be public (not in
BLOCKED_NETS and ip.is_global). Redirects are followed manually so each hop is checked before it is
requested; the original guard checked only the first host and let httpx follow redirects unchecked.

DNS rebinding: the request connects to the exact address that was checked (the URL host is
replaced by that IP, with the original name sent as Host and as the TLS SNI/verification name), so
a resolver answering public-then-private cannot redirect the connection.

The body is read raw (identity encoding requested, compressed responses refused) and capped while
streaming, and the whole fetch runs under one total deadline.
"""
import asyncio
import ipaddress
import socket
import urllib.parse
from typing import Awaitable, Callable, Iterable, Optional

MAX_REDIRECTS = 5
TOTAL_TIMEOUT_SECONDS = 30.0
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DENY_HOSTS = {
    # Cloud metadata endpoints -- block to prevent SSRF to instance creds.
    "169.254.169.254", "metadata.google.internal", "metadata", "169.254.170.2",
    # Loopback / unspecified: a fetch of the router itself would be a recursion vector.
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
}
# Explicit, because ip.is_global differs across Python patch releases (the router runs 3.12.3) and
# does not cover addresses that EMBED a private IPv4 (NAT64, 6to4, Teredo) or multicast.
BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64",
    "2001::/32", "2001:db8::/32", "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
)]

Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


async def default_resolve(host: str, port: int) -> list:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _is_public(ip) -> bool:
    if any(ip in net for net in BLOCKED_NETS if net.version == ip.version):
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and not _is_public(mapped):
        return False
    return ip.is_global


async def check_target(url: str, resolve: Resolver):
    """(error_dict, None) if the URL may not be requested, else (None, (parsed, host, vetted_ip))."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as e:
        return {"error": "invalid_url", "message": str(e)}, None
    if parsed.scheme not in ("http", "https"):
        return {"error": "scheme_not_allowed", "scheme": parsed.scheme}, None
    if not host:
        return {"error": "missing_host"}, None
    if host in DENY_HOSTS:
        return {"error": "host_denied", "host": host}, None
    try:
        ipaddress.ip_address(host)
        addrs = [host]                      # a literal IP is checked as-is, never resolved
    except ValueError:
        try:
            addrs = list(await resolve(host, port))
        except (OSError, socket.gaierror, UnicodeError) as e:
            return {"error": "dns_resolution_failed", "message": type(e).__name__}, None
    if not addrs:
        return {"error": "dns_resolution_failed", "message": "no addresses"}, None
    vetted = []
    for addr_str in addrs:
        try:
            ip = ipaddress.ip_address(addr_str.split("%", 1)[0])
        except ValueError:
            return {"error": "host_denied_unparseable_ip", "host": host, "addr": addr_str}, None
        if not _is_public(ip):
            return {"error": "host_denied_private_range", "host": host, "resolved": addr_str}, None
        vetted.append(ip)
    return None, (parsed, host, vetted[0])


def _pinned(parsed, host: str, ip) -> tuple:
    """URL that connects to the vetted IP, the Host header value, and the request extensions."""
    ip_text = f"[{ip}]" if ip.version == 6 else str(ip)
    netloc = ip_text + (f":{parsed.port}" if parsed.port else "")
    url = urllib.parse.urlunparse(parsed._replace(netloc=netloc))
    host_header = host + (f":{parsed.port}" if parsed.port else "")
    ext = {"sni_hostname": host} if parsed.scheme == "https" else {}
    return url, host_header, ext


async def _fetch(url, client, resolve, headers, max_bytes, max_redirects) -> dict:
    current = url
    for _ in range(max_redirects + 1):
        err, target = await check_target(current, resolve)
        if err is not None:
            return err
        parsed, host, ip = target
        pinned_url, host_header, ext = _pinned(parsed, host, ip)
        req_headers = {**headers, "Host": host_header, "Accept-Encoding": "identity"}
        request = client.build_request("GET", pinned_url, headers=req_headers, extensions=ext)
        response = await client.send(request, stream=True)
        try:
            location = response.headers.get("location")
            if response.status_code in REDIRECT_STATUSES and location:
                current = urllib.parse.urljoin(current, location)
                continue
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                return {"error": "content_encoding_not_supported", "content_encoding": encoding}
            body = bytearray()
            truncated = False
            async for chunk in response.aiter_raw():
                room = max_bytes + 1 - len(body)
                body.extend(chunk[:room])
                if len(body) > max_bytes:
                    truncated = True
                    break
            body_bytes = bytes(body[:max_bytes])
            return {
                "status": response.status_code,
                "url": current,
                "content_type": response.headers.get("content-type", ""),
                "content_length": len(body_bytes),
                "truncated": truncated,
                "body": body_bytes.decode("utf-8", errors="replace"),
            }
        finally:
            await response.aclose()
    return {"error": "too_many_redirects", "max_redirects": max_redirects}


async def guarded_get(url: str, *, client, resolve: Resolver = default_resolve, headers: dict,
                      max_bytes: int, max_redirects: int = MAX_REDIRECTS,
                      total_timeout: float = TOTAL_TIMEOUT_SECONDS) -> dict:
    """GET url with every hop checked and pinned. `client` must not follow redirects itself."""
    if getattr(client, "follow_redirects", False):
        raise ValueError("guarded_get needs a client with follow_redirects=False")
    if not isinstance(url, str) or not url.strip():
        return {"error": "missing 'url' field"}
    try:
        return await asyncio.wait_for(_fetch(url, client, resolve, headers, max_bytes, max_redirects),
                                      timeout=total_timeout)
    except asyncio.TimeoutError:
        return {"error": "timeout", "seconds": total_timeout}
