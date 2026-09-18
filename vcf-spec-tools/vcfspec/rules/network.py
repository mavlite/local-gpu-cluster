"""Cross-field network rules the schema cannot express.

These run on documents that may have failed schema validation, so every lookup
is defensive: a malformed entry is skipped, not raised on.
"""
from __future__ import annotations

import ipaddress
from collections import defaultdict

from ..findings import Finding, Result
from . import finding_for

TEP_MTU_MIN = 1600
TEPS_PER_HOST = 2


def check_networks(inventory: dict) -> Result:
    networks = _usable_networks(inventory)
    findings: list[Finding] = []
    findings += _gateway_rules(networks)
    findings += _overlap_rules(networks)
    findings += _vlan_rules(networks)
    findings += _mtu_rules(inventory)
    findings += _host_rules(inventory, networks)
    findings += _tep_pool_rules(inventory)
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


def _mapping(value: object) -> dict:
    """Return value when it is a mapping, else an empty one.

    Rules run on documents that failed schema validation, so a field of the
    wrong type must be skipped, not raised on.
    """
    return value if isinstance(value, dict) else {}


def _network(value) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except (TypeError, ValueError):
        return None


def _address(value):
    try:
        return ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return None


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
    for index, host in enumerate(inventory.get("hosts") or []):
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
    hosts = len(inventory.get("hosts") or [])
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
