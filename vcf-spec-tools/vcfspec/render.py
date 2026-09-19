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
from .schema import DEFAULT_VERSION

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


# Only these three are real VCF network types for this shape. TEP traffic is
# configured through nsxtSpec, not as a networkSpec.
NETWORK_TYPES = {"management": "MANAGEMENT", "vmotion": "VMOTION", "vsan": "VSAN"}
NETWORK_ORDER = ("management", "vmotion", "vsan")


@lru_cache(maxsize=4)
def _defaults_text(version: str) -> str:
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
                    "nameservers": list(dns.get("nameservers", []))},
        "ntpServers": list((inventory.get("ntp") or {}).get("servers", [])),
        "networkSpecs": [_network_spec(purpose, networks[purpose])
                         for purpose in NETWORK_ORDER if purpose in networks],
        "vcenterSpec": _vcenter_spec(appliances, creds, default),
        "sddcManagerSpec": {
            "hostname": (appliances.get("sddcManager") or {}).get("hostname", ""),
            "rootPassword": _credential(creds, "sddcManagerRoot"),
        },
        "hostSpecs": [_host_spec(host, creds)
                      for host in inventory.get("hosts") or []],
    }

    if nsx:
        spec["nsxtSpec"] = _nsxt_spec(nsx, creds, default)
    if storage.get("type") == "VSAN_ESA":
        spec["datastoreSpec"] = {"vsanSpec": {
            "datastoreName": storage.get("datastoreName", "vsan-datastore"),
            "esaConfig": {"enabled": True},
            "failuresToTolerate": int(storage.get("failuresToTolerate", 1)),
        }}
    vsp = appliances.get("vsp")
    if vsp:
        spec["vspClusterSpec"] = {
            "platformFqdn": vsp.get("platformFqdn", ""),
            "instanceFqdn": vsp.get("instanceFqdn", ""),
            "ipv4Pool": {"ipRange": {"startIpAddress": vsp.get("poolStart", ""),
                                     "endIpAddress": vsp.get("poolEnd", "")}},
            "internalClusterCidrIpv4": vsp.get("internalCidr", "198.18.0.0/15"),
        }
    return spec, Result(tuple(findings))


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
        "nsxtManagers": [{"hostname": name} for name in nsx.get("managers", [])],
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
                "ipAddressPoolRanges": [{"start": r["start"], "end": r["end"]}
                                        for r in pool.get("ranges", [])],
            }],
        }
    return spec


def _host_spec(host: dict, creds: dict) -> dict:
    """hostname is the SHORT name: the Installer prefixes it to the subdomain."""
    return {
        "hostname": host.get("name", ""),
        "credentials": {"username": "root", "password": _credential(creds, "esxRoot")},
    }
