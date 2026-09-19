# VCF deployment-spec authoring and validation — design

**Status:** design under review (revised after adversarial review, 2026-09-18)
**Scope:** stage 3 of the bare-metal-to-SDDC roadmap

## Purpose

Give an AI agent the ability to take a VMware Cloud Foundation deployment
specification, tell the operator whether it is valid, explain anything wrong in
terms of the VCF documentation, and render it into the JSON the VCF Installer
accepts.

The first deployment this serves is **one VCF instance, `workflowType: VCF`,
management domain only, with business workloads co-resident: three physical
hosts, VCF 9.1.1, vSAN ESA as principal storage.**

Note on terminology: VCF 9 retired the Standard/Consolidated architecture names.
A fleet contains VCF instances; each instance has a management domain and
optional VCF domains. "Workloads beside management VMs" is a placement choice,
not a named architecture. The shape switch that matters in the JSON is
`workflowType` (`VCF`, `VVF`, `VCF_EXTEND`, `VCF_COMPLETE`).

## Why this stage first

Hosts are still being built, and this stage needs no hardware. It is also where
mistakes are most expensive: a malformed spec fails during bring-up, after the
maintenance window has started.

## Non-goals

- Multiple instances, federation, stretched clusters, additional VCF domains.
- Planning and Preparation Workbook import/export.
- Sizing an environment from workload requirements.
- Submitting the spec or running bring-up — stage 4.
- Any live VCF API mutation. Stage 3 is read-only.
- Bare-metal provisioning (stages 1-2).
- **Supplemental NFS datastores.** The Installer configures *principal* storage
  only; adding supplemental NFS is a day-2 SDDC Manager action (natively
  supported from 9.1.1). The lab's NFS is therefore post-bring-up and out of
  this spec.

## Background that shaped this design

1. **The VCF Installer never images bare metal.** ESX must already be installed
   and configured before commissioning, so hosts are an input.
2. **No official MCP server exists for vSphere or VCF.** VCF 9.1 ships the MCP
   *consumer* side (Private AI Services registers third-party servers). The
   credible community servers are unlicensed or ship ungated write tools.
3. **Broadcom publishes OpenAPI 3.0.3 specs**, so schema validation derives from
   vendor artifacts rather than hand-written rules.
4. **The documentation defers host-count minimums** to the Installer's planner,
   so sizing floors come from tables we own.

## What actually gates a three-host lab

Not host count — **capacity**. Installer planning accepts 3 hosts for vSAN (2
for external storage). What fails a small lab is the mandatory 9.1 stack:

- **VCF Management Services** (`vspClusterSpec`), roughly 40 vCPU / 82 GB RAM /
  3 TB, plus vCenter, NSX Manager, SDDC Manager, VCF Operations and Operations
  Fleet Management; VCF Automation is optional and large (24 vCPU / 96 GB).
- **vSAN ESA Auto-RAID** is the 9.1 default for new clusters: FTT=1 RAID-5 (2+1)
  at 3-5 hosts, FTT=2 RAID-6 at 6+, both 1.5× overhead. Mirroring is no longer
  the default, and RAID-5 (4+1)/6-host figures are OSA-era.

So the validator must include a **capacity rule** that compares the mandatory
appliance footprint plus 1.5× storage overhead against the declared hosts. A
host-count-only check green-lights specs that fail in the Installer's Prepare
step.

**Operational caveat:** at three hosts with FTT=1, one host in maintenance drops
the cluster below its policy floor — no rebuild capacity, objects non-compliant
until it returns.

## Target lab profile (confirmed 2026-09-18)

**Hosts — 3 × Minisforum 795S7:** Ryzen 9 7945HX (16 cores / 32 threads), 96 GB
DDR5, 4 TB NVMe for vSAN, 500 GB NVMe for memory tiering, 1 TB NVMe for other
storage. Dual-port 10G NIC in the PCIe slot plus the onboard 2.5G — three pNICs.

**Capacity, computed from the 9.1 appliance tables:** the mandatory stack
(vCenter Small, NSX Manager Medium, SDDC Manager, Operations Fleet Manager,
2 × vCLS, VCF Operations Medium, Operations Collector Medium, VCFMS 1 control +
3 workers) is **76 vCPU / 219 GB RAM / ~3.0 TB**.

- All three hosts up: 219 of 288 GB — fits at 76% committed.
- **N-1: 192 GB available against 219 GB needed — does not fit.** A routine host
  reboot cannot host the stack on RAM alone.
- **Memory tiering is the lever**: the 500 GB NVMe at a 1:1 ratio presents about
  192 GB effective per host (384 GB total, 256 GB at N-1), which clears it.
- Storage: 12 TB raw → ~8 TB usable at Auto-RAID's 1.5×, against 3 TB consumed.
- CPU: 76 vCPU against 48 cores / 96 threads — normal oversubscription.

Two hardware caveats: 96 GB exceeds Minisforum's official 64 GB maximum (it
works, but is out of spec), and NIC speeds must not be mixed within one VDS
uplink set — use the dual 10G ports for the VDS and leave the 2.5G for host
management.

**Networking:** an isolated lab on MikroTik switches, uplinked to the home
192.168.6.0/24. IP and DNS naming are therefore free choices, which removes two
traps: the 12 consecutive addresses for `vspClusterSpec` are easy to reserve, and
`198.18.0.0/15` collides with nothing. MTU 9000 is available end to end.

- **CRS309-1G-8S+IN** (RouterOS): L3, VLANs, DHCP, NTP.
- **CSS610-8G-2S+IN** (SwOS Lite): L2 access only — it cannot serve DNS or NTP.
- **DNS does not live on the switch.** RouterOS auto-generates a PTR per static A
  record and tolerates multiple PTRs for one IP; VCF validates forward *and*
  reverse for every host and appliance, and ambiguous reverse records fail
  bring-up obscurely.

**Infrastructure services: the VIS appliance** (community-built, 2 vCPU / 4 GB /
30 GB) provides DNS with proper forward and reverse records, NTP, DHCP, the
**software depot** the Installer needs, an SFTP backup target, a container
registry, LDAP/OIDC and KMIP. It runs **on the Proxmox host, not on the three lab
hosts** — those are consumed by bring-up, and DNS and the depot must survive the
lab being torn down and rebuilt. VIS is a lab tool, not a production dependency;
an LXC running BIND or dnsmasq is the alternative if we want DNS we control and
automate ourselves.

## Sections a real 9.1 spec contains

The renderer must cover, at minimum: `sddcId`, `vcfInstanceName`, `workflowType`,
CEIP, `dnsSpec` (domain, subdomain, search, nameservers), `ntpServers`,
datacenter and cluster names, `networkSpecs` (MANAGEMENT, VMOTION, VSAN,
HOST_TEP, EDGE_TEP, uplinks — at least seven VLANs), `dvsSpecs` (MTU,
`vmnicsToUplinks`, `nsxTeamings`, optional `lagSpecs`), `vsanSpec`,
`vcenterSpec` (size, SSO domain, credentials), `nsxtSpec` (TEP pools, VIP,
credential sets), `sddcManagerSpec`, `vcfOperationsSpec`,
`vcfOperationsFleetManagementSpec`, the Operations Collector, `vspClusterSpec`
(`ipv4Pool` of 12-30 **consecutive** IPs, minimum /28, plus
`internalClusterCidrIpv4`, default `198.18.0.0/15`), `hostSpecs` (FQDN,
credentials, per-host management/vMotion/vSAN/TEP IPs, ESA disk identifiers),
and proxy/depot plus certificate handling. Appliance specs carry
`useExistingDeployment`.

**Licensing is not in the spec.** VCF 9.x has no license keys: licensing is
subscription-based, delivered as license files and assigned in VCF Operations
after deployment, with a 90-day evaluation. The renderer emits no licence fields
and the validator raises an `info` finding that the lab runs in evaluation.

Two practical traps for a lab on a single /24: finding **12 consecutive free
addresses** for `vspClusterSpec`, and the default internal CIDR `198.18.0.0/15`
colliding with existing lab ranges. Both get explicit rules.

## Architecture

```
skills/            SKILL.md procedures naming tools and required checks
   |
mcp server         thin transport wrapper, 5 tools, stdio + Streamable HTTP
   |
core library       spec model, validation layers, renderer, safety tiers
```

The core has no MCP dependency and is unit-testable without VMware
infrastructure. The MCP server holds no VCF knowledge.

**Language: Python** — matches our existing MCP servers and Broadcom's official
SDK (Python and Java only; there is no JavaScript SDK) for stages 4-5.

**VCF-Design-Studio** is not a dependency. Its exported design becomes one input
adapter later, and its workbook-derived tables are the reference for ours, with
attribution (MIT).

## Input contract

Input is a **spec document**, YAML or JSON, validated permissively: report what
is missing rather than refusing incomplete work.

**Shape detection is explicit, never guessed.** Our inventory carries
`apiVersion` and `kind`; native Installer JSON is detected by required installer
keys. Ambiguous or unrecognised input returns `VCF-INPUT-UNRECOGNISED`. Every
tool accepts an optional `input_kind` argument that overrides detection.

The **lab inventory schema is a normative deliverable** of the implementation
plan: a JSON Schema file listing every field, type, cardinality and required
flag, published with one complete three-host example that doubles as the first
golden input fixture. The design fixes its shape only at the level of sections:
instance identity, hosts (FQDN, management IP, vmnics), networks (per-purpose
VLAN, subnet, gateway, MTU, pools), DNS/NTP, storage choice, VCF Management
Services pool, and credential references.

### Credential references

A credential field must match the reference grammar `${IDENTIFIER}` exactly.
**Any other string is rejected** with a critical finding — this prevents a pasted
real password from being treated as a value. Stage 3 never holds secrets, so it
validates *reference presence and position only*; password-policy checks (length,
character set, dictionary rules) belong in stage 4 where the value exists, and
stage 3 emits an `info` finding naming the policy instead.

## Validation layers

1. **Schema** — JSON Schema dereferenced from Broadcom's OpenAPI spec, vendored
   per VCF version with a checksum. A checksum mismatch is a **hard startup
   failure**, not a warning. A `scripts/vendor-schema.sh <version>` script
   performs extraction, and a CI drift test detects upstream change. The
   implementation plan names the exact source document and operation whose
   request body is the deployment spec.
2. **Rules** — cross-field checks, shipped as a `rules/*.yaml` catalogue (id,
   severity, source, citation) so the rule set is enumerable and each rule has
   exactly one test.
3. **Probes** (opt-in) — DNS forward *and* reverse, NTP, TCP reachability.
   Probes **fail soft**: unreachable is "unknown", never a failure.

**Layer gating is mechanical:** a schema error suppresses rule evaluation only
for the subtree at the failing JSON pointer; rules still run elsewhere. Probes
run only when there are zero `critical` findings. Every result reports
`layers_run` and `layers_skipped` with reasons.

### Rules the lab needs

Beyond structural checks (subnet containment, overlap, pool sizing, VLAN ranges,
gateway-in-subnet, uniqueness of every IP and FQDN):

- **FQDNs must be lowercase** — uppercase fails 9.1 deployment (known issue).
- Forward **and reverse** DNS for every host and appliance, including the
  platform, instance, fleet and service FQDNs.
- Identical NTP server list everywhere.
- TEP MTU ≥1600 (1700 recommended, 9000 end-to-end with physical ≥9216).
- Host TEP and edge TEP in **different, routable** VLANs on 2-pNIC hosts.
- vSAN and vMotion on separate VLANs; at least two physical NICs per host.
- TEP pool sized per host (typically two TEPs per host).
- `vspClusterSpec` pool is 12-30 **consecutive** addresses, minimum /28.
- `internalClusterCidrIpv4` must not collide with any declared network.
- Single hardware vendor, homogeneous hosts, no stateless ESX.
- Capacity fit (see above), reported against the declared host hardware.
- NSX at bring-up is a **single** Manager node, expanded post-deploy via SDDC
  Manager — rules must not assume a 3-node cluster or VIP at bring-up.

IPv6 dual-stack and the alternate internal CIDR are JSON-only 9.1 features:
rules must not assume IPv4-only and reject valid specs.

### Rule provenance

Each rule records `source`: `docs` (with URL), `schema`, or `table`
(workbook-derived). Where authorities disagree — as they do on management-domain
host count, where KB 392993's "four hosts" is Cloud Builder-era and contradicted
by 9.1's support for NFS principal storage — the rule reports the caveat and
cites the dissent rather than silently choosing. Our own RAG corpus produced
three inconsistent answers to that question, including an invented table: corpus
output is a lead, never an authority.

## Findings model

| Field | Meaning |
|---|---|
| `code` | stable rule ID, e.g. `VCF-IP-POOL-TOO-SMALL` |
| `severity` | `critical`, `error`, `warning`, `info` |
| `path` | JSON pointer into the spec |
| `message` | what is wrong, in one sentence |
| `fix` | what to change |
| `source` | `docs` / `schema` / `table`, plus URL where one exists |

**Severity semantics:**

- `critical` — the document cannot be processed further (unparseable, wrong
  kind, credential value where a reference belongs).
- `error` — the Installer would reject or the deployment would fail.
- `warning` — supported but risky, or disputed between sources.
- `info` — advisory, including defaults applied and evaluation licensing.

A single `valid` boolean is derived: true when no `critical` or `error` findings
exist. Rendering proceeds despite `error` findings so the operator can see the
whole picture, but the output is marked `valid: false`.

Tools never surface raw tracebacks; an internal failure is a finding with code
`INTERNAL` and severity `critical`.

## Renderer

A declarative mapping (inventory path → installer JSON pointer → transform) plus
a versioned `defaults-<version>.yaml` where every default records its
provenance. Three disposal rules, stated so nothing is silent:

- Required installer field, no inventory source, no default → `VCF-RENDER-UNMAPPED`
  critical finding.
- Optional field, no source → omitted.
- Default applied → emitted, with an `info` finding naming the default's source.

## Tool surface

Five read-only tools. The server is **stateless**: every call takes the whole
document (inline or by path), there are no handles and no ordering requirements.
Each tool takes an optional `vcf_version` (default 9.1.1) selecting the rule and
schema tables.

| Tool | Input | Returns |
|---|---|---|
| `vcf_spec_schema` | shape, version | required/optional fields, worked example |
| `vcf_render_spec` | inventory, version | installer JSON, findings, summary |
| `vcf_validate_spec` | document, layers, version | findings, `valid`, layers run/skipped |
| `vcf_explain_finding` | rule `code`, optional path | explanation, citation, fix |
| `vcf_diff_spec` | two documents | keyed semantic diff |

`vcf_diff_spec` keys list members explicitly (hosts by FQDN, networks by
purpose, pools by name) so diffs are stable. `vcf_explain_finding` works from the
bundled catalogue alone; RAG enrichment is optional and never required.

## Safety, security and trust

Stage 3 is read-only, but the machinery ships now because retrofitting it is how
the community servers ended up with 43 ungated write tools.

- **Tiers** (`read-only`, `mutating`, `destructive`) are enforced in a module,
  not in prompt text. Stage 3 registers only `read-only` operations.
- **"Read-only" is defined concretely:** the server may write its log file and
  nothing else. No spec content is persisted beyond a log line; no caches are
  written outside a configured directory.
- **Redaction:** all tool output — findings, diffs, rendered specs, error text —
  passes through one redaction filter before returning, on the assumption that
  secrets will appear despite the reference contract.
- **Probe containment:** probe targets must be inside both the spec's declared
  networks *and* an operator-configured allowlist, because a hostile spec
  declares its own networks. Per-probe timeout, bounded concurrency, server-side
  rate limiting, and an audit log line per probe. Without this the probe layer is
  a port scanner an agent can drive.
- **Input hardening:** `yaml.safe_load` only, with size, depth and alias-count
  limits enforced before parsing; file references canonicalised and confined to a
  configured root.
- **Transport:** stdio binds nothing. Streamable HTTP defaults to loopback or the
  LXC-internal address, requires a token, and is never exposed publicly by
  default. Registering with Private AI Services is a trust-boundary decision to
  review before stage 4 adds mutation.
- **Findings are data, not directives.** `fix` text and any RAG-enriched content
  are display material; skills must not auto-execute remediation.
- **Logging:** rule IDs, severities, paths, probe targets and results. Never full
  spec bodies, never resolved secrets, never raw tracebacks.

Tier enforcement is adapted from `vchaindz/claude-vsphere-skill` (Apache-2.0,
attribution required). `giulianoberteo/vcf-mcp` and `2501-ai/vmware-mcp` have no
licence: study, do not copy.

## Distribution

A standalone package our cluster is the first consumer of: two transports,
environment-based configuration, no assumption of an LXC, router or local model,
and versioning tied to VCF releases since rule tables are version-specific.

## Testing

- **Golden-file provenance is explicit.** Seed goldens only from vendored OpenAPI
  example payloads and Broadcom-published samples. Every unverified golden is
  marked `verified: false`, and stage 4 carries a task to re-verify each against
  a real Installer. Without this the goldens merely enshrine whatever the
  renderer first emitted.
- One test per catalogue rule, positive and negative.
- Probe tests against fakes; no network access in CI.
- Schema pinned in-repo; checksum verified at startup and in CI.

## Stage 4 seam

Live validation and submission use the Installer API: authenticate at
`POST /v1/tokens`, submit at `POST /v1/sddcs`, and retrieve with
`GET /v1/sddcs/{id}/spec`. Not implemented here.

## Open questions

1. **Does memory tiering deliver the assumed 1:1 ratio on this hardware?** The
   capacity maths above depends on it for the N-1 case. Confirm the supported
   ratio and the resulting effective RAM once a host is running ESX 9.1.1.
2. **Does `fipsEnabled` still exist in the 9.1 schema?** It is Cloud Builder-era;
   confirm against the vendored spec before exposing it as an operator input.
3. **Which sections the Installer treats as mandatory** for this shape — resolved
   by vendoring the schema, which must happen before layer 1 is written.

## Follow-on stages this design enables

- **Stage 1.5, network prep:** the MikroTik RouterOS REST API turns "the VLANs,
  MTU and trunks exist" from a manual prerequisite into an automated one, and is
  the natural second MCP server.
- **Stage 4:** submit and monitor bring-up via the Installer API.
- **Stage 5:** day-2 operations, where the spec-explorer pattern (a few tools
  over the OpenAPI surface) fits better than hand-written tools.
