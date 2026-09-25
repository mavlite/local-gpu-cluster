"""Stage 1: the artifacts that get ESX onto bare metal.

The same inventory that renders an SddcSpec renders these, so the operator
describes the lab once and the two outputs cannot drift.

This module emits text. It never writes to a boot server, never reads a file,
never opens a socket, and never powers anything on -- publishing the files and
turning the hosts on are the operator's, and a host with no BMC needs a
switched PDU for the latter anyway. That purity is why the ESX boot.cfg
arrives as an *argument* rather than being read from an ISO here.

Three things the spike (2026-09-17) settled that shape everything here:

* VCF does not image bare metal. ESX must already be installed and
  basic-configured before a host can be commissioned, so this fills a real
  gap rather than duplicating the Installer.
* The ESX installer does not validate TLS for `ks=https://`. The provisioning
  VLAN is the trust boundary, not the transport -- which is why the kickstart
  carries a SHA-512 crypt *hash* rather than a password, and why the manifest
  says so out loud.
* There is no BMC. Every choice below is biased towards "an install that
  completes unattended", because the alternative is a drive to the rack.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .findings import Finding, Result
from .firstboot import (memory_tiering_block, render_firstboot,
                        workarounds_block)
from .render import InsecureCredentialError
from .inventory import REFERENCE_RE
from .rules import finding_for
from .rules.coerce import as_sequence as _sequence
# One definition of what a safe device identifier is, shared with the rules
# layer. These used to live here, where `vcfspec validate` could not reach
# them -- so an inventory naming an unstable boot disk validated clean while
# the renderer would have refused it. Importing rather than duplicating is
# what stops the validator and the renderer disagreeing about the same fact.
from .rules.devices import DISK_PATH_PREFIX as _DISK_PATH_PREFIX
from .rules.devices import RUNTIME_DISK_RE as _RUNTIME_DISK_RE
from .rules.devices import STABLE_DISK_RE as _STABLE_DISK_RE
from .rules.devices import boot_disk_finding as _boot_disk_finding
from .rules.devices import collision_finding as _collision_finding
from .rules.devices import device_key as _device_key
from .rules.devices import device_path as _device_path
from .rules.devices import plain as _plain
from .rules.devices import tier_ratio_pct as _tier_ratio_pct
from .rules.devices import tiering_findings as _tiering_findings

DEFAULT_ESX_VERSION = "9.1.1.0"


# A SHA-512 crypt hash, which is what `rootpw --iscrypted` wants. Deliberately
# strict about the `$6$` prefix: `$1$` (MD5) and `$5$` (SHA-256) are accepted by
# crypt(3) and are not what we are asking for, and a bare string that merely
# starts with `$` is almost always someone's password with a `$` in it.
_SHA512_CRYPT_RE = re.compile(r"^\$6\$[^$:\s]+\$[./A-Za-z0-9]{86}$")


# An OpenSSH public key, as it appears in a .pub file: type, base64 blob, and
# an optional comment. Deliberately strict -- this value is written verbatim
# into a file published over unauthenticated HTTP, and the single quotes it is
# echoed inside are the only thing between it and the shell, so no character
# that could close them is allowed to reach the template.
_SSH_PUBLIC_KEY_RE = re.compile(
    r"^(?:ssh-ed25519|ssh-rsa|ssh-dss"
    r"|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r"\s+[A-Za-z0-9+/]+={0,3}"
    r"(?:\s+[^\s'\\]+)?$")
# A private key pasted where the public one belongs. Not merely a wrong value:
# it is a secret headed for a world-readable file, so it gets the same refusal
# a plaintext password gets rather than a finding.
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


class _NotAPublicKey(ValueError):
    """provisioning.sshPublicKey is present but is not a public key.

    Its own class, not a bare ValueError, because InsecureCredentialError is
    *also* a ValueError: catching the broad one here would swallow the private
    key refusal, which is the one thing that must never be swallowed.
    """

# One place for every kickstart directive, so correcting the syntax is a
# single edit rather than a hunt. Real hardware is the only authority here.
# The %firstboot body lives in firstboot.py; this is everything above it.
#
# On `install`: --overwritevmfs, and deliberately *not* --novmfsondisk. The two
# read as a contradictory pair, and with no BMC the only failure mode that
# costs a physical visit is an install that stops and waits. Without
# --overwritevmfs a reinstall onto a disk that already carries a VMFS aborts;
# --novmfsondisk buys nothing but the absence of a local datastore, which is
# cosmetic and removable over SSH once the host is up.
#
# --overwritevsan is different: it is conditional, not unconditional like
# --overwritevmfs, because it is wrong in both directions and this tool has no
# way to know which one applies. Verified against a real ESX 9.1.1 installer,
# not inferred from docs: on a fresh disk that vSAN has never claimed, emitting
# --overwritevsan aborts the install outright --
#   install --overwritevsan specified but disk <id> is not claimed by vSAN.
# -- and the converse is just as real: a rebuild of a host whose boot disk a
# *previous* install gave to vSAN fails without it, because the existing vSAN
# partition blocks the install. A greenfield host -- the common case, and the
# one the example inventory describes -- must not carry the flag; a rebuild of
# a former vSAN member must. Only the operator knows which state the disk is
# actually in, so hardware.bootDiskClaimedByVsan (default false) decides it;
# see its schema description for both failure messages.
_KICKSTART_HEAD = """\
# ESX kickstart for {fqdn}
# Generated by vcfspec from the lab inventory. Do not hand-edit: regenerate.
#
# The root credential below is a ${{reference}} to a SHA-512 crypt hash, not a
# password. Substitute it when you publish this file. The file sits on an
# unauthenticated HTTP server that every host on the provisioning VLAN can
# read, so a hash is the only acceptable thing to put here.
vmaccepteula
rootpw --iscrypted {root_hash}
install --disk={boot_disk} --overwritevmfs{overwritevsan_opt}
network --bootproto=static --device={mac} --ip={ip} --netmask={netmask} \
--gateway={gateway} --nameserver={nameservers} --hostname={fqdn} \
--addvmportgroup=0{vlan_opt}
reboot

"""



@dataclass(frozen=True, slots=True)
class ProvisioningArtifacts:
    """What the operator publishes, plus what stopped us generating it.

    `kickstarts` is keyed by filename; `boot_configs` is keyed by *path*
    relative to the boot server root, because the per-MAC mechanism is a
    directory name and not a filename.

    `findings` is not an afterthought: a host missing `provisioningMac` or
    with an unsafe `bootDisk` yields no files for that host, and saying so is
    more useful than emitting a kickstart that would install to the wrong
    device.
    """

    kickstarts: dict[str, str]
    boot_configs: dict[str, str]
    manifest: str
    findings: tuple[Finding, ...]

    @property
    def result(self) -> Result:
        return Result(self.findings)


def _netmask(subnet: str) -> str | None:
    try:
        return str(ipaddress.ip_network(subnet, strict=False).netmask)
    except (ValueError, TypeError):
        return None


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _root_hash(creds: dict) -> str | None:
    """The `rootpw --iscrypted` value: a ${reference}, or a `$6$` hash.

    The collision worth naming: crypt hashes and this tool's reference syntax
    both start with `$`. REFERENCE_RE wants `${name}`, which `$6$salt$hash`
    fails, so a hash falls straight through to the plaintext refusal unless it
    is recognised here first. It is not a plaintext secret -- publishing a
    kickstart that carries one is the whole point of moving to --iscrypted --
    so a `$6$` literal is allowed through where a password never is.

    Returns None when the credential is absent (the caller raises a finding);
    raises InsecureCredentialError when it is present and is a plaintext
    literal, which is the same refusal render() makes.
    """
    value = creds.get("esxRootHash")
    if value in (None, ""):
        return None
    text = str(value)
    if REFERENCE_RE.match(text) or _SHA512_CRYPT_RE.match(text):
        return text
    raise InsecureCredentialError(
        "credential 'esxRootHash' must be a ${reference} (e.g. "
        "${esx_root_hash}) or a SHA-512 crypt hash ($6$...); refusing to "
        "render a literal password into a kickstart that is published over "
        "unauthenticated HTTP.")


def _ssh_public_key(prov: dict) -> str:
    """provisioning.sshPublicKey, or "" when absent.

    Raises InsecureCredentialError for a private key, which is the same
    refusal _root_hash() makes and for the same reason: this file is published
    over unauthenticated HTTP. A merely malformed value is not a secret, so
    that is a finding instead -- raised by the caller, which has the path.

    Raises _NotAPublicKey for "present but not a public key" so the caller can
    attach the finding without this function needing to know JSON pointers.
    """
    value = prov.get("sshPublicKey")
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if _PRIVATE_KEY_RE.search(text):
        raise InsecureCredentialError(
            "provisioning.sshPublicKey holds a PRIVATE key; refusing to "
            "render it into a kickstart that is published over "
            "unauthenticated HTTP. Give the .pub half instead.")
    if not _SSH_PUBLIC_KEY_RE.match(text):
        raise _NotAPublicKey(text)
    return text




def _tiering(hardware: dict, hw_path: str, host: str) -> tuple[str, list[Finding]]:
    """The memory-tiering %firstboot block, or "" plus the reason it is absent.

    The gap that motivated the port: the inventory has declared
    hardware.memoryTieringGb since the capacity rules were written, and
    nothing ever turned tiering on. A host declares the size here and the
    device it comes from, and both have to be there.

    Tiering used to be load-bearing for N-1 and no longer is. Against the
    219 GB stack this module was written for, N-1 offered 180 GB and did
    not fit; the 2026-09-24 design in rules/tables.py is 139.25 GB, which
    N-1 clears on DRAM alone. What tiering buys now is headroom -- and it
    is what makes VCF Automation's +96 GB affordable if it is ever added,
    so this stays on.

    Never fatal to the host's artifacts: a host with no tiering still installs
    and still commissions, it simply has less memory than the plan assumed.
    That is a finding to read, not a reason to withhold a kickstart. A device
    *collision* is different, and is handled by the caller.

    Why this delegates rather than deciding for itself: the same questions are
    asked by `vcfspec validate` through rules/devices.py, and two copies of
    "is this device safe" is precisely how a validator comes to bless an
    inventory the renderer would refuse. `tiering_findings` owns the answers;
    this function owns only what to emit once they come back empty.
    """
    reasons = _tiering_findings(hardware, hw_path, host)
    if reasons:
        return "", reasons

    tier = hardware.get("memoryTieringGb")
    tier = float(tier) if isinstance(tier, (int, float)) else 0.0
    if tier <= 0:                       # declared nothing: nothing to emit
        return "", []
    device = str(hardware.get("memoryTieringDevice") or "")
    ram = hardware.get("ramGb")
    ram = float(ram) if isinstance(ram, (int, float)) else 0.0
    ratio = _tier_ratio_pct(tier, ram)
    return memory_tiering_block(_device_path(device), ratio, tier, ram), []



def _rewrite_boot_cfg(iso_boot_cfg: str, prefix: str, kernelopt: str) -> str:
    """Point the ISO's own boot.cfg at a published payload directory.

    Broadcom's procedure for network-booting the installer is: copy the ISO
    contents to a directory, set `prefix=` to it, and strip the leading `/`
    from the `kernel=` and `modules=` lines so they resolve relative to that
    prefix. Everything else -- `modules=` itself, `build=`, `updated=` --
    comes from the ISO and cannot be synthesised: the module list is
    build-specific and mboot.efi will not boot without it.

    So this rewrites exactly two lines and relativises two more. It also adds
    no comments: the boot.cfg grammar mboot.efi parses is key=value, comment
    support is undocumented, and this file is the one thing that must load on
    a host we cannot watch.
    """
    out: list[str] = []
    seen_prefix = seen_kernelopt = False
    for line in iso_boot_cfg.splitlines():
        key = line.split("=", 1)[0].strip().lower() if "=" in line else ""
        if key == "prefix":
            out.append(f"prefix={prefix}")
            seen_prefix = True
        elif key == "kernelopt":
            out.append(f"kernelopt={kernelopt}")
            seen_kernelopt = True
        elif key in ("kernel", "modules"):
            _, _, value = line.partition("=")
            out.append(f"{key}={_relativise(value)}")
        else:
            out.append(line)
    if not seen_prefix:
        out.append(f"prefix={prefix}")
    if not seen_kernelopt:
        out.append(f"kernelopt={kernelopt}")
    return "\n".join(out).rstrip("\n") + "\n"


def _relativise(value: str) -> str:
    """Strip the leading `/` from each module path so `prefix=` applies."""
    return " ".join(token[1:] if token.startswith("/") else token
                    for token in value.split())


def render_provisioning(inventory: dict,
                        esx_boot_cfg: str | None = None) -> ProvisioningArtifacts:
    """Render one kickstart per host and one boot config per MAC.

    `esx_boot_cfg` is the *content* of the boot.cfg shipped in the ESX ISO,
    read by the caller. Without it there are no boot configs, because a
    synthesised boot.cfg has no module list and cannot boot -- but the
    kickstarts still render, since they are independently useful.
    """
    findings: list[Finding] = []
    kickstarts: dict[str, str] = {}
    boot_configs: dict[str, str] = {}

    prov = _mapping(inventory.get("provisioning"))
    boot_url = str(prov.get("bootServerUrl", "")).rstrip("/")
    esx_version = str(prov.get("esxVersion") or DEFAULT_ESX_VERSION)
    if not boot_url:
        findings.append(finding_for("VCF-PROV-NO-BOOT-SERVER", "/provisioning"))
        return ProvisioningArtifacts({}, {}, "", tuple(findings))
    # The installer payload is the unpacked ISO, which is not the directory
    # the kickstarts live in: `prefix=` must reach b.b00 and the modules.
    payload_url = str(prov.get("installerPayloadUrl")
                      or f"{boot_url}/esx-{esx_version}").rstrip("/")

    mgmt = _mapping(_mapping(inventory.get("networks")).get("management"))
    netmask = _netmask(str(mgmt.get("subnet", "")))
    gateway = mgmt.get("gateway")
    vlan = mgmt.get("vlan")
    if netmask is None or not gateway:
        findings.append(finding_for("VCF-PROV-NO-MGMT-NETWORK",
                                    "/networks/management"))
        return ProvisioningArtifacts({}, {}, "", tuple(findings))

    # Same refusal render() makes, and it matters more here: this file is
    # published to an HTTP server the whole provisioning VLAN can read.
    root_hash = _root_hash(_mapping(inventory.get("credentials")))
    if root_hash is None:
        findings.append(finding_for("VCF-PROV-NO-ROOT-HASH",
                                    "/credentials/esxRootHash"))
        return ProvisioningArtifacts({}, {}, "", tuple(findings))

    dns = _mapping(inventory.get("dns"))
    subdomain = str(dns.get("subdomain", "")).strip(".")
    nameserver_list = [str(x) for x in _sequence(dns.get("nameservers"))]
    nameservers = ",".join(nameserver_list)
    # The boot-time option is documented as a single address, and a comma list
    # is not. One resolver is all the installer needs to fetch the kickstart;
    # the kickstart line below carries the full list for the installed host.
    boot_nameserver = nameserver_list[0] if nameserver_list else ""
    ntp = [str(x) for x in _sequence(_mapping(inventory.get("ntp")).get("servers"))]
    ntp_opts = " ".join(f"--server={x}" for x in ntp)

    host_workarounds = [str(x) for x in _sequence(prov.get("hostWorkarounds"))]

    # vmk0 and vSwitch0 take the MANAGEMENT MTU, never nsx.fabricMtu -- see
    # the block comment in firstboot.py. A missing or malformed value leaves
    # the ESX default, which is also 1500.
    mtu = mgmt.get("mtu")
    mtu = int(mtu) if isinstance(mtu, int) else 1500

    # A public key is not a credential, so it may be a literal here. A private
    # key is, and _ssh_public_key() raises on one; anything else that is not a
    # public key is a finding and no key is injected.
    try:
        ssh_public_key = _ssh_public_key(prov)
    except _NotAPublicKey:
        ssh_public_key = ""
        findings.append(finding_for("VCF-PROV-SSH-KEY-NOT-PUBLIC",
                                    "/provisioning/sshPublicKey"))

    if esx_boot_cfg is None:
        findings.append(finding_for("VCF-PROV-NO-ESX-BOOT-CFG", "/provisioning"))

    # A VLAN of 0 or 1 is untagged; emitting `--vlanid=1` makes the installer
    # tag a network the switch expects untagged, which strands the host with
    # no console to recover it.
    tagged = isinstance(vlan, int) and vlan > 1
    vlan_opt = f" --vlanid={vlan}" if tagged else ""
    vlan_boot = f" vlanid={vlan}" if tagged else ""

    for index, host in enumerate(_sequence(inventory.get("hosts"))):
        host = _mapping(host)
        name, path = host.get("name", ""), f"/hosts/{index}"
        mac = str(host.get("provisioningMac", "")).lower()
        boot_disk = str(host.get("bootDisk") or "")
        if not mac:
            findings.append(finding_for("VCF-PROV-NO-MAC", path, host=name))
            continue
        if not boot_disk:
            findings.append(finding_for("VCF-PROV-NO-BOOT-DISK", path, host=name))
            continue
        unsafe = _boot_disk_finding(boot_disk, f"{path}/bootDisk", str(name))
        if unsafe is not None:
            findings.append(unsafe)
            continue

        hardware = _mapping(host.get("hardware"))
        # Every role the inventory declares for a physical device on this
        # host. Two of them on one NVMe is the mistake that costs a drive to
        # the rack, so it withholds the host's artifacts rather than emitting
        # a kickstart that would act on it.
        roles = [("bootDisk", boot_disk)]
        for field in ("vsanDevice", "memoryTieringDevice"):
            if hardware.get(field):
                roles.append((field, str(hardware[field])))
        collision = _collision_finding(roles, path, str(name))
        if collision is not None:
            findings.append(collision)
            continue

        fqdn = f"{name}.{subdomain}" if subdomain else str(name)
        ks_name = f"ks-{name}.cfg"
        ks_url = f"{boot_url}/{ks_name}"
        tiering, tiering_findings = _tiering(
            hardware, f"{path}/hardware", str(name))
        findings += tiering_findings
        # See hardware.bootDiskClaimedByVsan in the schema, and the block
        # comment above _KICKSTART_HEAD, for why this is conditional rather
        # than always-on: emitting it for a disk vSAN never claimed fails the
        # install exactly as hard as omitting it for a disk vSAN did claim.
        overwritevsan_opt = (
            " --overwritevsan" if bool(hardware.get("bootDiskClaimedByVsan"))
            else "")
        kickstarts[ks_name] = (
            _KICKSTART_HEAD.format(
                fqdn=fqdn, root_hash=root_hash, boot_disk=boot_disk, mac=mac,
                ip=host.get("mgmtIp", ""), netmask=netmask, gateway=gateway,
                nameservers=nameservers, vlan_opt=vlan_opt,
                overwritevsan_opt=overwritevsan_opt,
            )
            + render_firstboot(
                fqdn=fqdn, ntp_opts=ntp_opts, mtu=mtu,
                # "datastore1" on all three hosts otherwise, disambiguated by
                # vCenter in registration order. Named from the host, never
                # from storage.datastoreName -- that is the vSAN datastore.
                local_datastore=f"{name}-local",
                ssh_public_key=ssh_public_key,
                tiering=tiering,
                workarounds=workarounds_block(
                    host_workarounds, hardware.get("cpuModel")),
            )
        )
        if esx_boot_cfg is None:
            continue
        # mboot.efi looks for `01-<mac>/boot.cfg` beside itself, dash-separated
        # and lowercase. The `01-` is the ARP hardware type (Ethernet) and is
        # mandatory: without it mboot.efi never requests the path, every host
        # silently falls back to the default boot.cfg, and three hosts install
        # identically with one host's addressing.
        cfg_path = f"01-{mac.replace(':', '-')}/boot.cfg"
        kernelopt = (
            f"ks={ks_url} bootproto=static netdevice={mac} "
            f"ip={host.get('mgmtIp', '')} netmask={netmask} gateway={gateway} "
            f"nameserver={boot_nameserver}{vlan_boot}")
        boot_configs[cfg_path] = _rewrite_boot_cfg(
            esx_boot_cfg, payload_url, kernelopt)

    return ProvisioningArtifacts(
        kickstarts, boot_configs,
        _manifest(boot_url, payload_url, esx_version, kickstarts, boot_configs),
        tuple(findings))


def _manifest(boot_url: str, payload_url: str, esx_version: str,
              kickstarts: dict[str, str], boot_configs: dict[str, str]) -> str:
    lines = [
        f"# ESX provisioning artifacts -- ESX {esx_version}",
        f"# Publish all of these under {boot_url}/",
        "#",
        "# BEFORE PUBLISHING: substitute the ${reference} in each kickstart's",
        "# rootpw line with a SHA-512 crypt hash (openssl passwd -6). These",
        "# files are served over unauthenticated HTTP and the ESX installer",
        "# does not validate TLS, so anything on the provisioning VLAN can",
        "# read whatever you put here. A hash, never a password.",
        "#",
        "# esxRoot and esxRootHash MUST be the same password. Nothing here can",
        "# check that: the tool holds neither value, only the references. If",
        "# they diverge the host installs with one password and VCF commissions",
        "# with another -- a clean install followed by a credential failure,",
        "# which is the expensive shape of this mistake. Verify before you",
        "# publish, using the salt from your own hash:",
        "#   openssl passwd -6 -salt \"$(printf %s \"$HASH\" | cut -d$ -f3)\" \"$PLAINTEXT\"",
        "# and confirm it reproduces $HASH exactly.",
        "#",
        f"# Unpack the ESX {esx_version} ISO to {payload_url}/ and place",
        f"# the UEFI loader at {boot_url}/. Each 01-<mac>/boot.cfg below is a",
        "# sibling of that loader, not of the kickstarts.",
        "#",
        "# The loader ships as EFI/BOOT/BOOTX64.EFI -- there is no file named",
        "# mboot.efi on the ISO, though the docs call it that. BOOTX64.EFI is",
        "# the multiboot loader they mean. MBOOT.C32 is the BIOS/syslinux",
        "# variant and is NOT it.",
        f"# Publish it as {boot_url}/bootx64.efi -- the HTTP script's `chain`",
        "# line below must name that same path.",
        "#",
        "# THE BOOT CHAIN IS PXE -> iPXE -> HTTP. Not UEFI HTTP Boot: this",
        "# firmware class does not have it. On the Minisforum BD795i SE the",
        "# HTTP Boot setup items are compiled into the image but their IFR is",
        "# `suppressif <uint64 constant 1>`, so they are unconditionally hidden",
        "# and only IPv4/IPv6 PXE remain. Look at Advanced > Network Stack",
        "# Configuration before assuming your board differs.",
        "#",
        "# TFTP then creates a second problem, because mboot resolves prefix=",
        "# using the transport IT was loaded over -- an http:// prefix is only",
        "# legal if the loader itself arrived over HTTP. Chainloading iPXE is",
        "# what reconciles the two: a small file over TFTP, everything else",
        "# over HTTP.",
        "#",
        "#   DHCP:  next-server    = a TFTP server on the client's OWN subnet",
        "#          boot-file-name = snponly.efi",
        "#   TFTP:  serve iPXE's snponly.efi. Debian's `ipxe` package ships it",
        "#          prebuilt at ~278 KiB (boot.ipxe.org no longer serves",
        "#          binaries). snponly binds the firmware's own SNP rather",
        "#          than needing a native iPXE driver for your NIC.",
        "#   iPXE:  build with EMBED= a script that only chains to a script on",
        "#          THIS server, so changing boot logic never means rebuilding",
        "#          and reflashing the binary.",
        "#",
        "# That HTTP script must PROBE for the per-MAC boot.cfg before chaining:",
        "#     imgfetch --name probe <cfg> || goto nopending",
        "#     imgfree probe",
        "#     chain <loader> -c <cfg> || goto nopending",
        "# `chain` reports success the moment the binary loads, so a missing -c",
        "# config is NOT caught by `||`. mboot takes control, iPXE is already",
        "# gone, and the host dies with 'Fatal error: 15 (Not found)' with",
        "# nothing left able to hand back to the firmware. The probe is the",
        "# only reason the fall-through below works.",
        "#",
        "# VLANs: PXE firmware sends UNTAGGED frames. If management is a tagged",
        "# VLAN -- it usually is -- the firmware's DHCP lands in whatever the",
        "# switch port's PVID is and never reaches your server, while the",
        "# installed ESX works fine on the same cable because ESX tags. Give the",
        "# host ports a dedicated untagged PROVISIONING VLAN as their PVID and",
        "# leave management tagged on those same ports. boot.cfg's kernelopt",
        "# vlanid=/ip= then moves the INSTALLER onto the tagged management VLAN",
        "# in time to fetch the kickstart, so management never goes native.",
        "#",
        "# BIOS pass, once per chassis, before the install:",
        "#   - UEFI mode (not legacy/CSM)",
        "#   - Network stack + IPv4 PXE enabled (HTTP Boot will not be there)",
        "#   - Secure Boot OFF  <-- load-bearing: %firstboot is silently",
        "#     skipped when Secure Boot is on. SSH, NTP and the certificate",
        "#     regeneration below simply do not run, kickstart.log records the",
        "#     skip, and the host looks installed but fails commissioning.",
        "#   - NIC ahead of disk in boot order -- and LEAVE it there, see below",
        "#   - AC recovery = always on",
        "#",
        "# AFTER each host installs, while the installer is still running:",
        f"#   - Remove {boot_url}/01-<mac>/ for that host. That directory IS the",
        "#     install switch: present means install, absent means the HTTP",
        "#     stage exits and the firmware falls through to the local disk.",
        "#     The kickstart ends in `reboot` and the NIC is still first, so",
        "#     leaving it in place reinstalls the host forever and %firstboot",
        "#     never gets to run. Remove it once the kickstart has been fetched",
        "#     -- the running install does not need it any more.",
        "#   - Do NOT reorder the boot devices instead. Leaving the NIC first is",
        "#     what keeps a later rebuild a mkdir on this server rather than a",
        "#     trip to a chassis that has no BMC.",
        "#   - Secure Boot stays OFF unless your platform's TPM is FIFO/TIS.",
        "#     The Minisforum fTPM is CRB-only, which ESX does not support, so",
        "#     turning it back on buys no attestation and re-arms the",
        "#     %firstboot skip for the next rebuild.",
        "",
    ]
    for name in sorted(boot_configs):
        lines.append(f"{name}")
    for name in sorted(kickstarts):
        lines.append(f"{name}")
    return "\n".join(lines) + "\n"
