"""Cross-field network rules the schema cannot express.

These run on documents that may have failed schema validation, so every lookup
is defensive: a malformed entry is skipped, not raised on.
"""
from __future__ import annotations

from collections import defaultdict

from ..findings import Finding, Result
from . import finding_for
from .coerce import as_address as _address
from .coerce import as_mapping as _mapping
from .coerce import as_network as _network
from .coerce import as_sequence as _sequence

TEP_MTU_MIN = 1600
TEPS_PER_HOST = 2

# VCF 9.1.1 Installer's own `POST /v1/sddcs/validations` Deployment
# Specification check demands an IP pool (`includeIpAddressRanges`) for
# these two networks -- one vmk per host, per network -- and rejects the
# spec with NOT_ENOUGH_IPS / MISSING_INCLUSION_RANGE when it is missing or
# undersized. Management does not need one: hosts carry mgmtIp individually.
IP_POOL_REQUIRED_PURPOSES = ("vmotion", "vsan")

# The Installer's own remediation text: "Insufficient IP Addresses ...
# (IP Addresses ending with 0 and 255 are ignored)".
_EXCLUDED_LAST_OCTETS = (0, 255)


def check_networks(inventory: dict) -> Result:
    networks = _usable_networks(inventory)
    findings: list[Finding] = []
    findings += _gateway_rules(networks)
    findings += _overlap_rules(networks)
    findings += _vlan_rules(networks)
    findings += _mtu_rules(inventory)
    findings += _host_rules(inventory, networks)
    findings += _tep_pool_rules(inventory)
    findings += _ip_pool_rules(inventory)
    return Result(tuple(findings))


def _usable_networks(inventory: dict) -> dict[str, dict]:
    """Purpose -> {net, vlan, gateway, subnet}, skipping anything malformed."""
    out: dict[str, dict] = {}
    for purpose, entry in _mapping(inventory.get("networks")).items():
        if not isinstance(entry, dict):
            continue
        net = _network(entry.get("subnet"))
        if net is None:
            continue
        out[purpose] = {"net": net, "vlan": entry.get("vlan"),
                        "gateway": entry.get("gateway"), "subnet": entry["subnet"]}
    nsx = _mapping(inventory.get("nsx"))
    tep = _mapping(nsx.get("tepPool"))
    tep_net = _network(tep.get("cidr"))
    if tep_net is not None:
        out["nsx.tepPool"] = {"net": tep_net, "vlan": nsx.get("transportVlanId"),
                              "gateway": tep.get("gateway"), "subnet": tep["cidr"]}
    return out


def _gateway_rules(networks: dict) -> list[Finding]:
    out = []
    for purpose, entry in sorted(networks.items()):
        gateway = _address(entry["gateway"])
        if gateway is not None and gateway not in entry["net"]:
            out.append(finding_for("VCF-NET-GATEWAY-OUTSIDE-SUBNET",
                                   f"/networks/{purpose}/gateway",
                                   gateway=entry["gateway"], subnet=entry["subnet"],
                                   purpose=purpose))
    return out


def _overlap_rules(networks: dict) -> list[Finding]:
    out = []
    items = sorted(networks.items())
    for index, (purpose, entry) in enumerate(items):
        for other, other_entry in items[index + 1:]:
            if entry["net"].overlaps(other_entry["net"]):
                out.append(finding_for("VCF-NET-SUBNET-OVERLAP",
                                       f"/networks/{purpose}/subnet",
                                       purpose=purpose, subnet=entry["subnet"],
                                       other=other, other_subnet=other_entry["subnet"]))
    return out


def _vlan_rules(networks: dict) -> list[Finding]:
    seen: dict[int, str] = {}
    out = []
    for purpose, entry in sorted(networks.items()):
        vlan = entry["vlan"]
        if vlan is None:
            continue
        if vlan in seen:
            out.append(finding_for("VCF-NET-VLAN-REUSED", f"/networks/{purpose}/vlan",
                                   vlan=vlan, purpose=purpose, other=seen[vlan]))
        else:
            seen[vlan] = purpose
    return out


def _mtu_rules(inventory: dict) -> list[Finding]:
    mtu = _mapping(inventory.get("nsx")).get("fabricMtu")
    if isinstance(mtu, int) and mtu < TEP_MTU_MIN:
        return [finding_for("VCF-NSX-FABRIC-MTU-TOO-LOW", "/nsx/fabricMtu", mtu=mtu)]
    return []


def _host_rules(inventory: dict, networks: dict) -> list[Finding]:
    out: list[Finding] = []
    mgmt = networks.get("management")
    seen: dict[str, list[str]] = defaultdict(list)
    for index, host in enumerate(_sequence(inventory.get("hosts"))):
        if not isinstance(host, dict):
            continue
        ip, name = host.get("mgmtIp"), host.get("name", f"hosts[{index}]")
        address = _address(ip)
        if address is None:
            continue
        seen[str(ip)].append(name)
        if mgmt and address not in mgmt["net"]:
            out.append(finding_for("VCF-NET-HOST-IP-OUTSIDE-SUBNET",
                                   f"/hosts/{index}/mgmtIp", name=name, ip=ip,
                                   subnet=mgmt["subnet"]))
    for ip, owners in sorted(seen.items()):
        if len(owners) > 1:
            out.append(finding_for("VCF-NET-DUPLICATE-IP", "/hosts", ip=ip,
                                   where=", ".join(sorted(owners))))
    return out


def _tep_pool_rules(inventory: dict) -> list[Finding]:
    pool = _mapping(_mapping(inventory.get("nsx")).get("tepPool"))
    ranges = pool.get("ranges")
    if not isinstance(ranges, list):
        ranges = []
    hosts = len(_sequence(inventory.get("hosts")))
    size = 0
    for entry in ranges:
        if not isinstance(entry, dict):
            continue
        start, end = _address(entry.get("start")), _address(entry.get("end"))
        if start is None or end is None or int(end) < int(start):
            continue
        size += int(end) - int(start) + 1
    needed = hosts * TEPS_PER_HOST
    if hosts and size < needed:
        return [finding_for("VCF-NSX-TEP-POOL-TOO-SMALL", "/nsx/tepPool/ranges",
                            size=size, hosts=hosts, needed=needed)]
    return []


def _usable_pool_size(start, end) -> int:
    """Addresses in [start, end], excluding any ending in .0 or .255 --
    the VCF Installer ignores both when it counts a pool's supply."""
    start_int, end_int = int(start), int(end)
    if end_int < start_int:
        return 0
    total = end_int - start_int + 1
    for last_octet in _EXCLUDED_LAST_OCTETS:
        total -= _count_congruent(start_int, end_int, 256, last_octet)
    return total


def _count_congruent(low: int, high: int, modulus: int, remainder: int) -> int:
    """How many integers in [low, high] are congruent to remainder mod modulus.

    Closed-form on purpose: a pool's start/end are schema-valid addresses
    but nothing bounds how far apart they are, and this runs on
    caller-supplied documents -- iterating address-by-address would let a
    single 0.0.0.0-255.255.255.255 pool spin for billions of steps.
    """
    first = low + ((remainder - low) % modulus)
    if first > high:
        return 0
    return (high - first) // modulus + 1


def _ip_pool_rules(inventory: dict) -> list[Finding]:
    out: list[Finding] = []
    networks = _mapping(inventory.get("networks"))
    hosts = len(_sequence(inventory.get("hosts")))
    for purpose in IP_POOL_REQUIRED_PURPOSES:
        entry = _mapping(networks.get(purpose))
        if not entry:
            continue  # absent/malformed network; other rules cover its shape
        pool = _mapping(entry.get("pool"))
        start, end = _address(pool.get("start")), _address(pool.get("end"))
        if start is None or end is None:
            out.append(finding_for("VCF-NET-IP-POOL-MISSING", f"/networks/{purpose}/pool",
                                   purpose=purpose, purpose_upper=purpose.upper()))
            continue
        size = _usable_pool_size(start, end)
        if hosts and size < hosts:
            out.append(finding_for("VCF-NET-IP-POOL-TOO-SMALL", f"/networks/{purpose}/pool",
                                   purpose=purpose, purpose_upper=purpose.upper(),
                                   size=size, hosts=hosts))
    return out
