"""VCF platform rules: naming, VCF Management Services, capacity, licensing.

Rules run on documents that may have failed schema validation, so every
lookup is defensive: a malformed (wrong-typed) section is skipped, not
raised on -- mirroring the ``_mapping()`` discipline in rules/network.py.
"""
from __future__ import annotations

from ..findings import Finding, Result
from . import finding_for
from .coerce import as_address as _address
from .coerce import as_mapping as _mapping
from .coerce import as_network as _network
from .tables import (AUTO_RAID_OVERHEAD, ESX_HOST_RAM_OVERHEAD_GB, STACK_RAM_GB,
                     STACK_STORAGE_GB, TB_TO_GB, VSP_POOL_MIN)


def check_platform(inventory: dict) -> Result:
    findings: list[Finding] = []
    findings += _naming_rules(inventory)
    findings += _vsp_rules(inventory)
    findings += _capacity_rules(inventory)
    findings.append(finding_for("VCF-LIC-EVALUATION", "/instance"))
    return Result(tuple(findings))


def _sequence(value: object) -> list:
    """Return value when it is a list, else an empty one."""
    return value if isinstance(value, list) else []


def _named_values(inventory: dict) -> list[tuple[str, str]]:
    """(json pointer, value) for every name the deployment will publish."""
    out: list[tuple[str, str]] = []
    nsx = _mapping(inventory.get("nsx"))
    appliances = _mapping(inventory.get("appliances"))
    vsp = _mapping(appliances.get("vsp"))
    for pointer, value in (
        ("/nsx/vipFqdn", nsx.get("vipFqdn")),
        ("/appliances/vsp/platformFqdn", vsp.get("platformFqdn")),
        ("/appliances/vsp/instanceFqdn", vsp.get("instanceFqdn")),
        ("/appliances/vcenter/hostname", _mapping(appliances.get("vcenter")).get("hostname")),
        ("/appliances/sddcManager/hostname", _mapping(appliances.get("sddcManager")).get("hostname")),
    ):
        if isinstance(value, str):
            out.append((pointer, value))
    for index, host in enumerate(_sequence(inventory.get("hosts"))):
        if isinstance(host, dict) and isinstance(host.get("name"), str):
            out.append((f"/hosts/{index}/name", host["name"]))
    for index, manager in enumerate(_sequence(nsx.get("managers"))):
        if isinstance(manager, str):
            out.append((f"/nsx/managers/{index}", manager))
    return out


def _naming_rules(inventory: dict) -> list[Finding]:
    domain = str(_mapping(inventory.get("dns")).get("subdomain", "")).lower()
    out = []
    for pointer, value in _named_values(inventory):
        if value != value.lower():
            out.append(finding_for("VCF-NAME-NOT-LOWERCASE", pointer,
                                   value=value, where=pointer))
        if "." in value and domain and not value.lower().endswith(f".{domain}"):
            out.append(finding_for("VCF-NAME-WRONG-DOMAIN", pointer,
                                   value=value, domain=domain))
    return out


def _vsp_rules(inventory: dict) -> list[Finding]:
    vsp = _mapping(_mapping(inventory.get("appliances")).get("vsp"))
    out: list[Finding] = []
    start, end = _address(vsp.get("poolStart")), _address(vsp.get("poolEnd"))
    if start is not None and end is not None and int(end) >= int(start):
        size = int(end) - int(start) + 1
        if size < VSP_POOL_MIN:
            out.append(finding_for("VCF-VSP-POOL-TOO-SMALL",
                                   "/appliances/vsp/poolEnd", size=size))
        if not _same_subnet(inventory, start, end):
            out.append(finding_for("VCF-VSP-POOL-CROSSES-SUBNET",
                                   "/appliances/vsp/poolStart",
                                   start=vsp.get("poolStart"), end=vsp.get("poolEnd")))
    cidr = _network(vsp.get("internalCidr"))
    if cidr is not None:
        for purpose, entry in sorted(_mapping(inventory.get("networks")).items()):
            entry = _mapping(entry)
            subnet = _network(entry.get("subnet"))
            if subnet is not None and cidr.overlaps(subnet):
                out.append(finding_for("VCF-VSP-INTERNAL-CIDR-COLLISION",
                                       "/appliances/vsp/internalCidr",
                                       cidr=vsp["internalCidr"], purpose=purpose,
                                       subnet=entry["subnet"]))
    return out


def _same_subnet(inventory: dict, start, end) -> bool:
    for entry in _mapping(inventory.get("networks")).values():
        subnet = _network(_mapping(entry).get("subnet"))
        if subnet is not None and start in subnet and end in subnet:
            return True
    return False


def _capacity_rules(inventory: dict) -> list[Finding]:
    hosts = [h for h in _sequence(inventory.get("hosts")) if isinstance(h, dict)]
    out: list[Finding] = []
    usable: list[float] = []
    storage_gb = 0.0
    for index, host in enumerate(hosts):
        hardware = _mapping(host.get("hardware"))
        ram = hardware.get("ramGb")
        if not isinstance(ram, (int, float)) or ram <= 0:
            out.append(finding_for("VCF-CAP-UNKNOWN-HARDWARE", f"/hosts/{index}",
                                   name=host.get("name", f"hosts[{index}]")))
            continue
        tier = hardware.get("memoryTieringGb")
        tier = tier if isinstance(tier, (int, float)) else 0
        usable.append(max(0.0, float(ram) + float(tier) - ESX_HOST_RAM_OVERHEAD_GB))
        vsan_tb = hardware.get("vsanDeviceTb")
        vsan_tb = vsan_tb if isinstance(vsan_tb, (int, float)) else 0
        storage_gb += float(vsan_tb) * TB_TO_GB

    if not usable:
        return out

    total = round(sum(usable), 2)
    if total < STACK_RAM_GB:
        out.append(finding_for("VCF-CAP-RAM-SHORTFALL", "/hosts",
                               needed=STACK_RAM_GB, available=total))
    else:
        n1 = round(total - max(usable), 2)
        if n1 < STACK_RAM_GB:
            out.append(finding_for("VCF-CAP-N1-SHORTFALL", "/hosts",
                                   needed=STACK_RAM_GB, available=n1))

    storage_type = str(_mapping(inventory.get("storage")).get("type", ""))
    if storage_type.startswith("VSAN"):
        usable_storage = round(storage_gb / AUTO_RAID_OVERHEAD, 2)
        if usable_storage < STACK_STORAGE_GB:
            out.append(finding_for("VCF-CAP-STORAGE-SHORTFALL", "/hosts",
                                   needed=STACK_STORAGE_GB, available=usable_storage))
    return out
