"""Render a lab inventory into a VCF Installer SddcSpec.

Every field name here was verified against components.schemas.SddcSpec in
vcf-installer-openapi.json 9.1.1.0. Notable traps, all previously got wrong:
hostSpecs[].hostname is the SHORT name; there is no ipAddressPrivate; the
vCenter password field is rootVcenterPassword; vlanId and mtu are integers.

Trust boundary: inventory.py's validate_inventory() is the layer that is
*supposed* to run first and reject a literal (non-"${reference}") secret.
render() does not assume that happened -- it is the last code that touches
credential values before they leave the process, so every value it copies
out of the inventory's `credentials` section is re-checked against the
same ${reference} pattern here, and a non-conforming value raises rather
than being copied into the rendered spec.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .findings import Finding, Result, Severity
from .inventory import REFERENCE_RE
from .rules.coerce import as_mapping as _mapping
from .rules.coerce import as_sequence
from .schema import DEFAULT_VERSION, resolve_version

DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"


class InsecureCredentialError(ValueError):
    """Raised when render() would emit a credential that is not a ${reference}.

    These tools never hold or emit real secrets. The message intentionally
    never includes the offending value.
    """


def _credential(creds: dict, name: str) -> str:
    """Fetch credentials[name], refusing anything that is not a ${reference}."""
    value = creds.get(name, "")
    if not REFERENCE_RE.match(value):
        raise InsecureCredentialError(
            f"credential '{name}' must be a ${{reference}} (e.g. ${{{name}}}); "
            "refusing to render a literal value.")
    return value


def _sddc_manager_spec(appliances: dict, creds: dict) -> dict:
    """SddcManagerSpec, with the extra fields an *existing* deployment needs.

    When the VCF Installer appliance is itself the SDDC Manager -- which it is
    whenever the SDDC Manager OVA was deployed as the Installer -- a spec that
    names that same hostname is asking the Installer to deploy itself. A real
    9.1.1 Installer fails three whole checks on that: DNS Resolution
    (DUPLICATE_FQDN, DUPLICATE_IP_ADDRESS) and Network Configuration
    (IP_NOT_IN_USE), plus an EXISTING_SDDC warning. Setting
    useExistingDeployment cleared all three, measured 2026-09-22.

    The Installer then demands credentials it does not need for a fresh
    deployment: submitting useExistingDeployment with only rootPassword is
    refused outright with QUICK_START_VALIDATION_FAILED, "Empty local user
    password specified in SDDC Manager specification". So localUserPassword
    and sshPassword are emitted alongside it. SddcManagerSpec also declares
    sslThumbprint ("Need to be populated when using existing"), which this
    does not emit: the Installer accepted the spec without one, and inventing
    a field the tool cannot verify is how a spec acquires a value nobody
    checked.
    """
    manager = _mapping(appliances.get("sddcManager"))
    spec = {
        "hostname": manager.get("hostname", ""),
        "rootPassword": _credential(creds, "sddcManagerRoot"),
    }
    if manager.get("useExistingDeployment") is True:
        spec["useExistingDeployment"] = True
        spec["localUserPassword"] = _credential(creds, "sddcManagerLocalUser")
        spec["sshPassword"] = _credential(creds, "sddcManagerSsh")
    return spec


# Only these three are real VCF network types for this shape. TEP traffic is
# configured through nsxtSpec, not as a networkSpec.
NETWORK_TYPES = {"management": "MANAGEMENT", "vmotion": "VMOTION", "vsan": "VSAN"}
NETWORK_ORDER = ("management", "vmotion", "vsan")


def _defaults_text(version: str) -> str:
    """Read the defaults table for `version`, which must be a vendored one.

    Same gate as schema.schema_path(), for the same reason: `version` is
    caller-controlled (MCP `vcf_version`, CLI `--version`) and this was the
    second path it reached. It was the worse of the two for disclosure --
    `render.default()` puts `entry['value']!r` into a finding message and
    `entry['source']` into `source_url`, so any readable `.yaml` shaped
    like a defaults table would have had its contents echoed back to the
    caller verbatim. resolve_version() runs first and raises before any
    path exists to read.

    The cache is keyed on the *resolved* version, so caller text never
    becomes a cache key.
    """
    return _defaults_text_for(resolve_version(version))


@lru_cache(maxsize=4)
def _defaults_text_for(version: str) -> str:
    return (DEFAULTS_DIR / f"{version}.yaml").read_text(encoding="utf-8")


def load_defaults(version: str = DEFAULT_VERSION) -> dict:
    return yaml.safe_load(_defaults_text(version))


def render(inventory: dict, version: str = DEFAULT_VERSION) -> tuple[dict, Result]:
    defaults = load_defaults(version)
    findings: list[Finding] = []

    def default(name: str):
        entry = defaults[name]
        findings.append(Finding(
            code="VCF-RENDER-DEFAULT-APPLIED", severity=Severity.INFO,
            path=f"/{name}", message=f"Applied default {name}={entry['value']!r}.",
            fix="Set it explicitly in the inventory to override.",
            source="table", source_url=entry["source"]))
        return entry["value"]

    def required(section: str, pointer: str):
        value = inventory.get(section)
        if value is None:
            findings.append(Finding(
                code="VCF-RENDER-UNMAPPED", severity=Severity.CRITICAL, path=pointer,
                message=f"Inventory has no '{section}', and {pointer} is required.",
                fix=f"Add a '{section}' section to the inventory.", source="schema"))
        return value or {}

    instance = required("instance", "/sddcId")
    dns = required("dns", "/dnsSpec")
    networks = required("networks", "/networkSpecs")
    appliances = required("appliances", "/vcenterSpec")
    nsx = inventory.get("nsx") or {}
    storage = inventory.get("storage") or {}
    creds = inventory.get("credentials") or {}
    subdomain = dns.get("subdomain", "")

    spec: dict = {
        "sddcId": instance.get("sddcId", ""),
        "vcfInstanceName": instance.get("name", ""),
        "version": instance.get("vcfVersion", version),
        "workflowType": default("workflowType"),
        "ceipEnabled": default("ceipEnabled"),
        "skipEsxThumbprintValidation": default("skipEsxThumbprintValidation"),
        "dnsSpec": {"subdomain": subdomain,
                    "nameservers": as_sequence(dns.get("nameservers"))},
        "ntpServers": as_sequence((inventory.get("ntp") or {}).get("servers")),
        "networkSpecs": [_network_spec(purpose, networks[purpose])
                         for purpose in NETWORK_ORDER if purpose in networks],
        "vcenterSpec": _vcenter_spec(appliances, creds, default),
        "sddcManagerSpec": _sddc_manager_spec(appliances, creds),
        "hostSpecs": [_host_spec(host, creds)
                      for host in as_sequence(inventory.get("hosts"))],
    }

    if nsx:
        spec["nsxtSpec"] = _nsxt_spec(nsx, creds, default)
    if storage.get("type") == "VSAN_ESA":
        esa_config = {"enabled": True}
        # Consumer NVMe is not on the vSAN ESA HCL, and bring-up fails
        # VSAN_ESA_HOST_HCL_COMPATIBLE_ERROR without this. 9.1.1 supports it
        # natively in the spec; the 9.0-era mock VIB and the domainmanager
        # property edits are obsolete. Emitted only when the inventory asks
        # for it -- never defaulted on, because it disables a hardware check.
        if storage.get("skipHclAutoDiskClaim") is True:
            esa_config["skipHclAutoDiskClaim"] = True
        spec["datastoreSpec"] = {"vsanSpec": {
            "datastoreName": storage.get("datastoreName", "vsan-datastore"),
            "esaConfig": esa_config,
            "failuresToTolerate": int(storage.get("failuresToTolerate", 1)),
        }}
    vsp = appliances.get("vsp")
    if vsp:
        spec["vspClusterSpec"] = {
            "platformFqdn": vsp.get("platformFqdn", ""),
            "instanceFqdn": vsp.get("instanceFqdn", ""),
            # fleetFqdn addresses the FLEET-level services -- fleet lifecycle,
            # Salt RaaS, software depot -- exactly as instanceFqdn addresses the
            # instance-level ones. It is absent from SddcVspClusterSpec.required
            # because VCF_EXTEND must omit it, so a spec without it VALIDATES
            # CLEAN and then fails at deploy time: the installer PATCHes
            # {"fleetLcm":{...}} with no fqdn, SDDC Manager stores NULL, and
            # "Deploy Lifecycle Components" dies with
            # PUBLIC_LCM_COMPONENTS_DEPLOY_FLEET_LCM_FETCH_FAILED ~20s in.
            # Measured against a real 9.1.1 bring-up 2026-09-26, not inferred.
            # Required by the inventory schema for that reason: for a primary
            # instance there is no correct spec without it.
            "fleetFqdn": vsp.get("fleetFqdn", ""),
            "ipv4Pool": {"ipRange": {"startIpAddress": vsp.get("poolStart", ""),
                                     "endIpAddress": vsp.get("poolEnd", "")}},
            "internalClusterCidrIpv4": vsp.get("internalCidr", "198.18.0.0/15"),
        }
    # The five sections below are what Broadcom's decision table requires for
    # "deploy a new VCF fleet", and what this renderer previously omitted. The
    # installer materialises an omitted section as a size-only stub rather than
    # rejecting it, so their absence is silent until deployment fails.
    operations = _mapping(appliances.get("operations"))
    if operations:
        spec["vcfOperationsSpec"] = _operations_spec(operations, creds)
    collector = _mapping(appliances.get("operationsCollector"))
    if collector:
        spec["vcfOperationsCollectorSpec"] = _collector_spec(collector, creds)
    idb = _mapping(appliances.get("identityBroker"))
    if idb:
        # Identity broker is mandatory on the PRIMARY instance only; a
        # secondary instance omits vidbSpec.
        spec["vidbSpec"] = {"hostname": idb.get("hostname", "")}
    license_server = _mapping(appliances.get("licenseServer"))
    if license_server:
        spec["licenseServerSpec"] = {
            "hostname": license_server.get("hostname", ""),
            "useExistingDeployment": False,
        }
    return spec, Result(tuple(findings))


def _operations_spec(operations: dict, creds: dict) -> dict:
    """VcfOperationsSpec. Only `nodes` is schema-required; each node requires a
    hostname.

    A single master node is the non-HA shape. Replica and data nodes exist for
    HA and are deliberately not synthesised here: adding nodes this tool cannot
    size against real capacity is how a spec acquires a value nobody checked.
    """
    return {
        "nodes": [{
            "hostname": operations.get("hostname", ""),
            "rootUserPassword": _credential(creds, "operationsRoot"),
            "type": "master",
        }],
        "adminUserPassword": _credential(creds, "operationsAdmin"),
        "applianceSize": operations.get("size", ""),
        "useExistingDeployment": False,
    }


def _collector_spec(collector: dict, creds: dict) -> dict:
    """VcfOperationsCollectorSpec -- the cloud proxy / collector appliance."""
    return {
        "hostname": collector.get("hostname", ""),
        "rootUserPassword": _credential(creds, "operationsCollectorRoot"),
        "applianceSize": collector.get("size", ""),
        "useExistingDeployment": False,
    }


def _network_spec(purpose: str, entry: dict) -> dict:
    spec = {
        "networkType": NETWORK_TYPES[purpose],
        "vlanId": int(entry["vlan"]),
        "subnet": entry["subnet"],
        "gateway": entry["gateway"],
        "mtu": int(entry["mtu"]),
    }
    pool = entry.get("pool")
    if pool:
        spec["includeIpAddressRanges"] = [
            {"startIpAddress": pool["start"], "endIpAddress": pool["end"]}]
    return spec


def _vcenter_spec(appliances: dict, creds: dict, default) -> dict:
    vcenter = appliances.get("vcenter") or {}
    return {
        "vcenterHostname": vcenter.get("hostname", ""),
        "rootVcenterPassword": _credential(creds, "vcenterRoot"),
        "vmSize": vcenter.get("size") or default("vcenterSize"),
        "ssoDomain": vcenter.get("ssoDomain") or default("ssoDomain"),
        "adminUserSsoPassword": _credential(creds, "ssoAdmin"),
    }


def _nsxt_spec(nsx: dict, creds: dict, default) -> dict:
    pool = nsx.get("tepPool") or {}
    spec = {
        "nsxtManagers": [{"hostname": name} for name in as_sequence(nsx.get("managers"))],
        "vipFqdn": nsx.get("vipFqdn", ""),
        "nsxtManagerSize": nsx.get("size") or default("nsxSize"),
        "rootNsxtManagerPassword": _credential(creds, "nsxAdmin"),
        "nsxtAdminPassword": _credential(creds, "nsxAdmin"),
    }
    if "transportVlanId" in nsx:
        spec["transportVlanId"] = int(nsx["transportVlanId"])
    if pool:
        spec["ipAddressPoolSpec"] = {
            "name": pool.get("name", "tep-pool"),
            "subnets": [{
                "cidr": pool.get("cidr", ""),
                "gateway": pool.get("gateway", ""),
                # as_sequence and a dict filter, for the same reason as every
                # other document sequence here -- but note this one already
                # failed *safely*: r["start"] on a string element raised, and
                # api.py's broad except withheld the spec. Coercing keeps that
                # outcome (an empty ipAddressPoolRanges fails the vendored
                # schema's minItems, so the spec is still withheld) while
                # replacing a generic VCF-RENDER-FAILED with a VCF-SCHEMA
                # finding that names the offending field.
                "ipAddressPoolRanges": [{"start": r.get("start", ""),
                                         "end": r.get("end", "")}
                                        for r in as_sequence(pool.get("ranges"))
                                        if isinstance(r, dict)],
            }],
        }
    return spec


def _host_spec(host: dict, creds: dict) -> dict:
    """hostname is the SHORT name: the Installer prefixes it to the subdomain."""
    return {
        "hostname": host.get("name", ""),
        "credentials": {"username": "root", "password": _credential(creds, "esxRoot")},
    }
