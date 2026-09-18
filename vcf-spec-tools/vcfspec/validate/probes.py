"""Layer 3: optional live checks, contained so they cannot become a scanner.

A target outside the operator-configured allowlist must be neither resolved
nor connected to: a DNS lookup of an attacker-chosen name is itself the
exfiltration channel, since the query reaches whatever nameserver is
authoritative for it carrying whatever was encoded in the label. So
ProbeConfig.permits() is checked BEFORE resolve()/connect() are ever called,
not used to filter results afterward. An empty allowlist permits nothing.

The real resolver and connector touch the network, so they are never built
at import time or as a default-argument value (both are evaluated once, at
module load, which would make importing this module a network operation).
They are constructed lazily inside run_probes(), only when the caller did
not inject a fake.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass

from ..findings import Finding, Result
from ..rules import finding_for

ESX_PORT = 443


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Probes only ever touch addresses inside allowlist. Default: nothing."""

    allowlist: tuple[str, ...] = ()
    timeout_s: float = 2.0

    def permits(self, ip: object) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except (TypeError, ValueError):
            return False
        for cidr in self.allowlist:
            try:
                if address in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:
                continue
        return False


def _default_resolver(name: str, want_reverse: bool = False):
    try:
        return socket.gethostbyaddr(name)[0] if want_reverse else socket.gethostbyname(name)
    except OSError:
        return None


def _default_connector(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _bounded_resolve(resolve, name: str, want_reverse: bool, timeout_s: float):
    """Call resolve(name, want_reverse), but never wait past timeout_s.

    socket.gethostbyname/gethostbyaddr (the default resolver, and any
    caller-supplied one) do not honour socket.setdefaulttimeout reliably
    across platforms -- the interaction with the system resolver is
    outside Python's control, so a timeout that only configures the
    socket default is not a real bound. The only way to bound a call that
    may hang is to stop waiting for it, not to make it hang less: this
    runs it in a daemon thread and joins with a deadline. If the deadline
    passes, the thread is abandoned (never joined again, so a wedged
    resolver cannot itself keep the process alive) and this returns None
    -- the same value a normal resolution failure produces, so a timeout
    becomes an ordinary VCF-PROBE-* finding downstream, never an
    exception and never an indefinite wait.
    """
    box: list = [None]

    def worker():
        try:
            box[0] = resolve(name, want_reverse)
        except Exception:
            box[0] = None

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    return None if thread.is_alive() else box[0]


def run_probes(inventory: dict, config: ProbeConfig, resolver=None,
               connector=None) -> Result:
    resolve = resolver or _default_resolver
    connect = connector or _default_connector
    subdomain = (inventory.get("dns") or {}).get("subdomain", "")
    findings: list[Finding] = []

    for index, host in enumerate(inventory.get("hosts") or []):
        if not isinstance(host, dict):
            continue
        name, ip = host.get("name", ""), host.get("mgmtIp", "")
        path = f"/hosts/{index}"
        if not config.permits(ip):
            findings.append(finding_for("VCF-PROBE-TARGET-BLOCKED", path, target=ip))
            continue
        fqdn = f"{name}.{subdomain}" if subdomain else name
        resolved = _bounded_resolve(resolve, fqdn, False, config.timeout_s)
        if resolved is None:
            findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=fqdn,
                                        reason="no forward DNS answer"))
        elif resolved != ip:
            findings.append(finding_for("VCF-PROBE-FORWARD-MISMATCH", path,
                                        fqdn=fqdn, resolved=resolved, expected=ip))
        if _bounded_resolve(resolve, ip, True, config.timeout_s) is None:
            findings.append(finding_for("VCF-PROBE-NO-REVERSE-DNS", path, ip=ip,
                                        fqdn=fqdn))
        if not connect(ip, ESX_PORT, config.timeout_s):
            findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=ip,
                                        reason=f"no TCP {ESX_PORT} response"))
    return Result(tuple(findings))
