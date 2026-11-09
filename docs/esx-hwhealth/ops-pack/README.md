# VCF Operations pack for `lab.hwHealth`

How VCF Operations gets the `lab.hwHealth` report from hyp01-hyp03: the account it uses, the
request it makes, how each report field maps to a metric on the existing ESX Host object, and
the alert rules. Plan Task 8. The pack is built by hand in Management Pack Builder (MPB), and
its export goes in this folder.

Field names and thresholds come from the design spec
(`docs/superpowers/specs/2026-11-06-esx-hardware-health-design.md`, "Report contents" and
"Thresholds"). The request recipe comes from `docs/esx-hwhealth/spike-findings.md` (S3).

## 1. Account: `svc-hwh-ops` on each host

This is a local ESX user with no shell, whose only vAPI entitlement is `OBSERVABILITY`.
Operations never uses root. It is created by `scripts/esx-hwhealth/ops/ops_account.py`, one host
at a time. Every mode needs `--ca-file <knowledgeondemand-LabRoot-CA pem>` (the hosts'
certificates are issued by that CA), or an explicit `--insecure`.

| Step | Command | Changes the host |
|---|---|---|
| 0 | Add `LAB_HWH_OPS_PASS` to `~/.vcflab/credentials.env`: at least 12 characters, at least 3 classes. Store the same value in MPB as the credential. | no |
| 1 | `ops_account.py <ip> plan`: shows lockdown mode, whether the user exists, its host roles (root folder and host), and its ClientProfile grants | no |
| 2 | `ops_account.py <ip> apply`: creates the local user (no shell, no host role) and a ClientProfile `LU###svc-hwh-ops` with exactly `[OBSERVABILITY]`, then re-reads the state and fails unless it is exactly that | **yes** |
| 3 | `ops_account.py <ip> verify`: as admin, checks grants == `[OBSERVABILITY]` and roles none or ReadOnly; then as `svc-hwh-ops`, reads `lab.hwHealth` and checks that `client_profiles.list` is refused | no |

Run it with a Python that has pyVmomi, from the repo root. In this session that was the scratchpad
venv, `scvenv/Scripts/python.exe`. Host IPs: hyp01 `172.16.10.145`, hyp02 `.143`, hyp03 `.141`.
`plan` was run against hyp01 on 2026-11-09: not in lockdown, no user, no roles, no profile.

- **Not yet proven: whether a user with no host role can get a vAPI token.** If `verify` reports
  `FAIL token as svc-hwh-ops: unauthorized`, run `apply --readonly-role`. That adds the built-in
  ReadOnly role on the host root, which carries no write privilege. `unauthenticated` means a wrong
  password instead: an existing user keeps its old one. Record which way it went here.
- **`apply` refuses before changing anything** when: the host is in lockdown; the connection is not
  host-direct; the user already holds a host role other than ReadOnly; the ClientProfile holds any
  grant other than `OBSERVABILITY`; or the password is below the floor.
- `apply` is idempotent: a second run changes nothing. If ESX creates `LU###<user>` along with the
  user (root's profile existed without being listed), `apply` handles the `already_exists`, re-reads
  the profile, and sets its grants only if they are empty.
- If `apply` stops partway, its `step:` lines show what completed, and `plan` shows the state.
- The account is local to each host. A host rebuild loses it, so re-run `apply`.
- To remove the account: delete the ClientProfile `LU###svc-hwh-ops`, then the local user (host
  client: Manage > Security & users). The script does not do removal.

## 2. The request MPB has to make (per host)

There are two calls, both `POST https://<host>/api`, both vAPI JSON-RPC.

1. **Session (token).** Headers: `vapi-service: com.vmware.esx.authentication.token`,
   `vapi-operation: create`, `Content-Type: application/json`. Body:
   ```json
   {"jsonrpc":"2.0","method":"invoke","id":"1","params":{
     "serviceId":"com.vmware.esx.authentication.token","operationId":"create",
     "input":{"STRUCTURE":{"operation-input":{}}},
     "ctx":{"appCtx":{},"securityCtx":{"schemeId":"com.vmware.vapi.std.security.user_pass",
       "userName":"svc-hwh-ops","password":"<credential>"}}}}
   ```
   The token is at
   `result.output.STRUCTURE["com.vmware.esx.authentication.token.token_info"].access_token`.
   It is a 3-part JWT; on some builds it is wrapped as `{"SECRET": "<jwt>"}`.
2. **Report.** Headers: `vapi-service: com.vmware.esx.observability.reports`,
   `vapi-operation: get`, `Content-Type: application/json`. Body:
   ```json
   {"jsonrpc":"2.0","method":"invoke","id":"1","params":{
     "serviceId":"com.vmware.esx.observability.reports","operationId":"get",
     "input":{"STRUCTURE":{"operation-input":{"report":"lab.hwHealth"}}},
     "ctx":{"appCtx":{},"securityCtx":{"schemeId":"com.vmware.vapi.std.security.oauth",
       "accessToken":"<token from call 1>"}}}}
   ```
   The answer is at `result.output.STRUCTURE["com.vmware.esx.observability.reports.info"]`.

Gotchas:
- The `vapi-operation` header is required. Without it the call fails with
  `-31001 Mismatching operation identifier`.
- The REST form (`GET /api/esx/observability/reports/...`) hangs.
- vCenter does not proxy these reports, so every host is its own source.
- The certificates are lab-CA. Trust the lab CA in Operations rather than disabling verification.

### Settle these in MPB first (unproven, and they decide whether MPB can do this alone)

1. **The token goes in the request body, not in a header.** MPB session auth is documented and
   used for a token in a **header** (`${authentication.session.<var>}`). Whether MPB substitutes a
   session variable inside a POST **body** is not documented. One community thread reports MPB
   sending a JSON body as a raw string; another reply disputes it. Test: build the source, run
   "Test connection", and check whether call 2 returns data or `unauthorized`.
2. **`health_data` is a JSON document encoded as a string** (`"health_data": "{\"cpu\":...}"`).
   MPB's response paths walk JSON objects. Whether it can parse a string field as JSON is not
   documented, and this is the bigger risk. Test: in the request preview, check whether
   `health_data.cpu.tctl_c` is offered as a path.
3. **Arrays.** `cpu.ccd_c` is a list of numbers, and `nvme` is a list of objects keyed by
   `adapter`. Check whether MPB can index `ccd_c[0]` and `ccd_c[1]`, and whether it can make one
   object or instance per `nvme[*]`.

**Fallback if 1 or 2 fails:** a small read-only collector in its own LXC, following the lab's
deployment model. It would do the two-step login per host and serve the parsed report as plain
JSON over HTTPS, with a static bearer token, for MPB to read. This is not built yet and needs the
owner's go-ahead, because it adds a service. The field mapping below is the same either way.

## 3. Field mapping

All metrics attach to the **existing vSphere ESX Host object** (adapter kind `VMWARE`, resource
kind `HostSystem`). They are matched on the host **name as vCenter shows it**:
`hyp01.lab.knowledgeondemand.net`, `hyp02…`, `hyp03…`. The report itself does not contain the
host name: it is known only from the address that was called. Each MPB source (or the fallback
collector) must therefore carry the name as configuration and emit it as the matching property.
Do not use the IP, because the vSphere object name is the FQDN.

Metric group: `Hardware Health`. `info.*` is the API envelope; `hd.*` is the parsed
`health_data`.

| Report field | Ops metric (path under `Hardware Health`) | Unit | Kind | Notes |
|---|---|---|---|---|
| `info.status` | `Report\|Status` | 0/1/2 | metric | Map `OK`→0, `WARNING`→1, `ERROR`→2. The plugin's own verdict on the spec thresholds. |
| `info.status` | `Report\|Status Text` | — | property | The raw string, for the alert text. |
| `info.last_update_time` | `Report\|Age (min)` | min | metric | now − last_update_time. healthd runs the plugin every 5 min. |
| `hd.sampled_at` | `Report\|Sample Age (min)` | min | metric | now − sampled_at. The sampler runs every ~60 s. |
| `hd.errors` | `Report\|Error Count` | count | metric | `len(errors)`. A nonzero value means a reading failed. |
| `hd.errors[*].reading` + `reason` | `Report\|Errors` | — | property | Joined as `reading: reason; …`. |
| `hd.duration_ms` | `Report\|Sample Duration (ms)` | ms | metric | |
| `hd.schema` | `Report\|Schema` | — | property | Must be 1. A change means the field contract changed. |
| `hd.cpu.supported` | `CPU\|Supported` | bool | property | |
| `hd.cpu.tctl_c` | `CPU\|Tctl (°C)` | °C | metric | Control temperature. The alert source. |
| `hd.cpu.ccd_c[0]` | `CPU\|CCD 0 (°C)` | °C | metric | |
| `hd.cpu.ccd_c[1]` | `CPU\|CCD 1 (°C)` | °C | metric | The hosts report 2 CCDs. |
| `hd.cpu.package_w` | `CPU\|Package Power (W)` | W | metric | RAPL package energy over the sample window. |
| `hd.cpu.eff_mhz_summary.min` | `CPU\|Effective Clock Min (MHz)` | MHz | metric | Chart only: drops for idle as well as heat. |
| `hd.cpu.eff_mhz_summary.avg` | `CPU\|Effective Clock Avg (MHz)` | MHz | metric | Chart only. |
| `hd.cpu.eff_mhz_summary.max` | `CPU\|Effective Clock Max (MHz)` | MHz | metric | Chart only. |
| `hd.cpu.eff_mhz[*]` | — | — | not mapped | 32 per-thread values. Use the summary. |
| `hd.cpu.family` / `model` | `CPU\|Family` / `CPU\|Model` | — | property | 25 / 97. |
| `hd.nvme[a].temp_c` | `NVMe:<adapter>\|Temperature (°C)` | °C | metric | Instance per `adapter` (`vmhba0`, `vmhba1`, `vmhba2`). |
| `hd.nvme[a].wctemp_c` | `NVMe:<adapter>\|Warning Temp (°C)` | °C | property | The drive's WCTEMP. The ERROR level. |
| `hd.nvme[a].cctemp_c` | `NVMe:<adapter>\|Critical Temp (°C)` | °C | property | |
| `hd.nvme[a].spare_pct` | `NVMe:<adapter>\|Available Spare (%)` | % | metric | |
| `hd.nvme[a].spare_threshold_pct` | `NVMe:<adapter>\|Spare Threshold (%)` | % | property | null when the drive doesn't report it. |
| `hd.nvme[a].used_pct` | `NVMe:<adapter>\|Endurance Used (%)` | % | metric | |
| `hd.nvme[a].media_errors` | `NVMe:<adapter>\|Media Errors` | count | metric | |
| `hd.nvme[a].critical_warning` | `NVMe:<adapter>\|Critical Warning` | bitmask | metric | 0x01 spare, 0x02 temperature, 0x04 reliability, 0x08 read-only, 0x10 volatile backup. |
| `hd.nvme[a].model` | `NVMe:<adapter>\|Model` | — | property | |

The `vmhbaN` numbering was the same on all three hosts on 2026-11-09: vmhba0 = CT4000P310 (4 TB),
vmhba1 = Samsung MZVLB512HAJQ (PM981, 512 GB), vmhba2 = CT1000P310 (1 TB). It can change after a
hardware change, so the `Model` property is there to catch that.

## 4. Alert definitions

The plugin already applies the spec thresholds, and `Report|Status` carries its verdict. Alerting
on the status alone would catch everything. The per-metric alerts below exist so that the email
names the reading. Severities follow the spec's WARNING/ERROR.

| Alert | Condition | Severity |
|---|---|---|
| CPU hot | `CPU\|Tctl` or any `CCD n` ≥ 90 °C | Warning |
| CPU critical | `CPU\|Tctl` or any `CCD n` ≥ 95 °C | Critical |
| NVMe hot | `NVMe:*\|Temperature` ≥ 70 °C | Warning |
| NVMe critical temperature | `NVMe:*\|Temperature` ≥ that drive's `Warning Temp` (80 °C if missing) | Critical |
| NVMe critical warning | `Critical Warning` has any of bits 0x01-0x10 | Critical |
| NVMe spare low | `Available Spare` ≤ threshold + 10 | Warning |
| NVMe spare exhausted | `Available Spare` < threshold (10 if missing) | Critical |
| NVMe wear | `Endurance Used` ≥ 80 / ≥ 100 | Warning / Critical |
| NVMe media errors | `Media Errors` > 0 | Critical |
| Report not OK | `Report\|Status` ≥ 1 (with `Status Text` and `Errors` in the message) | Warning (1) / Critical (2) |
| Sampler stopped | `Report\|Error Count` > 0 for 2 cycles, or `Sample Age` > 5 min | Warning |
| Report stale | `Report\|Age` > 15 min (healthd or the plugin stopped) | Warning |
| Collection failing | MPB adapter instance collection failed (token, network, account removed) | Warning |

Nothing alerts on the effective clock. It drops for idle reasons as well as heat (spec,
"Builder's call").

Two notes:
- A dead sampler shows up twice, on purpose. The plugin itself reports `WARNING` with the error
  `cache stale` once the cache is over 180 s old, so `Report not OK` fires too.
- After a **live** vLCM install, the sampler loses its CPU readings until the vCenter restart
  (runbook, "After a vLCM install"). `Report not OK` and `Sampler stopped` are what will show it.

## 5. Dashboard "Lab hardware health"

- Tctl, CCD 0 and CCD 1 per host over time, with 90 and 95 °C lines.
- Package power per host.
- NVMe temperature per host and adapter, with a 70 °C line.
- A table of the current `Report|Status Text`, `Errors`, `Sample Age` and `Endurance Used`.

## 6. Owner steps still to record here

- Which way token creation went: no role, or `--readonly-role`.
- The answers to the three "settle in MPB" questions, and whether the fallback collector was
  needed.
- The pack export (file name and sha256), the collection interval (5 min), and the email
  plugin's SMTP relay and recipient. Any secret goes in `credentials.env` under a named key.
- The alert proof: which host's WARNING threshold was lowered, the time the email arrived, and
  that the threshold was restored.
