# VCF bring-up execution — design

**Status:** design under review
**Scope:** stage 4 of the bare-metal-to-SDDC roadmap
**Depends on:** stage 3 (`2026-09-17-vcf-spec-authoring-mcp-design`), stages 1–2
(`2026-09-21-esx-provisioning-design`)

## Purpose

Take a validated deployment spec and a prepared lab, and turn them into a
running VCF instance without a human at a browser: deploy the VCF Installer
appliance, configure its software depot, submit the spec, and monitor bring-up
to completion or to a failure a person can act on.

This is the first stage that **mutates infrastructure**. Everything before it
either produced a file or read state.

## Why this stage now

Stages 1–2 are proven: three physical hosts installed unattended on 2026-09-24,
each with its own FQDN certificate, SSH policy persisted, and memory tiering
enabled. Stage 3 renders a spec that validates clean and DNS that round-trips
for all eleven names. **The only remaining manual step in the whole roadmap is
the one this stage covers** — and it is currently a browser form, which is the
least reproducible part of the entire build.

## Non-goals

- **Day-2 operations, drift detection and governance — stage 5.** Bring-up ends
  when the instance is up; keeping it correct is a different problem with a
  different shape.
- **Workload domains, additional VCF instances, federation, stretched clusters.**
- **NSX Edge deployment.** Not part of bring-up in 9.x; it is a day-2 vCenter
  task.
- **VCF Automation.** Deferred by the approved design (+20 vCPU / +96 GB, and
  20 vCPU is the floor, not 16). Adding it later is stage 5.
- **Supplemental NFS datastores.** Principal storage only, as in stage 3.
- **Re-running bring-up to "fix" a failed one.** See the failure model: most
  states are destroy-and-redeploy, and pretending otherwise is how a lab gets
  into a condition nobody can reason about.

## Background that shaped this design

This section is unusually long because stage 4 is the first stage where a wrong
instruction costs a rebuild rather than a re-run. Every item below was found by
adversarial review on 2026-09-24 and verified against govmomi source, Broadcom
9.1 TechDocs, or this lab's own hardware.

### The Installer must not live on a host it is deploying

VCF bring-up builds a VDS and migrates `vmk0` **and the uplinks** off
`vSwitch0`. A VM portgroup on that vSwitch is left with no uplink, so the
Installer loses its network **mid-bring-up, on the host it is driving**. It
surfaces as the `Migrate ESXi Host Management vmknic(s) to vSphere Distributed
Switch` sub-step failing, which points at networking rather than at placement.

The usual mitigation — park it on a vSwitch backed by a NIC the VDS will not
claim — is **unavailable here**: `esxcli network nic list` on all three hosts
shows only `vmnic0`/`vmnic1`, both Intel 82599. The onboard Realtek 2.5G is not
claimed by stock ESX at all.

**Therefore the Installer is deployed onto `mgmt` (172.16.10.21)**, the
standalone host that is not part of the VCF instance. This is a placement
decision the tooling must enforce, not infer.

### Standalone ESX has no vApp property store

ESX has no OVF-property store. Passing a property map alone yields a
**correctly-imported, permanently-misconfigured** appliance: no hostname, no IP,
no root password, a one-shot firstboot that ran against nothing, and no console
recovery. Not repairable.

The import options must set **`InjectOvfEnv: true` and `PowerOn: true` in the
same spec**, because govc's deploy path runs inject → power-on in order and
writes the properties into `ExtraConfig["guestinfo.ovfEnv"]` before the VM
starts. **Import and power-on are one indivisible operation.** Any design that
lets them be separate steps — including a retry that resumes at "power on" —
produces a bricked appliance.

### Property keys are per-artifact and asymmetric

Verified for 9.0.0.0.24703748: `vami.hostname` carries **no instance suffix**
while its siblings (`vami.ip0.SDDC-Manager`, `vami.netmask0.SDDC-Manager`, …)
do. The network label is `Network 1`, not `VM Network`. The 9.1 OVA reportedly
adds IPv6 keys. **The 9.1.1 set is unverified and must not be guessed.**

Keys are therefore **derived from the artifact at run time**
(`govc import.spec -hidden <ova>`), and every key the tooling intends to set is
asserted present in the derived list. A literal key table is a guaranteed break
on the next OVA. A mistyped key does **not** fail the import — govc treats
unknown properties as a warning and continues — so the assertion is the only
thing standing between a typo and a silent misconfiguration.

### Two govc defaults are wrong for this deployment

- `DiskProvisioning` defaults to `flat` (thick). 914 GB of flat extents on an
  862 GB datastore fails before a byte uploads. Must be `thin`.
- `MarshalManual()` does **not XML-escape** property values. One `&`, `<`, `>`
  or `"` in a password produces malformed XML and firstboot configures nothing —
  a symptom indistinguishable from the vApp problem above. Broadcom's documented
  special set (`@ ! # $ % ? ^`) is XML-safe, so enforcing the documented password
  policy also closes the escaping hole.

Also: manifest verification is off by default, and `WaitForIP` has no timeout.

### 200 does not mean the depot works

Authentication is `admin@local` with `LOCAL_USER_PASSWORD` — **not root**, which
returns a 401 that reads as an appliance fault. Success is three assertions, not
an HTTP code:

1. response body carries `offlineAccount.status == "DEPOT_CONNECTION_SUCCESSFUL"`
2. `GET /v1/releases` is non-empty and not `404 LCM_MANIFEST_NOT_FOUND`
3. the depot's own access log shows the appliance fetching `vcfManifest.json`

The metadata must be expanded under the depot root **before** the PUT, or the
depot accepts a configuration with an empty catalogue.

### Readiness is not reachability

ICMP and a 443 connect both succeed long before the API exists. Readiness is
`GET /vcf-installer-ui/login` returning 200.

## Architecture

Stage 4 keeps the split that makes stage 3's tools safe to expose:

```
MCP tools  = the ORACLE    pure judgment over documents, including captured
                           live state. Deterministic, no side effects.
Agent      = the ACTUATOR  performs all I/O with its own tools.
Skill      = the PROCEDURE ordering, gates, and what failure looks like.
```

The consequence worth stating plainly: **the hardest gates in this stage are
probes of live state that no inventory file can express** — whether
`disable_apichv` is present *and the host has rebooted since*, whether
`172.16.10.135` is genuinely unoccupied, how much datastore headroom remains,
what the vSwitch topology actually is. These are not rendered. They are
**gathered by the agent and judged by a pure tool**, which returns findings with
catalogue codes and fix text, exactly as stage 3's rules do.

This is also why stage 4 adds no command-rendering. Stage 3 already demonstrated
the failure mode: `provision.py`'s operator manifest rendered a UEFI HTTP Boot
procedure the hardware could not perform, was wrong for 100% of its lifetime,
and was certified by nine green substring tests through a hardware-verified
correction to the line directly above the defect. **Prose rendered as data is
not testable, and this roadmap has already paid for that lesson once.**

### Where mutation lives — DECISION REQUIRED

**Correction, 2026-09-24, after this spec was first committed.** An earlier
draft of this section claimed stage 3 "ships a tier module
(`read-only`/`mutating`/`destructive`) enforced in code, registering only
`read-only`, explicitly so that stage 4 does not have to retrofit it." **That is
false.** Stage 3's *design* promised it — "Tiers are enforced in a module, not in
prompt text" — and it was never built. `ToolDef` carries `name`, `description`,
`schema`, `handler`, `reports_valid`, `reports_layers`; grepping the package for
a tier concept returns only memory tiering. The claim was read out of a design
document and asserted as shipped fact without checking the code, which is the
same defect this spec's Architecture section warns about.

The consequence for the decision below: **(a) and (b) are both greenfield.** The
"one retrofits, one does not" argument is void. The tier field is ~10 lines on
`ToolDef` plus an assertion at registration that everything registered is
`read-only`, and it should be built regardless of which option wins, because it
is what lets one server stay safe as the tool count grows.

Two readings are open:

- **(a) A second MCP server** that registers `mutating` tools, with stage 3's
  server staying read-only. Preserves "the spec server never acts"; costs a
  second package and a second trust boundary to review.
- **(b) The agent executes via a skill**, and no MCP tool ever mutates. The
  standing constraint ("the MCP server performs no network I/O") is then
  absolute rather than per-server.

This spec assumes **(b)** until decided, because it is the option that cannot be
wrong: a skill-driven executor can later be promoted to (a), but a mutating tool
cannot be un-shipped. **Recommend (b) for this stage** — noting that the
recommendation survives the correction above, since it never depended on the
tier module existing, only on the asymmetry of what can be undone.

## Non-negotiable safety requirements

Adapted from adversarial security review, 2026-09-24. Any executor — skill,
script or future tool — must satisfy all of these or not ship:

1. **Key-based SSH only.** No `sshpass` in an automated path. Stages 1–2 already
   render `sshPublicKey` into the kickstart; use it.
2. **`govc` pinned to a specific, signature-verified release committed to source
   control.** No runtime fetch. This matches how the repo already vendors
   `vcf-installer-openapi.json` rather than fetching it live.
3. **Explicit TLS thumbprint pinning per host**, captured once and recorded
   alongside `provisioningMac`/`bootDisk`. `-k` is acceptable only as a
   loudly-logged first-contact bootstrap, never as steady state.
4. **Secrets reach subprocesses via an explicitly constructed minimal
   environment**, never `os.environ` inheritance; `GOVC_DEBUG` and verbose SSH
   hard-disabled on the execute path. `GOVC_DEBUG=1` writes full SOAP traffic
   including credentials to `~/.govc/debug/`.
5. **A last-line-of-defence scan**, mirroring `render.py`'s `_credential()`:
   every constructed argv, URL and log line checked for a resolved secret before
   it touches disk, stdout or argv — raise rather than emit.
6. **Rendered artifacts keep `${reference}` form permanently.** If the renderer
   is ever asked to interpolate a real value "to make the plan runnable as
   printed", containment is gone. Treat that request as a standing threat.

Additionally: the appliance's OVF environment persists in the host's `.vmx` as
`guestinfo.ovfEnv`, readable by anyone with datastore access, **forever**.
Whether it can be stripped after firstboot without breaking later boots is an
open question.

## Failure model

Stage 4's defining property is that most failures are **not resumable**, and
pretending otherwise is more dangerous than failing loudly.

| State | Recoverable? | Action |
|---|---|---|
| Portgroup exists, wrong VLAN | yes | fix VLAN; static IP persists |
| Import failed mid-upload | partially | orphaned lease + stranded disks; require explicit `--force-destroy` |
| Imported, `guestinfo.ovfEnv` present, power-on failed | yes | power on; properties already injected |
| Imported, `guestinfo.ovfEnv` absent | **no** | destroy and redeploy |
| Booted, firstboot ran against bad properties | **no** | destroy and redeploy; no console recovery |
| Depot PUT failed | yes | idempotent; re-run |
| Bring-up failed mid-flight | **depends** | out of scope to auto-repair; report and stop |

Idempotency checks must compare **name *and* VLAN *and* vSwitch** for the
portgroup, and **presence of `guestinfo.ovfEnv`** for the VM. Existence alone is
insufficient in both cases.

## Ordering gates

Each of these produces a misleading failure when violated. They are the skill's
spine and the pre-flight tool's rule set:

1. `monitor_control.disable_apichv="TRUE"` in `/etc/vmware/config` **and the host
   rebooted since** — else VMs fail to power on with a scheduler error that
   reads as a VM problem.
2. Forward *and* reverse DNS for the Installer FQDN resolve, **and nothing
   answers on its IP** — Broadcom requires each FQDN resolve to a *currently
   unassigned* address; a leftover appliance makes the next run fail as a DNS
   fault.
3. NTP reachable, host clock sane — the appliance generates certificates at
   firstboot; skew poisons the fleet later, not now.
4. VM portgroup exists **with the correct VLAN**, on a vSwitch the VDS will not
   claim.
5. Datastore headroom sufficient — 862 GB against a documented 914 GB
   requirement is already out of spec and must be recorded as accepted risk, not
   papered over.
6. `DiskProvisioning: thin`, `InjectOvfEnv: true`, `PowerOn: true` in one spec.
7. Depot metadata expanded → depot PUT → three-part verification.
8. Readiness by `/vcf-installer-ui/login`, not ping.

## Tool surface

Pure tools, consistent with stage 3's model. All take documents — including
captured live state — and return findings.

- `vcf_check_bringup_readiness(inventory, host_state)` — judges gates 1–5
  against state the agent gathered. New catalogue codes.
- `vcf_check_installer_identity(inventory, installer_fqdn, installer_ip)` — the
  one derivation with a measured failure cost: a mismatch against
  `sddcManagerSpec.hostname` under `useExistingDeployment` costs three failed
  validation checks, measured 2026-09-22.
- `vcf_render_depot_settings(inventory)` — the `PUT /v1/system/settings/depot`
  body. A VMware-defined payload, the same *kind* of artifact as an SddcSpec, so
  redaction and verification apply unchanged. Requires a `depot` block in the
  inventory schema.
- `vcf_explain_bringup_failure(task_response)` — maps Installer task output to
  catalogue findings. Stage 3's `explain_finding` pointed at a different corpus.

No tool renders a command. No tool performs I/O.

## Testing

The hard part: how do you test something whose whole job is to mutate?

- **Pure tools test as stage 3's do** — one test per catalogue rule, positive
  and negative, against captured-state fixtures.
- **Fixtures are real captures**, not hand-written. `esxcli --formatter=json`
  output, `govc import.spec` output, and Installer API responses recorded from
  the live lab and committed. A hand-written fixture tests the author's belief
  about the format.
- **Assertions are on findings, never on substrings of prose.** This is a
  standing rule for the roadmap after the stage-3 manifest defect.
- **The executor is not unit-tested; it is exercised against the lab** and its
  correctness is established by the pre-flight tools plus the failure model
  above. A mocked deployment proves nothing about ESX.
- Stage 3 carries a task to re-verify every `verified: false` golden against a
  real Installer. **This stage is where that debt is paid.**

## Open questions

1. **Mutation home** — decision (a) or (b) above. Recommend (b).
2. **Does `mgmt` (172.16.10.21) have capacity** for the appliance — ~100 GB thin
   and 16 GB RAM alongside DNS01, Wings01, Panel and traefik? Unmeasured.
3. **Can `guestinfo.ovfEnv` be stripped** after firstboot without breaking later
   boots? It is a durable plaintext secret on the datastore until answered.
4. **govc or ovftool as primary executor?** Lam's proven path is ovftool; govc's
   injected OVF environment is a leaner hand-rolled subset and whether VCF
   firstboot accepts it is unverified. Prove one by hand, then pin it.
5. **Does the 9.1.1 SDDC Manager OVA set a 100% memory reservation or
   latency-sensitivity high?** The former is harmless and helps; the latter is
   incompatible with memory tiering and must be cleared before power-on.
6. **Token TTL** on the Installer API, and whether a refresh token is issued.
   Assume short and re-authenticate per call until measured.

## What this stage settles for stage 5

Day-2 governance is the same shape pointed at a running fleet: capture actual
state, compare against declared intent, emit findings. `diff_spec` with live
input *is* drift detection. Stage 5 therefore needs more tools of the same kind,
not a different architecture — provided stage 4 does not set the precedent that
tools may act.
