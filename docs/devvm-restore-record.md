# Developer VM restore record

Captured 2026-09-26, before the planned VCF rebuild. These three VMs belong to an
external developer and must survive the rebuild. Everything needed to recreate
them faithfully is here.

**Why this file exists:** the rebuild destroys the vSAN datastore and the VDS that
carries their networking. The guest disks survive an export/import, but MAC
addresses and portgroups do not survive automatically, and cloudflared's health
depends on egress working after the restore.

## Current state

All three were storage-migrated off `vsan-lab01` to host-local VMFS on
2026-09-26. Each runs on a distinct host, so each sits on that host's own
datastore and is pinned there (no vMotion).

| VM | Host | Datastore | VMX path |
| --- | --- | --- | --- |
| devvm01 | hyp01 | `hyp01-local` | `[hyp01-local] devvm01/devvm01.vmx` |
| devvm02 | hyp02 | `hyp02-local` | `[hyp02-local] devvm02/devvm02.vmx` |
| devvm03 | hyp03 | `hyp03-local` | `[hyp03-local] devvm03/devvm03.vmx` |

Local datastores are **on the 1 TB boot device** (CT1000P310SSD2), 803.2 GB
usable. Actual per-VM footprint is ~98 GB against a 200 GB provisioned disk.
This matters: `vcfspec` emits `install --disk=... --overwritevmfs`, so a reimage
**destroys these datastores**. The VMs must be exported off-cluster first.

## Restore-critical configuration

| | devvm01 | devvm02 | devvm03 |
| --- | --- | --- | --- |
| MAC | `00:50:56:bd:89:9d` | `00:50:56:bd:fb:b1` | `00:50:56:bd:58:53` |
| IP (in guest) | 172.16.70.10/24 | 172.16.71.10/24 | 172.16.72.10/24 |
| Gateway | 172.16.70.1 | 172.16.71.1 | 172.16.72.1 |
| Portgroup | `dev-vm1-vlan70` | `dev-vm2-vlan71` | `dev-vm3-vlan72` |
| VLAN | 70 | 71 | 72 |
| Tunnel hostname | devvm01.knowledgeondemand.net | devvm02… | devvm03… |

Common to all three: 8 vCPU, 49152 MB RAM, BIOS firmware (**not** EFI),
`debian12_64Guest`, one 209,715,200 KB disk, vmxnet3 NIC, pvscsi controller.

UUIDs, should they be needed:

```
devvm01  uuid 423d309a-971f-d6ea-859a-0d20101514b1  instance 503d6ea1-86a3-b091-fd59-807be0235173
devvm02  uuid 423d4690-dac7-4315-a287-4ba942c83a62  instance 503d7d06-3c4f-4279-2108-4371ed8f9122
devvm03  uuid 423d9359-c29f-7d44-08c2-6dc0ba9db9eb  instance 503d93fa-38e4-b9ac-b138-f60be13c14fb
```

## What does and does not need preserving

**Lives on the guest disk, survives any export/import** — the `factory` and
`viktor` accounts and their authorized_keys, both Unix passwords locked,
`PasswordAuthentication no`, the cloudflared systemd unit and its
`EnvironmentFile` holding the per-VM tunnel token, and the static IP in
`/etc/netplan/50-cloud-init.yaml`.

**Does NOT survive, must be recreated** — the VDS
(`lab01-cluster-001-vds-001`) and the three dev portgroups on it; VM
registration in vCenter; the MAC, unless set explicitly on import.

**Unaffected by the rebuild entirely** — the CRS309 VLANs 70/71/72 and their
gateways, the deny-all-private isolation rules, the `devvm-tester-filecopy`
accepts, and all three Cloudflare tunnels and Access applications. cloudflared
dials outbound, so it reconnects on its own once egress works.

## cloudflared: what actually breaks it

Not the MAC, and not the IP. The tunnel is outbound-only and authenticates with
a token stored in the guest. It breaks only if the VM cannot reach the internet,
which after a rebuild means one of:

- the portgroup does not exist, or carries the wrong VLAN tag
- the VLAN is not tagged on the uplink toward the CRS309
- the guest's gateway (`172.16.7X.1`) is not answering on the CRS309

Verify by tunnel health at Cloudflare, not by pinging the VM — nothing may ping
it by design.

## Seed ISOs

Each VM **still has** a cloud-init NoCloud seed ISO attached from `vsan-lab01`
(`seed3.iso` on devvm01, `seed2.iso` on the others). **These contain Cloudflare
tunnel tokens.** They must be ejected for two reasons: the tokens should not sit
on a shared datastore, and a VM referencing a missing ISO can stall at power-on
once vSAN is gone.

**Eject them while the VMs are powered off, as step 1 of the restore.** An eject
attempt against the running guests on 2026-09-26 hung: a running Linux guest can
lock the CD-ROM tray, and vSphere then waits rather than failing. Do not fight
it on a live VM; the power-off before export is the natural moment.

Ejecting them does not change the guest. cloud-init already applied its config
and will simply find no datasource on the next boot; the netplan file and
accounts persist on disk. **Do not re-attach a seed ISO with a new
`instance-id`** during restore — that makes cloud-init treat the VM as new and
re-run, which is how an earlier round of this work rewrote guest state
unintentionally.

## Restore procedure

1. **Before the rebuild**: power off, eject the seed ISOs (see above), then
   export each VM off-cluster. OVF writes only allocated blocks, so expect
   ~98 GB each rather than 200 GB. The destination must not be this cluster.
2. Reimage the hosts and complete VCF bring-up with the corrected spec.
3. Recreate the three portgroups on the new VDS with VLANs 70/71/72.
4. Import each VM onto its host's local datastore.
5. **Set the MAC explicitly** from the table above, with address type static —
   an import otherwise assigns a fresh MAC.
6. Power on. Do not attach any seed ISO.
7. Verify: tunnel healthy at Cloudflare; `ssh` via the Access ProxyCommand
   reaches sshd; `factory` has no sudo; `viktor` has NOPASSWD sudo.

## Known outstanding item

The guest filesystems are 2.8 GB on a 200 GB disk — `growpart` never ran
because `cloud-guest-utils` is absent from the Debian generic image. The two
commands to fix it, run as `viktor`:

```bash
sudo apt-get update && sudo apt-get install -y cloud-guest-utils
sudo growpart /dev/sda 1 && sudo resize2fs /dev/sda1
```

Worth doing after the restore rather than before, since the export only carries
allocated blocks either way.
