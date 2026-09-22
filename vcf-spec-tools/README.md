# vcfspec — VCF deployment-spec tools

Validate a VMware Cloud Foundation 9.1.1 deployment specification, and
render one from a compact lab inventory — before you commit a rack of
hardware to a multi-hour bring-up. No VMware infrastructure required: this
is a static checker plus a renderer, nothing more.

## Quick start

```
python -m pip install -e ".[dev]"
python -m vcfspec.cli validate vcfspec/examples/lab-3-host.yaml
```

That validates the bundled three-host example. A clean run looks like
this (real output, `valid: true`, exit code `0`):

```json
{
  "valid": true,
  "findings": [
    {
      "code": "VCF-LIC-EVALUATION",
      "severity": "info",
      "path": "/instance",
      "message": "VCF 9.x has no license keys; this deploys in 90-day evaluation.",
      "fix": "Assign a subscription licence in VCF Operations after deployment.",
      "source": "docs",
      "source_url": "https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/licensing/licensing-overview/licensing-model.html"
    }
  ],
  "layers_run": ["detect", "schema", "rules"],
  "layers_skipped": {
    "probes": "no probe configuration supplied"
  }
}
```

`valid: true` here does not mean "no findings" — it means no `critical` or
`error` finding. The one finding present is `info`: VCF 9.x has no
license keys, so a fresh deployment always runs in 90-day evaluation.
`layers_skipped` tells you the probe layer did not run because no
`--probe`/`--allowlist` was given (see "What it checks" → Probes, below)
— read it before treating a clean-looking result as a complete one.

## What a failing run looks like

Real bring-up specs fail before they pass. Here the example's management
gateway was moved outside its own subnet:

(`broken-gateway.yaml` is the bundled example with its management gateway
changed to `10.50.99.1`, outside `10.50.10.0/24`.)

```
$ python -m vcfspec.cli validate broken-gateway.yaml
{
  "valid": false,
  "findings": [
    {
      "code": "VCF-NET-GATEWAY-OUTSIDE-SUBNET",
      "severity": "error",
      "path": "/networks/management/gateway",
      "message": "Gateway 10.50.99.1 is not inside subnet 10.50.10.0/24 for network 'management'.",
      "fix": "Set a gateway address within the declared subnet.",
      "source": "docs",
      "source_url": "https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/design/design-library/vsphere-detailed-design/esx-design.html"
    },
    {
      "code": "VCF-LIC-EVALUATION",
      "severity": "info",
      "path": "/instance",
      "message": "VCF 9.x has no license keys; this deploys in 90-day evaluation.",
      "fix": "Assign a subscription licence in VCF Operations after deployment.",
      "source": "docs",
      "source_url": "https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/licensing/licensing-overview/licensing-model.html"
    }
  ],
  "layers_run": ["detect", "schema", "rules"],
  "layers_skipped": { "probes": "no probe configuration supplied" }
}
$ echo $?
1
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | The document is valid (no `critical`/`error` finding). |
| `1` | The document is invalid — a structured finding explains why — or an internal error was caught and reported as one. |
| `2` | Usage error: bad CLI arguments, a path that doesn't exist or isn't readable, or `--probe` without `--allowlist`. This is deliberately distinct from `1`: a file that exists and parses but is a bad spec is not a usage error. |

`validate` also accepts `--fail-on {warning,error}`, default `error`. It
widens what counts as a failing run: with `--fail-on warning`, exit `1` if
*any* finding is `warning` severity or above, not just `error`/`critical`.
Default `error` is today's behaviour — unchanged unless you pass the flag.
**It only ever moves the exit code.** The JSON on stdout is identical
either way; a document with only `warning` findings still reports
`"valid": true`, because `Result.valid` means "no `critical`/`error`
finding" regardless of `--fail-on`. Use this in a CI gate that wants to
block on, say, `VCF-NAME-VSP-LOCAL-SUFFIX` (a `warning`) without changing
what the payload asserts about the document itself.

Both `validate` and `render` also accept `--input-kind {inventory,sddc_spec}`
to force the document kind instead of relying on auto-detection — the CLI
equivalent of the MCP server's `input_kind` argument on `vcf_validate_spec`
and `vcf_render_spec`. A value outside that closed pair is a usage error
(exit `2`), never a silent skip. `render` only ever accepts an inventory,
so forcing `--input-kind sddc_spec` there is only useful to turn a
misdetection into an explicit, honest `VCF-RENDER-WRONG-KIND` rather than
guessing wrong silently.

A usage error, for real:

```
$ python -m vcfspec.cli validate /no/such/file.yaml
cannot read /no/such/file.yaml: no such file
$ echo $?
2
```

## Rendering

`render` turns a lab inventory into the VCF Installer's `SddcSpec` JSON,
applying documented lab defaults (telemetry off, `workflowType: VCF`,
ESX thumbprint validation skipped because freshly-imaged hosts have none
yet) and reporting each one as an `info`-level `VCF-RENDER-DEFAULT-APPLIED`
finding:

The finding codes present, and the top-level shape of `spec` (both are
real output, piped through `jq` for brevity — the full envelope also
includes each finding's `message`/`fix`/`source` and the complete spec
body):

```
$ python -m vcfspec.cli render vcfspec/examples/lab-3-host.yaml | jq '[.findings[].code]'
[
  "VCF-LIC-EVALUATION",
  "VCF-RENDER-DEFAULT-APPLIED",
  "VCF-RENDER-DEFAULT-APPLIED",
  "VCF-RENDER-DEFAULT-APPLIED"
]
$ python -m vcfspec.cli render vcfspec/examples/lab-3-host.yaml | jq '.spec | keys'
[
  "ceipEnabled",
  "datastoreSpec",
  "dnsSpec",
  "hostSpecs",
  "networkSpecs",
  "nsxtSpec",
  "ntpServers",
  "sddcId",
  "sddcManagerSpec",
  "skipEsxThumbprintValidation",
  "vcenterSpec",
  "vcfInstanceName",
  "version",
  "vspClusterSpec",
  "workflowType"
]
$ python -m vcfspec.cli render vcfspec/examples/lab-3-host.yaml | jq '.spec.hostSpecs[0]'
{
  "hostname": "esx01",
  "credentials": {
    "username": "root",
    "password": "${esx_root}"
  }
}
```

**The rendered spec is itself validated before it is returned.** That is
the point of the tool, so it is not left to the operator to run `validate`
on the output afterwards: `render` finishes with a `verify` layer (visible
in `layers_run`) that checks the spec it just built against the vendored
VMware schema *and* walks it for any field name the schema does not
declare — a check `jsonschema` cannot make here, because no `$def` in the
vendored schema sets `additionalProperties: false`.

This matters because the inventory schema is deliberately looser than
VMware's. `networks.management.gateway: "nope"` is a plain string, so the
inventory schema accepts it; the rule layer skips it (it is not a parseable
address); and it is copied verbatim into `networkSpecs[0].gateway`, where
the vendored schema rejects it. Without the `verify` layer that rendered,
exited `0`, and reported `valid: true`.

A spec that fails `verify` is **not returned**: there is no `spec` key,
exactly as on the insecure-credential path below, because handing back a
spec the Installer would refuse — with a finding attached that whoever
pipes `.spec` into a file will never read — defeats the entire purpose.
Note the line that draws: a *rule* finding about the inventory (an
undersized TEP pool, a gateway outside its subnet) still returns the spec,
because that describes the input and an operator fixes it by iterating on
the render.

The three `VCF-RENDER-DEFAULT-APPLIED` findings are `workflowType: VCF`,
`ceipEnabled: false` and `skipEsxThumbprintValidation: true` — each one
names exactly which field it defaulted and why (see "What it checks"
above); nothing is defaulted silently.

Rendering with an insecure credential does not partially succeed — there
is no `spec` key at all, `render` never runs, and it exits `1`:

(`broken-cred.yaml` is the bundled example with `credentials.esxRoot`
changed from `${esx_root}` to the literal string `hunter2literal`.)

```
$ python -m vcfspec.cli render broken-cred.yaml
{
  "valid": false,
  "findings": [
    {
      "code": "VCF-CRED-NOT-A-REFERENCE",
      "severity": "critical",
      "path": "/credentials/esxRoot",
      "message": "Credential 'esxRoot' is not a reference. These tools never hold secrets.",
      "fix": "Use ${name}, e.g. ${esx_root}; resolve it at submit time.",
      "source": "docs",
      "source_url": ""
    },
    {
      "code": "VCF-LIC-EVALUATION",
      "severity": "info",
      "path": "/instance",
      "message": "VCF 9.x has no license keys; this deploys in 90-day evaluation.",
      "fix": "Assign a subscription licence in VCF Operations after deployment.",
      "source": "docs",
      "source_url": "https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/licensing/licensing-overview/licensing-model.html"
    },
    {
      "code": "VCF-RENDER-INSECURE-CREDENTIAL",
      "severity": "critical",
      "path": "/credentials",
      "message": "render() refused to emit a credential that is not a ${reference}.",
      "fix": "Use ${name} references for every credential, never a literal value. Run validate_document on the same input to see which credential field is affected.",
      "source": "schema",
      "source_url": ""
    }
  ],
  "layers_run": ["detect", "schema", "rules"],
  "layers_skipped": { "render": "refused an insecure credential" }
}
$ echo $?
1
```

## What it checks

1. **Schema** — against `SddcSpec` from Broadcom's VCF Installer OpenAPI
   document (`vmware/vcf-api-specs`, `9.1.1.0`), vendored, converted from
   its OpenAPI 3.0.1 dialect to JSON Schema draft 2020-12, and
   checksum-verified at load. The compact lab inventory has its own
   schema (`vcfspec/schemas/inventory/v1.schema.json`).
2. **Rules** — gateways inside their declared subnet, subnet and VLAN
   collisions (including the NSX TEP pool), NSX fabric MTU (VCF 9.1
   requires at least 1600 for overlay traffic), lowercase names, the VCF
   Management Services (VCFMS) pool's size and placement, and capacity
   against the mandatory 9.1 appliance stack — including memory tiering
   and Auto-RAID storage overhead, and what is left with one host in
   maintenance. Also `.local` on the VCF Management Services (VSP) names
   (`VCF-NAME-VSP-LOCAL-SUFFIX`, `warning`): `dns.subdomain` and the two
   VSP FQDNs, `appliances.vsp.platformFqdn` and `.instanceFqdn`, and
   **only those** — never vCenter, NSX, SDDC Manager or `hosts[].name`.
   That scope is deliberately narrow, not an oversight: VMware's
   split-domain design still permits `.local` on the rest during a
   transition window, and only VCF Identity Broker, VCF Automation and
   vSphere Supervisor actually lost support for it — all three run on the
   VSP platform, which is exactly what this rule's three pointers cover.
   `appliances.vcenter.ssoDomain` is untouched by this rule and is
   correctly `vsphere.local` by default: it names an identity namespace,
   not a DNS domain, so it was never a candidate. Read this before
   "fixing" a `.local` vCenter or NSX name — that is a supported design,
   not a bug this tool is telling you about.
3. **Probes** (opt-in, CLI only) — forward/reverse DNS and TCP-443
   reachability for each host, run only against CIDRs the operator
   explicitly allowlists with `--allowlist`, and resolving only names
   under a DNS suffix they explicitly allowlist with `--allowlist-domain`.
   **Appliance names are probed too** — vCenter, SDDC Manager, the NSX
   managers, the NSX VIP and the two VSP names
   (`appliances.vsp.platformFqdn`/`.instanceFqdn`). The short names
   (vCenter, SDDC Manager, NSX managers) are composed with
   `dns.subdomain`, the same as a host; the three FQDN fields
   (`nsx.vipFqdn`, `vsp.platformFqdn`, `vsp.instanceFqdn`) are already
   fully qualified and used as-is. `nsx.vipFqdn` and `vsp.platformFqdn`
   are VIP-like — they front a pool rather than one machine — so a
   missing PTR for either is not reported as a defect; the other four
   names still require one. Real output, run from a machine that is not
   on the example's
   `10.50.10.0/24` lab network, so every host in it is
   reachable-in-principle (inside the allowlist) but not actually
   resolvable from here:

   ```
   $ python -m vcfspec.cli validate vcfspec/examples/lab-3-host.yaml \
       --probe --allowlist 10.50.10.0/24 --allowlist-domain vcf.lab.knowledgeondemand.net \
       --probe-timeout 0.5
   ...
   {
     "code": "VCF-PROBE-UNKNOWN",
     "severity": "info",
     "path": "/hosts/0",
     "message": "Could not probe esx01.vcf.lab.knowledgeondemand.net: no forward DNS answer.",
     "fix": "Re-run from a host on the management network to confirm.",
     "source": "docs",
     "source_url": ""
   },
   {
     "code": "VCF-PROBE-NO-REVERSE-DNS",
     "severity": "error",
     "path": "/hosts/0",
     "message": "No reverse DNS record for 10.50.10.11 (esx01.vcf.lab.knowledgeondemand.net).",
     "fix": "Add a PTR record; VCF validates forward and reverse for every host.",
     "source": "docs",
     "source_url": "https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/deployment/deploying-a-new-vmware-cloud-foundation-or-vmware-vsphere-foundation-private-cloud-/preparing-your-environment.html"
   },
   ...
   ```

   Omitting `--allowlist` with `--probe` is refused as a usage error (exit
   `2`) rather than silently probing nothing. A host outside the allowlist
   is reported as `VCF-PROBE-TARGET-BLOCKED`; an unreachable one inside it
   is `VCF-PROBE-UNKNOWN` (info) or `VCF-PROBE-NO-REVERSE-DNS` (error) —
   the DNS or connectivity may simply not be wired up yet, which is exactly
   what an operator needs to know before bring-up, not a tool failure.
   Probes never run if a `critical` finding is already present.

   Two containment rules are worth stating on their own, because both
   close gaps an IP allowlist alone cannot:

   - **Names are gated separately from addresses.** The forward lookup
     resolves `<host.name>.<dns.subdomain>`, two strings taken straight
     out of the document, and a DNS query for an attacker-chosen name
     *is* the exfiltration channel — the label reaches whatever
     nameserver is authoritative for it. An IP allowlist cannot gate
     that, because you do not learn the IP until after the query. So
     `--allowlist-domain` gates the name, and it fails closed: with no
     suffix configured, **no forward lookup is issued at all** and each
     one is reported as `VCF-PROBE-NAME-BLOCKED`. The reverse lookup
     needs no gate — it takes the already-allowlisted IP. Suffix matching
     is on whole labels, so `vcf.lab.knowledgeondemand.net` does not permit `evil-vcf.lab.knowledgeondemand.net`.
   - **An allowlist that matches nothing is not a pass.** If probes were
     asked for and the allowlist permitted none of the document's hosts
     (`--allowlist 203.0.113.0/24` against a `10.50.10.0/24` lab), that is
     `VCF-PROBE-NOTHING-PERMITTED` at `error` severity — exit `1`, not a
     clean exit `0` with `probes` in `layers_run` and zero lookups made.
     Partial blocking is legitimate and is *not* this case: the permitted
     hosts are really probed and the verdict stands on their results.
   - **An appliance is never connected to, on any path.** Pre-Installer
     none of these appliances exist yet, so a 443 probe of one would be
     guaranteed noise — and unlike a host, an appliance declares no
     address of its own; the only address in play is whatever the zone's
     A record says, chosen by whoever controls that zone, not the
     operator. So the resolved address is checked against `--allowlist`
     before anything else happens with it, and **a resolved address
     outside the allowlist is refused, not followed** —
     `VCF-PROBE-TARGET-BLOCKED`, the same as a blocked host, with no
     reverse lookup and no connection attempt. This is the same
     "the query is the exfiltration channel" logic that gates host names
     by `--allowlist-domain`, applied one step further down the chain: an
     IP allowlist alone cannot gate an address you only learn about after
     the query that reveals it, so the address is gated the moment it is
     known and before anything downstream of it runs.

   Running `--probe --allowlist ...` **without** `--allowlist-domain`
   fails closed on appliance names exactly like it does on host names:
   expect one `VCF-PROBE-NAME-BLOCKED` warning per appliance name, in
   addition to one per host — six extra warnings on a document with
   vCenter, SDDC Manager, one NSX manager, the NSX VIP and both VSP
   names, even though nothing is actually broken. That is correct,
   fail-closed behaviour, not a regression: an IP allowlist cannot gate a
   name, because the DNS query is itself the exfiltration channel, so
   every unresolved name is accounted for individually rather than
   folded into one summary line. Pass `--allowlist-domain` with your
   lab's real suffix to make the count meaningful.

   One more code is reachable only from Python, by calling
   `run_probes()` directly and injecting your own seam:
   `VCF-PROBE-SEAM-UNUSABLE` at `error`. (`validate_document()` takes no
   seam arguments — it always calls `run_probes()` with the real
   resolver, connector and resolver-configuration reader — so neither it,
   nor the CLI, nor the MCP server can produce this.) Each seam is
   checked against the call it will actually receive:
   `resolve(name, want_reverse, want_canonical)`,
   `connect(host, port, timeout)` and a zero-argument reader. A value
   that is not callable, or a callable whose readable signature cannot
   accept that call, is reported once and probes nothing. It is an
   `error` rather than a quiet skip for the reason this whole layer
   exists: a resolver's `TypeError` is caught alongside every genuine
   resolution failure, so the alternative is a run that reports every
   name in the document as unresolvable, at `info`, and still says
   `valid: true`.

   The arity check cannot police a **return** shape, so `resolve()`'s is
   documented here instead: `want_canonical=True` must return a
   `(canonical name, address)` pair, or `None` when the name does not
   resolve; `want_reverse=True` returns a name; a plain forward call
   returns an address. Both the host and the appliance paths now issue
   the combined call, so a resolver that accepts `want_canonical` and
   ignores it — returning a bare address — is reported as
   `VCF-PROBE-UNKNOWN` for every name, not silently tolerated.

   One thing `run_probes()` does **not** guard is the `ProbeConfig` it is
   given. The injected seams are guarded everywhere, so nothing they
   raise escapes; but `permits()` and `permits_name()` are never wrapped,
   because "it raised, carry on" is indistinguishable from "it said
   yes" — and a gate failure read as a pass turns a closed allowlist into
   an open one. A `ProbeConfig` **subclass** whose gates raise will
   therefore propagate out of `run_probes()` by design. The stock
   `ProbeConfig`, which is all the CLI and the MCP server ever construct,
   cannot raise.

   A separate, `info`-level note can show up alongside any of the above:
   `VCF-PROBE-RESOLVER-MISMATCH` fires when the answers above did not
   come from any of the nameservers `dns.nameservers` declares — the
   probe layer always resolves through the machine's own configured
   resolver, never through the document's declared nameservers directly,
   so this is a provenance note, not a failure. It is `info`, not a
   higher severity, because it is expected background noise on most
   setups: a corporate or lab forwarder trips it while still resolving
   the zone correctly, `systemd-resolved` reports only `127.0.0.53`
   regardless of what it actually forwards to, and Windows has no
   `/etc/resolv.conf` at all (that case instead surfaces as
   `VCF-PROBE-UNKNOWN` on `/dns/nameservers`, since the comparison can't
   run). Re-run from the management network if you need certainty that
   the declared nameservers themselves produced these answers. It is
   reported only when at least one lookup was actually made: with every
   target blocked there are no answers for it to describe the provenance
   of.

### Behaviour change: an uppercase `dns.subdomain` now fails

`dns.subdomain` is now checked as one of the names the deployment
publishes, which puts it through the pre-existing `VCF-NAME-NOT-LOWERCASE`
rule at `error`. A document declaring `dns.subdomain: VCF.lab.example.net`
therefore reports `valid: false` and exits `1` where it previously passed.

This is deliberate and it is the only change in this release that can
flip a previously valid document to invalid. VCF rejects uppercase FQDNs,
and the subdomain is composed into every host and appliance name the
deployment publishes, so an uppercase one was never going to deploy — it
simply was not being checked. Lowercase the value to fix it.

### Behaviour change: a CNAME'd host name is no longer a reverse mismatch

If `hosts[].name` composes to a name that is a **CNAME**, the host used to
report `VCF-PROBE-REVERSE-MISMATCH` at `error` on a perfectly healthy
zone. `gethostbyname()` follows a CNAME silently, but `gethostbyaddr()`
hands back the *canonical* name, so the round trip compared
`esx01.vcf.lab.example.net` against a PTR legitimately naming
`esx01-real.vcf.lab.example.net` and called the disagreement a defect.

Hosts now accept the canonical name alongside the queried name, exactly
as appliance names already did — the two probe paths had drifted, and
this closes that gap. For any document validated through the CLI, the
MCP server or `validate_document()`, the only verdict that changes is a
CNAME'd host name going from `error` to clean; nothing that passed
before now fails.

**If you inject your own resolver, this is a breaking change.** The host
forward call changed from `resolve(fqdn, False, False)`, which returned a
bare address, to `resolve(fqdn, False, True)`, which must return a
`(canonical name, address)` pair — the same call the appliance names
already made. A three-argument resolver that accepts `want_canonical` and
then *ignores* it was previously valid for hosts and now returns the
wrong shape, which is reported as `VCF-PROBE-UNKNOWN` ("forward answer
was not a (canonical name, address) pair") for every host. Return the
pair, or `None` when the name does not resolve.

Two limits are worth knowing, because they are what keep the check a
check:

- **The canonical name must itself be inside `--allowlist-domain`.** A
  canonical name the operator never allowlisted is not evidence about the
  operator's zone, so it certifies nothing and the mismatch is still
  reported. Otherwise whoever controls the forward zone could nominate
  the very name that makes the round trip pass, and a check the checked
  party can satisfy by asserting it is not a check is worse than no check
  at all — it still reports success.
- **Only the canonical name, never the alias list.** The forward zone's
  aliases are its own claims about which names it answers to. The round
  trip exists to confirm that the forward and reverse zones — two
  separate authorities — agree, so the accept-set may not be one the
  forward zone can extend.

A PTR naming neither the queried name nor the allowlisted canonical name
is still `VCF-PROBE-REVERSE-MISMATCH`, and a host whose *name* is blocked
by `--allowlist-domain` issues no forward query at all, so it has no
canonical name to offer and is compared against the queried name alone —
its reverse lookup and its TCP 443 check still run, because the `mgmtIp`
was declared by the operator and is independently useful.

## Credentials

Credential fields hold references such as `${esx_root}`, never secrets.
Anything else — `VCF-CRED-NOT-A-REFERENCE` on validate, an outright
refusal on render — is rejected before it can leave the process. Schema
validation substitutes a compliant placeholder internally, because the
vendored schema imposes a `minLength` on password fields that a bare
`${reference}` string can violate; nothing about that placeholder ever
reaches a finding or the rendered spec.

## Security model

- **No secrets, ever.** These tools were built to never hold, log or emit
  a real credential. A credential is always `${reference}`; a literal
  value is rejected outright, when validating and when rendering, and on
  **both** document kinds — a lab inventory's free-form `credentials`
  block and an `SddcSpec`'s own credential fields
  (`hostSpecs[].credentials`, `rootVcenterPassword`,
  `adminUserSsoPassword`, `rootNsxtManagerPassword`, `rootPassword`, …).
  One structural walk (`vcfspec/credentials.py`) covers both, matching on
  position inside a `credentials` block *and* on credential-shaped key
  names, so a credential key no regex has been taught is still caught.
- **Probes are opt-in and contained.** They exist only in the CLI, never
  in the MCP server (see below), run only when `--probe` is passed, touch
  only addresses inside an explicit `--allowlist`, and resolve only names
  under an explicit `--allowlist-domain`. Both gates fail closed. An
  unreachable, disallowed or unresolvable target is reported, never
  silently skipped or silently probed anyway — and if the allowlist
  permitted *no* host in the document, that is a blocking finding rather
  than a clean pass over nothing.
- **The MCP server performs no network I/O at all.** None of its five
  tools accept a probe configuration. That's deliberate: the probe layer
  bounds a hung DNS lookup by abandoning a daemon thread rather than
  waiting on it, which is harmless in a short-lived CLI process but would
  leak threads without bound across the many calls a long-lived server
  handles. If a future tool genuinely needs probing from the MCP surface,
  that needs its own design, not a quietly added parameter.
- **A known, stated limitation.** `vcf_diff_spec` masks any value nested
  under a `credentials` key (at any depth) and anything matching a
  credential-shaped key name elsewhere. It does **not** catch a secret
  sitting under an unrecognisable key name in a place the schema would
  not normally allow a free-form object at all — closing that gap would
  require re-validating every diff input against the schema, which this
  tool deliberately does not do, since it diffs whatever it is handed,
  valid or not. This limitation is recorded in the docstring of
  `_change_entry` in `vcfspec/mcp_server.py`; read it there for the full
  reasoning, not just this summary.
- **No raw exception text in a finding.** Only an exception's class name is
  used; `str(exc)` can echo spec content (including a secret) verbatim.
- **A `jsonschema` message is never used where it could carry a secret.**
  `jsonschema` embeds the offending *instance* in the text it builds, and
  for a container it pretty-prints the whole dict as a Python repr —
  credentials included. So the schema layer builds its own message, from
  the error's JSON pointer and failing validator alone, whenever the
  instance is a container or the pointer sits at or under a
  credential-shaped position. Nothing is echoed, so there is no pattern
  for a masker to miss. Only a scalar at a non-credential position keeps
  `jsonschema`'s own wording, and that still passes through `redact()`.

## The MCP server (optional)

```
python -m pip install -e ".[mcp]"
python -m vcfspec.mcp_server
```

starts a stdio MCP server exposing `vcf_spec_schema`, `vcf_validate_spec`,
`vcf_render_spec`, `vcf_explain_finding` and `vcf_diff_spec`. Every tool
is a plain function of its arguments — `document`/`left`/`right` are YAML
or JSON **text**, not file paths — and every call goes through one
boundary (`call_handler` in `vcfspec/mcp_server.py`) that never lets an
exception escape: a malformed call comes back as `VCF-MCP-BAD-ARGS`
(retryable), a handler failure as `INTERNAL` (not retryable, and its
message carries only the exception's class name).

`vcf_validate_spec` and `vcf_render_spec` both also take an optional
`vcf_version` (default `9.1.1.0`, the only version currently vendored —
see "Updating for a new VCF release" below); an unvendored value is
rejected as `VCF-MCP-BAD-ARGS`, the same as any other bad argument, not
treated as an internal failure.

`vcf_version` is never used to build a filesystem path from what the
caller typed. It is resolved against the set of versions actually
vendored in the package — discovered by listing the schemas directory —
and anything outside that set is refused before any file is touched. The
refusal is deliberately uniform: a traversal, an absolute path, an
embedded NUL and a plain unknown `9.9.9.9` all produce the same envelope,
and the requested version is *not* quoted back in it (the vendored
versions are named instead). That uniformity is the control; without it
the tool is a filesystem existence oracle for whoever is driving it.

If the vendored schema itself fails its checksum at load, that is
**not** reported as a bad argument. It gets its own critical
`VCF-SCHEMA-INTEGRITY` finding saying the installation's integrity check
failed, because a supply-chain compromise should not look like a typo,
and retrying it is pointless.

`vcf_version` has no effect on an
inventory-kind document, which has one fixed schema regardless of VCF
release; supplying a non-default one anyway does not reject the call (an
inventory-kind call carrying an irrelevant `vcf_version` is legitimate),
but it is reported back as an `info`-level `VCF-VERSION-NOT-CONSULTED`
finding, so the result never silently implies something was checked that
was not. `vcf_validate_spec` and `vcf_render_spec` both also take an
optional `input_kind` to skip auto-detection — a closed enum of
`"inventory"` or `"sddc_spec"`, enforced at this boundary: anything else
(a typo included) is rejected the same way, before the call does
anything. See `.claude/skills/vcf-spec-authoring/SKILL.md` for how an
agent should use these tools.

### Envelope shape

The five tools do not all return the same keys, and that asymmetry is
deliberate, not an oversight — each key is present exactly where it means
something, and never present only on one of success or failure (a key
that only shows up when something went wrong is worse than one that is
always there or never there, because a caller cannot tell "nothing to
report" from "this tool doesn't report that"):

| Key | Present on |
|---|---|
| `findings` | Every path of `vcf_validate_spec`, `vcf_render_spec`, `vcf_diff_spec`; and the failure path only of `vcf_spec_schema`/`vcf_explain_finding` (their own success responses describe something other than a finding). |
| `valid` | Every path (success and failure) of `vcf_validate_spec`, `vcf_render_spec`, `vcf_diff_spec` — the three tools with a real validity concept. Never present for `vcf_spec_schema` or `vcf_explain_finding`, which validate nothing; forcing a `valid` value onto either would answer a question nobody asked. |
| `layers_run` / `layers_skipped` | Every path (success and failure) of `vcf_validate_spec` and `vcf_render_spec` only — the two tools with an actual layered pipeline. `result["layers_run"]` is therefore safe to read unconditionally on those two tools, including when the call failed, which is exactly when an operator most needs to see it. |

`vcf_diff_spec`'s `valid` means "both `left` and `right` were readable
documents, **and the diff you are holding is complete**" — it never
validates the documents it diffs, so `valid` there is never a statement
about whether either one is a good VCF spec.

It also always carries a `truncated` boolean. The diff's output is
bounded (10,000 changes / 1,000,000 characters) because bounding the
*input* does not bound the output: a 10 KB document using YAML aliases,
inside every input limit, previously produced a 306.8 MB response in
15.8 s. If either bound is hit, `truncated` is `true`, `valid` is
`false`, and a `VCF-DIFF-TRUNCATED` finding says so. **Do not read a
truncated diff as "nothing else changed"** — the changes reported are
correct as far as they go, but the walk stopped early. Diff a smaller
pair of documents, or narrower sections of them.

`vcf_explain_finding` returns the same flat shape
(`code`/`severity`/`summary`/`fix`/`source`/`source_url`) whether or not
the code was found — an unrecognised code explains
`VCF-EXPLAIN-UNKNOWN-CODE` itself rather than switching to a different
response shape. It does **not** echo the code you asked about (see below).

### What is and is not reflected back to you

Tool **arguments** are never echoed into a finding message: not
`vcf_version`, not `code`, not `input_kind`. Each rejection names what
this package actually ships instead. The caller already knows what it
sent, and these envelopes are read by an AI agent that may be handling a
document from an untrusted source.

Document **bodies** are a different matter, and the rule does not extend
to them — it cannot, because reporting what is wrong with your document
*is the job*. Values and key names from the document do appear in finding
messages and JSON pointers. The largest instance is a `hosts` array that
exceeds `maxItems`: jsonschema serialises the whole array into its
message, which is ~10,300 characters at 65 hosts.

Credential values are masked before any of that leaves the process —
verified with a literal planted inside an over-length `hosts` array, which
came back `***REDACTED***`. The distinction to hold onto is: **arguments
are not reflected; document content is reported, and redacted on the way
out.**

## Updating for a new VCF release

```
python scripts/vendor_schema.py <version> [path-or-url] [--accept-new-upstream]
```

Fetches `vcf-installer-openapi.json` from `vmware/vcf-api-specs` (or a
local path, if your network blocks GitHub), extracts and converts the
`SddcSpec` subtree, and writes a checksummed copy under
`vcfspec/schemas/<version>/`. It refuses to write anything if the
document's own declared version doesn't match what you asked for, or if
any schema reference is dangling.

**It also refuses to change anything it has already vendored.** Two
digests are recorded — `sddc-spec.source.sha256` (the fetched upstream
document) and `sddc-spec.schema.json.sha256` (the extracted bundle) — and
both are checked before any write, including before the directory is
created, so a refused run leaves the filesystem exactly as it found it.
Without that, the script computed the checksum from the bytes it had just
written, which attests integrity-at-rest and nothing about provenance: a
MITM'd fetch or a wrong `source` argument produced a perfectly
self-consistent schema+sidecar pair that the runtime check would then
certify.

To legitimately update a vendored schema:

1. Re-run the command. It prints both digests as `old -> new` and exits
   non-zero if either would change.
2. Review the change. Diff the upstream document, not the 1,685-line
   generated JSON.
3. Re-run with `--accept-new-upstream` and quote both printed digests in
   the commit message, so the bump is reviewable from the commit rather
   than from the JSON diff.

An unchanged re-vendor is a no-op and needs no flag. A changed upstream
whose extracted bundle happens to be byte-identical is still refused:
the extraction drops `example`, `discriminator`, `xml` and
`externalDocs`, so "the part we vendor is unchanged" is a weaker claim
than "upstream is unchanged".

A vendored schema whose digest sidecar is **missing** is also refused,
rather than treated as a fresh start. Otherwise deleting the sidecars
would be enough to bypass the guard entirely, which is fail-open — the
wrong default for an integrity control. Restore the sidecar from git, or
re-vendor deliberately with the flag.

One consequence worth knowing before you hit it: the currently vendored
`9.1.1.0` predates `sddc-spec.source.sha256` and does not have one, so
the first re-vendor of `9.1.1.0` **will** refuse and require
`--accept-new-upstream` once, purely to record the missing digest. That
is friction rather than a warning about the schema itself, and it is
accepted deliberately — special-casing "this particular sidecar may be
absent" would reintroduce the fail-open path the guard exists to close. Rule tables (capacity, defaults) are
versioned separately in `vcfspec/rules/tables.py` and
`vcfspec/defaults/<version>.yaml`, and are not touched by this script.

## Known limits

- The renderer covers what a lab needs, not all of `SddcSpec`'s
  properties — `vcfOperationsSpec` and `fleetDepotSpec` are not emitted,
  and whether `workflowType: VCF` requires them against a real Installer
  is unverified.
- Schema validation cannot catch a wrong `networkType` or an invalid
  appliance size, because the vendored document declares no enums for
  either — those live in the rules layer instead.
- There is no live validation against a running Installer
  (`POST /v1/sddcs/validations`); everything here is static.
- Password-policy validation is out of scope — it would need the real
  secret, which is exactly what these tools are built to never see.
- Supplemental NFS is a day-2 action and is not part of the spec this
  tool renders.

## Attribution

Capacity and default-value tables are transcribed from
[VCF-Design-Studio](https://github.com/mavlite/VCF-Design-Studio) (MIT),
whose values come from the VCF Planning and Preparation Workbook's static
reference tables.
