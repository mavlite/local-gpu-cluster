# Rendering a deployable VCF 9.1.1 spec — design

**Date:** 2026-09-21
**Status:** proposed
**Extends:** `docs/superpowers/specs/2026-09-17-vcf-spec-authoring-mcp-design.md`
**Supersedes as the active thread:** `2026-09-20-vcf-dns-preflight-design.md` (rev 3),
which is paused — see "Relationship to the DNS work".

## The problem

`vcf-spec-tools` renders an `SddcSpec` that **VMware Cloud Foundation would
reject.** For `workflowType: VCF` — a new fleet, i.e. a primary instance —
Broadcom's
[per-workflow JSON specification table](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/deployment/deploying-a-new-vmware-cloud-foundation-or-vmware-vsphere-foundation-private-cloud-/use-a-json-specification-to-deploy-vmware-cloud-foundation-or-vmware-vsphere-foundation.html)
marks `vcfOperationsSpec`, `licenseServerSpec` and `vidbSpec` as mandatory. We
emit none of them.

This outranks everything else the tool does. A validator that carefully checks
DNS for a spec that cannot deploy is checking the wrong thing.

The renderer is schema-valid — the vendored schema's top-level `required` is only
`['dnsSpec', 'networkSpecs', 'sddcId', 'vcenterSpec']` — which is exactly why
this went unnoticed. **Schema-valid is not deployable.**

## The reference, and its version trap

`lamw/vcf-91-in-box` `config/three-node-vsan-esa.json` is a working three-node
vSAN ESA spec with all 28 top-level sections. It becomes a vendored golden
fixture and the basis of the acceptance test.

**It is `version: 9.1.0.0`; we target 9.1.1.0.** Use it for *structure and
semantics*, never as a literal template. A verified example of the difference:
in 9.1.0 the LCM hostnames live in `fleetLcmSpec.hostname` and
`sddcLcmSpec.hostname`; in the 9.1.1 schema those definitions carry only `size`
and `version`, and the names have moved to `vspClusterSpec.fleetFqdn` and
`.instanceFqdn`. Copying the reference verbatim yields an invalid 9.1.1 spec.

Every field taken from the reference is checked against
`vcfspec/schemas/9.1.1.0/sddc-spec.schema.json`. Where they disagree, **9.1.1
wins** and the difference is recorded.

## Two conflicts to resolve before changing anything

Both are cases where our renderer matches the 9.1.1 schema and the working
9.1.0 spec does something else. Do not "fix" either by copying the reference.

| Field | We emit | Reference (9.1.0) | 9.1.1 schema |
|---|---|---|---|
| `hostSpecs[].hostname` | `esx01` | `esx01.vcf.lab` | *"prefixed to the DNS subdomain name and should not include the domain name itself"* |
| `networkSpecs[].vlanId` | `1610` (integer) | `"30"` (string) | `type: integer` |

Resolve each against the 9.1.1 schema plus a 9.1.1-era working sample or the
Installer's validation response — not by picking whichever source is nearest.
Until resolved, keep current behaviour and record the open question. These are
the two fields most likely to make an otherwise-complete spec fail.

## The gap

Measured by diffing our rendered output against the reference.

**Absent top-level sections (13).** Most are small:

- **Empty objects** — `fleetDepotSpec`, `saltSpec`, `saltRaasSpec`,
  `telemetryAcceptorSpec`. The reference sends `{}`; 9.1.1 declares optional
  `size`/`version`. No operator input.
- **One name each** — `licenseServerSpec.hostname`, `vidbSpec.hostname`. (Note
  `fleetLcmSpec`/`sddcLcmSpec` are *not* in this group for 9.1.1; see above.)
- **Naming** — `clusterSpec` (`datacenterName`, `clusterName`).
- **Real content** — `vcfOperationsSpec` (`nodes[]` with a `master`,
  `adminUserPassword`, `applianceSize`), `vcfOperationsCollectorSpec`,
  `vcfAutomationSpec`, and `dvsSpecs`.

**Gaps inside sections we already emit:**

- `networkSpecs` — missing `activeUplinks`, `standbyUplinks`, `teamingPolicy`,
  `portGroupKey`, `ipAddressVersion`, and `includeIpAddressRanges` on VMOTION
  and VSAN. Missing the `VM_MANAGEMENT` network type entirely.
- `vspClusterSpec` — missing `fleetFqdn`, `name`, `size`, `systemUserPassword`.
- `useExistingDeployment` — absent everywhere; the reference sets it throughout.
  This is also what makes the brownfield DNS rule implementable, which it is not
  today.
- `nsxtSpec` — missing `nsxtAuditPassword`; `vcenterSpec` — missing
  `storageSize`.

## Design

### Completeness is a rule, not an omission

A new rule checks the rendered spec against the **mandatory-section table for
its `workflowType`**, so "you are missing `vcfOperationsSpec`" is a finding with
a fix, not silence. This is the rule whose absence caused the problem, and it
generalises: a future workflow type gets its own row rather than new code.

### Defaults versus operator input

Most of the gap is boilerplate. The inventory stays small; the renderer supplies
the rest from a versioned defaults file, as it already does. Only genuine
decisions reach the inventory: appliance hostnames, cluster and datacenter
names, appliance sizes, and the VM management network. Anything the reference
shows as fixed or derivable is a default, and the defaults file is per-version
so 9.1.2 can differ.

### Credentials stay out

New sections introduce `adminUserPassword`, `rootUserPassword`,
`systemUserPassword`, `nsxtAuditPassword`. All are `${reference}` strings like
every existing credential; the structural walk in `vcfspec/credentials.py`
already covers them by position, and must be confirmed to.

### Relationship to the VCF validation API

VCF exposes `POST /v1/sddcs/validations` (polled via
`GET /v1/sddcs/validations/{id}`), which resolves every FQDN forward and
reverse, checks NTP, host readiness and thumbprints, and enforces the mandatory
sections. It is the authoritative gate and it is **strictly stricter** than the
schema.

**We do not call it,** because it requires real credentials and our binding
constraint is that these tools never hold a secret. Instead the tool *prepares*
the submission: it renders the spec and emits a ready-to-run request that
substitutes secrets from the operator's environment at execution time. The
division is:

| | This tool | Validation API |
|---|---|---|
| When | Before an Installer exists | Once one does |
| Secrets | Never | Required |
| Cost | Seconds, offline | Async submit-and-poll |
| Catches | Missing sections, naming, capacity, network, DNS sanity | The authoritative gate |

Optionally vendor the Installer OpenAPI from the same `vmware/vcf-api-specs`
repo the SddcSpec already comes from, to map validation responses back onto our
finding codes. Offline, no secret handling.

### Acceptance test

A golden-file test: render the example and assert **structural equivalence** to
the vendored reference — every top-level section present, every field within a
section present, types matching the 9.1.1 schema. Values differ (addresses,
names, `${references}` for passwords); structure does not. This is the test
whose absence let a non-deployable spec pass 395 others.

## Relationship to the DNS work

Rev 3 of the DNS pre-flight spec is paused, not discarded. What survives:

- The nested lab and the `VCF-PROBE-REVERSE-MISMATCH` fix, both already merged.
- The finding that the Installer's own validation does forward and reverse DNS,
  which narrows our DNS work's value to *before an Installer exists* — a real
  niche, but a smaller one than rev 3 was designed for.
- The containment analysis, which applies unchanged whenever probing resumes:
  the nameserver must come from operator configuration rather than the document,
  and `dns.query.udp` with an owned socket replaces `Resolver`.

Resume DNS after this work, scoped against what the Installer already covers.

## Out of scope

- Calling the validation API, or handling any real credential.
- Secondary instances (`VCF_EXTEND`), VVF, and brownfield conversion — the
  mandatory-section table differs per workflow and this work targets
  `workflowType: VCF` only. The completeness rule is table-driven so the others
  are additive.
- Resolving the `hostname` and `vlanId` conflicts by assumption; they are open
  questions with a stated resolution method.
