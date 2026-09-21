"""Layer 3: optional live checks, contained so they cannot become a scanner.

A target outside the operator-configured allowlist must be neither resolved
nor connected to: a DNS lookup of an attacker-chosen name is itself the
exfiltration channel, since the query reaches whatever nameserver is
authoritative for it carrying whatever was encoded in the label. So
ProbeConfig.permits() is checked BEFORE resolve()/connect() are ever called,
not used to filter results afterward. An empty allowlist permits nothing.

**An IP allowlist cannot gate a name.** That invariant was stated here from
the beginning and the code still broke it, because every check -- and every
test -- looked at `host["mgmtIp"]`. The forward lookup then built
`f"{name}.{subdomain}"` from two strings taken verbatim out of the document
and resolved it unchecked, so an inventory naming a host
`stolen-data-abc123` under `dns.subdomain: exfil.attacker.example` issued a
real query to the attacker's nameserver carrying the label. The mgmtIp only
had to be *some* address the operator allowlisted; it never had to be real.

You cannot fix that with an IP allowlist, because you do not learn the IP
until after the query that is itself the leak. So names are gated by their
own policy: ProbeConfig.domain_allowlist, a list of permitted DNS suffixes,
checked by permits_name() before any forward lookup. It **fails closed** --
with no configured suffix, no forward lookup is issued at all, and each
name becomes a VCF-PROBE-NAME-BLOCKED finding instead of a query.

For a **host**, the reverse lookup needs no further gate: it takes the
mgmtIp, an address the operator declared and permits() already cleared.
The **appliance** names have no declared address at all -- pre-Installer
the appliances do not exist -- so the only address in play is whatever
the A record says, chosen by whoever controls the zone. permits() is
therefore applied to the *resolved* address before anything downstream of
the forward answer happens, and an appliance is never connected to on any
path: a 443 probe of a machine that does not exist yet is guaranteed
noise, and it would open a TCP session to an address nobody declared.

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
from ..rules.coerce import as_mapping as _mapping

ESX_PORT = 443


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Probes only ever touch addresses inside allowlist and names inside
    domain_allowlist. Both default to nothing, and both fail closed."""

    allowlist: tuple[str, ...] = ()
    timeout_s: float = 2.0
    domain_allowlist: tuple[str, ...] = ()

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

    def permits_name(self, fqdn: object) -> bool:
        """True only for a name at or under a configured DNS suffix.

        Fails closed in three ways that matter: an empty domain_allowlist
        permits nothing (so the default configuration issues zero forward
        lookups); the match is on whole labels, so 'evil.example.com' is
        not permitted by the suffix 'ample.com'; and a non-string or empty
        name is refused rather than coerced.
        """
        if not isinstance(fqdn, str):
            return False
        candidate = fqdn.strip().rstrip(".").lower()
        if not candidate:
            return False
        for suffix in self.domain_allowlist:
            if not isinstance(suffix, str):
                continue
            wanted = suffix.strip().strip(".").lower()
            if not wanted:
                continue
            if candidate == wanted or candidate.endswith("." + wanted):
                return True
        return False


def _default_resolver(name: str, want_reverse: bool = False,
                      want_aliases: bool = False):
    """Forward, reverse, or the name's canonical name plus its aliases.

    The third mode exists because gethostbyname() follows a CNAME silently
    and gethostbyaddr()[0] hands back the *canonical* name, so a CNAME'd
    appliance looks like a reverse mismatch on a perfectly healthy lab.
    gethostbyname_ex() is the only stdlib call that reports the alias
    chain, so it is how the round-trip check learns the other names the
    queried one legitimately answers to.
    """
    try:
        if want_aliases:
            canonical, aliases, _ = socket.gethostbyname_ex(name)
            return (canonical, *aliases)
        return socket.gethostbyaddr(name)[0] if want_reverse else socket.gethostbyname(name)
    except OSError:
        return None


def _default_connector(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dns_name(value: object) -> str:
    """Normalise a DNS name for comparison. Resolvers return a trailing dot
    and arbitrary case; neither is a difference, and treating either as one
    would fail every correctly-configured lab.
    """
    return str(value).strip().rstrip(".").lower()


def _bounded_resolve(resolve, name: str, want_reverse: bool, timeout_s: float,
                     want_aliases: bool = False):
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
            # The third argument is passed only when it is needed, so a
            # two-parameter resolver -- every fake written before the
            # alias mode existed, and any caller-supplied one -- keeps
            # working unchanged.
            box[0] = (resolve(name, want_reverse, want_aliases) if want_aliases
                      else resolve(name, want_reverse))
        except Exception:
            box[0] = None

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    return None if thread.is_alive() else box[0]


# Appliance names, by field. Never inferred from a value's shape: rules
# run on schema-invalid documents, so "it has a dot in it" proves nothing
# about whether a field holds a short name or an FQDN. The split below is
# the inventory schema's own: nsx.managers[], appliances.vcenter.hostname
# and appliances.sddcManager.hostname are $defs/shortname, while
# nsx.vipFqdn, vsp.platformFqdn and vsp.instanceFqdn are free-form names
# that are already fully qualified.
def _appliance_targets(inventory: dict, subdomain: str) -> list[tuple[str, str, bool]]:
    """(json pointer, fqdn, ptr_required) for each appliance name declared.

    ptr_required is False for the VIP-like names: the NSX VIP and the VSP
    platform name front pools, so a missing PTR is not a defect there.
    """
    appliances = _mapping(inventory.get("appliances"))
    nsx = _mapping(inventory.get("nsx"))
    vsp = _mapping(appliances.get("vsp"))
    out: list[tuple[str, str, bool]] = []

    def compose(short: object) -> str | None:
        if not isinstance(short, str) or not short.strip():
            return None
        return f"{short}.{subdomain}" if subdomain else short

    for pointer, value, ptr_required in (
        ("/appliances/vcenter/hostname",
         compose(_mapping(appliances.get("vcenter")).get("hostname")), True),
        ("/appliances/sddcManager/hostname",
         compose(_mapping(appliances.get("sddcManager")).get("hostname")), True),
    ):
        if value:
            out.append((pointer, value, ptr_required))
    # A wrong-typed managers section is skipped, not iterated: enumerate()
    # over a bare string would happily yield its characters as names.
    managers = nsx.get("managers")
    if not isinstance(managers, (list, tuple)):
        managers = ()
    for index, manager in enumerate(managers):
        composed = compose(manager)
        if composed:
            out.append((f"/nsx/managers/{index}", composed, True))
    # Already fully qualified -- appending the subdomain again is the bug.
    for pointer, value, ptr_required in (
        ("/nsx/vipFqdn", nsx.get("vipFqdn"), False),
        ("/appliances/vsp/platformFqdn", vsp.get("platformFqdn"), False),
        ("/appliances/vsp/instanceFqdn", vsp.get("instanceFqdn"), True),
    ):
        if isinstance(value, str) and value.strip():
            out.append((pointer, value, ptr_required))
    return out


_RESOLV_CONF = "/etc/resolv.conf"


def _default_resolv_conf_reader() -> tuple[str, ...]:
    """The runner's configured resolver addresses, in file order.

    Raises OSError when the file is absent or unreadable (Windows, a
    minimal container) -- the caller turns that into VCF-PROBE-UNKNOWN.
    """
    found = []
    with open(_RESOLV_CONF, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                found.append(parts[1])
    return tuple(found)


def _resolver_vantage_point(inventory: dict, reader) -> list[Finding]:
    """Compare the runner's own resolver configuration against what the
    inventory declared, and say nothing stronger than "these answers came
    from somewhere else". This is a note about provenance, not a defect --
    a corporate resolver that forwards the lab zone trips this while
    resolving perfectly, systemd-resolved always shows 127.0.0.53, and
    Windows has no resolv.conf at all.
    """
    # A wrong-typed nameservers section is skipped, not iterated: on a
    # schema-invalid document it may be a bare string, and iterating a
    # string yields its characters -- "10.50.10.5" becomes a "declared"
    # list of ('1', '0', '.', '5', '0', ...), which is exactly the kind of
    # garbled operator-facing message this package's rules never produce.
    raw_nameservers = _mapping(inventory.get("dns")).get("nameservers")
    if not isinstance(raw_nameservers, (list, tuple)):
        raw_nameservers = ()
    declared = [str(x) for x in raw_nameservers if isinstance(x, str)]
    if not declared:
        return []          # nothing declared, nothing to compare against
    try:
        runner = reader()
    except Exception as exc:
        # resolv_conf_reader is a caller-injected callable -- a public
        # keyword parameter, same as resolver/connector -- so the
        # exception type it raises is not this module's to assume. Caught
        # broadly for the same reason _bounded_resolve catches Exception
        # rather than OSError: api.py's invariant is that nothing here
        # ever lets an exception reach the caller. type(exc).__name__
        # only, never str(exc): redact() is a pattern masker, not a
        # sanitizer, and an OSError message carries a filesystem path.
        return [finding_for("VCF-PROBE-UNKNOWN", "/dns/nameservers",
                            target=_RESOLV_CONF,
                            reason=f"resolver configuration unreadable ({type(exc).__name__})")]
    if not runner or set(runner) & set(declared):
        return []
    return [finding_for("VCF-PROBE-RESOLVER-MISMATCH", "/dns/nameservers",
                        runner=", ".join(runner), declared=", ".join(declared))]


def run_probes(inventory: dict, config: ProbeConfig, resolver=None,
               connector=None, *, resolv_conf_reader=None) -> Result:
    resolve = resolver or _default_resolver
    connect = connector or _default_connector
    read_resolvers = resolv_conf_reader or _default_resolv_conf_reader
    subdomain = (inventory.get("dns") or {}).get("subdomain", "")
    findings: list[Finding] = []

    candidates = permitted = 0

    for index, host in enumerate(inventory.get("hosts") or []):
        if not isinstance(host, dict):
            continue
        candidates += 1
        name, ip = host.get("name", ""), host.get("mgmtIp", "")
        path = f"/hosts/{index}"
        if not config.permits(ip):
            findings.append(finding_for("VCF-PROBE-TARGET-BLOCKED", path, target=ip))
            continue
        permitted += 1
        fqdn = f"{name}.{subdomain}" if subdomain else name
        # The name gate sits here, before the lookup, for the same reason
        # the IP gate sits before connect(): the query IS the leak, so
        # filtering its answer afterwards is already too late.
        if not config.permits_name(fqdn):
            findings.append(finding_for("VCF-PROBE-NAME-BLOCKED", path, name=fqdn))
        else:
            resolved = _bounded_resolve(resolve, fqdn, False, config.timeout_s)
            if resolved is None:
                findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=fqdn,
                                            reason="no forward DNS answer"))
            elif resolved != ip:
                findings.append(finding_for("VCF-PROBE-FORWARD-MISMATCH", path,
                                            fqdn=fqdn, resolved=resolved, expected=ip))
        reverse = _bounded_resolve(resolve, ip, True, config.timeout_s)
        if reverse is None:
            findings.append(finding_for("VCF-PROBE-NO-REVERSE-DNS", path, ip=ip,
                                        fqdn=fqdn))
        elif _dns_name(reverse) != _dns_name(fqdn):
            # A PTR that exists but names a different host is worse than a
            # missing one: it looks healthy and VCF fails obscurely on the
            # disagreement. Checking only that reverse returned *something*
            # is why this went undetected until the tool met real dnsmasq.
            findings.append(finding_for("VCF-PROBE-REVERSE-MISMATCH", path,
                                        ip=ip, resolved=reverse, fqdn=fqdn))
        if not connect(ip, ESX_PORT, config.timeout_s):
            findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=ip,
                                        reason=f"no TCP {ESX_PORT} response"))

    # The appliance names. Three things separate this loop from the host
    # loop above, and all three are containment properties, not polish:
    #
    #   * It never touches `candidates`/`permitted`. Those count hosts, and
    #     VCF-PROBE-NOTHING-PERMITTED's message says "the N host(s) in this
    #     document" -- an appliance inflating that count makes the message
    #     a lie and, worse, can hide the all-blocked case.
    #   * It never calls connect(). Pre-Installer these appliances do not
    #     exist, so a 443 probe is guaranteed noise; and the address came
    #     from the zone rather than from the operator, so connecting would
    #     mean opening a TCP session to somewhere nobody declared.
    #   * config.permits() gates the *resolved* address. A host is gated on
    #     the mgmtIp the operator wrote down; an appliance declares no
    #     address at all, so the only address in play is the one whoever
    #     controls the zone put in the A record. Nothing downstream of the
    #     forward answer -- not the reverse lookup, not anything else --
    #     may happen before that address has been checked.
    for pointer, fqdn, ptr_required in _appliance_targets(inventory, subdomain):
        if not config.permits_name(fqdn):
            findings.append(finding_for("VCF-PROBE-NAME-BLOCKED", pointer, name=fqdn))
            continue
        resolved = _bounded_resolve(resolve, fqdn, False, config.timeout_s)
        if resolved is None:
            findings.append(finding_for("VCF-PROBE-UNKNOWN", pointer, target=fqdn,
                                        reason="no forward DNS answer"))
            continue
        if not config.permits(resolved):
            findings.append(finding_for("VCF-PROBE-TARGET-BLOCKED", pointer,
                                        target=resolved))
            continue
        reverse = _bounded_resolve(resolve, resolved, True, config.timeout_s)
        if reverse is None:
            if ptr_required:
                findings.append(finding_for("VCF-PROBE-NO-REVERSE-DNS", pointer,
                                            ip=resolved, fqdn=fqdn))
            continue
        # gethostbyname() follows a CNAME silently and gethostbyaddr()[0]
        # returns the canonical name, so on a lab where the appliance name
        # is an alias the PTR legitimately names something else. Accept the
        # queried name or any name it answers to; only a third name is a
        # mismatch.
        names = {_dns_name(fqdn)}
        aliases = _bounded_resolve(resolve, fqdn, False, config.timeout_s,
                                   want_aliases=True)
        if isinstance(aliases, (tuple, list)):
            names |= {_dns_name(alias) for alias in aliases}
        if _dns_name(reverse) not in names:
            findings.append(finding_for("VCF-PROBE-REVERSE-MISMATCH", pointer,
                                        ip=resolved, resolved=reverse, fqdn=fqdn))

    findings.extend(_resolver_vantage_point(inventory, read_resolvers))

    # An operator who asked for probes and got none must not be handed a
    # pass. VCF-PROBE-TARGET-BLOCKED is per-host and `warning`, which is
    # correct -- partial blocking is legitimate, and must still probe the
    # permitted hosts -- but `warning` is not in BLOCKING, so an allowlist
    # that matched *nothing* produced exit 0, valid: true, layers_run
    # including "probes", and zero lookups. That is the misleading clean
    # pass the CLI's empty-allowlist check was written to prevent;
    # non-emptiness was simply the wrong test for it. This is the right
    # one, and it lives here rather than in the CLI because the condition
    # is only knowable after reading the document, and because every
    # caller of run_probes needs it, not just the one with argv.
    if candidates and not permitted:
        findings.append(finding_for("VCF-PROBE-NOTHING-PERMITTED", "/hosts",
                                    count=candidates))
    return Result(tuple(findings))
