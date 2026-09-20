# VCF DNS and NTP pre-flight — design

**Date:** 2026-09-20
**Status:** approved, ready for planning
**Extends:** `docs/superpowers/specs/2026-09-17-vcf-spec-authoring-mcp-design.md`
**Code:** `vcf-spec-tools/`

## Why this exists

VCF hard-fails on DNS. The reason is structural, not incidental: the vendored
`SddcSpec` carries **hostnames and no IP addresses** — `SddcManagerSpec` and
`NsxtManagerSpec` each declare `hostname` and nothing else. VCF resolves those
names at deploy time, so DNS *is* the binding between the spec and reality. A
wrong record does not produce a validation error at submit time; it produces a
failure hours into bring-up, against the wrong address, with an obscure message.

The pre-flight probe layer exists to catch that before an operator commits to a
multi-hour build. Run against real DNS for the first time on 2026-09-20 (see
`[[vcf_nested_lab]]`), it did not.

## What already works

Do not rebuild these:

- `VCF-NAME-NOT-LOWERCASE` (error) — uppercase FQDNs fail VCF 9.1 deployment.
- `VCF-NAME-WRONG-DOMAIN` (warning) — name outside the declared subdomain.
- `VCF-PROBE-FORWARD-MISMATCH` (error) — A record disagrees with the inventory.
- `VCF-PROBE-NO-REVERSE-DNS` (error) — no PTR at all.
- `VCF-PROBE-REVERSE-MISMATCH` (error) — PTR names a different host (added
  2026-09-20 after the nested lab exposed it).
- IP and domain-suffix allowlists on host probes, failing closed, verified by
  packet capture.

## Gaps this closes

Each was reproduced, not inferred.

1. **Forward-resolution failure is `info`.** `VCF-PROBE-UNKNOWN` covers both "no
   forward DNS answer" and "no TCP 443 response" at `info`. With the nameserver
   stopped entirely, validation blocked only because the *reverse* checks are
   errors — the blocking was incidental. This is the finding parked in Task 10
   as cosmetic; it is not cosmetic.
2. **Appliances are never probed.** `run_probes` targets only
   `hosts[].mgmtIp`. vCenter, SDDC Manager, NSX managers, the NSX VIP and the
   VSP platform/instance FQDNs — every name VCF will resolve during bring-up —
   are unchecked.
3. **Ambiguous PTRs are undetectable.** `socket.gethostbyaddr` returns one name.
   Multiple PTRs on one IP is exactly the RouterOS auto-PTR failure that drove
   the "DNS must not live on the switch" decision.
4. **We validate the wrong resolver.** Probes use the runner's
   `/etc/resolv.conf`, not the `dns.nameservers[]` the spec declares. The lab
   will use the latter.
5. **A declared nameserver that is down produces no specific finding.**
6. **NTP is never probed.** VCF also hard-fails on time skew.

## Design

### Resolution

Add `dnspython>=2.6,<3`, **lazily imported** so the core library, the renderer
and the MCP server stay importable and testable without it. Probing without it
installed is a structured finding, never an ImportError.

Queries go to the nameservers the **spec declares**, not the host's resolver.
Validating whatever DNS the tool happens to sit beside answers the wrong
question.

### Containment — the binding constraint

The inventory is untrusted input that causes network traffic. A DNS query
exfiltrates by being *asked*: `<secret>.attacker.example` reaches the attacker's
nameserver whether or not anything responds. This already happened once — the
host FQDN was built from `name + subdomain` and resolved past a gate that only
checked the IP, and it survived a directive, an implementer test and a
reviewer's target enumeration because all three checked the IP.

This work multiplies targets from three to roughly a dozen and adds two
qualitatively new classes:

- `dns.nameservers[]` — we send DNS **directly to an IP from the file**.
  `169.254.169.254` is the cloud metadata endpoint; any RFC1918 address turns
  the tool into an internal reachability oracle via response-vs-timeout.
- `ntp.servers[]` — the same, over UDP.

Note the escalation dnspython brings: `socket.gethostbyname` goes through the OS
resolver and inherits its filtering, logging and policy. Querying a chosen
nameserver directly bypasses all of it, making the tool a general-purpose packet
sender to attacker-chosen destinations.

Therefore, and without exception:

- **Every** target — host IP, nameserver IP, NTP server IP, and every FQDN
  including appliance names — passes the allowlist gate **before any packet is
  sent**.
- No configured policy means **no probing of that class**, not permissive
  probing.
- Containment is proven by **packet capture in the nested lab**, not by fake
  resolvers. Fakes return what the test author expected; only the wire shows
  what left the machine.

### New findings

| Code | Severity | Meaning |
|---|---|---|
| `VCF-PROBE-NO-FORWARD-DNS` | error | name does not resolve — blocks |
| `VCF-PROBE-TCP-UNREACHABLE` | info | host not answering yet — does not block |
| `VCF-PROBE-AMBIGUOUS-PTR` | error | more than one PTR for an IP |
| `VCF-PROBE-NAMESERVER-UNREACHABLE` | error | declared nameserver does not answer |
| `VCF-PROBE-APPLIANCE-DNS` | error | appliance FQDN missing or inconsistent |
| `VCF-PROBE-NTP-UNREACHABLE` | error | declared NTP server does not answer |
| `VCF-PROBE-NTP-SKEW` | warning | offset beyond tolerance |
| `VCF-PROBE-DNS-UNAVAILABLE` | info | dnspython not installed; DNS probes skipped |

Splitting `VCF-PROBE-UNKNOWN` is deliberate: DNS faults block because VCF cannot
proceed without DNS, while TCP-unreachable stays informational because a spec is
legitimately validated before the hardware exists. The two severities now tell
one consistent story instead of two.

### Appliance probing

Probe every name VCF will resolve: `appliances.vcenter.hostname`,
`appliances.sddcManager.hostname`, each `nsx.managers[]`, `nsx.vipFqdn`,
`appliances.vsp.platformFqdn` and `.instanceFqdn`.

Appliance IPs are **optional** in the inventory, because `SddcSpec` does not
carry them. Absent a declared IP, check **round-trip consistency**: resolve the
FQDN to an address, resolve that address back, and require the original name.
That catches the RouterOS trap with no declared IP at all. When an optional
`ip:` is declared, additionally require an exact match.

### NTP

Reachability of each `ntp.servers[]` entry and an offset sanity check, both
behind the same IP allowlist. Unreachable is an error; skew beyond tolerance is
a warning.

### Inventory schema

Additive and backward-compatible: an optional `ip` on appliance entries. Every
existing inventory stays valid. No field becomes required.

## Testing

Unit tests with injected fakes remain the bulk, but they are not sufficient —
they are what hid the reverse-DNS bug for the whole build. Every containment
property is additionally proven in the nested lab (`[[vcf_nested_lab]]`) with
`tcpdump` on `vmbrlab`, asserting on **which names actually appear on the wire**.

Negative cases to prove, each by breaking real dnsmasq: missing A, missing PTR,
mismatched PTR, two PTRs on one IP, dead nameserver, appliance record absent,
NTP server unreachable.

## Out of scope

- DNSSEC validation.
- Probing from the ESXi hosts themselves (they do not exist yet).
- Any change to rendering or to the vendored schema.
