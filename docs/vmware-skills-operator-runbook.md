# vmware-skills — operator runbook (VCF lab integration)

**Status:** evaluated 2026-10-19, **not yet deployed**. The nested VCF lab is
currently **powered off** (CT 190 `lab-svc` + VMs 191–194 `lab-esx01..04`
stopped), so there is nothing live to connect to until the lab is up. This
runbook is the prepared procedure for that moment. Background + rationale:
memory `vmware-skills-family`.

## What this is

`github.com/orgs/vmware-skills` — a family of community Claude Code skills +
**standard stdio MCP servers** + CLIs for live VMware ops, by a Broadcom
engineer (Wei Zhou), MIT on the focus repos. vSphere 9.1 is explicitly
supported (pyvmomi `<10.0` over SOAP). This is the *live-ops* layer; it is
complementary to our local `vcfspec` MCP (API reference, not live calls).

## The one safety rule

**The real boundary is a dedicated vCenter Read-Only service account — not the
skill's policy layer.** `vmware-policy` (deny rules + audit) is library-level,
has a `VMWARE_POLICY_DISABLED=1` bypass, and a no-op `change_limits`. Treat it
as audit + convenience, and let vCenter RBAC be the gate.

## Host

**CT 190 `lab-svc` (10.50.10.100, on `vmbrlab`).** It already sits on the lab
network, reaches the nested vCenter/NSX, and keeps the community PyPI packages
off both the Proxmox host and the workstation. Needs Python ≥3.10 + `uv`.
(The Proxmox host also reaches `vmbrlab` at 10.50.10.1, but we keep the
hypervisor clean — LXC deployment model. The workstation is wrong: its VPN has
blocked lab DNS before.)

## Precondition — bring the lab up first

```
pct start 190                       # lab-svc (DNS/jump)
qm start 191 192 193 194            # nested ESXi lab-esx01..04
# wait for the nested vCenter + NSX Manager VMs to boot inside the nested ESXi
# then discover their FQDNs/IPs from lab-svc DNS (lab is OFF now, so unknown):
pct exec 190 -- sh -c 'getent hosts vcenter; getent hosts nsx'   # adjust names to the lab zone
```
Record the vCenter and NSX Manager FQDN/IP here once known: `vCenter=____  NSX=____`

## Pre-adoption checks (do once, before installing)

- Pin exact versions: Monitor/Storage `==2.4.2`, Harden `==1.16.0`,
  NSX-Security `==1.17.1`, Pilot `==2.4.0`, Policy (dep) current.
- Review source + commit history (community packages; no CI on the repos).
- **License check:** focus repos are MIT; the **NSX and Aria repos shipped with
  no license field** — confirm before adopting those two.

## Phase 1 — read-only pilot (Monitor + Policy)

```
# on CT 190
uv tool install vmware-monitor==2.4.2          # pulls vmware-policy as a dep
vmware-monitor init                            # writes ~/.vmware-monitor/config.yaml + .env (0600)
#   config.yaml: target name, vCenter host, verify_ssl:false ONLY for self-signed lab certs
#   .env: VMWARE_<TARGET>_PASSWORD=...  (<TARGET> = uppercased target name, hyphens->underscores)
vmware-monitor doctor                          # verifies connectivity + privileges
vmware-monitor attention                       # first live read: ranked issues across the vCenter
vmware-monitor investigate host lab-esx01 --html   # correlated host report -> ~/vmware-health/*.html
```

**RO service account (in vCenter):** a dedicated user with the built-in
**Read-Only** role **propagated from the inventory root**. Two reads need more,
grant only if wanted: `list_active_sessions` → `Sessions.TerminateSession`;
host-log reads (`scan_host_logs`) → `Global.Diagnostics`. An account with *no*
role gets no session at all.

**Governance from day one:** create `~/.vmware/rules.yaml` with deny-globs for
mutating ops + a `maintenance_window:`, label the target `environment:`
appropriately. Audit trail at `~/.vmware/audit.db`; inspect with
`vmware-audit log` / `vmware-audit policy`.

## Phase 2 — Storage (vSAN health, read)

```
uv tool install vmware-storage==2.4.2
# vSAN reads: get_vsan_health / get_vsan_capacity / get_vsan_efficiency (+ datastore reads)
```
Note: covers **vSAN ESA** health, **not** the ESXi memory-tiering NVMe layer
(our recurring tier-drive pain is not visible here). The only mutating tools are
iSCSI (`enable_iscsi`, `add/remove_iscsi_target`, `rescan_storage`) — don't use.

## Phase 3 — Harden (compliance scan, read)

```
uv tool install "vmware-harden[collectors]==1.16.0"   # collectors = read libs (aiops/storage/nsx-sec)
# baselines: vSphere SCG v9, CIS ESXi 9.0, STIG (experimental on 9.x); scans VCF 9.1
# optional: ANTHROPIC_API_KEY for the remediation advisor (advice only; no infra writes)
```
All 8 tools are read-only; `scan_target` writes only a local DuckDB cache.
Installing the `[collectors]` libs does **not** expose aiops's mutating MCP
server — Harden uses them as read libraries only.

## Phase 4 — NSX-Security (DFW visibility, LAB-ONLY, gated)

Add `vmware-nsx-security==1.17.1` for DFW policy/group/tag/Traceflow visibility
(`list_dfw_exclusions` is useful — VCF mgmt VMs are commonly DFW-excluded).
Its deletes are `confirm=True` + blast-radius gated. Run any mutation behind
`vmware-pilot==2.4.0` approval gates. NSX talks to the NSX Manager Policy API
(separate creds from vCenter).

## Consuming it from an agent

- **Simplest:** the CLIs above, run on CT 190 — immediate value, no MCP wiring.
- **MCP:** standard stdio, portable (opencode/Claude Code/Cursor). Minimal entry:
  `{"mcpServers":{"vmware-monitor":{"command":"vmware-monitor","args":["mcp"]}}}`.
  Run the client on CT 190, or use the servers' HTTPS+bearer remote mode to reach
  them from the workstation.

## Do NOT

- Install **Aria** or **Log-Insight** — they require the VCF Operations / Log
  Insight appliances, which we don't run in Minimal mode.
- Put mutating skills (`vmware-aiops`, full `vmware-pilot` dispatch) anywhere but
  the lab until soaked — their mutation paths only got "first real-vSphere run"
  hardening in mid-Oct 2026.
- Rely on `vmware-policy` as the safety boundary — the RO account is.
