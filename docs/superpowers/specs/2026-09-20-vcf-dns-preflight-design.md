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

### 1. `.local` on the VSP names only — static, no network

`VCF-NAME-VSP-LOCAL-SUFFIX` (**warning**, not error). The restriction is
narrower than rev 4 first claimed. VMware's
[split-domain post](https://blogs.vmware.com/cloud-foundation/2026/04/17/bridging-the-local-gap-a-split-domain-design-for-vmware-cloud-foundation-deployment/):

> *"To provide a transition window, core infrastructure components still allow
> .local configuration: VCF Operations, vCenter Server, VCF Networking (NSX),
> SDDC Manager."*

Only **VIDB, VCF Automation and vSphere Supervisor** lost `.local`, and those
run on the VSP platform. So the rule applies to `dns.subdomain` and
`appliances.vsp.platformFqdn` / `.instanceFqdn` — **not** to vCenter, NSX,
SDDC Manager or `hosts[].name`, where `.local` is a supported design and
flagging it would reject a working spec.

Report at `/dns/subdomain` **first** when that is the source, since it is the
one-line fix; `dns.subdomain` is currently absent from `_named_values()` in
`rules/platform.py` and must be added, or findings land on three VSP paths
while the cause sits elsewhere.

`appliances.vcenter.ssoDomain` is exempt and is correctly `vsphere.local` — an
identity namespace, not a DNS domain. Test that exemption explicitly.

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
`VCF-PROBE-RESOLVER-MISMATCH` (**`info`**, not warning) fires when the runner's
configured resolvers include none of the declared nameservers.

It is `info` because it is a vantage-point note, not a defect: a corporate
resolver that *forwards* the lab zone correctly trips it while resolving
perfectly, a `systemd-resolved` host shows only `127.0.0.53` and can never
match, and Windows has no `resolv.conf` at all. The `fix` text must say so
plainly — *"the answers above came from a different resolver than the lab will
use; this is not necessarily wrong, a forwarder may resolve the zone correctly;
re-run from the management network to be certain"* — or it misleads more than
it helps. A missing or unreadable file emits the existing `VCF-PROBE-UNKNOWN`
(info), never this code.

The reader goes behind an injected seam for **testability**, not purity:
`probes.py` is the I/O layer by design and the purity test that monkeypatches
`builtins.open` covers `render_provisioning`, not probes. Build it lazily
inside `run_probes()` — never at import, never as a default-argument value.

### 3. Probe the appliance names the inventory can express

Today only `hosts[].mgmtIp` is probed. Extend the **existing** socket-based
mechanism to the appliance names — but three things must be specified, because
appliances differ from hosts in ways that break the existing assumptions.

**Gating.** A host is gated by `config.permits(mgmtIp)` *before* any lookup
(`probes.py:161`), using an address the operator declared. **An appliance has no
declared address**, so that gate cannot fire and the resolved address is chosen
by whoever controls the zone. Therefore: after the forward resolve,
`config.permits(resolved)` gates the reverse lookup, and **appliances are never
connected to**. Pre-Installer they do not exist, so a 443 probe is guaranteed
noise; and it would be a connection to an address we did not choose. This is
rev 3's re-gate-derived-targets principle, which still applies even though its
machinery does not.

**Composition, by field and not by sniffing.** Two shapes:

| Already an FQDN | Short name, needs `+ dns.subdomain` |
|---|---|
| `nsx.vipFqdn` | `appliances.vcenter.hostname` |
| `appliances.vsp.platformFqdn` | `appliances.sddcManager.hostname` |
| `appliances.vsp.instanceFqdn` | `nsx.managers[]` |

The existing code composes unconditionally (`probes.py:165`), which would query
`nsx.vcf.lab.example.net.vcf.lab.example.net`. Name the two sets **explicitly by
field** — never by testing for a dot, because rules also run on schema-invalid
documents.

**CNAMEs — deferral reversed.** `gethostbyname` follows a CNAME silently and
`gethostbyaddr()[0]` returns the *canonical* name (`probes.py:95`), so a CNAME'd
appliance alias produces `VCF-PROBE-REVERSE-MISMATCH` at `error` on a perfectly
valid lab. Appliance names are the ones most likely to be aliases, so extending
round-trip probing to them makes this bite. No new dependency: compare against
the alias list from `gethostbyname_ex`, same stdlib call.

**PTR exemption.** `nsx.vipFqdn` and `appliances.vsp.platformFqdn` are both
VIP-like — the VSP platform's addresses come from `vsp.poolStart..poolEnd` — so
both are exempt from requiring a PTR, not just the NSX VIP.

**Counters.** Keep appliances out of the `candidates`/`permitted` tallies
(`probes.py:205`) so `VCF-PROBE-NOTHING-PERMITTED`'s `/hosts` message stays
true.

Out of scope until the inventory models them: VCF Operations, Automation, VIDB
and the licence server.

## Severity

**Applies to new codes only.** Existing codes keep their catalogue severities —
in particular `VCF-PROBE-NO-REVERSE-DNS` stays `error`. Re-severitying it would
flip `Result.valid` (`findings.py:37`) for every existing user, which is not a
change this work is entitled to make.

For new codes: **contradictions are `error`** (forward mismatch, reverse
mismatch), **absences are `warning`**.

`--fail-on {warning,error}` changes the **CLI exit code only**; `Result.valid`
keeps its meaning and the MCP surface is untouched. **Default `error`** — that
is today's behaviour (`cli.py:8`, `cli.py:208`), and `--fail-on` applies to all
findings, so defaulting to `warning` would change the exit contract for every
user of a tool whose probing is opt-in. Rev 4's earlier justification had this
backwards.

## Explicitly deferred, with the reason

Not "out of scope" hand-waving — each of these is covered by the Installer's
own validation, which runs before anything is deployed:

- **dnspython and querying declared nameservers.** Replaced by the mismatch
  warning above. If this returns, rev 3's containment analysis applies in full
  and should be re-read first: the nameserver is a destination taken from an
  untrusted document.
- **Multi-PTR ambiguity, wildcard-zone detection, nameserver disagreement.**
  (CNAME handling is no longer deferred — see §3.) Real DNS pathologies, but the Installer's DNS Resolution
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
