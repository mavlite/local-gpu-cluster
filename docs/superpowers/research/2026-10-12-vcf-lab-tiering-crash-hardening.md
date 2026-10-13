# VCF lab: settings to minimise crash risk with memory tiering on consumer NVMe (2026-10-12)

Purpose: make it safer to run the VCF 9.1 management stack and the coding engine's agent tier (three 24 GB LLM worker
VMs + three 16 GB runner VMs) on the three Minisforum hosts at the same time, with ESXi memory tiering on the consumer
Samsung tier drives. Sources: two research reports in this folder (`vcf-host-tiering-nvme-settings.md`,
`vcf-cluster-burst-settings.md`, with URLs and VERIFIED/VENDOR-BLOG/COMMUNITY/UNVERIFIED labels) plus our own incident
analysis (2026-09-27..10-04). **Nothing here has been applied.**

## The bottom line

- **No ESXi setting prevents a consumer NVMe controller hang.** Broadcom documents exactly our failure (KB 413903:
  the device ignores I/O, reset fails, power cycle needed) and its effect (a VM that faults on a page held on the dead
  device powers off). 9.1 has no tunables for tier promotion/demotion rate or thresholds — only the tier size.
- What settings *can* do: keep the most important memory **off** the tier, **remove the bursts** that preceded two of
  the three hangs, and **stop one hang from cascading** into the next host.
- The durable fix is hardware: tier drives with datacenter firmware (Broadcom requires endurance class D, ≥ 7,300 TBW;
  e.g. Intel Optane P1600X 118 GB, M.2, covers a 1:1 tier on 96 GB), or 9.1 software mirroring (needs a second NVMe per
  host — no free slot on the BD795i SE).
- Our drive is reportedly the **PM981** (MZVLB512HAJQ), not the PM981a (that is MZVLB512HBJQ); no public firmware newer
  than 5L2QEXA7 was found and cross-flashing reportedly fails.

## Ranked settings

Respecting the standing decisions: tiering stays on, DRS stays PartiallyAutomated, nsxa's reservation stays removed.

### Tier 1 — do these first (cheap, low risk, big effect)

| # | Setting | Effect | Risk / cost | Verify |
|---|---|---|---|---|
| 1 | **Agent-tier VMs off the tier:** on each worker and runner VM, `sched.mem.enableTiering = "FALSE"` **and** a full memory reservation (24 / 16 GB). VM must be powered off to change | Model weights (read every token) and test workloads stay in DRAM; the agent tier no longer competes for tier I/O. Broadcom: excluding a VM *requires* a full reservation; a reservation alone does **not** keep pages in DRAM | 120 GB of DRAM reserved cluster-wide; fine at 40 GB of 96 per host | `memstats -r vmtier-stats` shows 0 tiered pages for those VMs |
| 2 | **VM Monitoring off** (cluster-wide, or per VM for sddc-manager and the Ops VMs) | Removes the "Tools heartbeat failure" resets seen during the 10-04 incident (KB 337335 workaround) | A truly hung guest is not auto-reset | Cluster > vSphere HA > VM Monitoring = Disabled |
| 3 | **HA restart shaping:** restart priority vCenter High, NSX Medium, SDDC Manager Low; "start next priority when: Guest heartbeats detected" + 120-300 s additional delay; restart **disabled** for the Ops collector and fleet management, with `das.reregisterrestartdisabledvms = true` | Restarts become sequential and spaced, so a failed host does not dump every VM onto a survivor at once | Slower recovery; Ops VMs need a manual power-on after a host failure | HA settings page; test with one VM |
| 4 | **Admission control off** | With no spare host it only blocks power-ons and upgrade prechecks (KB 421536 gives this fix for 9.1 upgrades) | None in a 3-host lab with no N-1 headroom | Cluster > HA > Admission Control = Disabled |
| 5 | **DRS tuning (stay PartiallyAutomated):** migration threshold 1 (Conservative), Predictive DRS off, VM Distribution off; worker/runner VMs get a "must run on" rule + DRS override Disabled. Never put vCenter under a "must" rule | No automatic vMotions; maintenance mode stalls until you move VMs one by one (KB 451053), which is what we want | VCF Installer/brownfield import require DRS Fully Automated — switch only for those operations | DRS settings page |
| 6 | **Power-on order:** after a host boots, agent tier first (DRAM is empty), one VM at a time; then the VCF stack in the KB 440862 order (control plane, wait 5 min, then the rest). Never power on a large VM when the host is above ~80% DRAM consumed | Tiering does not cover power-on allocation (the hyp02 PSOD on 10-03) | None | Host memory before each power-on |
| 7 | **Pre-flight before any vMotion or maintenance mode:** `esxcli system health report get -r vmw.memTierHealth` on source and destination, no NVMe aborts in `vmkernel.log`, destination stays < 80% DRAM after the move, one migration at a time | Never migrate onto or off a sick tier drive | A few minutes per operation | Make it a script in the vcf-lab toolkit |

### Tier 2 — reduce burst intensity and drive stress (test on one host first)

| # | Setting | Effect | Risk / cost | Verify |
|---|---|---|---|---|
| 8 | **vMotion bandwidth limit:** vDS Network I/O Control v3, vMotion system traffic Limit ~3 Gbps (tune 2-4), exactly one active uplink on the vMotion port group | Caps migration speed; that it also slows the drive bursts is an inference, not documented | vMotions take ~3x longer | NIOC page; time one vMotion |
| 9 | **NVMe power management:** read first — `esxcli nvme device feature apst get` and `feature pm get`; if APST is enabled, disable it (`feature set -f 0x0c ...`, syntax unconfirmed) and/or hold power state 0 (`feature pm set -v 0`), re-applied at boot from `/etc/rc.local.d/local.sh`; BIOS: PCIe ASPM and PCIe power saving off | APST/ASPM low-power transitions are a well-known cause of consumer-NVMe controller hangs on Linux; ESXi status unknown | Slightly higher idle power; the set syntax must be confirmed on a host | Re-read the features after a reboot |
| 10 | **Tier size 50%** (`esxcli memtier` tier size pct 50; maintenance mode, no reboot) | Halves how much can tier, so DRS/HA cannot over-pack a host; capacity still 3 x (96 + 47) = 429 GB vs ~339 GB needed for both stacks | Less headroom if both stacks grow | `esxcli memtier config get` |
| 11 | **vSAN ESA:** `/VSAN/DOMNetworkSchedulerThrottleComponent = 1` on every host (KB 410662); confirm no old manual resync throttle remains (KB 326963). Set host advanced settings in vSphere Configuration Profiles, or they are reverted as drift | Smoother vSAN traffic over 10G after a host blip | Low; verify the KB applies to our build | Advanced settings per host |
| 12 | **Alerting:** the 9.1 alarm "NVMe Memory Tiering device is not healthy" with an email/webhook action; forward vmkernel/vobd logs and alert on NVMe timeouts/aborts | Earliest available signal (SMART did not warn: failed drives showed 0 media errors) | None | Trigger a test alarm |

### Tier 3 — decisions for the owner

| # | Option | Trade-off |
|---|---|---|
| 13 | **Replace the tier drives** with datacenter-firmware M.2 drives (e.g. Optane P1600X 118 GB) | The only change that addresses the hang itself; cost of three drives |
| 14 | **Exclude vcsa from tiering** (`sched.mem.enableTiering = FALSE` + full 21 GB reservation) | vCenter survives a tier hang (it crashed on 10-04). Cheapest exclusion. Extending it to nsxa/sddc-manager conflicts with the decision to keep nsxa's reservation removed |
| 15 | **Run both stacks together at all** | With 1-12 in place it becomes a reasonable trial: one lab weekend with monitoring, then decide. Without them, keep the approved design (Full mode pauses the agent tier) |

## Not recommended

Latency Sensitivity High, reservations alone (do not keep pages in DRAM), undocumented `Mem.Tier*` keys, firmware
cross-flashing, the unsupported `config.vpxd.ResourceManager.costPerVmotionESX6x` concurrency hack, shortening NVMe
timeouts (only declares the drive dead sooner), and the VKS `powerState` patch for VSP (documented for VKS only; stop
VSP only via its shutdown script, KB 440874, or Fleet Management power-off — VSP also self-heals by cloning a new node,
KB 389510, which is itself a burst).

## Note on the workers

The worker and runner VMs live on each host's local datastore (`hypNN-local`), so they cannot be vMotioned or
HA-restarted onto another host. A host failure therefore never pushes their memory onto a survivor; it only stops
that host's share of the agent tier, which the queue already tolerates (design §3.2.1).
