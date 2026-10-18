# Proxmox host crashes: findings and next steps (handoff)

Date: 2026-10-13. Host: `gpu-cluster`, 192.168.6.175. Nothing on the host has been changed yet; every step below is a
proposal awaiting the owner's go-ahead.

## TL;DR

- The host has crashed at least five times since 2026-10-05. Every crash with a saved record is the **same kernel
  Oops**: an idle CPU, entering a low-power state, dereferences NULL (once an invalid opcode) inside the kernel's
  high-resolution timer code, on kernel **7.0.14-12-pve**.
- It is **not** memory exhaustion (no OOM kill ever logged) and **not** CPU load. The last crash (2026-10-13 06:12)
  happened with no lab VM running.
- Recommended: (1) update the kernel to **7.0.14-23** (kernel package only), (2) disable the deepest CPU idle state
  (C3 = Ryzen CC6) at runtime now and permanently in the BIOS, (3) hold the GPU firmware update until (1) and (2) have
  proved stable.

## Host facts

| Item | Value |
|---|---|
| Board / BIOS | ASUS ProArt X870E-CREATOR WIFI, BIOS 2202 (2026-04-09) |
| CPU | AMD Ryzen 5 7600 (6 cores / 12 threads), microcode 0xa60120c, `amd64-microcode` 3.20251202.1 |
| RAM | 4 x 32 GB Crucial CT32G52C42U5 (DDR5, non-ECC), running at 3600 MT/s, 1.1 V |
| Kernel | 7.0.14-12-pve (installed also: 7.0.2-4, 6.17.13-21, 6.17.2-1) |
| Cmdline | `quiet amd_iommu=on iommu=pt amdgpu.ppfeaturemask=0xfff73fff` |
| CPU idle | driver `acpi_idle`; states POLL, C1, C2, C3 (C3 latency 350 us; most idle time is spent in C3) |
| GPUs | 2 x Radeon Pro V620 (Navi 21, 03:00.0 and 07:00.0), Raphael iGPU (7e:00.0); all on in-kernel `amdgpu` |
| Swap / ZFS ARC | 8 GB swap; `zfs_arc_max` 16 GB |

## Evidence

Crash records are in `/var/lib/systemd/pstore/<epoch>/001/` (EFI pstore). Read them with
`grep -aE "BUG:|RIP:|Comm:|Oops" /var/lib/systemd/pstore/*/001/dmesg-efi_pstore-*0[1-3]001`.

| Crash (CDT) | Record | Signature |
|---|---|---|
| 2026-10-06 17:01 | yes | `BUG: kernel NULL pointer dereference`, RIP `__hrtimer_setup+0x33`, `swapper/6` |
| 2026-10-06 17:43 | yes | `Oops: invalid opcode`, RIP `hrtimer_reprogram+0xd1`, `swapper/8` |
| 2026-10-07 11:03 | yes | NULL dereference, RIP `__hrtimer_setup+0x33`, `swapper/9` |
| 2026-10-10 17:45 | yes | NULL dereference, RIP `__hrtimer_setup+0x33`, `swapper/8` |
| 2026-10-13 06:12 | no record | journal just stops; no lab VM was started in that boot |

The common call trace: `do_idle` -> `tick_nohz_idle_stop_tick` -> `tick_nohz_stop_tick` -> `timer_base_try_to_set_idle`
-> `timerqueue_iterate_next` -> `hrtimer_setup`. An idle CPU finds a corrupt entry in its timer queue. `journalctl -b -1`
never shows an OOM kill or a machine-check (`mce`) error.

## Ruled out, and one earlier wrong theory

- **Memory exhaustion: ruled out.** It was the first theory, because on 2026-10-10 the owner's guests (CT 151 64 GB,
  CT 153-156) were started at 17:30 and 17:37 (`vzstart 151` and a `startall` through pvedaemon) beside the nested lab
  (about 44 GB), and the host died 27 s after the lab's 24 GB VM started. The pstore records disprove it: the crash is
  a kernel bug in the idle path, not an OOM.
- **The lab code: ruled out** as the cause. The lab (VMIDs 190-194, VCF-Readiness-Tool repo, `python -m lab`) only
  starts, stops and snapshots its own tagged VMs. The 2026-10-13 crash had no lab VM running.
- **Possible amplifier:** the lab runs nested ESXi (KVM -> ESXi -> nested VMs) on this AMD host. The 7.0.14-23 kernel
  fixes two AMD nested-virtualisation bugs that let a nested guest affect the host (below); stale translations or a
  leaked MSR are a plausible source of corrupted kernel memory that an idle CPU trips over later.

## Recommended steps

### 1. Kernel update (one reboot)

7.0.14-23 is available from `pve-no-subscription`. Its changelog since -12 has **no amdgpu / amdkfd changes**; it
includes:
- KVM, AMD hosts: "a failed VMRUN could drop a pending TLB flush, so stale address translations of another VM could stay
  usable" and "the speculation control MSR value of a nested guest could stay active on the host";
- "posix-cpu-timers: Prevent freeing a timer which is queued on the expiry list";
- network, filesystem and CVE fixes.

```bash
apt-get update            # the enterprise repo answers 401 (no subscription); harmless
apt install proxmox-kernel-7.0      # kernel meta-package only -> 7.0.14-23; NOT a full upgrade
reboot
```

Do not run `apt full-upgrade` in the same step: `pve-firmware` (GPU blobs) and the OVMF/edk2 firmware (Windows 11 VM 170
boots on it) would change too.

Rollback: `proxmox-boot-tool kernel pin 7.0.14-12-pve && reboot` (or pin 6.17.13-21-pve).

### 2. Ryzen idle-state fix

Runtime, now (lost at reboot, so re-apply after each reboot until the BIOS is set):

```bash
for f in /sys/devices/system/cpu/cpu*/cpuidle/state3/disable; do echo 1 > "$f"; done
cat /sys/devices/system/cpu/cpu0/cpuidle/state3/disable      # expect 1
```

Permanent, at the console: BIOS -> Advanced -> AMD CBS -> **Power Supply Idle Control = Typical Current Idle** (the name
can differ slightly). Alternative: kernel parameter `processor.max_cstate=2`. Cost: roughly 5-10 W more at idle. Also check
ASUS for a BIOS newer than 2202 (newer AGESA).

Changing the kernel and the idle state together aims at stability, not at telling the two apart. To isolate the cause,
do step 1 alone first and add step 2 only if it crashes again.

### 3. Verify after the reboot

- `uname -r` shows `7.0.14-23-pve`.
- `ls -l /dev/dri/by-path/`: 03:00.0 -> renderD128, 07:00.0 -> renderD129, 7e:00.0 -> renderD130. CT 151 passes
  `/dev/dri/renderD128` and `renderD129` by name; if the order ever moves, switch its `dev1`/`dev2` to the by-path names.
- CT 151: `rocminfo` shows two gfx1030 (V620) agents; `llamacpp-chat`, `llamacpp-embed` and `llamacpp-rerank` are active
  and answer a request.
- Host: `v620-fan-bridge` active, `v620-powercap` applied (it reads `amdgpu.ppfeaturemask`, which stays on the cmdline).
- `zpool status` healthy; the onboot guests (151, 153-156) are running.
- Watch: `journalctl -k -p warning -f` and new directories under `/var/lib/systemd/pstore/`.

### 4. Later, separately: GPU firmware

- **VBIOS:** both V620s run `113-D6030500-100`. No public update exists (AMD's V620 page carries drivers only; the card
  is a server/cloud part). Do not cross-flash W6800 or RX 6800 XT VBIOS: forum reports show lost compute units or memory.
- **Driver-loaded blobs:** `pve-firmware` 3.18-5 -> 3.18-7 (linux-firmware 20260916) changes two V620 files,
  `sienna_cichlid_sdma.bin` and `sienna_cichlid_ta.bin`; every other V620 blob (ME/PFP/MEC, RLC, SMU 58.91.0, VCN, PSP
  SOS) is unchanged. Unrelated to the crashes. Apply it on its own after the kernel has proved stable, then repeat the
  CT 151 checks in step 3.

### 5. If it still crashes

1. Pin 6.17.13-21-pve and see if the signature changes or stops.
2. Add `nohz=off` to the cmdline (skips the idle tick-stop path the Oops is in).
3. Run memtest86 overnight (non-ECC RAM; four crashes at one code path point more to software than to random bit flips,
   but it is not excluded).

## Compatibility audit (done 2026-10-13, read-only)

| Area | Finding | Risk from the kernel update |
|---|---|---|
| GPU passthrough to CT 151 | `/dev/kfd` + renderD128/129; ROCm 7.2.3 userspace inside the CT; no ROCm or DKMS on the host | Low: in-kernel amdgpu, no amdgpu changes in the changelog; verify the renderD mapping |
| Out-of-tree modules | only `spl`/`zfs`, shipped inside the Proxmox kernel package | None |
| Host GPU services | `v620-fan-bridge.sh`, `v620-powercap-apply.sh` resolve hwmon paths dynamically | Low |
| Containers 153-158 | unprivileged Docker-in-LXC (nesting, keyctl) | Low |
| VMs 170, 172, 176, 177, 191-194 | no PCI passthrough; QEMU is not part of the update | None |
| Microcode | `amd64-microcode` current; late loading blacklisted (`blacklist microcode`) | None |

## Other notes

- **Guests that start on their own:** the owner's guests have `onboot: 1`, and something issued `vzstart 151` and a
  `startall` through pvedaemon on 2026-10-10 (possibly the swap webhook or the redteam-mode scripts). Keep this in mind
  before heavy workloads such as the nested lab.
- **Memory guard in the lab:** the VCF-Readiness-Tool lab now refuses to start a lab VM unless the host has its memory
  plus 8 GB free (`lab/hostmem.py`, commit 54f1f29). This is hygiene only; it does not fix the crash.
- **Pending lab work:** the lab's Phase 1 runs (two variants, about 1.5 h, guests off) wait until the host is stable.
