# VCF DNS pre-flight — design (rev 3)

**Date:** 2026-09-20
**Status:** rewritten after four parallel adversarial reviews
**Extends:** `docs/superpowers/specs/2026-09-17-vcf-spec-authoring-mcp-design.md`
**Code:** `vcf-spec-tools/`
**Lab:** `[[vcf_nested_lab]]` — real dnsmasq, real listeners, packet capture

Rev 1 and rev 2 are superseded. Both were reviewed adversarially and both had
errors that changed the design, recorded below so they are not reintroduced.

## Why this exists

VCF hard-fails on DNS. Broadcom's
[FQDN and IP planning page](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/planning-and-preparation/vcf-components-fqdns-and-ip-addresses/first-vcf-instance-fqdns-and-ip-addresses.html)
is explicit: each FQDN must resolve to *"a unique, currently unassigned IP
address"*, `.local` suffixes are unsupported, capital letters are not allowed,
and forward **and** reverse resolution is required for each component.

Run against real dnsmasq on 2026-09-20 the probe layer missed a PTR that named
a different host entirely, returning `valid: true`. That bug is fixed; this work
closes the class.

## Three components, three different checks

Rev 2 claimed a two-way split and got both halves wrong. The accurate picture
comes from the vendored schema:

1. **Hostname components** — `SddcHostSpec.hostname` (*"prefixed to the DNS
   subdomain name and should not include the domain name itself"*),
   `SddcManagerSpec.hostname`, `SddcVcenterSpec.vcenterHostname`,
   `NsxtManagerSpec.hostname`. All `maxLength: 63`. **VCF composes the FQDN
   from the short name and `dnsSpec` subdomain.** For ESX hosts the inventory
   also declares `mgmtIp`, so the check is equality against that; for the
   appliances there is no declared address, so the check is round-trip
   consistency.
2. **FQDN components** — `SddcNsxtSpec.vipFqdn`, and VSP's `platformFqdn`,
   `instanceFqdn`, `fleetFqdn`. Full names in the rendered spec.
3. **Collision-exclusion ranges** — `nsx.tepPool` and `appliances.vsp` pools.
   **Nothing resolves into or out of a TEP pool**; TEPs are host VTEP
   interfaces with no FQDN anywhere in the schema. These ranges exist only so
   other names can be checked for *not* colliding with them.

**VSP pool semantics, corrected.** Rev 2 said VSP names should resolve *into*
the declared pool. Broadcom says the opposite: these FQDNs *"must resolve to
unique and unused IP addresses **outside** of the IP range provided for VCF
services runtime, but still on the network that hosts the VCF services runtime
component."* So the check is: outside `ipv4Pool`, and on the management subnet.

## Containment

The inventory is untrusted input that causes network traffic; via MCP its author
may be whoever wrote a ticket.

**Rev 2's model was theatre.** A suffix allowlist chooses the *recipient* of
exfiltration, not whether it happens: `<63-bytes>.vcf.lab.example.net` passes
`is_subdomain` cleanly, and a forwarding resolver delivers the label to the
zone's real authoritative nameserver. Note that VCF's own schema does not help —
`platformFqdn`'s pattern is `^[a-zA-Z0-9]([-a-zA-Z0-9]{0,61}[a-zA-Z0-9])?(?:\..*)?$`,
bounding the first label and permitting **anything** after the first dot.

### Reconstruct, never accept

Every probed name is **built**, never taken from the document:

    <label validated against ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$, ≤63 chars>
      + "." + <the configured allowlisted suffix>

The inventory therefore carries **short names only** — `nsx.vip`,
`appliances.vsp.platform`, `.instance`, `.fleet` replace the `*Fqdn` fields,
matching `hosts[].name`, `appliances.vcenter.hostname` and
`appliances.sddcManager.hostname`, which are already short. The renderer
composes full FQDNs for the rendered `SddcSpec` exactly as VCF composes host
FQDNs. There is no free-text name anywhere in the probe path. A multi-domain
deployment is a deliberate future schema addition, not a hole left open now.

### Target classes

Three, each with its own allowlist; a class that is requested but probes nothing
emits its own `error` finding, matching the `VCF-PROBE-NOTHING-PERMITTED`
precedent:

- **Resolve targets** — reconstructed names, gated by suffix.
- **Reverse names** — `dns.reversename.from_address(ip)`, derived from an
  already-IP-gated address. Exempt from the suffix gate by construction: a
  reverse name is under `in-addr.arpa`, under no lab suffix. Rev 2's "every
  name re-enters the same gate" would have made reverse checks impossible.
- **Connect targets** — addresses we open TCP to. Its own allowlist, required
  to be **narrower than or equal to** the resolve allowlist.

Addresses learned from answers re-enter the connect gate before any connection.
An off-allowlist answer is a finding, never a follow-up.

### Mechanics that must be specified, not assumed

- Canonicalise both candidate and allowlist with
  `dns.name.from_text(v, origin=dns.name.root)`, assert `is_absolute()`, and
  query the parsed object. With `origin=None`, `is_subdomain` silently inverts
  when only one side has a trailing dot.
- **Reject empty or root allowlist entries** as a hard configuration error.
  Every name is a subdomain of root, so a blank entry is allow-all — and today's
  `permits_name` already refuses this, so accepting it would be a regression.
- **Re-validate nameservers as IP literals inside `probes.py`**, never relying
  on the schema. Probes run on schema-invalid documents: `api.py` gates probing
  on `CRITICAL` only and schema findings are `error`. dnspython turns a non-IP
  nameserver string into a **DoH endpoint** — `https://dns.attacker.example/dns-query`
  becomes outbound HTTPS that no IP allowlist can see. A name here is a hard
  refusal, never a coercion. `DnsSpec.nameservers` is capped at two entries.
- `Resolver(configure=False)`, **one nameserver per Resolver instance** so
  rotation cannot silently retarget, `lifetime` equal to the per-target budget,
  `tcp=False` with truncation reported as a finding rather than an automatic
  fallback, and a hard cap on total queries per run.
- A **bounded executor** with explicit socket cancellation. Measured today:
  52 hung lookups leave 53 live threads; `hosts.maxItems: 64` implies 128. A
  deadline that bounds waiting but not emitting is not a deadline.
- The **wildcard control probe** uses a fixed constant label —
  `vcfspec-wildcard-probe-do-not-create` — prefixed to an allowlisted suffix
  only, never a document-derived zone. Constant so it carries zero bits and is
  greppable in packet-capture assertions.
- Every finding and log line reports `parsed_name.to_text()`, never the document
  string. The same wire name has unbounded textual spellings.

**One promise that holds:** dnspython's CNAME chasing is in-response only
(`resolve_chaining` walks the chain inside the received message), so a CNAME
target does reach our code before any follow-up query.

**Accepted and bounded:** `VCF-PROBE-ADDRESS-IN-USE` makes reachability probing
a feature. The allowlist *is* the blast radius; cap distinct addresses per run
and say so in the threat model.

## Severity

**There is no phase flag.** Rev 2 proposed phase-rated severity; the codebase
has exactly one severity path — `catalogue.yaml` → `RuleMeta.severity` →
`finding_for` — and `Result.valid` is computed from severities already baked
into each `Finding`. Phase would have made validity depend on a call-time flag
with nothing in the envelope saying so.

Instead severity is fixed in the catalogue, and strictness is a caller decision:

- **Contradictions are `error`** — forward mismatch, reverse mismatch, no PTR
  matching the declared name, an address inside an excluded pool, a duplicate
  (FQDN, IP). Wrong now, wrong later.
- **Absences are `warning`** — no A record, no PTR, nothing answering.
- **`--fail-on {warning,error}` changes the CLI exit code only.** `Result.valid`
  keeps its exact current meaning and never shifts under a flag; MCP needs no
  change at all. **Default is `warning`**: probing is already opt-in, so an
  operator who asked to probe wants absences to count. `--fail-on error` is the
  explicit opt-out when DNS is known not to be live yet.

## False positives to avoid

Each is a configuration that is correct in practice and must not produce an
error:

- **Multiple PTRs** are routine with AD-integrated dynamic DNS.
  `VCF-PROBE-AMBIGUOUS-PTR` fires only when **none** of the PTRs matches the
  declared name. (Rev 2 contradicted itself here, forgiving multi-PTR in one
  section and erroring on it in another.)
- **Brownfield and redeploy.** `useExistingDeployment` exists on eight `$defs`
  and `workflowType` accepts `VCF_EXTEND`; reusing the VCF Installer as SDDC
  Manager is a documented path. `VCF-PROBE-ADDRESS-IN-USE` applies **only to
  components being newly deployed**; where existing deployment is declared, the
  inversion reverses and *nothing* answering is the defect. The finding reports
  what answered (TLS certificate CN/SAN) so a known appliance is
  distinguishable from a squatter.
- **A redundant primary/secondary pair mid-propagation** must not read as
  split-horizon. `VCF-PROBE-NAMESERVERS-DISAGREE` requires a settle-and-retry
  window and distinguishes content disagreement from convergence lag.
- **Security resolvers that sinkhole NXDOMAIN** must not trip the wildcard
  check for the whole run; scope it under the declared subdomain and report the
  answering address.
- **A slow but correct nameserver** must not yield a different finding set on
  each run. Separate "never responded" from "answered within budget"; escalate
  only on consistent failure.
- **Multi-A on a VCF component name is itself a finding**, because Broadcom
  requires a *unique* address. RRset tolerance applies to nameserver-agreement
  comparison, not to component round-trips.

## Vantage point

Resolution is performed against the nameservers **the spec declares**, not the
runner's `/etc/resolv.conf`. Demonstrated on 2026-09-20: the same name resolved
correctly from the lab runner (declared nameserver) and returned nothing from
the Proxmox host, whose resolver is `1.1.1.1` — which would also have published
the lab's internal naming to a third party. The spec states which vantage point
is authoritative and reports when probing cannot run from it.

## Case sensitivity

Comparison stays case-insensitive so a trailing dot or case difference is not a
false mismatch — but VCF's own comparison is case-sensitive, and an uppercase
PTR is a real documented failure
([KB 415466](https://knowledge.broadcom.com/external/article/415466)). A
**non-lowercase A or PTR answer is its own finding**, distinct from the
inventory-side `VCF-NAME-NOT-LOWERCASE`, so the two cannot collapse.

## Findings

Existing codes keep their meaning and gain `/appliances/...` paths.
`VCF-PROBE-UNKNOWN` is **retired**; its two live call sites in `probes.py` are
exception/timeout fallbacks, not absence cases, so they migrate to a new
`VCF-PROBE-RESOLVER-ERROR`, not to the absence codes. README and tests change
with it.

| Code | Severity | Meaning |
|---|---|---|
| `VCF-NAME-UNSUPPORTED-SUFFIX` | error | `.local` and the enumerated unsupported suffixes |
| `VCF-NAME-TOO-LONG` | error | short name over 63 chars or failing RFC 1123 |
| `VCF-PROBE-NO-FORWARD-DNS` | warning | name does not resolve |
| `VCF-PROBE-TCP-UNREACHABLE` | warning | nothing answering |
| `VCF-PROBE-RESOLVER-ERROR` | warning | SERVFAIL, timeout or truncation |
| `VCF-PROBE-AMBIGUOUS-PTR` | error | PTRs exist, none matches the declared name |
| `VCF-PROBE-ANSWER-NOT-LOWERCASE` | error | A or PTR answer contains uppercase |
| `VCF-PROBE-MULTIPLE-A` | error | component name resolves to more than one address |
| `VCF-PROBE-ADDRESS-IN-USE` | error | newly-deployed component's address answers |
| `VCF-PROBE-ADDRESS-EXPECTED-IN-USE` | error | existing-deployment component's address does not answer |
| `VCF-PROBE-ADDRESS-NOT-UNIQUE` | error | two names share an address |
| `VCF-PROBE-ADDRESS-IN-POOL` | error | address inside an excluded pool |
| `VCF-PROBE-ADDRESS-OFF-SUBNET` | error | VSP name resolves outside the management subnet |
| `VCF-PROBE-CNAME` | warning | name is a CNAME; VCF expects an A record |
| `VCF-PROBE-WILDCARD-DNS` | error | zone answers for the constant control name (one per zone) |
| `VCF-PROBE-NAMESERVERS-DISAGREE` | error | declared nameservers disagree after settle |
| `VCF-PROBE-NAMESERVER-UNREACHABLE` | error | declared nameserver never answered |
| `VCF-PROBE-NAMESERVER-INVALID` | critical | a nameserver entry is not an IP literal |
| `VCF-PROBE-NOTHING-RESOLVED` | error | resolve class requested, nothing checked |
| `VCF-PROBE-NOTHING-CONNECTED` | error | connect class requested, nothing checked |
| `VCF-PROBE-DNS-UNAVAILABLE` | error | dnspython missing while DNS probing requested |

`VCF-PROBE-NAMESERVER-UNREACHABLE` suppresses downstream per-name findings by
**not emitting them and recording the reason in `layers_skipped`** — a dead
nameserver produces one finding, not a dozen.

## Scope and sequencing

Three plans, in order. (1) and (2) block (3):

1. **Inventory schema and name reconstruction** — short-name fields replacing
   `*Fqdn`, nameserver IP literals, `fleetFqdn` modelled, renderer composes
   FQDNs, `--fail-on` replacing nothing (new flag), example and lab updated.
2. **Containment primitives** — parsed-name gating, the three target classes,
   resolver configuration, bounded executor, packet-capture test harness.
3. **The DNS checks themselves** — the finding table above, against the lab.

## Out of scope, stated rather than omitted

- **NTP.** Correct validation needs reply parsing (LI, stratum, KoD, root
  dispersion) and server-to-server comparison; an offset measured from a
  container whose clock is the host's is meaningless. Its own work item.
- **The VCF Installer's own `/etc/hosts`.** A `127.0.0.1 <FQDN>` line there
  fails fleet deploy and a DNS-only probe cannot see it. The installer's own
  A/PTR is also an unmodelled required record.
- **A deployable first-instance spec.** The inventory cannot express VCF
  Operations (`nodes` is required when the section is present), VCF Automation,
  VIDB or the licence server, and Broadcom requires a licence-server FQDN. This
  is a gap in the tool's *output*, not a scoping choice about DNS, and needs its
  own work.
- DNSSEC, IPv6 and `ip6.arpa`, and probing from the ESXi hosts themselves.
