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

## Go/no-go evidence for the owner

### a. First real-host run, hyp01, 2026-11-08 03:18-03:20 UTC

Code at `cde1820`, approved by the owner. One manual `lab_hwhealth.py --stdout` run.

- Before: root complex `0x14d81022`, SMN index `0x00000000`, `vmkwarning.log` 1806 lines.
- `--stdout` run: exit 0 in 2.65 s, status 0, supported true, `tctl_c` 77.75, `ccd_c` [70.375, 72.25],
  `package_w` 83.7 W, 32 `eff_mhz` values (min 4246.5, avg 4726.1, max 4961.5).
- NVMe: vmhba0, vmhba1, vmhba2 at 44/45/43 C; wctemp 86/81/86; cctemp 88/82/88. `errors` [].
- After: SMN index `0x00000000`. A hand SMN read gave `0x7f9b0000` = 78.5 C, within 3 C of the
  plugin. Index `0x00000000` again. `vmkwarning.log` still 1806 lines. The `/tmp` copy was removed.

### b. SMN race risk (spec, "SMN safety rule")

The SMN index register is shared state and ESX has no lock for it. A small race remains between
the index check and the write, and nobody knows whether vmkernel ever uses this register.
Mitigations in the code:

- the index is read first; non-zero means the temperatures are skipped for that run;
- the index is restored to 0 in a `finally`;
- the root-complex gate: SMN is touched only when config offset 0x0 reads `0x14d81022`;
- only the three SMN addresses are used;
- runs are at least 60 s apart.

### c. Acceptance level

PartnerSupported to **CommunitySupported** on hyp01-hyp03 (the VIB is community-level).
Decision: TODO owner.

### d. Secure Boot

Re-check `Disabled` on all three hosts before install:
`/usr/lib/vmware/secureboot/bin/secureBoot.py -s`. hyp01 showed Disabled on 2026-11-07
(findings, Task 1A section 6). hyp02, hyp03: TODO paste real output.

### e. Remote reads (S3)

Remote reads need an `OBSERVABILITY` ClientProfile entitlement on each host. A dedicated local user
with only that entitlement is preferred to root. vCenter does **not** proxy host reports
(`/api/esx/observability/reports` returns 404; none of its vAPI services is observability).
Decision: TODO owner.

## Rollout (owner)

Plan Task 7, Steps 2-5. Each step: TODO paste real output.

- [ ] **Baseline.** Per host: `esxcli software acceptance get`, `secureBoot.py -s`,
  `vsish -e get /hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/0x0`,
  `wc -l /var/log/vmkwarning.log`. TODO: paste real output.
- [ ] **Depot.** Upload the bundle to the vLCM depot and add the component to the cluster image.
  Confirm the depot kept `configSchemas/` (the S4 note). TODO: paste real output.
- [ ] **hyp01.** Maintenance mode, remediate, then watch `vmkernel.log` and `vmkwarning.log` for
  30 min. TODO: paste real output.
- [ ] **hyp02**, same steps. TODO: paste real output.
- [ ] **hyp03**, same steps. TODO: paste real output.
- [ ] **Verify** with the S3 remote recipe below. TODO: paste real output.

A live `component apply` runs the init script's `start`, so the sampler needs no manual start
(Task 6, section 2). healthd still launches a **newly installed** plugin only after
`/etc/init.d/health restart` or a reboot. Whether vLCM remediation runs that restart is
unproven: after remediating, check `esxcli system health report list | grep lab` and restart
healthd if the report is missing.

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
