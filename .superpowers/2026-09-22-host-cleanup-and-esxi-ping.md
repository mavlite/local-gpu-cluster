# Proxmox host cleanup + ESXI_PING investigation — 2026-09-22

## Cleanup (authorised, evidence-led)
tank-lxc 85.58% -> 56.24%; tank-backups 24.64% -> 6.75%. ~264 GB reclaimed.

| Removed | Size | Evidence it was not in use |
|---|---|---|
| `tank/models@pre-coder-swap-20260525-1619` | 46 GB | 4 months old; the swap it guarded was superseded twice (Devstral, Coder-Next, qwen3.8). No holds, no clones; `zfs destroy -nv` confirmed reclaim before acting. |
| VM 172 `tester` | ~204 GB | Stopped; config untouched since 12 Sep; its `feat/tester-vm` branch was fully merged. 200 GB zvol was thick-provisioned holding 688 MB. Config saved to `/root/decommissioned/`. |
| 3 WS2022-auto ISOs | 14 GB | Grepped every qemu-server and lxc config: the only ISOs any guest references are SERVER_EVAL and virtio-win-0.1.266, both kept for se-qa. |

**Kept, deliberately:** VM 170 `se-qa` (started/stopped 2 days ago -- evidence of
live use, contradicting "stopped = disposable"), LXC 157 `redteam-agent`, LXC 158
`aider-bench` (both deliberate infrastructure, stopped-when-idle), the Aug-19
backup set (single coherent restore point), all nested ESXi.

**Correction to my own first read:** `pvesm` reported tank-lxc at 85.58% but
`zpool list` showed 40% CAP / 549 GB free -- the Proxmox figure is logical usage
before compression. The situation was less urgent than the dashboard implied.

## ESXI_PING root cause — a duplicate-address conflict on vmbrlab
`ESXI_PING.error` failed for all three hosts while ICMP, SSH and the SDK all
answered from the lab network. Recorded as "appliance-side, unexplained".

**Found:** LXC 160 `lab-targets` was still running `labtarget@10.50.10.{11,12,13}
.service` -- "Lab ESXi stand-in listener" -- holding the SAME three addresses as
the real nested ESXi hosts (`bc:24:11:00:0a:01/02/03`) on the SAME bridge.

The stand-ins were built on 2026-09-20 for hosts that did not exist yet. The
hosts now exist. Two devices claimed each address; which one a given host
reached depended on whose ARP entry won. That is exactly why it looked
"appliance-side": we tested from the Proxmox host and lab-runner, whose caches
pointed at real ESXi, while the Installer at 10.50.10.22 had its own cache.

**Fix:** `pct stop 160` (reversible; config saved). After an ARP flush all three
addresses resolve unambiguously to the real hosts -- ping ok, and TLS presents
`CN=esx01/02/03.vcf.lab.knowledgeondemand.net`, which incidentally confirms the
stage-1 `%firstboot` certificate generation worked.

**Still to confirm by running:** whether this alone clears ESXI_PING. Re-submitted
`/tmp/sddcspec2.json` (the IP-pool-corrected spec, short hostnames) so the only
changed variable is the conflict.

If lab-targets is wanted again for probe failure-injection, re-address it off
.11/.12/.13 first -- do not restart it as-is.

## Offline depot — built from what was already downloaded
`PUT /v1/system/settings/depot {"depotConfiguration":{"isOfflineDepot":true,
"url":"http://10.50.10.1:8080"}}` -> `DEPOT_CONNECTION_SUCCESSFUL`.
Served from `/tank/vcf-depot` by a `vcf-depot.service` unit bound to the lab
bridge. The content is the 7.1 MB `metadata-*.zip` that was already in
`~/Downloads/VCF Automation` -- no Broadcom portal access needed for the
catalogue half.

Measured delta: `/v1/releases` 404 LCM_MANIFEST_NOT_FOUND -> **19 releases**;
`/v1/bundles` empty -> **281 bundles**. Five errors cleared
(COMPATIBLE_RELEASES_NOT_FOUND, FAILED_TO_VALIDATE_COMPONENT_VERSIONS_NO_RELEASE,
FAILED_TO_VALIDATE_COMPONENT_BINARIES_NULL_VERSION,
FAILED_TO_VALIDATE_INTEROPERABILITY_COMPONENT_NULL_VERSION,
ESXI_VERSION_VALIDATION_STATUS) and replaced by ONE precise error:
`FAILED_TO_RETRIEVE_COMPONENT_BINARY` for VCF services runtime
9.1.1.0.25714471. Note the downloaded fleet-depot tgz is build 25713941 --
a different build, so it does not substitute.

**The 9.1 Installer UI cannot configure an HTTP offline depot at all; the API
is the only route.**

## The self-collision is worth THREE failed checks, not three errors
`DNS Resolution` FAILED and `Network Configuration` FAILED are caused ENTIRELY
by the spec asking the Installer to deploy `sddcm01` at `10.50.10.22`, which is
the Installer itself:
- DNS Resolution      <- DUPLICATE_FQDN + DUPLICATE_IP_ADDRESS
- Network Configuration <- IP_NOT_IN_USE
Nothing is wrong with lab DNS or the lab network. Verified separately that
lab-dns answers correctly for every name.

Fix is `SddcManagerSpec.useExistingDeployment: true`, but the Installer then
demands more: a bare attempt returned
`QUICK_START_VALIDATION_FAILED - Empty local user password specified in SDDC
Manager specification`. The schema shows why: `localUserPassword`,
`sshPassword` and `sslThumbprint` ("Need to be populated when using existing")
all exist on SddcManagerSpec, and our renderer emits only hostname +
rootPassword. **That is a real renderer gap**, and the inventory cannot express
any of it.

## Capacity shortfalls are WARNINGS, not errors
CAPACITY_NUMBER_OF_CORES / MEMORY / STORAGE are all `.warning`. They do not
block. The hard blockers are vSAN (no eligible disks, not HCL compatible) and
the nested NIC limits -- which is why a vSAN-free VVF spec is likely worth more
than chasing nested hardware compliance.

## vSAN: the failures were lab provisioning, not certification
Researched Lam's vSAN ESA mock HW VIB
(github.com/lamw/nested-vsan-esa-mock-hw-vib). Install is
`esxcli software acceptance set --level CommunitySupported`,
`esxcli software vib install -v ... --no-sig-check`,
`/etc/init.d/vsanmgmtd restart`; no reboot. It ships a stress.json that hides
the storage controller so HCL pre-checks pass.

**Two blockers, and the second is the real story:**
1. It lists ESXi **7.x/8.x only** -- not 9.x. We run 9.1.1.0.25714478.
2. It requires an **NVMe controller**. Every nested VM here is SATA.

Then checking the VMs directly:
| host | disks |
|---|---|
| esx01 | sata0 64G boot + sata1 400G data -> VMFS `lab-ds`, 403G free |
| esx02 | sata0 64G boot ONLY -- no VMFS datastore at all |
| esx03 | sata0 64G boot ONLY -- no VMFS datastore at all |

So `NO_STORAGE_POOL_ELIGIBLE_DISKS_FOR_VSAN_ESA on esx03` is not an HCL
artefact: **esx03 has no data disk**. And esx01's data disk is SATA, which is
not ESA-eligible -- vSAN ESA wants NVMe. Two independent provisioning causes I
had been treating as one certification problem. The VIB would have fixed
neither.

Also tested and FAILED to help: `VsanEsaConfig.skipHclAutoDiskClaim` and
`SddcSpec.skipGatewayPingValidation`. Identical 7/7/3. I misread the field
name -- it is "skip HCL-auto-disk-claim" (disk claim behaviour), not "skip HCL".
The description says so: "Whether to enable or disable vSAN auto disk claim".
Lam's "native 9.1.1 bypass" is therefore something other than these fields.

Lam's note that using the API downgrades the 10GbE check to a warning was
VCF 5.2.1 behaviour; on 9.1.1 via the API `VMNICS_MIN_SPEED` is still `.error`.

## Capacity: our per-component table is RIGHT, the profile is wrong
`useExistingDeployment` dropped the requirement by exactly
**4 vCPU / 16 GB / 914 GB** -- byte-for-byte our `("SDDC Manager", 4, 16, 914)`
row, because it is no longer being deployed. So the component figures are
accurate; the 219 vs 139 gap is which components are counted and at what size
(the VCFMS/VSP profile), exactly as the operator suspected when asking about
the 9.1.1 non-HA compact design.
Installer totals: full VCF 56/139/5149; with existing SDDC Manager 52/123/4235.

## VVF validation: 1 FAILED + 16 SKIPPED  ->  7 SUCCEEDED / 2 FAILED / 4 WARNING

Iteration history, each round clearing its errors and surfacing the next:
| spec | change | result |
|---|---|---|
| VCF baseline | (ESXI_PING blocked everything) | 1 FAILED + 16 SKIPPED |
| +depot | offline depot metadata | 5 version errors -> 1 precise binary error |
| +useExistingDeployment | SDDC Manager import | 4 SUCCEEDED -> 7 SUCCEEDED |
| VVF v1 | workflowType VVF, no NSX/vSAN | rejected: storage + Operations required |
| VVF v2 | +Operations, existingDatastoreName | rejected: that is not storage; License Server required |
| VVF v3 | +NFS storage, +License Server | rejected: NFS needs an NFS network |
| VVF v4 | +NFS networkSpec (+host alias 10.50.12.1) | 6 SUCCEEDED / 5 FAILED |
| VVF v5 | **remove sddcManagerSpec** | 7 SUCCEEDED / 4 FAILED -- DNS Resolution passes |
| NIC work | vmxnet3 x2 on all three hosts | DVS_SPECS_VMNICS_MISSING cleared |
| VVF v6 | vMotion MTU 9000->1500, unmount NFS | **7 SUCCEEDED / 2 FAILED / 4 WARNING** |

### The two remaining failures are environmental, not spec defects
1. `FAILED_TO_RETRIEVE_COMPONENT_BINARY` -- VCF services runtime
   9.1.1.0.25714471. Needs the VCF Download Tool and a Broadcom entitlement;
   the depot's catalogue half is done, the binary half is not.
2. `VMNICS_MIN_SPEED` -- vmnic0 at 1000 MB/s. **Not fixable in a nested QEMU
   lab**: QEMU's vmxnet3 advertises 1 Gb, `esxcli network nic set -S 10000`
   does not override it for a virtual NIC, and no QEMU model presents 10 Gb to
   ESXi. Disappears on the physical hosts with their X520s.

### Gotchas worth keeping
- **NFS catch-22**: pre-mounting gives `DATASTORE_ALREADY_EXISTS`, not
  pre-mounting gives `NFS_DATASTORE_NOT_FOUND` on an earlier round. The
  Installer wants to CREATE it -- leave it unmounted.
- **NFS mounts do not survive an ESXi reboot here.** All three were silently
  gone after the NIC work.
- **VCF disables SSH on the ESXi hosts during validation**, every run. Re-enable
  via the vSphere SDK before any host work.
- vMotion declared MTU 9000 on a 1500 bridge made `vmkping` fail by
  construction; the error only became visible (`ESXI_SINGLE_PORTGROUP_MTU`)
  once the earlier blockers cleared.
- `ESXI_SERVICE_POLICY` (esx02 ntpd) was STALE, not wrong -- it cleared on
  reboot. Correctly not "fixed" in firstboot; the live hosts said policy was on.

## Depot moved to the TrueNAS NAS, Proxmox scaffolding removed (2026-09-23)
The Proxmox `vcf-depot.service` on 10.50.10.1:8080 was always scaffolding. Torn
down: unit disabled and deleted, `/tank/vcf-depot` (20 GB) removed, nothing on
8080. The real depot is the operator's TrueNAS box.

**TrueNAS depot** `\truenas.knowledgeondemand.net\VCF-Offline-Depot\`:
- Structure is `<share>/PROD/PROD/...` -- the OUTER `PROD` is the nginx html
  root mount, the inner one is the depot's PROD namespace, so URLs are
  `/PROD/COMP/...`. Served by an nginx container (TrueNAS app
  `vcf-offline-depot`) with **Basic Auth** per `conf/nginx.conf`.
- It held VCF **5.x-9.0.2 only** (manifest dated 2026-01-12, newest 9.0.2.0)
  and none of the six components VVF 9.1.1 needs. ~163 GB of existing content.
- Our 9.1.1 metadata is a strict SUPERSET -- 19 releases vs their 15, adding
  5.2.3/5.2.4/9.1.0/9.1.1 and retaining every 9.0.x they had. Replaced it after
  backing the original up to `PROD/PROD/_backup-metadata-20260923/`.
- Uploaded the six missing components into `PROD/COMP/<NAME>/`.

**The reachability trap:** the app was bound to `172.16.10.50:8080`, and that
address lives on a `vlan10` interface with a **/32** mask -- a single-host
address with no subnet, left from a previous lab. Nothing routes to it; the
Proxmox host sends it to the default gateway and gets nothing. The NAS's LAN
address is `br0 192.168.6.170/24`. Fix is to rebind the app to `0.0.0.0` or
`192.168.6.170`, making the depot `http://192.168.6.170:8080`.
An all-ports nmap of 192.168.6.170 found only 22/80/443/139/445/5357/8765 --
the depot simply was not on the LAN.

**Still open:** Basic Auth. Our depot config passed a bare URL with no
credentials; the API's `offlineAccount` field probably carries them.

## NAS depot live — 2026-09-23
Basic Auth removed (`auth_basic off` in `conf/nginx.conf`, original saved as
`nginx.conf.bak-20260923`) and the app rebound off the unroutable
`172.16.10.50/32` onto the LAN. Depot is now **http://192.168.6.170:8080**.

Verified: `200` on root/manifest/catalogue, **`206` on a ranged request**
(needed for resumable bundle downloads), Installer reports
`DEPOT_CONNECTION_SUCCESSFUL`, `/v1/releases` resolves 19 releases.

**Credentials in the URL are rejected.** `http://user:pass@host` fails with
"Request URI authority contains deprecated userinfo component". It is separate
`username`/`password` fields in `depotConfiguration`, or no auth at all. A
deliberately wrong password returned `DEPOT_INVALID_CREDENTIAL - 401`, which
usefully proved connectivity and path correctness before auth was removed.

Content: ~59 GB of 9.1.1 added beside the existing ~163 GB of 9.0.x.
**16/16 files SHA-256 verified** against their depot-manifests, **9/9 media
size-verified**. The SDDC Manager OVA had a " (1)" duplicate-download suffix
and was written under its canonical name.

## The real bottleneck is the appliance's nested storage, not the depot
| path | throughput |
|---|---|
| Proxmox host -> NAS depot | **116 MB/s** (200 MB in 1.8 s) |
| appliance writing the bundle | **~1.2-1.4 MB/s** |

~100x slower than the link. sddcm01 lives on esx01, whose datastore is a
virtual SATA disk on ZFS on one Ryzen host. VSP progress at time of writing:
appliance disk 24097 -> 28308 MB, i.e. **4.2 GB of 16.5 GB, ~2.5 h remaining**.

**Correction to an earlier call:** I described the first attempt as "stalled".
It was almost certainly just SLOW (~0.26 MB/s against the Proxmox depot vs
~1.4 MB/s against the NAS). The zero depot requests in that sample made
"stalled" defensible, but "slow" fits the fuller evidence. Measure bytes moved
over a long window before concluding a transfer is stuck.

Resume point: bundle id `d2382554-01eb-5164-a17f-7841ed8f0dff`; task id in
`/tmp/vsptask2` on the Proxmox host. When `downloadStatus` reaches a terminal
state, re-run validation with `/tmp/vvf6.json` and expect `Versions and
Bundles` to clear, leaving `VMNICS_MIN_SPEED` as the only failure.
