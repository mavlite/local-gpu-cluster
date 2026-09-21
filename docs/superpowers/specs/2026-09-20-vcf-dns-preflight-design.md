# VCF DNS pre-flight — design (rev 4)

**Date:** 2026-09-21 (rev 4; rev 3 dated 2026-09-20)
**Status:** proposed, scoped down after the Installer's own validation was observed
**Code:** `vcf-spec-tools/`
**Lab:** `[[vcf_nested_lab]]`

## Why this is now much smaller

Rev 3 proposed 21 new finding codes, dnspython, a three-class containment
model, wildcard control probes and nameserver-agreement checks. Then a real
VCF 9.1.1 Installer was stood up and asked to validate our spec. It has its own
**DNS Resolution** check, plus **Network Configuration** and three capacity
checks, and it runs them against the same environment.

That reframes our value. We are not a second implementation of the Installer's
validation — we are the check you can run **before an Installer exists**, in
seconds, offline, with no credentials. The failures that matter in that window
are mundane: a record that is missing, wrong, or pointing the wrong way, and a
name VCF will refuse on sight. Not multi-PTR ambiguity.

So rev 4 keeps what is cheap and proven, and defers what the Installer already
does. Rev 3's containment analysis is not discarded — it is simply not needed
for a design that adds no new query classes.

## What already works — do not rebuild

`VCF-NAME-NOT-LOWERCASE` (error), `VCF-NAME-WRONG-DOMAIN` (warning),
`VCF-PROBE-FORWARD-MISMATCH`, `VCF-PROBE-NO-REVERSE-DNS`,
`VCF-PROBE-REVERSE-MISMATCH` (all error), the IP and domain allowlists, the
bounded resolver, and `VCF-PROBE-NOTHING-PERMITTED`. The reverse-mismatch check
was found and fixed by running against real dnsmasq and is merged.

## Three additions, all stdlib

### 1. Unsupported name suffixes — static, no network

`VCF-NAME-UNSUPPORTED-SUFFIX` (error). Broadcom: *"Domain suffixes such as
.local are not supported."* This is not hypothetical — the shipped example
inventory and the whole nested lab were built on `lab.local` and would have
been rejected by VCF. Enumerate the unsupported suffixes explicitly rather than
writing "non-RFC", which has no source.

**`appliances.vcenter.ssoDomain` is exempt** and is correctly `vsphere.local`:
the SSO domain is an identity namespace, not a DNS domain. A rule that flags it
false-positives on every valid spec. Test that exemption explicitly.

### 2. Vantage-point mismatch — detect, do not re-query

The probe layer resolves through the runner's `/etc/resolv.conf`, not the
`dns.nameservers` the spec declares. Demonstrated on 2026-09-21: the same name
resolved correctly from the lab runner and returned nothing from the Proxmox
host, whose resolver is `1.1.1.1` — which would also have published the lab's
internal names to a third party.

Rev 3's answer was to query the declared nameservers directly, which requires
dnspython and drags in the entire containment problem: a nameserver taken from
an untrusted document is a destination, and gating it is the hard part.

**Rev 4's answer is to report the mismatch instead.**
`VCF-PROBE-RESOLVER-MISMATCH` (warning) fires when the runner's configured
resolvers do not include any declared nameserver. The operator learns that the
answers came from somewhere other than the DNS the lab will use, which is the
actual risk, and we add no query class, no dependency and no new attack
surface. Reading `/etc/resolv.conf` is a file read in a module that is
otherwise pure, so it belongs behind the same injected seam as `resolver` and
`connector`.

### 3. Probe the appliance names the inventory can express

Today only `hosts[].mgmtIp` is probed. Extend the **existing** socket-based
mechanism — already hardened, already gated — to
`appliances.vcenter.hostname`, `appliances.sddcManager.hostname`,
`nsx.managers[]`, `nsx.vipFqdn`, `appliances.vsp.platformFqdn` and
`.instanceFqdn`.

These have no declared address, so the check is **round-trip consistency**:
resolve the name, resolve the address back, require the original name. Exempt
the NSX VIP from requiring a PTR — a VIP legitimately may have none.

Out of scope until the inventory models them: VCF Operations, Automation, VIDB
and the licence server.

## Severity

Unchanged from rev 3, which was sound: severity stays fixed in the catalogue —
**contradictions are `error`** (forward mismatch, reverse mismatch, wrong
suffix), **absences are `warning`** (no A, no PTR, nothing answering) — and
strictness is the caller's choice via `--fail-on {warning,error}`, which
changes the **CLI exit code only**. `Result.valid` keeps its current meaning and
never shifts under a flag; the MCP surface needs no change. Default `warning`,
because probing is already opt-in.

## Explicitly deferred, with the reason

Not "out of scope" hand-waving — each of these is covered by the Installer's
own validation, which runs before anything is deployed:

- **dnspython and querying declared nameservers.** Replaced by the mismatch
  warning above. If this returns, rev 3's containment analysis applies in full
  and should be re-read first: the nameserver is a destination taken from an
  untrusted document.
- **Multi-PTR ambiguity, wildcard-zone detection, nameserver disagreement,
  CNAME reporting.** Real DNS pathologies, but the Installer's DNS Resolution
  check sees them against the same environment, and our pre-Installer window is
  not where they bite.
- **`VCF-PROBE-ADDRESS-IN-USE` and the brownfield inversion.** Unimplementable
  as specified: the inventory cannot express `useExistingDeployment`, so the
  conditional has no input. Needs the schema change first.
- **NTP.** Its own work item. An offset measured from a container whose clock is
  the host's is meaningless; correct validation needs reply parsing and
  server-to-server comparison.

## Testing

Unit tests with injected fakes are the bulk but explicitly not sufficient — a
fake returns whatever the test author expected, which is how the reverse-DNS
bug survived the entire build. Every check is additionally exercised against
the nested lab's real dnsmasq by breaking a record and observing the finding.
