# ESX hardware health for the Ryzen lab hosts: design

Status: approved direction 2026-11-06 (owner). Work starts **after** the UniFi network migration
(`docs/superpowers/plans/2026-11-05-ubiquiti-lab-network-migration.md`) is finished.

## Why

The three VCF lab hosts (`hyp01`-`hyp03`, Minisforum BD795i SE, AMD Zen 4 mobile CPU, ESX 9.1.1)
have no BMC. vSphere reports **0 hardware sensors** on each of them and no IPMI. On 2026-11-05
`hyp02` cut out three times under vSAN resync load with no crash dump. The cause turned out to
be a physically blocked CPU fan (thermal protection). Nothing in vSphere or VCF Operations could
have shown that: the temperature was invisible.

Windows (HWiNFO) and Linux (`lm-sensors`) can read these boards' sensors. ESX can't, because it
has no drivers for them. The owner wants those readings as first-class vSphere data, so that
VCF Operations can chart them, dashboards can show them and alerts can fire before a thermal
trip.

## Goals

1. **v1 (this design): a userspace health plugin VIB.** Each host publishes, every minute:
   - CPU package temperature (Tctl) and per-chiplet (CCD) temperatures, in °C;
   - CPU package power, in W;
   - per-core effective clock and a host-wide throttle indicator;
   - NVMe drive temperature and SMART health (spare, % used, critical warning, media errors).
   It also sets an overall OK/WARNING/ERROR status from thresholds.
2. The readings are reachable **through vSphere's own API surface**, the ESX health report service
   (`com.vmware.esx.observability.reports`), the same mechanism that feeds the VCF 9.1 NVMe
   Tiering health report. There's **no SSH polling**.
3. **VCF Operations** ingests the readings per ESX host object through a Management Pack Builder
   pack, with a dashboard and alert definitions.
4. **v2 (separate design, later): a kernel-module VIB**, found by trial and error in nested ESX.
   It covers fan speeds and board/VRM temperatures from the board's ITE Super I/O, and possibly
   publishing everything as native hardware sensors (`numericSensorInfo`, the Hardware Health
   tab, built-in vCenter alarms). This document only records what is already known for it.

## Non-goals (v1)

- Fans, board, VRM and chipset temperatures (need kernel code; v2).
- Native vCenter hardware sensors or the Hardware Health tab (v2).
- Any kernel module, any write to hardware other than the SMN index register below.
- Hosts other than the three BD795i SE lab hosts. The decode is Zen 4 family 19h model 0x61
  specific. Other CPUs must be refused with a clear report message, not guessed at.
- The Proxmox GPU host (it has Linux `lm-sensors`).

## Anti-goals (a failure even if it "works")

- **SSH polling** from a collector. The owner rejected it.
- Anything that can **crash a host** (PSOD) or hang `healthd`. v1 is userspace only.
- **Secrets in output.** Credentials come from `~/.vcflab/credentials.env` by key name only, as
  the existing scratchpad scripts do. Reports, logs and test fixtures contain no credentials.
- Breaking **VCF lifecycle**: an ESX upgrade or vLCM remediation must not brick a host. The cost
  of the VIB is at worst "removed by an upgrade, reinstall it", never "host won't boot".
- Silent wrong numbers. A reading that can't be trusted is reported as missing with a reason,
  never as a plausible-looking value.

## What is already proven (read on `hyp01`, 2026-11-06)

All by hand from the ESX shell (`vsish`, Python 3.11 that ships with ESX). No VIB was installed.

| Reading | Source | Proven value |
|---|---|---|
| CPU family/model | `esxcli hardware cpu list` | family 25 (0x19), model 97 (0x61), AuthenticAMD |
| Secure Boot, acceptance | `secureBoot.py -s`, `esxcli software acceptance get` | `Disabled`, `PartnerSupported` (hyp01; plan re-checks all three) |
| PCI config read | `vsish -e get /hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/0x0` | `0x14d81022` (AMD Raphael root complex) |
| SMN index/data | same node, `addr/0x60` (index), `addr/0x64` (data); `vsish -e set …/0x60 <smn>` works | index read `0x0` before and after |
| Tctl | SMN `0x00059800` | `0x7d3b0000` → 76.1 °C |
| CCD0, CCD1 temp | SMN `0x00059b08`, `0x00059b0c` (`0x59800 + 0x308 + 4*n`) | `0xb8f` → 64.9 °C, `0xbec` → 76.5 °C |
| RAPL energy unit | `vsish -e get /hardware/msr/pcpu/0/addr/0xC0010299` | `0xa1000` (energy unit 2^-16 J) |
| Package energy | MSR `0xC001029B` | about 150 J over about 2 s → about 75 W |
| ESX's own CPU power | `vsish -e get /power/hostStats` | `cpuPower: 75 W`, min 50, max 89 |
| APERF/MPERF | `vsish -e get /power/pcpu/<n>/perf` | APERF and MPERF readings present |
| ESX temperature node | `/power/pcpu/0/temp` | `Not supported` (Intel-only) |
| NVMe SMART | `esxcli nvme device log smart get -A <vmhba>` | built in |

Decode rules (from the Linux `k10temp` driver, matched to the readings above):

- Tctl: `t = ((v >> 21) & 0x7ff) * 0.125`; if bit 19 (`CUR_TEMP_RANGE_SEL`) is set, `t -= 49`.
  As in `k10temp`, `t -= 49` also applies when bits 17:16 (`CUR_TEMP_TJ_SEL`) are `0b11`, even
  with bit 19 clear (subtracted once either way).
  `k10temp`'s offset table has no entry for family 19h, so it reports Tctl as Tdie for this part.
- CCD: valid only if bit 11 is set; `t = (v & 0x7ff) * 0.125 - 49`.
- RAPL: energy unit `= 2^-((unit >> 8) & 0x1f)` J. Package power = Δenergy / Δt, with the 32-bit
  counter wrap handled.
- Effective clock: `nominal_MHz * ΔAPERF / ΔMPERF` per core.

What is proven **not** to work from userspace (this is why fans are v2): `iopl`/`ioperm` return
`ENOSYS`; `vsish /hardware/port/…` GET is not implemented; `/dev/char/mem/port` opens but
`pread` returns `EIO`/`EINVAL` (its ioctl interface is undocumented); `/hardware/mem/physical/…`
GET is not implemented. CIM is gone in 9.1 (no `sfcbd`, no `esxcli system wbem`), so a CIM
sensor provider is not an option. The `healthd` plugin route is what is left.

### The `healthd` plugin mechanism (read on `hyp01`)

- `healthd` (`/usr/lib/vmware/healthd/bin/healthd -r /var/lib/vmware/osdata/healthd`) launches
  plugins from `/usr/lib/vmware/healthd/plugins/bin/`. Some are Python and subclass
  `vmware.healthd.healthPlugin.HealthPlugin` (`/lib64/python3.11/site-packages/vmware/healthd/`),
  for example `mobility_network.py`: build a dict, then call
  `self.sendReport(REPORT_NAME, data, structuredData={'status': …, 'magnitude': …})`.
- A plugin is launched as `<binary> ++group=healthd-plugins,mem=40 -u http://!vmwLocalSocketHealthd`,
  and posts its report to that local socket. Reports land in `/var/lib/vmware/osdata/healthd/`.
- Registration is a **ConfigStore** entry: component `esx`, group `health`, key `<plugin>`, with
  `path`, `enabled`, `frequency` (`MEDIUM` for `ntp_health`), `max_lifetime` (30),
  `criticality`, `tags`, `resource_group`, `resource_group_size_MB`, `working_dir` and a `log`
  block. The defaults ship in `/etc/vmware/default-config/esx-base-<ver>-default.json`.
- Reports are served at `GET /api/esx/observability/reports` and `…/reports/{report}` through
  rhttpproxy (header `vapi-service: com.vmware.esx.observability.reports`, backend
  `localhost:9984`). `esxcli system health report get -r <name>` reads them locally.
- **Unproven:** a remote call from the workstation timed out, and `/api/session` on the host
  returned 400/503. The working remote authentication method is not yet known.

## Design (v1)

### Pieces

1. **Sensor library** (Python 3.11, standard library only, because it runs on ESX):
   - Pure decode functions (raw register values → °C, W, MHz), unit-tested against the proven raw
     values above.
   - Thin readers that call `vsish` and `esxcli`, with the command runner injected so tests need
     no ESX.
2. **Health plugin** `hwhealth`, report name `lab.hwHealth`:
   - Samples once per run: the RAPL counters and APERF/MPERF are read twice about 1 s apart
     inside one run.
   - Builds the report and sets the status (thresholds below).
   - Sends the report through `HealthPlugin.sendReport`.
   - Has a `--stdout` mode that prints the same JSON, for testing without `healthd`.
3. **VIB package** `lab-hwhealth`:
   - Contents: the plugin and the library under `/usr/lib/vmware/healthd/plugins/bin/` (or a
     path the spike proves `healthd` will launch), plus whatever registers the plugin with
     ConfigStore.
   - Acceptance level: CommunitySupported. The hosts are PartnerSupported today, so installing it
     means lowering host acceptance. That's an **owner decision at rollout time**, recorded in the
     plan.
4. **Operations pack**, built with VCF Operations Management Pack Builder:
   - Polls each host's report endpoint and maps the fields to metrics on the existing ESX Host
     objects.
   - The pack export is kept in the repo.
   - Ships with one dashboard and the alert definitions.

### Report contents (`lab.hwHealth`)

`structured`: `status` (0 OK, 1 WARNING, 2 ERROR, as the built-in plugins use), `magnitude`,
`tags: ["hardware"]`.
`unstructured`:

```json
{
  "schema": 1,
  "cpu": {"family": 25, "model": 97, "supported": true,
          "tctl_c": 76.1, "ccd_c": [64.9, 76.5],
          "package_w": 75.2,
          "eff_mhz": [4810, 4795, "…one per logical CPU"],
          "eff_mhz_summary": {"min": 3900, "avg": 4800, "max": 5300}},
  "nvme": [{"adapter": "vmhba2", "model": "CT4000P310", "temp_c": 41,
            "spare_pct": 100, "spare_threshold_pct": 5, "used_pct": 1,
            "critical_warning": 0, "media_errors": 0, "wctemp_c": 86, "cctemp_c": 88}],
  "errors": [{"reading": "ccd_c[1]", "reason": "valid bit clear"}],
  "sampled_at": "2026-11-06T15:40:00Z", "duration_ms": 1300
}
```

These field names are the contract with the Operations pack. Changing one means bumping
`schema`.

`critical_warning` is the NVMe Critical Warning bit mask, built from esxcli's named flags:
0x01 spare below threshold, 0x02 temperature, 0x04 reliability degraded, 0x08 read-only,
0x10 volatile memory backup failed. `spare_threshold_pct` is the drive's own Available Spare
Threshold, or null when the drive doesn't report it. Both changed in 1.0.1 with `schema` kept
at 1, because no consumer existed yet. The only earlier install was 1.0.0-1 on hyp01, for
testing, which reported `critical_warning` as 0 or 1.

**Builder's call: no throttle percentage in v1.** The effective clock is the
APERF/MPERF-weighted clock over the sampling window, measured only while the core was not
halted, per logical CPU plus a summary. It drops for idle and power-state reasons as well as
heat, so v1 charts it but **nothing alerts on it**; alerts are on temperature only. A real
throttle signal (for example the PROCHOT or HTC status in SMN) is a v2 question.

### Thresholds

The CPU rows are the builder's defaults. The NVMe rows were approved by the owner on 2026-11-08 and
come from the drives' documentation (hyp01: Crucial P310 1 TB and 4 TB, Samsung PM981 512 GB):

- 70 °C is the vendor operating maximum. It is quoted from secondary sources, because the
  datasheets were not reachable.
- The P310 throttles at about 81 °C (CDRLabs measurement), below its 86 °C WCTEMP. A warning at
  WCTEMP would therefore fire only after throttling had begun, so WCTEMP is the ERROR level.
- Spare uses each drive's own Available Spare Threshold: P310 5 %, PM981 10 %.
- NVMe defines `used_pct` 100 as rated endurance consumed. The 80 % warning is a judgement call.
- Media errors count unrecovered data-integrity errors. Error-log entries are not alerted on:
  the PM981 has 475 of them with 0 media errors.
- These thresholds catch wear and heat, not sudden failure. The lab's memory-tier drives failed
  with clean SMART data.

| Reading | WARNING | ERROR |
|---|---|---|
| Tctl / any CCD | ≥ 90 °C | ≥ 95 °C (the part trips near 100 °C) |
| NVMe temp | ≥ 70 °C | ≥ the drive's warning composite temperature (WCTEMP), else 80 °C |
| NVMe critical warning | any bit outside 0x01-0x10 | any of bits 0x01-0x10 (each named in the reason) |
| NVMe spare | ≤ the drive's spare threshold + 10 points | < the drive's spare threshold (10 % if not reported) |
| NVMe endurance used (`used_pct`) | ≥ 80 % | ≥ 100 % |
| NVMe media errors | — | > 0 |
| CPU unsupported or all CPU readings missing | WARNING | — |

### SMN safety rule

The SMN index register is shared state. Linux protects it with a lock that ESX doesn't have, so
a race with any kernel user would make one of the readers see the wrong register. Rules:

- Read the index first. **If it is not 0, skip the temperature readings for this run** and
  report the reason, because something else is using it.
- Write the index, read the data and **always restore the index to 0**, in a `finally`.
- Touch only the three SMN addresses above. Runs are at least 60 s apart.
- The plan's real-host trial watches `vmkernel.log` and `vmkwarning.log` for anything new.
- A small race remains between the check and the write, and nobody knows whether vmkernel ever
  uses this register. **Builder's call:** the plan has an explicit owner go/no-go gate before
  the plugin runs on the first real host, which presents this risk and the trial evidence.

### Testing

- **Unit tests** (workstation, pytest, `scripts/esx-hwhealth/tests/`): decode functions against
  the proven raw values; readers against recorded `vsish`/`esxcli` output; report building,
  thresholds and status; the SMN rule (non-zero index skips, index restored on error); counter
  wrap; unsupported CPU.
- **Nested ESX 9.1.1** (a VM; virtual hardware, so it can't read real sensors): proves
  registration, launch, report delivery, the remote API path, VIB install and remove, and that
  the plugin degrades cleanly when the sensors aren't there.
- **One real host in maintenance mode** (no VMs) before all three.

## Owner decisions (2026-11-06)

1. **The VIB goes into the cluster's vLCM image.** It's packaged as a component in an offline
   bundle, uploaded to the depot and added to the cluster image, so remediation and upgrades
   keep it. The hosts move to CommunitySupported acceptance for this. The rollout task
   confirms that the image accepts it and that hosts report compliant afterwards.
2. **Fallback allowed:** if the `healthd` plugin's sandbox blocks the `vsish` register reads,
   v1 adds a separate userspace sampler, started at boot from the same VIB, that writes a cache
   file. The plugin then publishes that file. The SMN rule, the "missing with a reason" rule
   and the no-kernel-code rule still apply. A cache file older than 3 minutes is reported as
   missing, not reused.
3. **Alerts notify by email** through VCF Operations' outbound email plugin. The SMTP relay
   and recipient come from the owner at pack time; any SMTP secret goes in
   `~/.vcflab/credentials.env` under a named key. It must be reachable after the UniFi
   migration.

## What the builder still decides

- Exact file layout inside the VIB, and how ConfigStore registration is shipped (VIB default
  config, `configstorecli` at boot, or another way the spike finds).
- How the VIB is built (a hand-built `ar`/descriptor package, or a known VIB-authoring
  container).
- Remote authentication for the report API, and whether vCenter proxies it.
- Where the nested ESX VM runs. The owner suggested the Ryzen VCF hosts, under the dev VM yield
  policy (local datastore, low shares); the Proxmox host is the fallback.

## Known facts for v2 (kernel VIB, not designed here)

- The Super I/O is **ITE** (DSDT: `SIO1` `PNP0C02`, config port `SP1O = 0x2E`; hardware-monitor
  device `HHMD` on LDN 4, GPIO `SGPI` on LDN 7; I/O ranges `IO1B 0xA00/0x20`, `IO2B 0xA20/0x10`,
  `IO3B 0xA30/0x10`). There's no ACPI embedded controller. A Linux live boot running
  `sensors-detect` should give the exact chip and the `it87` register map.
- Routes to try in nested ESX: decode the `/dev/char/mem/port` ioctl interface (wrong guesses
  hit emulated ports), then a vmkernel module. The ESX driver headers aren't public, and
  Broadcom's licence limits reverse engineering, so that's the owner's call when v2 starts.
- Nested ESX can't see the real Super I/O, SMN or MSRs. The hardware-access step can only be
  tested on a real host in maintenance mode.

## Sources

- William Lam, "Quick Tip - Improved NVMe Tiering Device Health Monitoring in VCF 9.1",
  https://williamlam.com/2026/09/quick-tip-improved-nvme-tiering-device-health-monitoring-in-vcf-9-1.html
- Linux `k10temp` documentation, https://www.kernel.org/doc/Documentation/hwmon/k10temp.rst
- Broadcom KB 314303 (NVMe SMART on ESX), KB 423867 (host power via `vsish`)
