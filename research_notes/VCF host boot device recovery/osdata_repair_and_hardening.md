# ESXi OSDATA (VMFS-L) corruption: repair options, config recovery, and power-loss hardening

Method note: findings come from web search plus fetches of Broadcom KBs. Several Broadcom pages were read through a summarising fetch tool, so exact wording is paraphrase. Crucial's own pages returned "request rejected" to the fetcher, so no first-party P310 firmware data was obtained. About 20 tool calls used.

## Q1. What Broadcom says about OSDATA corruption, and whether reinstall is the only fix

### Takeaway
KB 370038 (ESXi 7.0/8.0) says reinstall is the only resolution for a corrupted OSDATA VMFS-L volume. VOMA check/dump may be run for diagnosis only, and VOMA fix/advfix is stated as unsupported. KB 452537 is about SAN datastores, not OSDATA, so it does not apply here. I found no Broadcom text blessing an installer "repair" or kickstart `upgrade` for this case.

### Cited Findings
- KB 370038 "OSDATA volume has been detected corrupted" (applies to vSphere ESXi 7.0 and 8.0, plus Telco Cloud Platform 5.1 and Telco Cloud Infrastructure 2.2). Symptom: vmkernel.log warns the volume "has been detected corrupted". A 7.x to 8.x upgrade can fail and datastores can appear missing. Resolution: reinstall ESXi, which recreates OSDATA; the article says OSDATA cannot be rebuilt otherwise. — [KB 370038](https://knowledge.broadcom.com/external/article/370038)
- KB 370038 diagnosis: find the device with `esxcfg-scsidevs -m | grep OSDATA`, then run VOMA check or dump against `/vmfs/devices/disks/naa.<id>:<partition>`. The fetched summary quotes the invocation as `voma -m vmfs -f check` (not `-m vmfsl`). Treat the module name as unverified; I did not find a `-m vmfsl` form documented anywhere. — [KB 370038](https://knowledge.broadcom.com/external/article/370038)
- KB 370038 says `voma -f fix` and `advfix` on OSDATA are unsupported and fail with "ERROR: Device Failed to close. Read-only file system." It also says a fix is "targeted for a future release". The article gives no repair path other than reinstall and says nothing on pre-reinstall config backup. — [KB 370038](https://knowledge.broadcom.com/external/article/370038)
- KB 452537 "VMFS volume severely corrupted following power outage" is explicitly for VMFS datastores (SAN, QLogic HBAs), not OSDATA. Cause: torn writes after power loss. Resolution: revert the LUN to a storage snapshot, or contact Broadcom Support to repair with VOMA on an unmounted datastore. Versions: ESXi 7.x and 8.x. — [KB 452537](https://knowledge.broadcom.com/external/article/452537/vmfs-volume-severely-corrupted-following.html)
- KB 392956 lists three installer options: "Upgrade ESXi, preserve VMFS datastore" (updates the install and retains current settings and the datastore), "Install ESXi, preserve VMFS datastore" (fresh install, keeps the datastore, and the KB does not say whether config carries over), and "Install ESXi, overwrite VMFS datastore" (wipes everything). Environment lists ESXi 6.5 to 8.x and ESX 9.x. The KB does not cover kickstart. — [KB 392956](https://knowledge.broadcom.com/external/article/392956)
- Community reports (lower weight) say the "Install, preserve VMFS" path keeps VMs but not host config, so VMs must be re-registered. — [Broadcom community thread](https://community.broadcom.com/vmware-cloud-foundation/discussion/lost-root-password-how-to-reset-it-how-to-preserve-existing-vms)
- KB 447384 warns that picking the datastore LUN as the install target can overwrite its VMFS. Confirm the installer targets the boot device. — [KB 447384](https://knowledge.broadcom.com/external/article/447384/vmfs-datastore-inaccessible-after-being.html)
- Jumpstart/ConfigStore: the exact string "Failed to connect to schemastore" returned no Broadcom hit. Related KBs found: KB 438104 and 446430 ("ALERT: Failed to backup ConfigStore", caused by concurrent backup processes locking the DB) and a ConfigStore ramdisk-full KB (fixed in 7.0.3 P08 / 8.0.1 P02 / 8.0 U3, per the search summary, KB number not captured). Neither addresses OSDATA-loss-induced schemastore failure. — [KB 438104](https://knowledge.broadcom.com/external/article/438104), [KB 446430](https://knowledge.broadcom.com/external/article/446430/esxi-alert-failed-to-backup-configstore.html)
- KB 454304 (ESXi 8.x): a full OSDATA blocks management tasks; check with `vdf -h` and clear logs/coredumps. Relevant because the root ramdisk filled in this incident. — [KB 454304](https://knowledge.broadcom.com/external/article/454304/troubleshooting-esxi-host-unresponsivene.html)

### Inferences
- Because the VMFS-L layer is flagged corrupt and fix is blocked, a supported in-place repair does not exist. Reinstall is the only Broadcom-documented fix.
- An "Upgrade, preserve VMFS" run on an already-corrupt OSDATA is unlikely to help: the upgrade path reads the old OSDATA and KB 370038 itself notes upgrades fail in this state. Treat it as untested and unsupported, not as a repair.
- "Install, preserve VMFS" is the closest supported analogue: it recreates the OSDATA partition and spares the local datastore (the same-disk datastore here), but config is not confirmed to carry over.
- Practical caution: before wiping, if the VMFS-L volume is still readable, copy `/vmfs/volumes/OSDATA-*` contents (logs, `/var/log` crash evidence) off the host.

### Gaps
- No Broadcom KB found for the exact Jumpstart "late-filesystems activation failed" plus "schemastore" combination.
- Whether `voma -m vmfsl` exists or works was not verified. KB text uses `-m vmfs`. Needs a test on the actual host, read-only.
- Whether "Install, preserve VMFS" on ESXi 9.1.1 keeps config: unverified.

## Q2. Recovering the host configuration (backup_config, state.tgz, ConfigStore)

### Takeaway
`backup_config`/`restore_config` is the supported route, but it needs a bundle taken before the failure, the same build, and a matching UUID (force flag overrides this unless TPM is enabled). If OSDATA is lost, the bootbank `state.tgz` is the only plausible config source, and the documented extraction procedure is old (ESXi 4.1 to 5.1 era). I found no ESXi 8/9 source confirming how fresh that copy is or that it restores cleanly.

### Cited Findings
- KB 313510: destination host must run the same build as the backup source (`vmware -vl`); host UUID must match unless forced (`restore_config 1`); from vSphere 7.0 U2, with TPM enabled the force override does not work and the same TPM is required. Backup: `vim-cmd hostsvc/firmware/sync_config`, then `vim-cmd hostsvc/firmware/backup_config`, which prints a download URL for `configBundle-<version>.tgz` (also stored in `/scratch/downloads/`). Restore: rename to `configBundle.tgz`, enter maintenance mode, copy to the host or a datastore, reboot (the KB says required), move to `/tmp/configBundle.tgz`, run `vim-cmd hostsvc/firmware/restore_config 0`; host reboots. VMs are not in the backup. Bootbank information is not in the backup. Not supported for DPU or Auto Deploy hosts. — [KB 313510](https://knowledge.broadcom.com/external/article/313510)
- The config-bundle path stores bundles under `/scratch/downloads/`, which lives on OSDATA by default, so it is lost with OSDATA. (Inference from KB 313510 plus scratch defaults.)
- KB 342062 (written for ESXi 4.1 to 5.1) describes copying `/bootbank/state.tgz` off the host, reinstalling, extracting the nested `local.tgz`, restoring files under `/`, then re-running `/sbin/auto-backup.sh`. The page returned 404 on direct fetch; content seen only through search snippets. — [KB 342062](https://knowledge.broadcom.com/external/article/342062/locally-restoring-an-esxi-configuration.html)
- KB 2043048 says auto-backup.sh puts files into `local.tgz`, which is stored in `state.tgz`. KB 2001780 says `state.tgz` is regenerated by a root cron entry running `/sbin/auto-backup.sh` and by a shutdown script running `/sbin/backup.sh 1`. Exact cron interval not captured. — [KB 2043048](https://kb.vmware.com/s/article/2043048), [KB 2001780](https://kb.vmware.com/kb/2001780)
- William Lam (2013, ESXi 5.x era) documents the simpler variant: copy `state.tgz` back into `/bootbank` and `reboot -f` so nothing newer overwrites it; a commenter reports editing the UUID in the bundle manifest allows cross-host restore (unofficial). — [williamlam.com 2013](https://williamlam.com/2013/02/how-to-backup-restore-free-esxi-host.html)
- A 2022 Broadcom community thread on ESXi 7.0 U3c reports a `local.tgz.VE` (encrypted) file rather than a plain `local.tgz`, complicating manual extraction on newer releases. — [community thread](https://community.broadcom.com/vmware-cloud-foundation/discussion/transferring-settings-from-existing-esx-host-to-fresh-esxi-70u3c-install-using-via-esxi-root-shell)
- KB 445039 (ESXi 7.x/8.x): a corrupted `state.tgz` causes boot failure "Fatal error: 15 (not found)". Shows that bootbank state.tgz is a real boot-time dependency and can itself be damaged by power loss. — [KB 445039](https://knowledge.broadcom.com/external/article/445039/esxi-failed-to-boot-up-with-error-fatal.html)
- Host Profiles are the KB 313510 alternative when keeping the same UUID does not matter.

### Inferences
- In ESXi 7+/8, config lives in ConfigStore (a database, with encryption `.VE` wrapping), so the old "extract local.tgz files into /" recipe may not capture it. Whether `state.tgz` now contains the ConfigStore DB is not confirmed by anything I fetched. Treat as unverified and test.
- Safe order of operations if attempted: from the installer shell (Shift+O or Alt+F1 shell where available), copy `/bootbank/state.tgz` and `/altbootbank/state.tgz` to external media before any reinstall; try the altbootbank copy too since the bootbank copy may have been written mid-power-loss.
- If the host has TPM-bound encryption, a reinstall on the same hardware keeps the same TPM, so restore remains possible; clearing the TPM would block it.

### Gaps
- No ESXi 8/9-specific source on: what exactly state.tgz holds, how often it syncs now (hourly cron figure not confirmed), or a tested "extract from installer shell then restore on fresh install of same build" procedure. No `configstorecli` backup/restore guidance retrieved (William Lam has configstorecli posts: https://williamlam.com/tag/configstorecli, snippets only).
- Whether `esxcli system settings` backups are a documented option: not found.

## Q3. System storage best practice (OSDATA size, scratch, syslog, coredump, VCF 9)

### Takeaway
OSDATA sizing is set at install with `systemMediaSize`; scratch, logDir and coredump should be pointed somewhere other than the OSDATA that failed, with remote syslog as the minimum. VCF 9 expects syslog forwarded to VCF Operations for Logs, usually via vCenter-integration auto-configuration that can overwrite manual logHost values.

### Cited Findings
- `systemMediaSize` (since 7.0 U1c), set via installer boot option (Shift+O) or `kernelopt` in boot.cfg / kickstart: `min` 32 GB, `small` 64 GB, `default` 128 GB, `max` all space. Sizes differ slightly between sources (KB 81166 lists 33/69 GB; William Lam's 2020 post originally had 25/55). — [KB 372207](https://knowledge.broadcom.com/external/article/372207/creation-of-local-datastore-post-install.html), [KB 81166](https://kb.vmware.com/s/article/81166), [William Lam](https://williamlam.com/2020/05/changing-the-default-size-of-the-esx-osdata-volume-in-esxi-7-0.html)
- KB 372207 (ESXi 8.x): local datastore is created from leftover space when the boot device is local and larger than 142 GB; `--novmfsondisk` prevents it. — [KB 372207](https://knowledge.broadcom.com/external/article/372207/creation-of-local-datastore-post-install.html)
- `autoPartitionOSDataSize` allows smaller OSDATA for homelabs but is unsupported. — [William Lam](https://williamlam.com/2020/05/changing-the-default-size-of-the-esx-osdata-volume-in-esxi-7-0.html)
- Broadcom guidance: minimum 32 GB persistent media, 128 GB recommended, otherwise scratch and coredumps fall to a volatile ramdisk. — [KB 317690](https://knowledge.broadcom.com/external/article/317690/system-logs-are-stored-on-nonpersistent.html)
- Persistent scratch (KB 317689, ESXi 7.0/8.0): `mkdir /vmfs/volumes/<ds>/.locker-<host>`; `vim-cmd hostsvc/advopt/update ScratchConfig.ConfiguredScratchLocation string <path>`; reboot. Do not use vSAN, SD/USB, a datastore root, or a path shared between hosts. If `Syslog.global.logDir` is left at default `/scratch/log`, moving scratch moves logs; if logDir is set elsewhere, scratch no longer matters for logs. You can split them (persistent logDir, ramdisk scratch). Set `Syslog.global.logDirUnique` on shared folders. — [KB 317689](https://knowledge.broadcom.com/external/article/317689/)
- Remote syslog: `esxcli system syslog config set --loghost=<proto://host:port>` then `esxcli system syslog reload`; blank loghost forwards nothing; keep to five or fewer targets; firewall ruleset must allow outbound. — [KB 318939](https://knowledge.broadcom.com/external/article/318939/configuring-syslog-on-esxi.html) (details via search summary)
- Network coredump (KB 344063, ESXi 8.x): `esxcli system coredump network set --interface-name vmk0 --server-ipv4 <IP> --server-port 6500`, then `esxcli system coredump network set --enable true`, verify with `network get`. 7.0 docs use `--server-ip`. Collector is not supported on an NSX-T N-VDS VMkernel interface; nested hosts on standard vSwitch need promiscuous mode. KB 340049: configure the network collector before disabling the local coredump file. — [KB 344063](https://knowledge.broadcom.com/external/article/344063/configuring-the-network-dump-collector-s.html), [KB 340049](https://knowledge.broadcom.com/external/article/340049/permanently-disable-esxi-coredump-file.html), [KB 423082 recreating coredump file in OSDATA](https://knowledge.broadcom.com/external/article/423082/recreating-a-core-dump-file-in-osdata.html)
- VCF 9 Operations for Logs: enable ESX log collection under Infrastructure Operations > Configurations > Log Collection by editing the management vCenter; point to the Logs cluster VIP. Syslog ports 514 (UDP/TCP), 1514 (UDP), 6514 (TLS). Auto-config is additive and may append node addresses (VCF Ops 9.1, ESXi 8.x per KB), and "Automatically configure all ESXi hosts" can revert manual `Syslog.global.logHost` edits. — [KB 431771](https://knowledge.broadcom.com/external/article/431771/configuring-component-log-forwarding-to.html), [KB 429023](https://knowledge.broadcom.com/external/article/429023), [KB 448889](https://knowledge.broadcom.com/external/article/448889/esxi-sysloggloballoghost-settings-contin.html)
- KB 449991/340049 note coredump-file handling on OSDATA; a file coredump in a full OSDATA is a likely contributor to full-disk failures (inference).

### Inferences
- For a 1 TB boot NVMe, a 120 GB OSDATA is the default; shrinking to `min`/`small` leaves a larger datastore but does not improve power-loss behavior. The more effective lever is moving write-heavy items (logs, coredump) to another device or network.
- Pointing scratch/logs to the local VMFS datastore on the same NVMe does not remove the single-device exposure, only the OSDATA-specific one; remote syslog plus network dump is what survives device failure.

### Gaps
- No ESXi 9.x-specific page for system storage was retrieved; most sources are 7.x/8.x. The ESXi 9 techdocs page was not located.
- No direct VCF 9 doc statement of "required" scratch/coredump settings; only log forwarding behaviour found.

## Q4. Power-loss resilience (consumer NVMe, Crucial P310, UPS)

### Takeaway
No source found documents ESXi's flush/FUA/HMB behaviour on DRAM-less NVMe, and no P310 firmware release notes for V8CR001 vs V8CR010 were retrievable. General guidance: consumer drives without PLP risk losing acknowledged writes on power loss; the practical mitigation is a UPS with orderly shutdown.

### Cited Findings
- Crucial's spec marketing lists "power loss immunity" for the P310, which is firmware-level behaviour, not capacitor-based PLP; the listing does not define it. Controller: Phison PS5027-E27T. — [StorageReview P310 review](https://www.storagereview.com/review/crucial-p310-ssd-review)
- Crucial's P310 support page appeared in search results with the text "No firmware updates at this time" (search-summary only, direct fetch rejected). Reviews list firmware V8CR000. No source mentions V8CR001 or V8CR010. — [Crucial P310 support](https://www.crucial.com/support/ssd-support/p310-support), [KitGuru](https://www.kitguru.net/components/ssd-drives/simon-crisp/crucial-p310-2280-2tb-ssd-review/)
- Crucial firmware update guide (generic) says the tool shows version, date and release notes before updating. — [Crucial update PDF](https://content.crucial.com/content/dam/crucial/ssd-products/ssd-family/documents/crucial-firmware-update-generic/crucial-firmware-update-all-en.pdf)
- General: non-PLP NVMe can lose data the host believes durable; SMART cannot tell if PLP exists. — [Netdata guide](https://www.netdata.cloud/guides/nvme/nvme-power-loss-protection-plp/) (secondary source). vSAN on consumer SSDs is outside the HCL and suitable only for labs. — [virten.net](https://www.virten.net/2020/12/vmware-vsan-on-consumer-grade-ssds-endurance-analysis/)
- UPS automation: community material (mostly ESXi 5/6 era) indicates NUT runs on a machine that stays up longest (not a VM on the host being shut down), the UPS off-delay must exceed the full ESXi shutdown time, and ESXi's VM auto-start/stop order and delays must be set. A native NUT client for ESXi exists per a VMware community post. — [Debian NUT list 2015](https://lists.alioth.debian.org/pipermail/nut-upsuser/2015-January/009478.html), [VMware community](https://communities.vmware.com/t5/ESXi-Discussions/Shutdown-ESXi-6-0-by-APC-UPS/m-p/1342463), [DEV: NUT homelab](https://dev.to/merbayerp/safely-shutting-down-your-homelab-during-a-ups-outage-with-nut-33g3)

### Inferences
- The V8CR001 (boot) vs V8CR010 (vSAN) mismatch cannot be evaluated from sources; V8CR001/V8CR010 are not on the pages I could see. Comparing `nvme id-ctrl`/`esxcli nvme device get` firmware strings against Crucial Storage Executive output on a Windows machine is the way to settle it.
- Mitigation priority (my judgment, not sourced): UPS plus graceful shutdown, remote syslog/netdump, and a regular `backup_config` stored off-host, since none of the above removes the volatile-cache risk.

### Gaps
- ESXi NVMe driver behaviour on flush/FUA, volatile write cache, and HMB on DRAM-less drives: no Broadcom or William Lam source found.
- Crucial P310 firmware changelog (V8CR001, V8CR010, later): not found; Crucial pages were blocked to the fetcher.
- No current ESXi 8/9 NUT-to-ESXi/vCenter shutdown walk-through from a primary source; only older community material.
