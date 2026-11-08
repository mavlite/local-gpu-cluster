# lab-hwhealth runbook (draft)

Status: **draft. Not installed on hyp01-hyp03.** Everything on a real host other than the hyp01
run in "Go/no-go evidence" is still `TODO`. Background and proofs: [spike-findings.md](spike-findings.md).
Design: `docs/superpowers/specs/2026-11-06-esx-hardware-health-design.md`.

## What it is

Two parts, one component:

- **Boot sampler.** `/etc/init.d/lab-hwhealth` starts `lab_hwhealth_sampler.py` in superDom (domain 0).
  Every 60 s it does the SMN, MSR and `esxcli nvme` reads that healthdDom may not do, and writes
  `/usr/lib/vmware/healthd-config/lab-hwhealth/cache.json` (RAM).
- **healthd plugin.** `lab_hwhealth.py` runs in healthdDom, reads the cache in-process (it execs
  nothing) and publishes it as report `lab.hwHealth`. Cache older than 180 s gives WARNING "cache stale".

Names: report `lab.hwHealth`; component and VIB `lab-hwhealth` (vendor `lab`, acceptance
`community`); healthd and ConfigStore key `lab_hwhealth` (`esx/health/lab_hwhealth`).

Install layout:

| Path on the host | Role |
|---|---|
| `/usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py` | plugin (healthd scans this directory at start) |
| `/usr/lib/vmware/healthd/lab-hwhealth/hwhealth/` | the Python package |
| `/usr/lib/vmware/healthd/lab-hwhealth/lab_hwhealth_sampler.py` | sampler, run with `python -B` |
| `/etc/init.d/lab-hwhealth` | starts and stops the sampler |
| `/etc/vmware/service/lab-hwhealth.xml` | hostd service entry, so vCenter can restart the sampler (1.0.2-1+) |
| `/usr/lib/vmware/configmanager/schemas/lab-hwhealth-<ver>-schema.json` | ConfigStore schema |

Reports are stored in `/var/lib/vmware/osdata/healthd/lab.hwHealth*`.

## Build

The bundle is built on an ESX 9.1.1 host with its own `esximage` library (the nested VM
`esx-hwh-lab`, as the Task 6 proof did). Copy `scripts/esx-hwhealth/` to `/tmp/t6` on the host
(the proof used vCenter Guest Operations), then:

```
python packaging/build_component.py /tmp/t6/out 0.1.0-1
```

Run it from `/tmp/t6`. Output is `/tmp/t6/out/lab-hwhealth-<ver>.vib` and
`/tmp/t6/out/lab-hwhealth-<ver>-bundle.zip`. Check the bundle:

```
esxcli software sources component list -d /tmp/t6/out/lab-hwhealth-0.1.0-1-bundle.zip
```

Files must be LF (the builder refuses CRLF). Copy the bundle back off the host and delete `/tmp/t6`.

### Build record

**1.0.0-1, 2026-11-08 12:00 UTC.** Built on `esx-hwh-lab` from commit `3a30b3c`
(`git archive 3a30b3c:scripts/esx-hwhealth`), which includes the stop-signal hold around the
SMN write window.

- `lab-hwhealth-1.0.0-1-bundle.zip`, 24609 bytes,
  sha256 `3c986f674ec7a05437bfc305de23890b89ef3a002fa6a2f67621823a5cb8fa46`.
- `lab-hwhealth-1.0.0-1.vib`, 18522 bytes,
  sha256 `85d6908df248f04923b4cf0000b3bab86687200448aa7f8100939d7c7b4aa286`.
- `esxcli software sources component list -d` reports `lab-hwhealth 1.0.0-1`, vendor `lab`,
  CommunitySupported.
- Fresh install on `esx-hwh-lab` with `component apply`: "Operation finished successfully",
  Reboot Required false, sampler running.
- After `/etc/init.d/health restart`, `esxcli system health report list` showed `lab.hwHealth`.
- `stop` took 1.68 s; `start` brought the sampler back.
- The first sample after install timed out on CPU and NVMe (20 s budget), as the Round 1 runs did;
  the next sample succeeded.
- The nested host proves plumbing only, not sensors.

**1.0.1-1, 2026-11-08 15:2x UTC.** Built from commit `5983f5d`. It adds the NVMe thresholds
derived from the drives' documentation (spec "Thresholds"; research in
`nvme-thresholds-research.md`), the Critical Warning bit mask, and `spare_threshold_pct`.

- `lab-hwhealth-1.0.1-1-bundle.zip`, 25361 bytes,
  sha256 `29f9f031cd01df7ee8e69c2f492b6c3e64afc055d75b41d2e452b95197dcb962`.
- `lab-hwhealth-1.0.1-1.vib`, 19276 bytes,
  sha256 `343696c72af9ca16f936bc96a75f3d08dfac59591886feb64c9169bffe9068d5`.
- In-place `component apply` over 1.0.0-1 on `esx-hwh-lab`: "Components Removed:
  lab-hwhealth_1.0.0-1", Reboot Required false, sampler running.

**1.0.2-1, 2026-11-08 19:00 UTC.** Built from commit `36b802b` (rebased as `b22f70b`, same tree for
`scripts/esx-hwhealth`). It adds `etc/vmware/service/lab-hwhealth.xml`, which registers the init
script as a hostd service so vCenter can restart the sampler in superDom (see "After a vLCM install").

- `lab-hwhealth-1.0.2-1-bundle.zip`, 25552 bytes,
  sha256 `c3c8af55b8bb39cc1a2f405f695f335b2cdb01c182a3f149d416077582c5f414`.
- `lab-hwhealth-1.0.2-1.vib`, 19488 bytes,
  sha256 `a32b7c83ccea0686ad201cd805b38cf516e90d8ec76053abd79c3eaa1feaf208`.
- esximage accepted the extra path for a community VIB.
- Nested proof on `esx-hwh-lab` (domain IDs from `ps -cZ`; 0 = superDom):
  - In-place apply over 1.0.1-1, from the guest-ops shell (domain 123): sampler in domain **0**
    (esxcli goes through hostd, so its `start` gets superDom).
  - `/etc/init.d/lab-hwhealth restart` straight from that shell: sampler in domain **123**, a
    stand-in for the vLCM case.
  - hostd API from the host itself: `RefreshServices`, then `serviceInfo` lists
    `lab-hwhealth` (running, policy `on`, source package `lab-hwhealth`); `RestartService` moved
    the sampler from domain **123 to 0**.
  - From a clean state (component removed, which also removes the XML; hostd restarted; then a
    live apply), `RefreshServices` alone was enough for hostd to list the service. No hostd restart.
  - `vim-cmd hostsvc/start_service|stop_service` are not a test of this: they take a firewall
    ruleset name and answer "This ruleset is not used by any service".

## hyp01 test install (owner-approved, 2026-11-08)

- **Baseline, 15:14 UTC:** PartnerSupported; Secure Boot Disabled; root complex `0x14d81022`;
  SMN index `0x00000000`; no lab-hwhealth component.
- **flock proof.** A second process held `/var/run/lab-hwhealth-smn.lock`. While it held the lock,
  `--stdout` reported `tctl_c` and `ccd_c` as "smn locked by another reader" and the index stayed
  `0x00000000`. After release, the run read Tctl 78.875, CCD [65.375, 79.5] and 87.28 W, with no errors.
- **Real-SIGTERM proof.** 20 `--stdout` runs were each sent TERM at 0.05-2.6 s. All exited
  -15 and every one left the index at `0x00000000`.
- **15:16 UTC install of 1.0.0-1.** `esxcli software acceptance set --level=CommunitySupported`,
  then `component apply` (Reboot Required false). After `/etc/init.d/health restart`, the report
  `lab.hwHealth` appeared.
  - healthd runs the plugin every 5 min (15:16:35, 15:21:35, 15:26:35, 15:31:35) and keeps
    `lab.hwHealth-{0,1,2}.json`.
- **15:33 UTC in-place update to 1.0.1-1** (no health restart). The cache carries
  `spare_threshold_pct` 5/10/5.
- **New vmkwarning lines were checked; none came from lab-hwhealth.** Apart from the existing
  "vsantraceFailover ramdisk is full" flood, the only new lines were NSX/netsched port messages at
  15:25:50 and 15:32:14. Those are the `esx-hwh-lab` VM's port connecting and disconnecting.
- **hyp01 has now drifted from its vLCM cluster image.** A remediation would remove the component.
  Resolved 2026-11-08: the component was added to the cluster image, and hyp01 scans COMPLIANT.

## Go/no-go evidence for the owner

### a. First real-host run, hyp01, 2026-11-08 03:18-03:20 UTC

Code at `cde1820`, approved by the owner. One manual `lab_hwhealth.py --stdout` run.

- Before: root complex `0x14d81022`, SMN index `0x00000000`, `vmkwarning.log` 1806 lines.
- `--stdout` run: exit 0 in 2.65 s, status 0, supported true, `tctl_c` 77.75, `ccd_c` [70.375, 72.25],
  `package_w` 83.7 W, 32 `eff_mhz` values (min 4246.5, avg 4726.1, max 4961.5).
- NVMe: vmhba0, vmhba1, vmhba2 at 44/45/43 C; wctemp 86/81/86; cctemp 88/82/88. `errors` [].
- After: SMN index `0x00000000`. A hand SMN read gave `0x7f9b0000` = 78.5 C, 0.75 C above the
  plugin's 77.75 C. Index `0x00000000` again. `vmkwarning.log` still 1806 lines. The `/tmp` copy was removed.

### b. SMN race risk (spec, "SMN safety rule")

The SMN index register is shared state and ESX has no lock for it. A small race remains between
the index check and the write, and nobody knows whether vmkernel ever uses this register.
Mitigations in the code:

- the index is read first; non-zero means the temperatures are skipped for that run;
- the index is restored to 0 in a `finally`;
- the root-complex gate: SMN is touched only when config offset 0x0 reads `0x14d81022`;
- only the three SMN addresses are used;
- our own readers are serialised: the sampler and any manual `--stdout` run take an exclusive,
  non-blocking `flock` on `/var/run/lab-hwhealth-smn.lock` around the whole SMN section (index
  check through restore). If it is held, that run skips the temperatures with "smn locked by
  another reader" and never waits or writes the index. The lock does not cover vmkernel or any
  other tool, and the zero-byte lock file stays in `/var/run` until reboot.

### c. Acceptance level

The VIB is community-level, so each host needs **CommunitySupported**. hyp01 reported
PartnerSupported on 2026-11-07; hyp02 and hyp03 are to be confirmed in the baseline.
Decision: TODO owner.

### d. Secure Boot

Re-check `Disabled` on all three hosts before install:
`/usr/lib/vmware/secureboot/bin/secureBoot.py -s`. hyp01 showed Disabled on 2026-11-07
(findings, Task 1A section 6). hyp02, hyp03: TODO paste real output.

### e. Remote reads (S3)

Remote reads need an `OBSERVABILITY` ClientProfile entitlement on each host. A dedicated local user
with only that entitlement is preferred to root. vCenter does **not** proxy host reports
(`/api/esx/observability/reports` returns 404; none of its vAPI services is observability).
Decision (owner, 2026-11-08): grant it. Done on **hyp01** for the `root` profile (`LU###root`, which
`list` does not show but `get` returns): `client_profiles.update` with grants
`[IDENTITY_MGMT, OBSERVABILITY]`, after which `reports.get lab.hwHealth` from the workstation returns
the report. hyp02 and hyp03 (granted by the owner with the same `update`, 2026-11-08 ~18:30 UTC):
`get after` shows `[IDENTITY_MGMT, OBSERVABILITY]` on both; `reports.list` and `reports.get vmw.ntpHealth`
now return data, and `reports.get lab.hwHealth` returns `not_found` until the component is installed.
A dedicated least-privilege user for the Operations pack remains open (Task 8).

## Rollout (owner)

Plan Task 7, Steps 2-5. Each step: TODO paste real output.

- [x] **Baseline** (read-only, owner-approved, 2026-11-08 18:25 UTC). Per host: `esxcli software acceptance get`, `secureBoot.py -s`,
  `vsish -e get /hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/0x0`,
  `wc -l /var/log/vmkwarning.log`. hyp03's SSH was started for the read and stopped afterwards.

  | | hyp02 | hyp03 |
  |---|---|---|
  | ESXi | 9.1.1 build 25714478 | 9.1.1 build 25714478 |
  | Acceptance | PartnerSupported | PartnerSupported |
  | Secure Boot | Disabled | Disabled |
  | Root complex (0x0) | `0x14d81022` | `0x14d81022` |
  | SMN index (0x60) | `0x00000000` | `0x00000000` |
  | CPU family / model | 0x19 / 0x61, 16C/32T | 0x19 / 0x61, 16C/32T |
  | vmkwarning lines (non-vsantrace) | 1786 (1) | 2407 (2) |
  | lab-hwhealth / lab report | none / none | none / none |
  | NVMe | vmhba0 CT4000P310SSD8, vmhba1 MZVLB512HAJQ-000L7, vmhba2 CT1000P310SSD2 | same models |
  | NVMe temp / spare / used | 43/43/42 °C, 100 %, 1/2/0 % | 42/42/37 °C, 100 %, 1/4/0 % |
  | Critical warning / media errors | none / 0 | none / 0 |

  The non-vsantrace lines are all `NetDVS: Failed to sync data from client com.vmware.common.port.statistics`.
- [x] **Depot** (2026-11-08). The bundle (sha256 `29f9f031…dcb962`) is kept at
  `PROD/esx/lab-hwhealth/` on the NAS depot, served as
  `http://172.16.10.50:8080/esx/lab-hwhealth/lab-hwhealth-1.0.1-1-bundle.zip`. vCenter imported it as
  offline depot `E461E583…` (PULL), and depot content lists `lab-hwhealth 1.0.1-1`.
  `lab01-cluster-001`'s image now carries the component (draft validate + commit, image commit 9;
  validation warned only that the compatibility data is out of date). A first draft
  committed with no change (commit 7) because the add call's body was wrong; it is harmless.
  The REST add takes the bare version string as the body: `PUT …/drafts/<d>/software/components/lab-hwhealth`
  with `"1.0.1-1"`.

  Compliance scan after the commit: hyp01 COMPLIANT (no impact); hyp02 and hyp03 INCOMPATIBLE:
  "Acceptance level(s) of component(s) 'lab-hwhealth' are 'CommunitySupported' …, which are incompatible
  with the host's acceptance level 'PartnerSupported'." Lower each host to CommunitySupported before
  remediating it.
- [ ] **hyp01.** Maintenance mode, remediate, then watch `vmkernel.log` and `vmkwarning.log` for
  30 min. TODO: paste real output.
- [x] **hyp02** (2026-11-08, owner-approved). devvm02 and devvm03 were shut down via guest tools first
  (15 s). Acceptance set to `community` through vCenter (`UpdateHostImageAcceptanceLevel`). The vLCM
  check for host-26 passed with warnings only (HCL, stale compatibility data, powered-off local VMs).
  `apply` for host-26 succeeded in 26 s **without maintenance mode** (the VIB is live-install), so no
  VM moved. Afterwards:
  - `lab.hwHealth` was missing until `/etc/init.d/health restart` (as expected for a new plugin).
  - **Finding: the CPU readings failed** — `rapl`, `eff_mhz`, `tctl_c`, `ccd_c` all
    `Operation not permitted`; NVMe worked. A vLCM live install runs the init script's `start` from
    vLCM's own (restricted) security domain, and the sampler inherits it. On hyp01 the install ran
    from an SSH shell, so the sampler had the unrestricted domain.
  - Workaround, proven: `/etc/init.d/lab-hwhealth restart` from SSH. 70 s later: Tctl 74.4 °C,
    CCD 71.4/74.1 °C, 84.9 W, eff avg 4743 MHz, `errors` empty, SMN index 0. A reboot should give the
    same result because init scripts run at boot, but that is not yet shown after a vLCM install.
  - Code fix to do: the init script should start the sampler in a domain that permits the reads
    rather than inheriting the caller's, or vLCM installs need the restart step.
- [x] **hyp03** (2026-11-08, run by the owner). Acceptance `partner` → `community`; check passed with
  warnings only; `apply` for host-28 succeeded in 26 s without maintenance mode. SSH started, then
  `health` and `lab-hwhealth` restarted from SSH: `lab.hwHealth` listed, Tctl 75.9 °C, CCD 65.8/68.4 °C,
  88.3 W, `errors []`, NVMe 42/42/37 °C, SMN index 0. SSH stopped again.
- [x] **Verify** (2026-11-08 ~18:46 UTC). The S3 remote recipe from the workstation returns
  `lab.hwHealth` on all three hosts with CPU readings and `errors []` (Tctl 78.4 / 74.4 / 74.8 °C).
  Cluster compliance scan: `lab01-cluster-001` COMPLIANT, every host `lab-hwhealth` 1.0.1-1 COMPLIANT.
- [x] **Upgrade to 1.0.2-1** (2026-11-08 19:30-19:37 UTC, owner-approved, no SSH needed). Bundle
  published to `http://172.16.10.50:8080/esx/lab-hwhealth/lab-hwhealth-1.0.2-1-bundle.zip` (sha256
  `c3c8af55…f414`), imported as offline depot `D1276354…`; image commit 11 sets 1.0.2-1. The scan showed
  all three hosts NON_COMPLIANT with NO_IMPACT. Each host: vLCM `apply`, then from vCenter
  `RefreshServices` + `RestartService("lab-hwhealth")`.
  - hyp01 right after `apply`: sampler in domain **102** (settingsdDom, the same ID as the settingsd
    process). This reproduces the bug on real hardware.
  - After the vCenter restart: domain **0**; Tctl 77.5 °C, CCD 79.6/64.5 °C, 85.0 W, `errors []`,
    SMN index 0. hyp02 likewise: domain 0, fresh sample, `errors []`.
  - Remote reads on all three: CPU readings present, `errors []` (Tctl 77.9 / 74.5 / 74.8 °C).
    Cluster COMPLIANT, every host `lab-hwhealth` 1.0.2-1.

### After a vLCM install

Why: which security domain the init script's `start` runs in depends on who runs it. At boot, from
an SSH shell, through `esxcli`, or from hostd (`hostdDom` has `-d initObj superDom file_exec grant`,
and `/etc/init.d/*` is labelled `initObj`) it gets superDom. vLCM applies through settingsd;
`settingsdDom` has no exec transition and grants neither `MSR_READ` nor the PCI write, so the
sampler inherits a domain where `rapl`, `eff_mhz`, `tctl_c` and `ccd_c` fail with
"Operation not permitted" (hyp02, 2026-11-08). NVMe still works.

**1.0.2-1 and later (hostd service):** after each vLCM remediation, from vCenter, per host:
`HostServiceSystem.RefreshServices()` then `RestartService(id="lab-hwhealth")`. No SSH. Then
check that the CPU fields are present and `errors` is empty (the remote read in "Verify").

Notes on the service entry:
- It has no firewall `<ruleset>` because the sampler opens no port. It shows in the host's Services
  list without a firewall tie-in.
- `<service id="0000">` is an index within the file, not a key. Every stock single-service file
  (`dpd`, `fdm`, `hbrsrv`, `vltd`, `vpxa`) uses `0000` too (hyp01, 2026-11-08).
- After `component remove`, hostd keeps listing the service until the next `RefreshServices` or
  hostd restart.

**1.0.1-1:** run `/etc/init.d/lab-hwhealth restart` from SSH on each host.

**First install only, any version:** healthd needs `/etc/init.d/health restart` (SSH) or a reboot
before it launches a new plugin. An upgrade keeps the plugin path, so it does not need this.

A live `component apply` runs the init script's `start` (Task 6, section 2). vLCM remediation does
not restart healthd: on hyp02 and hyp03 the report was missing until `/etc/init.d/health restart`.

## Verify

```
esxcli system health report get -r lab.hwHealth
```

Healthy: `structured.status` 0 and `magnitude` 0; `unstructured.cpu.supported` true with
`tctl_c`, `ccd_c`, `package_w` and 32 `eff_mhz` values; one `nvme` entry per adapter; `errors`
empty; `sampled_at` less than 180 s old. The report refreshes about every 5 minutes (FAST).

Remote read (S3), vAPI JSON-RPC (full bodies in findings, S3). Load credentials by key name
(`LAB_ESX_USER` / `LAB_ESX_PASS`), never print them or the token:

1. `POST https://<host>/api`, headers `vapi-service: com.vmware.esx.authentication.token`,
   `vapi-operation: create`, `Content-Type: application/json`, security context
   `com.vmware.vapi.std.security.user_pass`. The result holds a JWT in
   `...token_info.access_token`.
2. `POST https://<host>/api`, headers `vapi-service: com.vmware.esx.observability.reports`,
   `vapi-operation: get`, input `{"report": "lab.hwHealth"}`, security context
   `com.vmware.vapi.std.security.oauth` with the JWT. The fields are `health_data` (JSON string),
   `status`, `importance`, `tags`, `last_update_time`.

`unauthorized` means the user lacks `OBSERVABILITY`. REST-style GETs do not work.

## Remove

```
esxcli software component remove -n lab-hwhealth
rm -f /var/lib/vmware/osdata/healthd/lab.hwHealth*
/etc/init.d/health restart
```

Remove is live and runs the init script's `stop`. The last report stays served until the purge.
Optional: `rm -rf /usr/lib/vmware/healthd/lab-hwhealth` clears the empty RAM directories that
`component remove` leaves; a reboot clears them too. Removing the component also deletes any
`configstorecli` override for the key.

## Upgrade

An in-place `esxcli software component apply -d <bundle>` of a higher version works live
(Round 1, D and E): the old version is removed, the sampler is stopped and started, and an
updated plugin is picked up with no healthd restart. After an ESX upgrade that drops the
component, reinstall it (and re-check acceptance level).

## Known limits

- An NVMe `esxcli` read can take about 20 s on the nested host (`nvme: timeout` seen after install and boot).
- The first report after boot carries `esxcli` errors (503, refused, timeout) until hostd is up;
  the sampler recovers within about a minute.
- A cache older than 180 s gives WARNING "cache stale".
- Any manual root run without `-B` could write `__pycache__`; the entrypoints set
  `sys.dont_write_bytecode`, so this should not happen.
- The component is not tested on real hyp02 or hyp03 hardware: TODO.
- If the `finally` restore of the SMN index itself fails, nothing recovers it: every later run
  reports "smn index busy" until a reboot. A hand restore (`vsish -e set
  /hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/0x60 0x0`) is allowed only
  after reading the index and confirming it is one of the project's three addresses (`0x59800`,
  `0x59b08`, `0x59b0c`); any other value belongs to someone else, so leave it alone.
