# vmware-skills — operator runbook (VCF lab integration)

**Status: deployed 2026-10-22, read-only.** `vmware-monitor`, `vmware-storage` and
`vmware-harden` run on CT 190 `lab-svc` against the **physical** VCF 9.1.1 vCenter
(`vcsa.lab.knowledgeondemand.net`, see
[vcf-deployment-reference.md](vcf-deployment-reference.md)), and all three are wired
into opencode as MCP servers. `vmware-nsx-security` was skipped (NSX networking is not
in use). Nothing here can change the lab: the account is read-only and `vmware-monitor`
has no write code.

## What this is

`github.com/orgs/vmware-skills` — community Claude Code skills + **standard stdio MCP
servers** + CLIs for live VMware ops, by a Broadcom engineer (Wei Zhou), MIT on the repos
we use. Not an official Broadcom product. vSphere 9.1 is supported (pyvmomi `<10.0` over
SOAP). It is the *live* layer, complementary to the local `vcfspec` MCP (spec reference,
no network calls).

## The one safety rule

**The real boundary is the vCenter account, not the skill's policy layer.**
`vmware-policy` (deny rules + audit) is library-level, has a `VMWARE_POLICY_DISABLED=1`
bypass and a no-op `change_limits`. Treat it as audit and convenience; vCenter RBAC is
the gate.

## As deployed

| Item | Value |
|---|---|
| Host | CT 190 `lab-svc` (10.50.10.100 on `vmbrlab`). Reaches the lab's 172.16.x networks through the Proxmox host's route `172.16.0.0/16 via 192.168.6.11`. `/etc/hosts` has `172.16.10.129 vcsa.lab.knowledgeondemand.net` (lab-svc's resolver doesn't know the lab zone). Rootfs grown to 28 GB — three venvs plus collectors need ~2 GB |
| Target | `lab-vcsa` → `vcsa.lab.knowledgeondemand.net:443`, `type: vcenter`, `environment: lab`, `verify_ssl: false` (cert is from `knowledgeondemand-LabRoot-CA`; set `true` once lab-svc trusts that CA) |
| Account | **`svc-vmware-ro@vsphere.local`** — local SSO user (survives AD/dns01 outages), built-in **Read-only** role via Global Permissions, propagated. `access-info` confirms only `System.Read` + `System.View`. Set its password non-expiring, or SSO's default policy expires it in ~90 days |
| Packages | `vmware-monitor==2.4.2` → `/opt/vmware-venv`; `vmware-storage==2.4.2` → `/opt/vmware-venv-storage`; `vmware-harden[collectors]==1.16.0` → `/opt/vmware-venv-harden`. One venv per tool (what `uv tool install` would do). `vmware-policy` 1.24.2, pyvmomi 9.1.1.0 |
| Config | `~/.vmware-monitor/`, `~/.vmware-storage/`, `~/.vmware-aiops/` — each a `config.yaml` + `.env` (`chmod 600`, password var `VMWARE_LAB_VCSA_PASSWORD`, auto-rewritten to `b64:` on first load) |
| Audit | `~/.vmware/audit.db` (vmware-policy) |

### Install method actually used

lab-svc is minimal Debian 12: no `uv`, no `curl`, no `python3-venv`/`pip`, and the
`astral.sh` installer returned 403. So each tool went into a venv bootstrapped with
`get-pip.py` (only PyPI access needed):

```bash
python3 -m venv --without-pip /opt/vmware-venv-<tool>
python3 -c 'import urllib.request; urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py","/tmp/get-pip.py")'
/opt/vmware-venv-<tool>/bin/python /tmp/get-pip.py -q
/opt/vmware-venv-<tool>/bin/pip install -q <package>==<version>
```

`<tool> init` is interactive only, so the config was written by hand. Schema
(`config.example.yaml` in each repo):

```yaml
default_target: lab-vcsa
targets:
  - name: lab-vcsa
    host: vcsa.lab.knowledgeondemand.net
    port: 443
    username: svc-vmware-ro@vsphere.local
    type: vcenter
    environment: lab
    verify_ssl: false
scanner:
  enabled: false        # vmware-monitor only: no background daemon / webhooks
```

**`vmware-harden` reads its scan targets from vmware-aiops's config**
(`/root/.vmware-aiops/config.yaml`), not its own — that file had to exist (same target).
Installing `[collectors]` pulls aiops/storage/nsx-security as read libraries only; no
aiops MCP server is configured or run.

Verify each with `<tool> doctor` (all checks green, including "vSphere authentication").

## Useful commands

Run inside CT 190 (`pct exec 190 -- /opt/vmware-venv*/bin/…`):

```bash
vmware-monitor summary                         # cluster health, top issues
vmware-monitor health alarms
vmware-monitor access-info                     # account + which tools its privileges allow
vmware-monitor memory tiering                  # DRAM/NVMe per host (the tier-drive area)
vmware-monitor inventory hosts|vms|datastores|networks|clusters
vmware-monitor investigate host <fqdn> [--html]
vmware-storage vsan health lab01-cluster-001   # vsan subcommands REQUIRE the cluster name
vmware-storage vsan capacity lab01-cluster-001
vmware-storage vsan efficiency lab01-cluster-001
vmware-harden scan --target lab-vcsa --baseline vsphere-scg-v9-subset
vmware-harden report
vmware-harden drift                            # diff against the previous scan snapshot
```

`vmware-harden baseline list` names the 10 built-in baselines (`vsphere-scg-v9-subset`,
`cis-vmware-esxi-9.0-subset`, `vsphere-stig-v9-subset` — experimental on 9.x — and
others). There is no `baseline show`: rule text lives in the package's
`baselines/builtin/*.yaml`, and `vsphere-scg-v9-subset` simply `extends` the v8 subset.

## opencode wiring

All three are `type: local` stdio servers in `~/.config/opencode/opencode.json` (and the
identical `config.json`). opencode on the workstation spawns each one on CT 190 over the
existing key-based SSH to the Proxmox host:

```json
"vmware-monitor": {
  "type": "local",
  "command": ["ssh", "-T", "-o", "BatchMode=yes", "root@192.168.6.175",
              "pct exec 190 -- /opt/vmware-venv/bin/vmware-monitor mcp"]
}
```

(`vmware-storage` and `vmware-harden` are the same with their own venv paths.) Check with
`opencode mcp list` — all three should say `connected`. First call per session pays ~1–2 s
of SSH + Python start-up.

## Baseline results (2026-10-22)

- `summary`: 3/3 hosts, HA + DRS on; overall CRITICAL from the "ESXi Certificate Mode is
  set to custom" alarm — benign (Broadcom KB 417983, raised when VCF Operations replaces
  host certs). Reset To Green in the vSphere Client → overall WARN.
- `vsan`: all health groups green except "Online health (Disabled)" (CEIP off); 11.2 TB,
  compression on / dedup off (ESA).
- `memory tiering`: software tiering, 1:1 DRAM:NVMe on all three hosts.
- `harden` SCG v9: 37 violations — host Secure Boot off (expected, by design), VM Secure
  Boot off on most VMs, VMware Tools not running on 7, hardware version < 19 on 6.
  Encryption rules and host TPM/VLAN/MAC checks come back "unknown": the collectors don't
  gather those fields or the read-only account can't read them.

## Gotchas from the rollout

- Ship scripts into the container as base64 (`echo <b64> | base64 -d | bash`); nested
  ssh/pct/heredoc quoting breaks, and `(` in a remote command string is a parse error.
- Globs in `pct exec 190 -- du /path/*` expand on the **host**, not in the container —
  wrap in `bash -c '…'`.
- CT 190 ran out of disk once (the audit DB and the `.env` re-encode failed with
  `No space left on device`); watch `df -h /`.
- The read-only account cannot read cert details (`infra certs` shows "unknown"),
  `Global.Diagnostics`-gated host logs, or anything in SDDC Manager. Reusing it against
  other appliances needs a deliberate decision.
- License status returns 403 for the same reason.

## Not deployed, and why

- **`vmware-nsx-security`** — NSX networking not in use.
- **`vmware-aria` / `vmware-log-insight`** — require the VCF Operations / Log Insight
  appliances' own APIs and credentials; not set up.
- **`vmware-aiops` (as a server) / `vmware-pilot`** — mutating; out of scope for a
  read-only integration.
- **NiranEC77/vcf-mcp** (whole-fleet VCF API MCP, community, no licence) — evaluated,
  not deployed. It has **no write gate**: anything the credentials allow executes. If
  adopted, run it read-only (HTTP mode with only `VCF_READ_TOKEN`, or read-only accounts
  per appliance).

## Do NOT

- Rely on `vmware-policy` as the safety boundary — the account is.
- Give these tools a writable account without deciding to; the community tools' write
  paths had only "first real-vSphere run" hardening as of mid-October 2026.
- Do certificate work through them — certificates are VCF Operations' job.
