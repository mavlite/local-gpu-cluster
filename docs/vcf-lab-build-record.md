# VCF lab build record

How the physical VCF 9.1.1 lab (`lab01`, hosts hyp01–03) was built, programmatically,
from bare metal to a running VCF instance — and how to do it again. For *what* is
deployed (addresses, VMs, VLANs, current settings) see
[vcf-deployment-reference.md](vcf-deployment-reference.md).

Hosts were imaged unattended on 2026-09-24 and rebuilt after the 2026-09-26 bring-up
failures; bring-up completed late September 2026. Written 2026-10-22 from the repo,
the build notes, and the artifacts still served by the depot (read live that day).
**No credentials appear here** — every secret is a `${reference}` resolved from
`%USERPROFILE%\.vcflab\credentials.env` at publish time and shown as `<redacted>`.

---

## Pipeline at a glance

| Stage | What | Tooling |
|---|---|---|
| 0 | BIOS pass, once per chassis (no BMC) | by hand at the node |
| 1 | Network prep: VLANs, VLAN 99 provisioning PVID, DHCP + TFTP on the CRS309 | RouterOS, by hand |
| 2 | Image ESXi on all three hosts over the network | `vcfspec provision` → TrueNAS nginx → PXE → iPXE → HTTP install → kickstart |
| 3 | DNS / NTP / CA prerequisites on dns01 | `scripts/vcf-dns`, `scripts/vcf-ca` |
| 4 | Author and validate the deployment spec | `vcfspec validate --probe`, `vcfspec render` |
| 5 | Deploy the VCF Installer, configure the offline depot, download bundles, bring up | govc + Installer API |
| 6 | Post-bring-up: dev-VM port groups and restore, certificates, identity | `scripts/devvm-*.sh`, `scripts/vcf-ca` |
| Day 2 | Power control, DNS records, depot publishing | `scripts/vcf-lab`, `scripts/vcf-dns`, `scripts/vcf-depot` |

## The tooling: `vcfspec`

`vcf-spec-tools/` (Python 3.11+, `pip install -e ".[dev]"`, entry point `vcfspec`):

| Command | Purpose |
|---|---|
| `vcfspec validate <file> [--probe --allowlist CIDR …]` | Schema + rule checks on an inventory or SddcSpec; `--probe` adds forward/reverse DNS and TCP-443 checks. Exit 0 valid / 1 invalid / 2 usage |
| `vcfspec render <inventory>` | Emits the VCF Installer `SddcSpec` JSON, verified against the vendored 9.1.1.0 schema (unknown fields rejected) |
| `vcfspec provision <inventory> --esx-boot-cfg <ISO boot.cfg>` | Emits the per-host kickstarts, per-MAC `01-<mac>/boot.cfg` files and a publishing manifest. Writes nothing itself |

Key modules: `render.py` (spec), `provision.py` (kickstart head, `boot.cfg` rewrite,
manifest), `firstboot.py` (`%firstboot` body), `validate/probes.py`, `rules/`
(finding catalogue), `defaults/9.1.1.0.yaml`, vendored schema
`schemas/9.1.1.0/sddc-spec.schema.json`. `mcp_server.py` exposes the same functions
(schema/validate/render/explain/diff) as a no-network MCP server.

**Real inputs** — the only non-test lab files:
- `vcf-spec-tools/inventories/lab01-physical.yaml` — the physical lab inventory.
- `vcf-spec-tools/inventories/lab01-boot.ipxe` — the iPXE stage-2 script.

(`vcfspec/examples/lab-3-host.yaml` is the nested lab, not this one.)

The `vcf-spec-tools/README.md` "Known limits" section is out of date: `render.py`
does emit `vcfOperationsSpec`, `vcfOperationsCollectorSpec`, `vidbSpec`,
`licenseServerSpec` and `vspClusterSpec.fleetFqdn` (since 2026-09-26).
`fleetDepotSpec` is still not emitted. Datacenter, cluster and vDS specs are never
emitted — the Installer auto-generates them.

## Deployment spec parameters

From `lab01-physical.yaml` and `defaults/9.1.1.0.yaml`.

| Parameter | Value |
|---|---|
| Instance / sddcId | `lab01` |
| workflowType / CEIP / ESX thumbprint check | `VCF` / off / skipped |
| DNS subdomain / servers | `lab.knowledgeondemand.net` / 172.16.10.150 |
| NTP | 172.16.10.150 |
| Management | VLAN 10, 172.16.10.0/24, gw .1, **MTU 1500** (deliberate: management traffic leaves the 9000 fabric) |
| vMotion | VLAN 20, 172.16.20.0/24, MTU 9000, pool .20–.30 |
| vSAN | VLAN 30, 172.16.30.0/24, MTU 9000, pool .20–.30 |
| Host TEP | VLAN 40, pool `lab01-tep` 172.16.40.0/24, .20–.60, fabric MTU 9000 |
| NSX | VIP `nsx` .131, manager `nsxa` .132, size medium |
| Storage | `VSAN_ESA`, datastore `vsan-lab01`, FTT 1, `skipHclAutoDiskClaim: true` (native 9.1.1 field; the old mock-VIB HCL hacks are obsolete) |
| vCenter | `vcsa` .129, size small, SSO `vsphere.local` |
| SDDC Manager | `sddc-manager` .133 |
| VSP / VCFMS | platform `platform` .120, instance `lab01` .121, **`fleetFqdn` `fleet` .116**; node pool 172.16.10.160–.189; internal CIDR 198.18.0.0/15 |
| VCF Operations / collector | `ops` .122 (xsmall), `opscollector` .123 (small) |
| Identity Broker / License Server | `idb` .124 / `licsrv` .134 |
| VCF Automation | deliberately omitted |
| Credentials | `${esx_root}`, `${esx_root_hash}`, `${vcenter_root}`, `${sso_admin}`, `${nsx_admin}`, `${sddc_root}`, `${operations_admin}`, `${operations_root}`, `${operations_collector_root}` → `<redacted>` |

Not committed: the rendered SddcSpec JSON actually submitted, and the Installer's
validation output. Re-render from the inventory if you need it.

---

## Stage 0 — BIOS pass (once per chassis)

BIOS **1.17** first — flashing resets every setting, so flash, then configure, then
use Save/Load Profile to copy settings across the three nodes.

- UEFI mode, Network Stack + IPv4 PXE **on**, NIC first in boot order.
- **Secure Boot off, TPM off — permanently.** `%firstboot` is silently skipped under
  Secure Boot, and the fTPM is CRB-only, which ESXi cannot use.
- AC Loss Control = Always On.
- SVM (+ lock), IOMMU, NX, Above 4G Decoding, PSS Support, Core Performance Boost on;
  Re-Size BAR off; Global C-state Auto.
- Leave power limits at stock; do not undervolt; TjMax no lower than 78.
- **No UEFI HTTP Boot**: the options exist in the firmware but are permanently hidden.
  That is why the chain below goes through PXE and iPXE. (Older design notes describing
  native UEFI HTTP boot with DHCP option 67 = URL are wrong for this board.)

## Stage 1 — Network prep (CRS309)

- VLANs 10/20/30/40/50/60 (later 70–72), `mtu=9000 l2mtu=9196`, gateways `.1`.
- Host ports: **VLAN 99 untagged as PVID** (PXE firmware sends untagged frames), all
  other lab VLANs tagged. Originally every port had `pvid=1`, so firmware DHCP died in
  VLAN 1.
- DHCP on VLAN 99 with `next-server=<gateway on the client's own subnet>` and
  `boot-file-name=snponly.efi` (not raw options 66/67).
- `/ip tftp` serves iPXE **`snponly.efi`** (278 KiB, from Debian's `ipxe` package) built
  with an EMBED script that chains to the HTTP stage-2 script.
- RouterOS 7.20 traps: `/ip dhcp-server matcher` can't set options (hence the embedded
  script); `print where` / `find` silently match nothing — print unfiltered and `set`
  by `numbers=`; `/ping interface=` gives false negatives. Trust
  `/interface bridge host print`, the DHCP lease list and the TFTP hit counter.

**Not in the repo:** a RouterOS export of the VLAN/bridge/DHCP/TFTP/firewall
configuration, and the iPXE EMBED script source. Export the CRS309 config into the repo
when convenient.

## Stage 2 — Host imaging

### Boot chain

```
firmware PXE (untagged VLAN 99)
  └─ DHCP (CRS309) → TFTP snponly.efi (CRS309)           # iPXE, embedded script
       └─ HTTP  http://172.16.10.50:8080/esx/boot.ipxe   # stage 2
            ├─ probe 01-<mac>/boot.cfg  ── missing → exit → firmware boots local disk
            └─ chain bootx64.efi -c 01-<mac>/boot.cfg    # BOOTX64.EFI is the ESX loader
                 └─ kernelopt: static IP on VLAN 10, ks=http://…/esx/ks-<host>.cfg
                      └─ kickstart → install to the t10 boot disk → reboot → %firstboot
```

### What is served (TrueNAS nginx `http://172.16.10.50:8080/esx/`, read 2026-10-22)

| Path | Contents |
|---|---|
| `boot.ipxe` | Stage-2 iPXE script (Appendix B). Same as `inventories/lab01-boot.ipxe` |
| `bootx64.efi` | ESXi multiboot loader (from the ISO) |
| `esx-9.1.1.0/` | The unpacked ESXi 9.1.1.0 (build 25714478) ISO, **every file and folder renamed to lowercase** — otherwise mboot gets 404 on all ~130 modules |
| `ks-hyp01.cfg`, `ks-hyp02.cfg`, `ks-hyp03.cfg` | Kickstarts (Appendix A) |
| `_disarmed-01-98-b7-85-22-54-a4/` (hyp01), `…-22-65-3e/` (hyp02), `…-23-a4-ce/` (hyp03) | Per-MAC `boot.cfg` directories, **disarmed** (renamed) — Appendix C |
| `_ks-backup-20260926-131505/` | Kickstarts before the root-hash substitution (identical otherwise) |

**The install switch is the presence of `01-<mac>/boot.cfg`.** While it exists, every
PXE boot reinstalls that host (the kickstart ends in `reboot` and the NIC is first in
boot order). After a host fetches its kickstart, rename or delete its directory. All
three are currently disarmed. **To reimage a host:** re-render, publish its
kickstart, rename `_disarmed-01-<mac>` back to `01-<mac>` (or publish a fresh one),
power-cycle, then disarm again.

### Kickstart structure

Head (from `provision.py`), per host:

```
vmaccepteula
rootpw --iscrypted <redacted>     # ${esx_root_hash}: SHA-512 crypt, never a password
install --disk=/vmfs/devices/disks/t10.NVMe____CT1000P310SSD2…<serial> --overwritevmfs
network --bootproto=static --device=<vmnic0 MAC> --ip=<mgmt IP> --netmask=255.255.255.0
        --gateway=172.16.10.1 --nameserver=172.16.10.150 --hostname=<fqdn>
        --addvmportgroup=0 --vlanid=10
reboot
```

The generator refuses `--firstdisk` and runtime names (`mpx.*`, `vmhba*`) and rejects
boot / vSAN / tier device collisions — disks are addressed by stable `t10.` IDs.
`--overwritevsan` is emitted only when the boot disk is already claimed by vSAN (it
fails on fresh disks).

Per-host devices, read live 2026-10-22 (they match `lab01-physical.yaml`). All IDs are
under `/vmfs/devices/disks/`:

| Host | MAC / IP | Boot — CT1000P310 (fw V8CR001) | vSAN — CT4000P310 (fw V8CR010) | Tier — MZVLB512HAJQ (fw 5L2QEXA7) |
|---|---|---|---|---|
| hyp01 | 98:b7:85:22:54:a4 / .145 | `t10.NVMe____CT1000P310SSD2__________________________483D124F0175A000` | `t10.NVMe____CT4000P310SSD8__________________________9BF6F24E0175A000` | `t10.NVMe____SAMSUNG_MZVLB512HAJQ2D000L7______________478202718C382500` |
| hyp02 | 98:b7:85:22:65:3e / .143 | `t10.NVMe____CT1000P310SSD2__________________________25A8154F0175A000` | `t10.NVMe____CT4000P310SSD8__________________________C0B2F24E0175A000` | `t10.NVMe____SAMSUNG_MZVLB512HAJQ2D000L7______________97BBB38187382500` |
| hyp03 | 98:b7:85:23:a4:ce / .141 | `t10.NVMe____CT1000P310SSD2__________________________1EA609500175A000` | `t10.NVMe____CT4000P310SSD8__________________________5995F74E0175A000` | `t10.NVMe____SAMSUNG_MZVLB512HAJQ2D000L7______________3341B78187382500` |

hyp02's and hyp03's tier drives were replaced on 2026-10-03 (previously `…97EC007189382500`
and `…8AEC007189382500`). After any drive swap, update `memoryTieringDevice` in the
inventory before re-rendering. The hosts originally booted from a 59 GB USB stick
(`mpx.*`); remove it from the chassis if still fitted.

### `%firstboot` (busybox), in order

| # | Action | Why |
|---|---|---|
| 1 | Loop until `vim-cmd hostsvc/runtimeinfo` succeeds; enter maintenance mode | hostd isn't up when `%firstboot` starts; 9.1 tiering needs maintenance mode |
| 2 | `enable_ssh`, `start_ssh`, **`chkconfig SSH on`**; `SuppressShellWarning=1` | Only way in with no BMC. Without `chkconfig` the startup policy stays off and SSH is gone after the reboot. (VCF's own validation turns SSH off later anyway) |
| 3 | `esxcli system ntp set --enabled=yes --server=172.16.10.150` | Time source |
| 4 | Rename `datastore1` → `hypNN-local` | Stable, unique datastore names |
| 5 | `esxcli system coredump file set -s -e true` | PSOD evidence with no BMC |
| 6 | vSwitch0 + vmk0 MTU **1500** | Must match the management VLAN, not the 9000 fabric |
| 7 | `esxcli memtier enable -d <tier t10 id> -r 100` | 9.1 form; tier = 100% of DRAM (96/96) |
| 8 | Consumer-AMD workarounds: brand-string override in `/etc/vmware/config` (NSX Edge DPDK vendor check); `monitor_control.disable_apichv ="TRUE"` (tiered VMs won't power on without it); `entropySources=2`; `/VSAN/Vsan2ZdomCompZstd=0` (LZ4) | William Lam's VCF 9.1 lab workarounds |
| 9 | `esxcli system hostname set --fqdn=…`; `/sbin/generate-certificates` | A fresh host presents `CN=localhost.localdomain`; the Installer rejects it |
| 10 | Exit maintenance mode; reboot | VCF won't commission a host in maintenance mode |

### ⚠ The served kickstarts are an older render than the current generator

The kickstarts on the depot (and in its backup) were rendered before four
`firstboot.py` fixes made on 2026-09-26, and before the 2026-10-03 tier-drive
replacements. A host reimaged from the **served** files gets the old behaviour — and
hyp02/hyp03 would lose memory tiering. **Always re-render with `vcfspec provision`
before reimaging.**

| Item | Served kickstart | Current generator |
|---|---|---|
| **Tier device, hyp02/hyp03** | **the old, failed drives** (`…97EC…`, `…8AEC…`) — no longer fitted, so `memtier enable` would fail and the host would come up with no tiering | the replacement drives (`…97BBB…`, `…3341B…`) |
| ntpd startup policy | not set | `vim-cmd hostsvc/service/setpolicy ntpd on` |
| RDSEED mask for guests | absent | `cpuid.7.ebx = "-------------0------------------"` |
| Brand string | `"AMD EPYC 7945HX"` | `"AMD EPYC Ryzen 9 7945HX"` |
| Final reboot | `esxcli system shutdown reboot -d 10 …` | plain `reboot` |

The reboot form matters most: `esxcli system shutdown reboot` requires maintenance mode,
which step 10 has just exited, so it silently does nothing — hostd keeps serving the old
`localhost` certificate and bring-up fails at Datacenter Creation (this happened twice on
2026-09-26). Live check 2026-10-22: ntpd is on and running on all three hosts, so its
policy was set some other way; whether the RDSEED mask is present cannot be read with the
read-only account (assume not).

## Stage 3 — DNS, NTP and CA prerequisites

- **DNS** on dns01: every appliance needs A **and** PTR, lowercase, unique, and the IP must
  be *unassigned* at validation time (something already answering is a defect). A missing
  PTR caused one failed attempt. The appliance names reuse the previous VCF build's
  allocation (.129–.135), so create the pairs before bring-up. Manage with
  `scripts/vcf-dns/Set-VCFLabDnsRecord.ps1` (dry run unless `-Apply`).
- **NTP**: dns01 serves 123/udp. (The original plan of DNS/NTP on the CRS309 or a separate
  appliance was dropped.)
- **CA** (`scripts/vcf-ca/`, design `docs/superpowers/specs/2026-09-29-lab-certificate-authority-design.md`):
  Enterprise Root on dns01; `CAPolicy.inf` with `LoadDefaultTemplates=0`; CRL period 52
  weeks; CDP/AIA rewritten to `http://pki.knowledgeondemand.net/CertEnroll/` with
  registry flags **10** (CRL) and **2** (CA cert) — a fresh install writes flag 0 and
  issued certs then carry only `ldap:///`, which VCF appliances can't follow. Verify on an
  issued certificate with `Test-CaRevocationEndpoints.ps1`. Web enrolment Basic auth over
  TLS. Before any rollback-risky change, `New-Dns01RollbackClone.ps1` makes a cold clone
  (dns01's host has tiering on, so snapshots are refused).

## Stage 4 — Spec

```
vcfspec validate vcf-spec-tools/inventories/lab01-physical.yaml --probe \
    --allowlist 172.16.10.0/24 --allowlist-domain lab.knowledgeondemand.net
vcfspec render  vcf-spec-tools/inventories/lab01-physical.yaml > lab01-sddc-spec.json
```

Hard-won rules now encoded in the generator/inventory:

- **`vspClusterSpec.fleetFqdn` is mandatory in practice** — the schema doesn't require it,
  but omitting it validates clean and then fails "Deploy Lifecycle Components" with
  `PUBLIC_LCM_COMPONENTS_DEPLOY_FLEET_LCM_FETCH_FAILED` (killed the 2026-09-25 attempt).
  `fleetLcmSpec.fqdn` is silently ignored.
- A new fleet needs `vcfOperationsSpec`, `vcfOperationsCollectorSpec`, `licenseServerSpec`
  and `vidbSpec`; omitted ones become size-only stubs.
- The VSP node pool must contain **no in-use address** — moved to .160–.189 after a failed
  attempt polluted .100–.115.
- vMotion and vSAN each need an IP pool with one address per host (.0/.255 ignored).
- `CAPACITY_NUMBER_OF_CORES_VALIDATION_INSUFFICIENT` (48 vs 56 cores) stays a warning.
- The `ESXI_PING` check is a TCP connect by FQDN, not ICMP.
- `.local` is barred only for VIDB / Automation / Supervisor FQDNs; the SSO domain
  `vsphere.local` is fine.

## Stage 5 — Installer, depot, bring-up

**Installer appliance:** `VCF-SDDC-Manager-Appliance-9.1.1.0` build 25713928, deployed
with govc onto the standalone host `mgmt` (172.16.10.21, `mgmt-datastore1`) — **never** on
a host VCF will commission, because bring-up migrates vmk0 and the uplinks to the vDS.
IP `installer` .135 (briefly DHCP .230 during first boot); removed after bring-up.

- govc import in **one** spec: `DiskProvisioning: thin` (default flat needs 914 GB) +
  `InjectOvfEnv: true` + `PowerOn: true` — doing import, inject and power-on separately
  bricks the appliance.
- 14 OVF properties, read from the artifact with `govc import.spec -hidden`;
  `vami.ip_address_version` must be exactly `IPv4`; passwords min 15 chars using only
  `@!#$%?^` specials; network label `Network 1`. Write JSON without a BOM.
- Ready when `GET /vcf-installer-ui/login` returns 200 (~7 min). Use `curl.exe -k` from
  PowerShell 5.1 (no TLS 1.3).

**Offline depot:** TrueNAS app `vcf-offline-depot` (nginx in Docker, Basic auth off),
`http://172.16.10.50:8080/`, layout `/PROD/COMP/<COMPONENT>/…` from
`productVersionCatalog.json`, published with `scripts/vcf-depot/vcfdepot.py`
(`audit` / `plan` / `publish --apply`, SHA-256 checked). Configure through the API only:

```
POST /v1/tokens                     (admin@local)
PUT  /v1/system/settings/depot      {"depotConfiguration":{"isOfflineDepot":true,"url":"http://172.16.10.50:8080"}}
```

Then assert `DEPOT_CONNECTION_SUCCESSFUL`, a non-empty `/v1/releases` (the 7 MB metadata
zip turns `LCM_MANIFEST_NOT_FOUND` into 19 releases / 281 bundles), and the nginx log
showing the fetch. **Bundles need an explicit trigger:**
`PATCH /v1/bundles/{id} {"bundleDownloadSpec":{"downloadNow":true}}` → poll
`/v1/tasks/{id}` (~95 GB over 21 bundles, ~25 min; 18/21 succeeded, the failures being
upgrade variants). After bring-up, SDDC Manager reads its depot from
`https://fleet.lab.knowledgeondemand.net/depot-service/content-gateway` (SNI-routed), and
in 9.1 `GET /v1/system/settings/depot` returns 410 — use `/v1/services-config`.

TrueNAS traps: its `vlan10` interface was once `/32` (replies left via the home router and
vanished) — keep it `/24`; Docker bridges claim 172.16.0–4.0/24 sequentially, so set
`default-address-pools` outside 172.16/16; never add a 172.16.0.0/16 route on TrueNAS.

**Bring-up:** `POST /v1/sddcs/validations`, then submit the spec. Before reusing hosts from
a previous attempt, wipe vSAN ESA leftovers per host: `esxcli vsan cluster leave` →
`esxcli vsan storagepool remove -d <dev>` → `partedUtil mklabel <dev> gpt`.

## Stage 6 — After bring-up

- `scripts/devvm-portgroups.sh` — finds the VCF vDS and creates `dev-vm1-vlan70`,
  `dev-vm2-vlan71`, `dev-vm3-vlan72` (8 ports each), verifying the VLAN IDs.
  (The design planned NSX segments + DFW; plain VLAN port groups + CRS309 filters were
  built instead.)
- `scripts/devvm-restore.sh` — restores the dev VMs from `/tank/backups/devvm-export` onto
  `hypNN-local` with their recorded MACs. Both scripts read the vCenter password on stdin.
- Certificates: register the CA in VCF Operations, set `vpxd.certmgmt.mode=custom`, rotate
  in order NSX → VSP → ESXi (one host at a time) → vCenter → SDDC Manager. Each rotation
  must first `POST …/certificate-management/csrs`; verify on the CA's request table and the
  endpoint thumbprint, not on VCF's status. (Rotating ESXi certs raises the "ESXi
  Certificate Mode is set to custom" CRITICAL alarm — benign, Broadcom KB 417983; Reset To
  Green.)
- Identity: `Add-OpsIdentitySource.ps1` (POST, then PATCH back with the returned
  certificates) and `Import-OpsAdPrincipal.ps1`.
- Monitoring: local SSO `svc-vmware-ro@vsphere.local`, built-in Read-only at the root
  ([vmware-skills-operator-runbook.md](vmware-skills-operator-runbook.md)).

## Day 2

- **Power:** `scripts/vcf-lab/Start-VCFLab.ps1 -Mode Full|Minimal` /
  `Stop-VCFLab.ps1 [-IncludeHosts]` (PowerCLI, credentials from `credentials.env`).
  Power-on order: hosts (by hand) → vSAN has 3 members → vCenter → vpxa sanity → SDDC
  Manager → NSX → Ops → VSP control plane → VSP workers → licsrv → opscollector. Stop the
  VSP control plane before its workers; never hard-kill vCenter. Minimal mode refuses to
  start past 150 days without a license sync.
- **Known host issues:** consumer tier NVMe drives hang under burst I/O (vMotion, HA
  restarts) and need an AC power cycle — two failed/hung and hyp02/hyp03 drives were
  replaced 2026-10-03; hyp03 sometimes doesn't fully power off on a soft reboot. Hardening
  proposals (per-VM `sched.mem.enableTiering=FALSE` + reservation for critical VMs, NIOC
  vMotion cap, APST/ASPM off, 50% tier) are documented in
  `docs/superpowers/research/2026-10-12-vcf-lab-tiering-crash-hardening.md` and are **not
  applied**.

## Gaps

- No RouterOS export of the CRS309 (VLANs, bridge/PVID, DHCP, TFTP, firewall, NAT).
- The iPXE EMBED script and its build aren't in the repo.
- The submitted SddcSpec JSON, validation output and task IDs aren't committed.
- No script for the Installer deployment (procedure lives in this doc and the bring-up
  design `docs/superpowers/specs/2026-09-24-vcf-bringup-execution-design.md`).
- `mgmt` host addressing (172.16.10.21 vs 192.168.6.167) unconfirmed.
- SDDC Manager's view (network pools, domains, depot settings) was not read for this doc;
  the read-only account isn't granted there.

---

## Appendix A — rendered kickstart, hyp01 (as served, root hash redacted)

hyp02 and hyp03 differ only in the hostname, IP, MAC, boot-disk and tier-device IDs, and
datastore name (table above). Note this is the **pre-2026-09-26 render** (see the warning
above); the comments are the generator's own.

```
# ESX kickstart for hyp01.lab.knowledgeondemand.net
# Generated by vcfspec from the lab inventory. Do not hand-edit: regenerate.
#
# The root credential below is a ${reference} to a SHA-512 crypt hash, not a
# password. Substitute it when you publish this file. The file sits on an
# unauthenticated HTTP server that every host on the provisioning VLAN can
# read, so a hash is the only acceptable thing to put here.
vmaccepteula
rootpw --iscrypted <redacted>
install --disk=/vmfs/devices/disks/t10.NVMe____CT1000P310SSD2__________________________483D124F0175A000 --overwritevmfs
network --bootproto=static --device=98:b7:85:22:54:a4 --ip=172.16.10.145 --netmask=255.255.255.0 --gateway=172.16.10.1 --nameserver=172.16.10.150 --hostname=hyp01.lab.knowledgeondemand.net --addvmportgroup=0 --vlanid=10
reboot

%firstboot --interpreter=busybox
# NOTE: %firstboot is silently skipped when UEFI Secure Boot is enabled. None
# of the following runs, kickstart.log records the skip, and the host looks
# installed. Secure Boot must be OFF for the install and turned back on after.
#
# Enough to make the host commissionable by VCF; everything else is the
# Installer's job once it takes over.
#
# Structure ported from William Lam's vcf-91-in-box, config/KS-ESX01.CFG
# (https://github.com/lamw/vcf-91-in-box): wait for hostd, enter maintenance
# mode, do the work, leave maintenance mode, reboot. The earlier version of
# this block started issuing commands immediately, which races hostd -- every
# vim-cmd below fails if it wins that race, and on a host with no BMC a
# half-configured install is invisible until commissioning rejects it.

# hostd is not up when %firstboot starts. runtimeinfo is the cheapest call
# that keeps failing until it is, so this loop is the gate for everything
# after it. There is deliberately no attempt limit: a host that never brings
# hostd up is broken in a way a timeout cannot improve, and looping leaves the
# evidence in kickstart.log instead of continuing into a pile of failures.
while ! vim-cmd hostsvc/runtimeinfo > /dev/null 2>&1; do
  sleep 10
done

# Maintenance mode. Not merely tidy: ESX 9.1 stopped requiring a reboot to
# apply NVMe memory tiering and requires maintenance mode instead, so the
# tiering block below will not apply without this. Exited further down,
# before the reboot, because VCF refuses to commission a host that is in it.
esxcli system maintenanceMode set -e true

# SSH: the operator's only way in on a host with no BMC.
#
# NOT for VCF's benefit. An earlier version of this comment claimed SSH is
# "how VCF reaches it during commissioning"; that is wrong. Observed
# 2026-09-22: a VCF Installer validation run turned SSH off on all three
# hosts without rebooting them -- its Security Configuration check disables
# it. VCF reaches the host over 443/902 and the hostd SDK, which is also why
# a host with SSH shut still passes VCF's host-connect check.
#
# All three commands are needed, and the third is the one that was missing.
# enable_ssh/start_ssh change the RUNNING state; the service's startup POLICY
# is separate and defaults to off. This block is followed by a reboot, so
# without chkconfig the host comes back with SSH shut -- which is exactly what
# happened on all three lab hosts, and it fails VCF's host-connect check
# during commissioning while ICMP and the SDK still answer, so it does not
# look like an SSH problem. Verified on a live 9.1.1 host: the chkconfig line
# below is what makes `chkconfig --list` report the service on after a reboot.
vim-cmd hostsvc/enable_ssh
vim-cmd hostsvc/start_ssh
chkconfig SSH on
# Enabling SSH raises a permanent host warning. On one host that is cosmetic;
# on three it is three standing yellow alarms in vCenter that hide real ones.
# NOT enabling the ESXi Shell, which Lam's script does alongside this: with no
# BMC there is no remote console to use a local shell from, so it would be
# attack surface with no operator benefit here.
esxcli system settings advanced set -o /UserVars/SuppressShellWarning -i 1
esxcli system ntp set --enabled=yes --server=172.16.10.150

# Rename the local VMFS datastore. Every ESX install names it "datastore1", so
# three hosts arrive at vCenter with three datastores of the same name and
# vCenter disambiguates them by appending (1)/(2) in registration order --
# which is not stable, so the suffix a host gets is not either. Named from the
# host, not from storage.datastoreName: that field names the vSAN datastore
# this cluster is about to build, and reusing it here would collide with it.
vim-cmd hostsvc/datastore/rename datastore1 hyp01-local

# A coredump target. Without one a PSOD leaves nothing to read afterwards, and
# with no BMC the screen it painted is the only other copy -- which means a
# photograph, if anyone happened to be in the room. -s picks the best existing
# dump file rather than assuming a path the installer may not have used.
esxcli system coredump file set -s -e true

# vSwitch0 and vmk0 MTU, from networks.management.mtu (1500) and deliberately
# NOT from nsx.fabricMtu. vmk0 is the management vmkernel and must match the
# MTU the management VLAN actually carries; pinning it to the 9000 fabric MTU
# (as Lam's script hardcodes) would black-hole exactly the commissioning
# traffic this host exists to receive. nsx.fabricMtu describes the NSX
# transport fabric on the VDS that VCF builds during commissioning -- vSwitch0
# is replaced at that point and never carries a fabric frame.
esxcli network vswitch standard set -m 1500 -v vSwitch0
esxcli network ip interface set -i vmk0 -m 1500

# No "VM Network" portgroup VLAN line here, deliberately. Lam's script sets
# one because his install line asks the installer to create that portgroup;
# ours does not, so no such portgroup exists and the command would fail on
# every host. vmk0's own portgroup takes its VLAN from the `network`
# directive in the kickstart above, which is the only VLAN tag this host needs
# before VCF replaces vSwitch0 with a VDS. (The tag itself is deliberately not
# repeated in this comment: a generated file that merely mentions a VLAN
# option is indistinguishable, to a grep, from one that sets it.)

# NVMe memory tiering (hardware.memoryTieringDevice, hardware.memoryTieringGb).
# ESX 9.1 form: one command, applied in maintenance mode, no reboot. The
# three-command 9.0 sequence it replaced -- a kernel memory-tiering flag, a
# /Mem percentage, and a separate tier-device creation -- is NOT emitted, and
# neither is the `vmware` version probe that would have to choose between
# them. See the block comment above this template in firstboot.py.
#
# (None of the 9.0 command names appear above on purpose: a generated file
# that merely mentions a command is indistinguishable, to a grep, from one
# that runs it.)
#
# -r is the tier size as a percentage of DRAM, derived from the inventory
# rather than pinned at Lam's flat 100: 96 GB tier against 96 GB
# DRAM = 100%. The device is normally larger than the tier that ratio asks
# for, and the ratio is what decides how much of it gets used -- which is what
# makes hardware.memoryTieringGb load-bearing here rather than a number only
# the capacity rules ever read.
#
# This host's capacity plan depends on this line: without it the host boots
# with DRAM only, every capacity figure that counted tiered memory is wrong,
# and nothing says so until a VM fails to power on.
esxcli memtier enable -d /vmfs/devices/disks/t10.NVMe____SAMSUNG_MZVLB512HAJQ2D000L7______________478202718C382500 -r 100

# Consumer-AMD host workarounds (provisioning.hostWorkarounds: consumer-amd).
# William Lam, VCF 9.1 comprehensive ESX configuration workarounds for lab
# deployments:
# https://williamlam.com/2026/05/vcf-9-1-comprehensive-esx-configuration-workarounds-for-lab-deployments.html
#
# NSX Edge/VNA deployment fails on consumer AMD: a DPDK vendor check rejects
# the real Ryzen brand string. The value below exists to satisfy that check,
# not to describe this host honestly -- it is not this host's real CPU. No
# reboot needed.
echo 'cpuid.brandstring = "AMD EPYC 7945HX"' >> /etc/vmware/config
# NVMe memory tiering cannot power on VMs on AMD Ryzen without this. Needs a
# reboot, served by the one below.
echo 'monitor_control.disable_apichv ="TRUE"' >> /etc/vmware/config
# Zen 4/5 entropy collection is slow; widen the kernel's entropy source
# count. Needs a reboot, served by the one below.
esxcli system settings kernel set -s entropySources -v 2
# vSAN's default compression (Zstd) costs more CPU on this hardware than
# LZ4. No reboot needed.
esxcli system settings advanced set -o /VSAN/Vsan2ZdomCompZstd -i 0

# ESX generates its certificates before the hostname is configured, so a
# freshly installed host presents CN=localhost.localdomain. VCF Installer
# compares the certificate common name against the FQDN it is commissioning
# and rejects the host. Set the FQDN, regenerate, then reboot so hostd
# actually serves the new certificate.
esxcli system hostname set --fqdn=hyp01.lab.knowledgeondemand.net
/sbin/generate-certificates

# Out of maintenance mode before the reboot, not after: VCF will not
# commission a host that is in it, nothing below needs it, and a host that
# reboots while still in maintenance mode comes back still in it.
esxcli system maintenanceMode set -e false
esxcli system shutdown reboot -d 10 -r "hostname and certificate regenerated"
```

## Appendix B — `boot.ipxe` (stage 2)

```
#!ipxe

# Stage 2 of the ESX network install for the VCF lab.
# Reached from snponly.efi, which the CRS309 hands out over TFTP.
#
# The BD795i SE firmware has no UEFI HTTP Boot, so the loader must arrive
# over TFTP. But mboot resolves prefix= using the transport IT was loaded
# over, so an http:// prefix only works if the loader itself came over HTTP.
# iPXE is the bridge: tiny file over TFTP, then bootx64.efi and ~130 modules
# over HTTP from the depot. BOOTX64.EFI *is* the multiboot loader; there is
# no mboot.efi on the ISO despite the docs naming it that.
#
# PRESENCE OF 01-<mac>/boot.cfg IS THE INSTALL SWITCH. A host installs only
# while its directory exists; remove it and this script exits, handing back
# to firmware, which falls through to the local disk. That is what stops a
# kickstart ending in `reboot` from reinstalling forever with the NIC still
# first in boot order, and it is why %firstboot gets a chance to run.

set base http://172.16.10.50:8080/esx
set cfg ${base}/01-${net0/mac:hexhyp}/boot.cfg

echo
echo === VCF lab - ESX 9.1.1.0 network install ===
echo   MAC    : ${net0/mac}
echo   Config : ${cfg}
echo

# Probe for the per-MAC config BEFORE chaining. `chain` succeeds the moment
# bootx64.efi loads, so a missing -c config is NOT caught by || -- mboot
# takes control and dies with "Fatal error: 15 (Not found)" with iPXE long
# gone and no way back to the firmware. Probing first is the only thing that
# makes the fall-through work. Learned the hard way on hyp01, 2026-09-24.
imgfetch --name cfgprobe ${cfg} || goto nopending
imgfree cfgprobe

chain ${base}/bootx64.efi -c ${cfg} || goto nopending

:nopending
echo
echo No install pending for ${net0/mac}.
echo Handing back to firmware - it should boot the local disk next.
sleep 3
exit
```

## Appendix C — per-MAC `boot.cfg` (hyp01, currently disarmed)

`vcfspec provision` rewrites the ISO's `boot.cfg`: `prefix=` points at the HTTP ISO tree
and `kernelopt=` carries the kickstart URL and the installer's static network on VLAN 10.

```
bootstate=0
title=Loading ESXi installer
timeout=5
prefix=http://172.16.10.50:8080/esx/esx-9.1.1.0
kernel=b.b00
kernelopt=ks=http://172.16.10.50:8080/esx/ks-hyp01.cfg bootproto=static netdevice=98:b7:85:22:54:a4 ip=172.16.10.145 netmask=255.255.255.0 gateway=172.16.10.1 nameserver=172.16.10.150 vlanid=10
modules=jumpstrt.gz --- useropts.gz --- features.gz --- k.b00 --- … (the ISO's full module list, ~130 entries)
build=9.1.1-0.25714478
updated=0
```
