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

import inspect
import ipaddress
import socket
import threading
from collections.abc import Mapping
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
                      want_canonical: bool = False):
    """Forward, reverse, or a combined forward answer for the round trip.

    The third mode returns `(canonical name, primary address)` -- or None
    when the name does not resolve -- both taken from ONE
    gethostbyname_ex() answer.

    It returns the canonical name because gethostbyname() follows a CNAME
    silently while gethostbyaddr()[0] hands back the *canonical* name, so
    a CNAME'd appliance looks like a reverse mismatch on a perfectly
    healthy lab. It deliberately does NOT return the alias list:
    gethostbyname_ex()'s aliases are the forward zone's own claims about
    which other names it answers to, and the round-trip check exists to
    confirm that the forward and reverse zones -- two separate
    authorities -- agree. Letting the forward zone nominate the PTR it
    wants accepted makes the check unfalsifiable, which is worse than not
    running it, because it still reports success.

    It returns the address from the same answer so that the address the
    allowlist clears and the canonical name that certifies that address's
    PTR are two halves of one reply, not answers to two questions a zone
    is free to answer differently.
    """
    try:
        if want_canonical:
            canonical, _aliases, addresses = socket.gethostbyname_ex(name)
            return (canonical, addresses[0]) if addresses else None
        return socket.gethostbyaddr(name)[0] if want_reverse else socket.gethostbyname(name)
    except OSError:
        return None


def _default_connector(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _address_strings(value: object) -> list[str] | None:
    """The string entries of an iterable of addresses, or None if `value`
    is not a shape that can mean "a collection of addresses" at all.

    Used for BOTH sides of the resolver-vantage comparison: the declared
    `dns.nameservers` (which a schema-invalid document may make a scalar
    or a dict) and the injected reader's return value (which a caller may
    make anything at all).

    `str` and `bytes` are refused rather than iterated: iterating
    "10.50.10.5" yields its characters and renders as `1, 0, ., 5, 0, ...`
    in a message the operator has to act on. A mapping is refused because
    iterating one yields its keys, which is the same garbling wearing a
    different hat. Anything else iterable is accepted -- a set, a
    frozenset, a generator -- because `lambda: {line.split()[1] for line
    in handle}` is an entirely natural way to write this reader and the
    seam's return type is documented nowhere, so a caller cannot be said
    to have broken a contract they were never given.

    None (refused) is distinct from [] (an iterable that held no strings),
    because the two deserve different treatment: the caller's seam is
    broken in the first case and merely empty in the second.
    """
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping):
        return None
    try:
        return [x for x in value if isinstance(x, str)]   # type: ignore[union-attr]
    except TypeError:
        return None          # not iterable at all


def _dns_name(value: object) -> str:
    """Normalise a DNS name for comparison. Resolvers return a trailing dot
    and arbitrary case; neither is a difference, and treating either as one
    would fail every correctly-configured lab.
    """
    return str(value).strip().rstrip(".").lower()


def _bounded_resolve(resolve, name: str, want_reverse: bool, timeout_s: float,
                     want_canonical: bool = False):
    """Call resolve(name, want_reverse, want_canonical), but never wait
    past timeout_s.

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
            # All three arguments, every call. There was briefly a shim
            # here that passed the third only when it was needed, so that
            # two-parameter resolvers would "keep working" -- but the
            # appliance path always needs it, so what they actually did
            # was raise TypeError in this thread, get swallowed by the
            # except below, and return None: indistinguishable from a
            # name that does not resolve. Every appliance came back
            # VCF-PROBE-UNKNOWN at `info`, the run stayed valid, and the
            # round trip never ran. One contract, checked once up front
            # by _accepts_three_arguments, is honest; a shim that cannot
            # keep its promise is worse than no shim, because it fails
            # silently and in the reassuring direction.
            box[0] = resolve(name, want_reverse, want_canonical)
        except Exception:
            box[0] = None

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    return None if thread.is_alive() else box[0]


def _unusable_reason(seam: object, *sample_args: object) -> str | None:
    """Why this injected seam cannot be used, or None if it can be.

    `sample_args` is a representative call: resolver and connector take
    three positional arguments, resolv_conf_reader takes none, and the
    check is simply whether the callable can be called that way.

    Two failure shapes, and they are not the same:

    * **Not callable at all.** Refused outright. This is the one that
      matters, because the arity branch below fails open and a
      non-callable would sail through it -- inspect.signature(5) raises
      TypeError, which is indistinguishable from "unreadable signature".
      Every subsequent call would then raise inside _bounded_resolve's
      worker, be swallowed with every other resolver failure, and the run
      would report a document of unresolvable names and stay valid: the
      same silent pass a two-parameter resolver used to produce, reached
      by a different wrong type.
    * **Callable but the wrong arity.** Also refused -- but only when the
      signature can actually be read. An unreadable signature (a builtin
      or any C callable, of which socket.gethostbyname is one) is not
      evidence of anything, so it proceeds.

    This is an **interface** check, never a security gate. permits() and
    permits_name() remain the only things that decide whether a probe may
    happen; nothing here may widen or narrow them.
    """
    if not callable(seam):
        return f"not callable ({type(seam).__name__})"
    try:
        signature = inspect.signature(seam)
    except Exception:
        # Anything the object's __signature__ chooses to raise lands
        # here. The exception type an injected callable raises is not
        # this module's to assume -- the same reason
        # _resolver_vantage_point catches Exception around its reader --
        # and api.py calls run_probes without a wrapper, so letting one
        # escape hands the caller a traceback.
        return None                    # unreadable -- proceed, do not refuse
    try:
        signature.bind(*sample_args)
    except TypeError:
        return (f"cannot be called with {len(sample_args)} positional "
                f"argument{'' if len(sample_args) == 1 else 's'}")
    return None


def _guarded_connect(connect, host: str, port: int, timeout: float):
    """connect(), but a raising connector is data, not an escape.

    True/False are the connector's own answer. None means it raised --
    which is a different fact from "nothing answered on that port", and
    is reported as such, because an operator who reads "no TCP 443
    response" will go and look at the lab rather than at their code.

    This is the counterpart of _bounded_resolve's `except Exception`, and
    it closes the same hole one level up. _unusable_reason can only ask
    whether a seam is callable with the right arity; it cannot know what
    the call does. The default connector is not exempt: it catches
    OSError, so `create_connection((171051531, 443))` -- a legal YAML
    integer that ipaddress.ip_address(), and therefore permits(),
    accepts -- raised TypeError straight out of run_probes and through
    api.py's unwrapped call.

    Note what is NOT wrapped like this: config.permits() and
    config.permits_name(). A call that produces data is guarded; a call
    that decides permission never is, because "it raised, carry on" is
    indistinguishable from "it said yes".
    """
    try:
        return bool(connect(host, port, timeout))
    except Exception:
        return None


def _compose_name(short: object, subdomain: object) -> str | None:
    """`short.subdomain`, or None when `short` is not a usable name.

    Both halves of that are shared, and the second half is why this
    returns Optional. An earlier version unified only the *stripping*,
    and the two callers stayed out of step on *validity*: the appliance
    path rejected non-strings and blanks, the host path did not. So
    `hosts[0].name: null` asked the resolver for
    "None.lab.example.net", and `name: ""` asked for ".lab.example.net"
    -- which permits_name() permits, because ".suffix".endswith(".suffix")
    is true. Nothing escaped containment, but a finding naming
    `None.lab.example.net` is not something an operator can act on, and
    it is the same garbled-value class already fixed for dns.nameservers
    and nsx.managers.

    Stripping is load-bearing for the same reason: permits_name() strips
    before matching, so " esx01 " composes a name the gate permits and
    the resolver is then asked for with a space inside the label.
    """
    if not isinstance(short, str):
        return None
    head = short.strip()
    if not head:
        return None
    if not subdomain:              # preserves the original `if subdomain`
        return head                # test: None and "" both mean "no suffix"
    if not isinstance(subdomain, str):
        # The other half of the same guard, and it took one round longer
        # to arrive: a truthy non-string subdomain composed
        # "esx01.{'a': 1}", which permits_name() refuses -- so it never
        # left the process, but it did reach the operator, inside the
        # VCF-PROBE-NAME-BLOCKED message. A falsy one still means "no
        # suffix" above; only a subdomain that is present and unusable
        # lands here, and there is no name to compose from it.
        return None
    tail = subdomain.strip()
    return f"{head}.{tail}" if tail else head


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

    for pointer, value, ptr_required in (
        ("/appliances/vcenter/hostname",
         _compose_name(_mapping(appliances.get("vcenter")).get("hostname"),
                       subdomain), True),
        ("/appliances/sddcManager/hostname",
         _compose_name(_mapping(appliances.get("sddcManager")).get("hostname"),
                       subdomain), True),
    ):
        if value:
            out.append((pointer, value, ptr_required))
    # A wrong-typed managers section is skipped, not iterated: enumerate()
    # over a bare string would happily yield its characters as names.
    managers = nsx.get("managers")
    if not isinstance(managers, (list, tuple)):
        managers = ()
    for index, manager in enumerate(managers):
        composed = _compose_name(manager, subdomain)
        if composed:
            out.append((f"/nsx/managers/{index}", composed, True))
    # Already fully qualified -- appending the subdomain again is the bug.
    for pointer, value, ptr_required in (
        ("/nsx/vipFqdn", nsx.get("vipFqdn"), False),
        ("/appliances/vsp/platformFqdn", vsp.get("platformFqdn"), False),
        ("/appliances/vsp/instanceFqdn", vsp.get("instanceFqdn"), True),
    ):
        # Same stripping rule as compose(), for the same reason: these
        # were tested with .strip() and then appended without it.
        if isinstance(value, str) and value.strip():
            out.append((pointer, value.strip(), ptr_required))
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
    # A wrong-typed nameservers section is silently skipped, not reported:
    # that is a schema-invalid DOCUMENT, and the rules layer already says so.
    declared = _address_strings(_mapping(inventory.get("dns")).get("nameservers")) or []
    if not declared:
        return []          # nothing declared, nothing to compare against
    try:
        reader_answer = reader()
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
    # The same coercion as `declared`, and for the same reason. Guarding
    # the reader's CALL while consuming its RETURN VALUE unguarded left
    # `set(runner)` raising TypeError straight out of run_probes on
    # `lambda: 5`, and a string return iterating per character into the
    # operator's message -- the exact garbling the declared side already
    # refused. Three separate fixes on this branch guarded one half of a
    # pair and left the other: short/subdomain, declared/runner, call/
    # return value. One function both sides call is the fix that does not
    # need a fourth.
    runner = _address_strings(reader_answer)
    if runner is None:
        # The reader DID answer -- the answer was a shape that cannot mean
        # "a collection of addresses". Discarding it silently would leave
        # the operator with no vantage-point note and no reason for its
        # absence, which is this module's condemned pattern: a seam that
        # cannot keep its promise fails in the reassuring direction. The
        # resolver seam already reports its own wrong-shaped answer this
        # way; so does this one.
        return [finding_for("VCF-PROBE-UNKNOWN", "/dns/nameservers",
                            target=_RESOLV_CONF,
                            reason="the resolver configuration reader returned "
                                   f"{type(reader_answer).__name__}, which is not "
                                   "a collection of addresses")]
    if not runner or set(runner) & set(declared):
        return []
    return [finding_for("VCF-PROBE-RESOLVER-MISMATCH", "/dns/nameservers",
                        runner=", ".join(runner), declared=", ".join(declared))]


def run_probes(inventory: dict, config: ProbeConfig, resolver=None,
               connector=None, *, resolv_conf_reader=None) -> Result:
    # `is None`, not `or`: `connector or _default_connector` silently
    # replaces any FALSY non-callable -- [], 0, "" -- with the real
    # network-touching default, so it never reached _unusable_reason and
    # the README's promise that a non-callable "is reported once and
    # probes nothing" was false for exactly the values least likely to be
    # deliberate. Only an omitted seam gets the default.
    resolve = _default_resolver if resolver is None else resolver
    connect = _default_connector if connector is None else connector
    read_resolvers = (_default_resolv_conf_reader if resolv_conf_reader is None
                      else resolv_conf_reader)
    # _mapping, not `or {}`: `or {}` only rescues a falsy dns section. A
    # scalar or a list -- both of which a schema-invalid document really
    # produces, e.g. `dns: vcf.lab.example.net` written without the nested
    # key -- is truthy and has no .get(), so this raised AttributeError
    # through api.py's unguarded run_probes call and reached the operator
    # as a traceback rather than a finding.
    subdomain = _mapping(inventory.get("dns")).get("subdomain", "")
    findings: list[Finding] = []

    # One usability check per injected seam, before any lookup.
    # _bounded_resolve swallows everything the resolver raises -- which
    # is what keeps a wedged or exploding resolver from reaching the
    # caller -- so without this an unusable resolver is reported as a
    # document full of unresolvable names at `info`, and the run passes.
    # connect() has the opposite failure: it is called on this thread
    # with nothing catching it, so an unusable connector raises straight
    # out of run_probes, through api.py's unwrapped call, to the caller.
    # Refuse once, name the seam and the reason, and probe nothing.
    for seam_name, seam, sample in (
        ("resolver", resolve, ("argument", False, False)),
        ("connector", connect, ("argument", 0, 0.0)),
        ("resolver configuration reader", read_resolvers, ()),
    ):
        reason = _unusable_reason(seam, *sample)
        if reason is not None:
            return Result((finding_for("VCF-PROBE-SEAM-UNUSABLE", "/",
                                       seam=seam_name, reason=reason),))

    candidates = permitted = lookups = 0

    def lookup(name: str, want_reverse: bool, want_canonical: bool = False):
        """_bounded_resolve, counting. The count is what lets the
        vantage-point note stay silent when no answer was ever obtained:
        "Probe answers came from 1.1.1.1" is a claim about answers, and
        with every target blocked there are none to have a provenance."""
        nonlocal lookups
        lookups += 1
        return _bounded_resolve(resolve, name, want_reverse, config.timeout_s,
                                want_canonical)

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
        # permits() cleared this value, so ipaddress.ip_address() parsed
        # it -- but it parses more spellings than the rest of the layer
        # understands, and `mgmtIp: 171051531` is a legal YAML integer it
        # reads as 10.50.10.11. Every step after the gate has to agree
        # with the gate, so the canonical string is what gets used from
        # here on: the raw int otherwise went to create_connection()
        # (TypeError, escaping), to gethostbyaddr() (TypeError, swallowed
        # into a spurious VCF-PROBE-NO-REVERSE-DNS at `error`) and into
        # `resolved != ip`, where a string never equals an int, for a
        # spurious VCF-PROBE-FORWARD-MISMATCH.
        #
        # This canonicalises AFTER the gate and never before it. permits()
        # is unchanged and still refuses " 10.50.10.11 ", "010.050.010.011"
        # and anything else it does not parse; normalising first would
        # have widened the allowlist, which is the one thing this may not
        # do. The try is belt-and-braces for a subclassed ProbeConfig
        # whose permits() does not imply ip_address() succeeds.
        try:
            probe_ip = str(ipaddress.ip_address(ip))
        except (TypeError, ValueError):
            probe_ip = ip
        # _compose_name, not an f-string: it strips both parts and
        # rejects a name that is not usable as one. This sibling of the
        # appliance path composed " esx01 .lab.example.net" from a padded
        # host name (permits_name() permits it, because it strips before
        # matching) and "None.lab.example.net" from a null one.
        fqdn = _compose_name(name, subdomain)
        if fqdn is None:
            # No usable name: no forward query to issue, and nothing for
            # a PTR to be compared against. Reporting the host is the
            # point -- it is not silently dropped -- but reporting it
            # against a name assembled out of `None` is the garbled
            # message this guard exists to prevent. The schema layer is
            # what says the name is missing; this says why the probe
            # could not run.
            findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=probe_ip,
                                        reason="no usable DNS name: check the host's "
                                               "name and dns.subdomain"))
            continue
        # The name gate sits here, before the lookup, for the same reason
        # the IP gate sits before connect(): the query IS the leak, so
        # filtering its answer afterwards is already too late.
        if not config.permits_name(fqdn):
            findings.append(finding_for("VCF-PROBE-NAME-BLOCKED", path, name=fqdn))
        else:
            resolved = lookup(fqdn, False)
            if resolved is None:
                findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=fqdn,
                                            reason="no forward DNS answer"))
            elif resolved != probe_ip:
                findings.append(finding_for("VCF-PROBE-FORWARD-MISMATCH", path,
                                            fqdn=fqdn, resolved=resolved,
                                            expected=probe_ip))
        reverse = lookup(probe_ip, True)
        if reverse is None:
            findings.append(finding_for("VCF-PROBE-NO-REVERSE-DNS", path,
                                        ip=probe_ip, fqdn=fqdn))
        elif _dns_name(reverse) != _dns_name(fqdn):
            # A PTR that exists but names a different host is worse than a
            # missing one: it looks healthy and VCF fails obscurely on the
            # disagreement. Checking only that reverse returned *something*
            # is why this went undetected until the tool met real dnsmasq.
            findings.append(finding_for("VCF-PROBE-REVERSE-MISMATCH", path,
                                        ip=probe_ip, resolved=reverse, fqdn=fqdn))
        reachable = _guarded_connect(connect, probe_ip, ESX_PORT, config.timeout_s)
        if reachable is None:
            findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=probe_ip,
                                        reason="the connector raised"))
        elif not reachable:
            findings.append(finding_for("VCF-PROBE-UNKNOWN", path, target=probe_ip,
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
        # One forward query, carrying both halves of the answer: the
        # address the allowlist must clear, and the canonical name that
        # will certify that address's PTR. Asking twice would let a zone
        # answer the two questions inconsistently.
        answer = lookup(fqdn, False, want_canonical=True)
        if answer is None:
            findings.append(finding_for("VCF-PROBE-UNKNOWN", pointer, target=fqdn,
                                        reason="no forward DNS answer"))
            continue
        if not (isinstance(answer, (tuple, list)) and len(answer) == 2):
            # The resolver DID answer -- the answer was the wrong shape.
            # This is what an operator sees when a zone tries to smuggle
            # extra names into the accept-set, so it must not be dressed
            # up as a name that simply did not resolve.
            findings.append(finding_for("VCF-PROBE-UNKNOWN", pointer, target=fqdn,
                                        reason="forward answer was not a "
                                               "(canonical name, address) pair"))
            continue
        canonical, resolved = answer
        if not isinstance(resolved, str) or not resolved.strip():
            # Fails closed either way -- permits(None) is False -- but
            # "Refused to probe None: outside the configured allowlist"
            # told the operator nothing true about what happened.
            findings.append(finding_for("VCF-PROBE-UNKNOWN", pointer, target=fqdn,
                                        reason="forward answer carried no address"))
            continue
        # NB: `resolved`, not `resolved.strip()`. permits() rejects a
        # padded address, and that is the direction to fail in; stripping
        # here would widen what the allowlist accepts.
        if not config.permits(resolved):
            findings.append(finding_for("VCF-PROBE-TARGET-BLOCKED", pointer,
                                        target=resolved))
            continue
        reverse = lookup(resolved, True)
        if reverse is None:
            if ptr_required:
                findings.append(finding_for("VCF-PROBE-NO-REVERSE-DNS", pointer,
                                            ip=resolved, fqdn=fqdn))
            continue
        # gethostbyname() follows a CNAME silently and gethostbyaddr()[0]
        # returns the canonical name, so on a lab where the appliance name
        # is an alias the PTR legitimately names something else. The
        # canonical name, and only it, is therefore accepted alongside the
        # queried name -- never the forward answer's alias list, which is
        # the forward zone's own claim about which names it answers to. A
        # check the checked party can satisfy by asserting it is not a
        # check: with the alias list accepted, a zone returning
        # aliases=(fqdn, "attacker.example") and PTR="attacker.example"
        # produced zero findings on any input.
        #
        # And the canonical name must clear permits_name() itself. A name
        # outside the operator's --allowlist-domain is not evidence about
        # the operator's zone, so it certifies nothing; without this gate
        # the forward zone could still nominate any PTR it liked, just via
        # the canonical slot instead of the alias list.
        names = {_dns_name(fqdn)}
        if config.permits_name(canonical):
            names.add(_dns_name(canonical))
        if _dns_name(reverse) not in names:
            findings.append(finding_for("VCF-PROBE-REVERSE-MISMATCH", pointer,
                                        ip=resolved, resolved=reverse, fqdn=fqdn))

    # Only when at least one answer was actually obtained: see lookup().
    if lookups:
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
