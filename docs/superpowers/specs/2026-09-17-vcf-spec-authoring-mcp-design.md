# VCF deployment-spec authoring and validation — design

**Status:** approved design, not yet implemented
**Date:** 2026-09-17
**Scope:** stage 3 of the bare-metal-to-SDDC roadmap

## Purpose

Give an AI agent the ability to take a VMware Cloud Foundation deployment
specification, tell the operator whether it is valid, explain anything wrong in
terms of the VCF documentation, and render it into the JSON the VCF Installer
accepts.

The first deployment this serves is a **single-instance consolidated lab: three
physical hosts, VCF 9.1.1, vSAN as principal storage, NFSv3 supplemental.** The
tools are written so a larger design can be fed in later without redesigning
them, but nothing outside that shape is implemented now.

## Why this stage first

Hosts are still being built, and this stage needs no hardware. It is also the
stage where mistakes are most expensive: a malformed spec fails during bring-up,
after the maintenance window has already started.

## Non-goals

Explicitly out of scope, to be revisited as later stages:

- Multi-instance fleets, federation, stretched clusters, workload domains.
- Planning and Preparation Workbook import/export.
- Sizing an environment from workload requirements (this stage validates a spec;
  it does not design one).
- Submitting the spec or running bring-up — that is stage 4.
- Any live VCF API mutation. This stage is read-only against real systems.
- Bare-metal provisioning (stages 1-2; see the separate no-BMC findings).

## Background that shaped this design

Four findings from the research phase, each of which changed a decision:

1. **The VCF Installer never images bare metal.** ESX must already be installed
   and basic-configured before commissioning, so stage 3's input assumes hosts
   exist and are reachable.
2. **No official MCP server exists for vSphere or VCF.** VCF 9.1 ships the MCP
   *consumer* side (Private AI Services registers third-party servers over
   Streamable HTTP/SSE with per-tool admin approval). Nothing to adopt; the
   credible community servers are either unlicensed or ship write tools with no
   confirmation gate.
3. **Broadcom publishes OpenAPI 3.0.3 specs** for the whole VCF surface
   (`github.com/vmware/vcf-api-specs`). Schema validation is therefore derived
   from vendor artifacts, not hand-written.
4. **The documentation defers host-count minimums.** The 9.1 host-prep page
   states the number "depends on the type of storage and the deployment model"
   and points at the Installer's planner or the workbook. A validator built only
   from prose cannot answer "will three hosts work", so those floors must come
   from a table we own (see Rule sources).

## Architecture

Three layers, so later stages reuse this rather than duplicating it:

```
skills/            SKILL.md procedures naming tools and required checks
   |
mcp server         thin transport wrapper, ~5 tools, stdio + Streamable HTTP
   |
core library       spec model, validation layers, renderer, safety tiers
```

The core library has no MCP dependency and is unit-testable with no VMware
infrastructure. The MCP server contains no VCF knowledge — only tool plumbing.

**Language: Python.** It matches our existing MCP servers (`mcp-sdg`, the Memory
Vault bridge) and their systemd/venv patterns, and it is the language of
Broadcom's official SDK (VCF Python SDK 9.1.1, Python and Java only — there is no
JavaScript SDK), which stages 4-5 will need.

**Relationship to VCF-Design-Studio.** That repo (ours, MIT) holds a mature fleet
model, 80 validation rules, workbook-derived sizing tables and 1,788 tests. This
design does **not** depend on it: input is a spec document from any source. Its
exported design becomes one accepted input adapter later, and its sizing floor
tables are the reference for our own (with attribution). This keeps stage 3 small
and avoids a cross-repo, cross-language coupling while the scope is one lab.

## Input contract

The agent accepts a **spec document** — YAML or JSON — that is deliberately
permissive: tools validate what is present and report what is missing, rather
than refusing anything that is not complete. This matters because an operator
builds a spec incrementally and wants feedback at every step.

Three input shapes are recognised, in this order:

1. **Native VCF Installer JSON** — passed through to validation unchanged.
2. **Our lab inventory** (YAML) — a compact form covering the consolidated-lab
   shape: hosts, management network, VLANs and subnets, DNS/NTP, storage choice,
   licensing references, credential references. Rendered into installer JSON.
3. **Adapter output** (future) — e.g. a VCF-Design-Studio design export.

Credentials are never values. The inventory carries references such as
`${esx_root}`, validated for presence and for the VCF password policy, and
resolved only at submit time in stage 4. A spec document is therefore safe to
commit to git and safe for a model to read.

## Validation layers

Run in order; later layers do not run if an earlier one makes them meaningless.

1. **Schema** — JSON Schema extracted from Broadcom's OpenAPI spec for the
   Installer, vendored at a pinned VCF 9.1.1 version with a checksum, so
   validation is reproducible and offline.
2. **Rules** — cross-field checks that schema cannot express: subnet containment
   and overlap, IP pool sizing against host count, VLAN ranges, MTU floors
   (TEP minimum), gateway within subnet, FQDN and hostname format, storage
   choice against host count, duplicate IPs, management subnet reuse.
3. **Probes** (opt-in) — live environment checks from wherever the server runs:
   DNS forward and reverse resolution, NTP reachability, and TCP reachability of
   each host. Probes **fail soft**: an unreachable NTP server is reported as
   "unknown, could not probe", never as a validation failure, because the server
   may not sit on that VLAN.

A future fourth layer submits the spec to a real VCF Installer's validation API
(`POST /v1/tokens` then the validation endpoints) once an appliance exists. The
design keeps that seam open; it is not implemented here.

### Rule sources

Every rule carries a stable ID, a severity, and a citation. Rules come from three
places, and the source is recorded per rule:

- **Documentation** — a techdocs URL, for anything the docs state outright.
- **Vendor schema** — the OpenAPI spec, for structural constraints.
- **Workbook-derived tables** — vSAN policy floors (mirror FTT=1 minimum 3 hosts,
  RAID-5 minimum 6), appliance sizes, NIC profiles. These are transcribed from
  VCF-Design-Studio's tables, which were themselves taken from the Planning and
  Preparation Workbook's static reference tables. Attribution belongs in the
  source file (MIT).

The three-host lab sits exactly where this matters: vSAN principal at FTT=1
mirroring is the policy that three hosts can satisfy, and the tools must say so
rather than shrug.

## Findings model

Every tool returns findings in one shape, and this shape is the contract shared
with every future VCF server in the family:

| Field | Meaning |
|---|---|
| `code` | stable rule ID, e.g. `VCF-IP-POOL-TOO-SMALL` |
| `severity` | `critical`, `error`, `warning`, `info` |
| `path` | JSON pointer into the spec |
| `message` | what is wrong, in one sentence |
| `fix` | what to change |
| `source` | `docs` / `schema` / `table`, plus a URL where one exists |

Tools never raise raw tracebacks to the model. An internal error is itself a
finding with severity `critical` and a code of `INTERNAL`.

## Tool surface

Five tools. The count is deliberate: the local 27B model degrades with large tool
lists, which is why `mcp-sdg` exposes few. Every tool is read-only.

| Tool | Purpose |
|---|---|
| `vcf_spec_schema` | What a spec needs for a given deployment shape, with a worked consolidated-lab example |
| `vcf_render_spec` | Inventory to installer JSON, plus a plain-language summary of what it would build |
| `vcf_validate_spec` | Run the layers, return findings |
| `vcf_explain_finding` | Expand one finding with documentation context |
| `vcf_diff_spec` | Semantic diff between two specs or inventories |

`vcf_explain_finding` works from the bundled rule catalogue alone. Where our RAG
corpus is configured it enriches the answer, but the tool must never require it —
otherwise the server only works on our cluster.

## Safety model

Even though stage 3 is entirely read-only, the tier machinery ships now, because
stages 4-5 add mutation and retrofitting safety is how the community servers got
it wrong (one exposes 43 write tools with no confirmation gate in MCP mode).

- Every operation declares a tier: `read-only`, `mutating`, `destructive`.
- Enforcement lives in the safety module, **not** in prompt text.
- `mutating` requires explicit approval; `destructive` echoes the exact object
  list first and requires confirmation.
- Stage 3 registers only `read-only` operations, so the gate is exercised but
  never triggered.

This pattern is adapted from `vchaindz/claude-vsphere-skill` (Apache-2.0, needs
attribution). `giulianoberteo/vcf-mcp` and `2501-ai/vmware-mcp` carry **no
licence**, so their approaches may be studied and reimplemented but their code
must not be copied.

## Distribution

Built as a standalone package our cluster is the first consumer of, not as
cluster-specific code:

- **Two transports**: stdio (third-party clients) and Streamable HTTP (our LXC
  155 deployment, and VCF Private AI Services registration).
- **Configuration via environment**, with no assumption that an LXC, a router or
  a local model exists.
- **Versioned against VCF releases**, since rule tables are version-specific.

## Testing

- Golden-file tests for the renderer: inventory in, expected installer JSON out.
- One unit test per rule, positive and negative, using fixtures.
- Probe tests against fakes; no network access in CI.
- Schema pinned in-repo so validation results are reproducible.
- The full suite runs offline, with no VMware infrastructure.

## Host count at three nodes — resolved, with a caveat

Three hosts run a vSAN cluster. The storage policies whose floor is three hosts
are **Mirror FTT=1** and **RAID-5 (2+1) FTT=1** (ESA); RAID-5 (4+1) and RAID-6
need six. This is what VCF-Design-Studio's own `POLICIES` table encodes, and it
raises a warning at exactly three hosts.

Sources disagree about the *management domain* specifically, and the validator
must represent that honestly rather than pick a side:

- The VCF 9.1 host-prep page states no number: host count "depends on the type of
  storage and the deployment model", deferring to the Installer's planner or the
  Planning and Preparation Workbook.
- Broadcom KB 392993 says four hosts for a single availability zone — but it is
  **stale**: it refers to Cloud Builder (replaced by VCF Installer in 9.x) and
  asserts non-vSAN storage is unsupported in the management domain, which the
  9.1 NFS page contradicts (NFSv3 is supported as principal storage).
- Our own RAG corpus produced three mutually inconsistent answers to this
  question, including an invented table. Treat it as a lead, never as authority.

**Operational caveat that matters more than the count:** at three hosts with
FTT=1, one host in maintenance leaves two — below the policy floor. There is no
rebuild capacity and objects are non-compliant until it returns. Acceptable for a
lab, and the validator should say so before an upgrade rather than after.

### Consequence for the findings model

A rule is not always a flat pass or fail. Where authorities disagree, a finding
must carry its provenance and say so: `source` records whether the rule came from
docs, vendor schema or a workbook-derived table, and a rule may report "supported,
with this caveat" while citing the dissenting source. Rules that encode a
disputed value record both positions rather than silently choosing.

## Open questions

1. **Whether the VCF Installer itself enforces a four-host minimum** for the
   management domain in 9.1.1, independent of the vSAN policy floor. Only the
   Installer's planner, the workbook, or an actual bring-up attempt settles it.
   Until then the validator warns rather than blocks.
2. **Which spec sections the Installer treats as mandatory** for this shape,
   versus optional — from the OpenAPI spec once vendored.
