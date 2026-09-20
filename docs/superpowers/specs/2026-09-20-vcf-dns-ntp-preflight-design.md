# VCF DNS pre-flight — design (rev 2)

**Date:** 2026-09-20
**Status:** revised after adversarial review; NTP removed to its own work item
**Extends:** `docs/superpowers/specs/2026-09-17-vcf-spec-authoring-mcp-design.md`
**Code:** `vcf-spec-tools/`

> **Placeholder:** this document uses `vcf.example.com` as the lab DNS
> subdomain. The real subdomain is pending and gets substituted throughout
> before planning. It must not be `.local` — see "Name validity" below.

## Why this exists

VCF hard-fails on DNS, and the pre-flight probe layer exists to catch that
before an operator commits to a multi-hour bare-metal build. Run against real
dnsmasq for the first time on 2026-09-20, it did not.

Broadcom's requirements are explicit
([FQDN and IP planning](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/planning-and-preparation/vcf-components-fqdns-and-ip-addresses/first-vcf-instance-fqdns-and-ip-addresses.html)):

- *"Each FQDN must resolve to a unique, currently unassigned IP address."*
- *"Domain suffixes such as .local are not supported."*
- *"Do not use capital letters in the FQDN."*
- Forward **and** reverse resolution is required for each component.

**Correction to rev 1.** Rev 1 claimed `SddcSpec` carries no appliance
addresses and built its design on that. That is false. `SddcVspClusterSpec`
*requires* `ipv4Pool`, `VcfAutomationSpec` has `ipPool`, and `SddcNsxtSpec` has
`ipAddressPoolSpec`; this repo's own renderer emits the first at
`vcfspec/render.py:146`. The accurate statement is narrower and splits the
design in two:

- **Hostname-only components** — ESX hosts, vCenter, SDDC Manager, NSX managers
  (`SddcHostSpec`, `SddcManagerSpec`, `NsxtManagerSpec`, `SddcNsxtSpec.vipFqdn`)
  carry a name and no address, so DNS is the sole binding and must be verified
  by resolution.
- **Pool-backed components** — VSP and NSX TEP declare their address ranges in
  the spec itself. For these the stronger check is not round-trip resolution
  but **membership**: does the name resolve into the range the spec declares,
  and is that range free of collisions.

## What already works

Do not rebuild: `VCF-NAME-NOT-LOWERCASE` (error), `VCF-NAME-WRONG-DOMAIN`
(warning), `VCF-PROBE-FORWARD-MISMATCH`, `VCF-PROBE-NO-REVERSE-DNS`,
`VCF-PROBE-REVERSE-MISMATCH` (all error), and the IP/domain allowlist gates on
host probes, proven by packet capture.

## Containment — the binding constraint

The inventory is untrusted input that causes network traffic; through the MCP
server its author may be whoever wrote a ticket. A DNS query exfiltrates by
being *asked*. This has already gone wrong once here: the host FQDN was built
from `name + subdomain` and resolved past a gate that only checked the IP.

Rev 1's containment model had two leaks, both found in review:

1. **The gate compares strings; dnspython parses names.** `permits_name`
   (`probes.py:76-95`) does `.lower()` and `.endswith()`. dnspython honours DNS
   escapes, so `a.attacker.example\.lab.example` passes a suffix test while the
   wire name's parent zone is `example`. **Gate on the parsed object** —
   `dns.name.from_text(value, origin=None)` then `is_subdomain(allowed)` — and
   query that object, never the original string.
2. **Only declared targets were gated; derived ones were not.** An A record
   answering `127.0.0.1`, a CNAME to an off-allowlist name, or a PTR naming an
   arbitrary host all fed onward into further queries and `connect()`.
   (Loopback in particular is a real VCF failure —
   [KB 405013](https://knowledge.broadcom.com/external/article/405013).)
   **Every address and name learned from an answer re-enters the same gate
   before any further query or connection.** An off-allowlist answer is a
   finding, never a follow-up.

Further requirements:

- `dns.resolver.Resolver(configure=False)` with nameservers set explicitly.
  The default reads the runner's `/etc/resolv.conf`, which reintroduces the
  very "wrong resolver" defect this work exists to fix.
- **Per-class allowlists.** A host-IP allowlist must not implicitly authorise
  traffic to nameservers. Each class opts in separately; unconfigured means
  that class is not probed.
- `dns.nameservers[]` entries are constrained to **IP literals** in the
  inventory schema. Today they are free strings, which the IP gate silently
  blocks forever.
- **A total probe deadline.** ~26 sequential bounded resolves against a dead
  nameserver is ~52 s and 26 abandoned daemon threads.
- Containment is proven by **packet capture in the nested lab**, asserting on
  which names appear on the wire. Fake resolvers return what the test author
  expected; they are what hid the reverse-DNS bug for the entire build.

## Severity: absence versus contradiction

Rev 1 split on DNS-versus-TCP and was wrong; that split only relocates the
inconsistency, because a spec validated before the hardware exists has no DNS
records either. The real axis:

- **Contradictions block at any phase** — forward mismatch, reverse mismatch,
  ambiguous PTR, an address inside a declared pool, a duplicate (FQDN, IP).
  These are wrong now and will still be wrong later.
- **Absences take severity from a declared phase** — no A record, no PTR, no
  TCP answer. `--phase pre-build` (default) rates them `warning`;
  `--phase pre-deploy` rates them `error`.

Note the inversion for appliances: Broadcom requires appliance FQDNs to resolve
to *currently unassigned* addresses, so **something answering on an appliance
address is the defect**, not the reassurance.

## Name validity (static rules, no network)

- `VCF-NAME-UNSUPPORTED-SUFFIX` (error) — `.local` and other non-RFC suffixes.
  This invalidates the shipped example and the nested lab, both currently on
  `lab.local`; both change with this work.
- Existing lowercase and domain-suffix rules stay as they are.

## What gets probed

Only names the **inventory can express**: `appliances.vcenter.hostname`,
`appliances.sddcManager.hostname`, `nsx.managers[]`, `nsx.vipFqdn`,
`appliances.vsp.platformFqdn`, `appliances.vsp.instanceFqdn`, and
`hosts[].name`.

Explicitly **out of scope until the inventory models them**: VCF Operations
(`loadBalancerFqdn`, node and collector hostnames), VCF Automation, VIDB and the
licence server — all present in `SddcSpec` but absent from our inventory schema.

### Comparison rules

- Compare against the **full A RRset**, not one address; accept any PTR in the
  set. A single-name comparison false-positives on legitimate multi-A and
  load-balanced names.
- Report a **CNAME as a distinct fact** — VCF wants A records — rather than
  failing the round trip because the answer carries the canonical name.
- **Exempt the NSX VIP and pool-backed FQDNs from PTR-required**; a VIP
  legitimately may have no PTR, or one naming a member.
- **Global uniqueness**: assert every (FQDN, IP) pair is unique across the
  inventory, and that no resolved appliance address falls inside
  `appliances.vsp.poolStart..poolEnd`, `nsx.tepPool`, or `hosts[].mgmtIp`.
- **Wildcard control**: probe one random non-existent name per zone. A wildcard
  A record makes every forward lookup succeed, which would render a
  no-forward-DNS finding structurally unable to fire.
- Distinguish **NXDOMAIN from SERVFAIL from timeout**; they mean different
  things and must not collapse into one code.
- **Compare the declared nameservers to each other** — disagreement is
  split-horizon and is a finding.
- Report a **missing reverse zone once**, not once per name.

## Findings

Reuse the existing three DNS codes with `/appliances/...` paths rather than
inventing a grab-bag appliance code. New codes:

| Code | Severity | Meaning |
|---|---|---|
| `VCF-NAME-UNSUPPORTED-SUFFIX` | error | `.local` or other unsupported suffix |
| `VCF-PROBE-NO-FORWARD-DNS` | phase | name does not resolve |
| `VCF-PROBE-TCP-UNREACHABLE` | phase | nothing answering |
| `VCF-PROBE-ADDRESS-IN-USE` | error | appliance address already answers |
| `VCF-PROBE-AMBIGUOUS-PTR` | error | more than one PTR for an address |
| `VCF-PROBE-ADDRESS-NOT-UNIQUE` | error | two names share an address |
| `VCF-PROBE-ADDRESS-IN-POOL` | error | resolved address inside a declared pool |
| `VCF-PROBE-CNAME` | warning | name is a CNAME; VCF expects an A record |
| `VCF-PROBE-WILDCARD-DNS` | error | zone answers for a name that should not exist |
| `VCF-PROBE-NAMESERVERS-DISAGREE` | error | declared nameservers return different answers |
| `VCF-PROBE-NAMESERVER-UNREACHABLE` | error | declared nameserver does not answer |
| `VCF-PROBE-DNS-UNAVAILABLE` | error | dnspython missing while DNS probing requested |

`VCF-PROBE-NAMESERVER-UNREACHABLE` **suppresses** downstream per-name findings;
a dead nameserver must not emit a dozen identical errors.

`VCF-PROBE-UNKNOWN` is documented in `README.md:264,287` and asserted in
`tests/test_probes.py`. It is **retired**, replaced by the two phase-rated codes
above; the README and tests change with it, and the retirement is called out in
the release notes.

`VCF-PROBE-DNS-UNAVAILABLE` is `error`, not `info`, matching this package's own
precedent that "probes requested, nothing checked" is never a clean pass
(`VCF-PROBE-NOTHING-PERMITTED`).

## Dependency

`dnspython>=2.6,<3`, lazily imported so the core library, renderer and MCP
server stay importable and testable without it.

## Testing

Unit tests with injected fakes are the bulk but are explicitly **not
sufficient** — they are what hid the reverse-DNS bug. Every containment
property is additionally proven in the nested lab with `tcpdump` on `vmbrlab`.

Negative cases, each by breaking real dnsmasq: missing A; missing PTR;
mismatched PTR; two PTRs on one address; a wildcard zone; a CNAME; a dead
nameserver; two nameservers disagreeing; an appliance address that answers on
443; an address inside the VSP pool.

## Out of scope

- NTP. Removed from this work: an offset check from a container whose clock is
  the host's is close to meaningless, and correct NTP validation needs reply
  parsing (LI, stratum, KoD, root dispersion) and server-to-server comparison.
  Its own work item.
- DNSSEC, IPv6/`ip6.arpa` (state the position; the inventory accepts v6
  literals but this work does not probe them), probing from the ESXi hosts, and
  any change to rendering or the vendored schema.
