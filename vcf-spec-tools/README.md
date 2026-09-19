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
   maintenance.
3. **Probes** (opt-in, CLI only) — forward/reverse DNS and TCP-443
   reachability for each host, run only against CIDRs the operator
   explicitly allowlists with `--allowlist`, and resolving only names
   under a DNS suffix they explicitly allowlist with `--allowlist-domain`.
   Real output, run from a machine that is not on the example's
   `10.50.10.0/24` lab network, so every host in it is
   reachable-in-principle (inside the allowlist) but not actually
   resolvable from here:

   ```
   $ python -m vcfspec.cli validate vcfspec/examples/lab-3-host.yaml \
       --probe --allowlist 10.50.10.0/24 --allowlist-domain lab.local \
       --probe-timeout 0.5
   ...
   {
     "code": "VCF-PROBE-UNKNOWN",
     "severity": "info",
     "path": "/hosts/0",
     "message": "Could not probe esx01.lab.local: no forward DNS answer.",
     "fix": "Re-run from a host on the management network to confirm.",
     "source": "docs",
     "source_url": ""
   },
   {
     "code": "VCF-PROBE-NO-REVERSE-DNS",
     "severity": "error",
     "path": "/hosts/0",
     "message": "No reverse DNS record for 10.50.10.11 (esx01.lab.local).",
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
     is on whole labels, so `lab.local` does not permit `evil-lab.local`.
   - **An allowlist that matches nothing is not a pass.** If probes were
     asked for and the allowlist permitted none of the document's hosts
     (`--allowlist 203.0.113.0/24` against a `10.50.10.0/24` lab), that is
     `VCF-PROBE-NOTHING-PERMITTED` at `error` severity — exit `1`, not a
     clean exit `0` with `probes` in `layers_run` and zero lookups made.
     Partial blocking is legitimate and is *not* this case: the permitted
     hosts are really probed and the verdict stands on their results.

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
treated as an internal failure. `vcf_version` has no effect on an
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
documents" — it never validates the documents it diffs, so `valid` there
is never a statement about whether either one is a good VCF spec.

`vcf_explain_finding` returns the same flat shape
(`code`/`severity`/`summary`/`fix`/`source`/`source_url`) whether or not
the code was found — an unrecognised code explains
`VCF-EXPLAIN-UNKNOWN-CODE` itself, naming the code you asked about in its
`summary`, rather than switching to a different response shape.

## Updating for a new VCF release

```
python scripts/vendor_schema.py <version> [path-or-url]
```

Fetches `vcf-installer-openapi.json` from `vmware/vcf-api-specs` (or a
local path, if your network blocks GitHub), extracts and converts the
`SddcSpec` subtree, and writes a checksummed copy under
`vcfspec/schemas/<version>/`. It refuses to write anything if the
document's own declared version doesn't match what you asked for, or if
any schema reference is dangling. Rule tables (capacity, defaults) are
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
