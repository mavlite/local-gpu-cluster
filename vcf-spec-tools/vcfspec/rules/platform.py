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
from .coerce import as_sequence as _sequence
from .tables import (AUTO_RAID_OVERHEAD, ESX_HOST_RAM_OVERHEAD_GB, STACK_RAM_GB,
                     STACK_STORAGE_GB, TB_TO_GB, VSP_POOL_MIN)


def check_platform(inventory: dict) -> Result:
    findings: list[Finding] = []
    findings += _naming_rules(inventory)
    findings += _local_suffix_rules(inventory)
    findings += _vsp_rules(inventory)
    findings += _capacity_rules(inventory)
    findings.append(finding_for("VCF-LIC-EVALUATION", "/instance"))
    return Result(tuple(findings))


def _named_values(inventory: dict) -> list[tuple[str, str]]:
    """(json pointer, value) for every name the deployment will publish."""
    out: list[tuple[str, str]] = []
    nsx = _mapping(inventory.get("nsx"))
    appliances = _mapping(inventory.get("appliances"))
    vsp = _mapping(appliances.get("vsp"))
    for pointer, value in (
        ("/dns/subdomain", _mapping(inventory.get("dns")).get("subdomain")),
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
        if ("." in value and domain and value.lower() != domain
                and not value.lower().endswith(f".{domain}")):
            out.append(finding_for("VCF-NAME-WRONG-DOMAIN", pointer,
                                   value=value, domain=domain))
    return out


_LOCAL_SUFFIX = ".local"


def _is_local(name: str) -> bool:
    """True for the mDNS namespace, on whole labels.

    `.endswith(".local")` alone misses a single-label zone written as
    `local` -- unusual, but legal to write and the same unsupported
    namespace. Matching whole labels is also what keeps `nonlocal` and
    `mylocal.example.net` out of it: the same segment-versus-character
    distinction permits_name() makes for DNS suffixes.
    """
    return name == "local" or name.endswith(_LOCAL_SUFFIX)

# Only these carry the restriction. VCF Operations, vCenter, NSX and SDDC
# Manager still allow .local during the transition window, so a rule that
# flagged them would reject a supported design -- which is exactly what an
# earlier draft of this rule did.
_VSP_NAME_POINTERS = (
    "/dns/subdomain",
    "/appliances/vsp/platformFqdn",
    "/appliances/vsp/instanceFqdn",
)


def _local_suffix_rules(inventory: dict) -> list[Finding]:
    """Flag .local on the VSP names.

    A VSP name is suppressed only when it is actually composed from an
    already-flagged dns.subdomain (fixing dns.subdomain fixes that name
    too, in one edit -- what the catalogue fix text promises). A name that
    ends in .local for its own, unrelated reason -- not because it is built
    on the declared subdomain -- keeps its own finding at its own pointer:
    correcting dns.subdomain would not fix it.
    """
    named = dict(_named_values(inventory))
    subdomain = str(named.get("/dns/subdomain", "")).lower().rstrip(".")
    subdomain_is_local = _is_local(subdomain)
    out: list[Finding] = []
    if subdomain_is_local:
        out.append(finding_for("VCF-NAME-VSP-LOCAL-SUFFIX", "/dns/subdomain",
                               value=named["/dns/subdomain"], where="/dns/subdomain"))
    for pointer in _VSP_NAME_POINTERS:
        if pointer == "/dns/subdomain":
            continue
        value = named.get(pointer)
        if not isinstance(value, str):
            continue
        normalized = value.lower().rstrip(".")
        if not _is_local(normalized):
            continue
        if subdomain_is_local and (normalized == subdomain
                                    or normalized.endswith(f".{subdomain}")):
            continue  # composed from the already-flagged subdomain
        out.append(finding_for("VCF-NAME-VSP-LOCAL-SUFFIX", pointer,
                               value=value, where=pointer))
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
    workarounds = _sequence(_mapping(inventory.get("provisioning")).get("hostWorkarounds"))
    consumer_amd = "consumer-amd" in workarounds
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
        # Memory tiering is declared, but the AMD Ryzen workaround that makes
        # tiered VMs power on at all was not requested: the capacity plan is
        # counting RAM that will not actually be there. See William Lam's
        # VCF 9.1 lab workarounds, cited in the catalogue entry.
        if tier > 0 and not consumer_amd:
            out.append(finding_for("VCF-CAP-TIERING-NEEDS-WORKAROUND",
                                   f"/hosts/{index}/hardware/memoryTieringGb",
                                   host=host.get("name", f"hosts[{index}]"),
                                   tier=tier))
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
