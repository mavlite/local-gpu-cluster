"""Device-identifier facts about a lab inventory.

These checks lived inside `render_provisioning()`, which no shipped entry
point calls -- not `cli.py` (validate/render only), not `api.py`, not any MCP
tool. Measured 2026-09-24: `vcfspec validate` on the real three-host lab
inventory ran [detect, schema, rules] and emitted one finding,
VCF-LIC-EVALUATION. Not a single VCF-PROV-* rule. An inventory naming an
unstable boot disk validated clean, which is precisely what
VCF-PROV-BOOT-DISK-NOT-STABLE was written to prevent after a defect recorded
as one that "would have cost a drive to the rack".

They are facts about the inventory, not about rendering, so they belong here.
`provision.py` imports the same functions, so there is one definition and the
renderer and the validator cannot disagree about what a safe device is.

Nothing here does I/O or holds a secret, so it is safe on every surface the
rules layer already reaches, including MCP.
"""
from __future__ import annotations

import re

from ..findings import Finding
from ..firstboot import MAX_TIER_RATIO_PCT, MIN_TIER_RATIO_PCT
from . import finding_for
from .coerce import as_mapping, as_sequence

# Where ESX exposes block devices. bootDisk is passed to `install --disk=`
# uninterpreted because the installer accepts either spelling; the memtier
# device is normalised, because that command takes a path, not a device name.
DISK_PATH_PREFIX = "/vmfs/devices/disks/"

# A stable device identifier: the only boot-disk form this tool will emit.
# t10./eui./naa. are the device's own identity, reported by the device, and
# do not move when enumeration order does.
STABLE_DISK_RE = re.compile(
    r"^(?:/vmfs/devices/disks/)?(?:t10\.|eui\.|naa\.)[^\s]+$")
# Runtime names: assigned at boot, in discovery order, by the host. Two
# branches, not one, and case-insensitive -- the second catches any `mpx.`
# name whether or not it carries the vmhbaN:C:T:L tail, because `mpx.` alone
# already means "enumerated at boot" and that is the whole objection. Copied
# verbatim from provision.py rather than retyped: an earlier draft of this
# module reconstructed it from memory with only the first branch, which would
# have silently narrowed a safety check while claiming to consolidate it.
RUNTIME_DISK_RE = re.compile(
    r"^(?:/vmfs/devices/disks/)?(?:mpx\.)?vmhba\d+:C\d+:T\d+:L\d+$"
    r"|^(?:/vmfs/devices/disks/)?mpx\.", re.IGNORECASE)

# The three roles a host's NVMe devices are given. Order is the order they are
# reported in a collision message, so it is the order an operator reads.
_DEVICE_ROLES = (
    ("bootDisk", lambda host, hw: host.get("bootDisk")),
    ("vsanDevice", lambda host, hw: hw.get("vsanDevice")),
    ("memoryTieringDevice", lambda host, hw: hw.get("memoryTieringDevice")),
)


def device_key(selector: str) -> str:
    """`t10.X` and `/vmfs/devices/disks/t10.X` are one device, not two."""
    return selector[len(DISK_PATH_PREFIX):] if selector.startswith(
        DISK_PATH_PREFIX) else selector


def device_path(selector: str) -> str:
    """The `/vmfs/devices/disks/…` form `esxcli memtier enable -d` wants."""
    return selector if selector.startswith(DISK_PATH_PREFIX) else (
        DISK_PATH_PREFIX + selector)


def plain(value: float) -> str:
    """96.0 -> "96", so a finding message reads like the inventory does."""
    return str(int(value)) if float(value).is_integer() else str(value)


def tier_ratio_pct(tier_gb: float, ram_gb: float) -> int:
    """The `-r` percentage for `esxcli memtier enable`.

    Derived rather than pinned at a flat 100, because the device is normally
    larger than the tier the inventory asks for and this is what decides how
    much of it gets used. A non-positive ramGb yields 0, which the caller's
    range guard rejects with a message naming both numbers -- better than a
    ZeroDivisionError out of a pure function.
    """
    if ram_gb <= 0:
        return 0
    return int(round(tier_gb / ram_gb * 100))


def boot_disk_finding(selector: str, path: str, host: str) -> Finding | None:
    """None when `selector` is a stable device identifier, else why not."""
    if selector.startswith("--firstdisk"):
        return finding_for("VCF-PROV-BOOT-DISK-FIRSTDISK", path,
                           host=host, selector=selector)
    if RUNTIME_DISK_RE.match(selector):
        return finding_for("VCF-PROV-BOOT-DISK-RUNTIME-NAME", path,
                           host=host, selector=selector)
    if not STABLE_DISK_RE.match(selector):
        return finding_for("VCF-PROV-BOOT-DISK-NOT-STABLE", path,
                           host=host, selector=selector)
    return None


def collision_finding(roles: list[tuple[str, str]], path: str,
                      host: str) -> Finding | None:
    """None when every declared device role names a different device.

    `roles` is (field name, selector) for each of bootDisk, vsanDevice and
    memoryTieringDevice the host actually declares. Any two naming one device
    is the expensive mistake this whole area circles: the install overwrites
    the device it is given, vSAN ESA claims the device it is given, and
    `esxcli memtier enable` consumes the device it is given. None of the three
    asks whether something else is already there.
    """
    seen: dict[str, str] = {}
    for field, selector in roles:
        key = device_key(selector)
        if key in seen:
            return finding_for("VCF-PROV-DEVICE-COLLISION", path, host=host,
                               device=selector, first=seen[key], second=field)
        seen[key] = field
    return None


def tiering_findings(hardware: dict, hw_path: str, host: str) -> list[Finding]:
    """Why this host's declared memory tiering will not happen, or nothing.

    Never fatal: a host with no tiering still installs and still commissions,
    it simply has less memory than the capacity plan assumed. That is a
    finding to read, not a reason to withhold a kickstart -- but it is also
    not something to discover after bring-up, which is why it is here and not
    only in the renderer.
    """
    tier = hardware.get("memoryTieringGb")
    tier = float(tier) if isinstance(tier, (int, float)) else 0.0
    if tier <= 0:
        return []
    device = str(hardware.get("memoryTieringDevice") or "")
    if not device:
        return [finding_for("VCF-PROV-NO-TIERING-DEVICE", hw_path,
                            host=host, tier=plain(tier))]
    # One check, not the boot disk's three: --firstdisk is not something
    # `esxcli memtier enable -d` accepts, and a runtime name fails the stable
    # pattern anyway, so a second branch for it would be unreachable.
    if not STABLE_DISK_RE.match(device):
        return [finding_for("VCF-PROV-TIERING-DEVICE-NOT-STABLE",
                            f"{hw_path}/memoryTieringDevice",
                            host=host, selector=device)]
    ram = hardware.get("ramGb")
    ram = float(ram) if isinstance(ram, (int, float)) else 0.0
    ratio = tier_ratio_pct(tier, ram)
    if not MIN_TIER_RATIO_PCT <= ratio <= MAX_TIER_RATIO_PCT:
        return [finding_for("VCF-PROV-TIERING-RATIO-UNSUPPORTED",
                            f"{hw_path}/memoryTieringGb", host=host,
                            ratio=ratio, tier=plain(tier), ram=plain(ram))]
    return []


def device_findings(inventory: dict) -> list[Finding]:
    """Every device-identifier fact, for every host, in document order.

    This is the entry point the rules layer calls. `render_provisioning()`
    reaches the same checks through the functions above rather than through
    this walker, because it needs each answer at the point it is about to
    emit the artifact that depends on it.
    """
    findings: list[Finding] = []
    for index, host in enumerate(as_sequence(inventory.get("hosts"))):
        host = as_mapping(host)
        name = str(host.get("name", ""))
        path = f"/hosts/{index}"
        hardware = as_mapping(host.get("hardware"))

        boot_disk = str(host.get("bootDisk") or "")
        if not boot_disk:
            findings.append(finding_for("VCF-PROV-NO-BOOT-DISK", path,
                                        host=name))
        else:
            unsafe = boot_disk_finding(boot_disk, f"{path}/bootDisk", name)
            if unsafe is not None:
                findings.append(unsafe)

        declared = [(field, str(get(host, hardware)))
                    for field, get in _DEVICE_ROLES
                    if get(host, hardware)]
        collision = collision_finding(declared, path, name)
        if collision is not None:
            findings.append(collision)

        findings += tiering_findings(hardware, f"{path}/hardware", name)
    return findings
