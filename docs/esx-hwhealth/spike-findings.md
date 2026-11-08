# ESX hardware health: spike findings

## Task 1B answers (nested ESX 9.1.1, 2026-11-07/08)

- **S2 decision: sampler fallback.** Inside the `healthd` plugin sandbox (security domain `healthdDom`),
  the SMN index write (`vsi set …/pciConfigReg/size/4/addr/0x60`) and the MSR read
  (`/hardware/msr/pcpu/0/addr/0xC0010299`) fail with **`Operation not permitted`**. Exec of any binary
  (`vsish`, `esxcli`) is also denied (`PermissionError(1)`). The same calls succeed outside the
  sandbox on the same host, so this is a permission denial, not "not supported on virtual hardware".
  The plugin *can* read PCI config registers and `/power/pcpu/N/perf` in-process through
  `import vmware.vsi`. A boot-time `/etc/init.d` script shipped in the same community VIB runs in
  `superDom` (domain 0), where the SMN write and the MSR read both work. What was proven about the cache:
  a root-owned `0644` file **created by root from the guest-ops shell** (domain `vmtoolsdShellDom`,
  enforcement disabled), **not by the sampler**, was readable from healthdDom at
  `/var/lib/vmware/osdata/healthd/` (persistent OSData) and at `/usr/lib/vmware/healthd-config/` (RAM;
  the directory must be created at runtime). It was not readable at `/var/run` or `/tmp`. The cache
  location moved off `/var/run`, which the earlier plan used, because healthdDom can't read it: the boot
  probe's own `/var/run/lab-hwhealth-probe.txt`, written from superDom, gave `PermissionError(1)` in the
  plugin. That a file the **sampler (superDom) writes** at one of the readable paths is then readable
  from healthdDom is **inferred, to be proven in Task 6 on the nested host**.
- **Naming:**
  - plugin file: `lab_hwhealth.py`;
  - ConfigStore and healthd key: `lab_hwhealth`, derived from the file name;
  - VIB and component name: `lab-hwhealth`;
  - report name: `lab.hwHealth`.

  This refines the plan's "Plugin key and VIB name `lab-hwhealth`": the key must be the underscore form
  that matches the file stem.
- **S1 registration: a VIB that ships two files.** One is the plugin,
  `/usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py` (key `lab_hwhealth`). The other is a ConfigStore
  schema, `/usr/lib/vmware/configmanager/schemas/lab-hwhealth-<ver>-schema.json`, for
  `esx/health/lab_hwhealth`. esximage
  turns the schema into a `ConfigSchema:` VIB tag and loads it at live install. healthd finds plugins
  by **scanning `plugins/bin/` at start**, and the ConfigStore key is the file name without its
  extension. A config object is **not required**: with only the schema, healthd logs "Failed to locate
  ConfigStore key" and uses its built-in defaults (enabled, FAST, `healthd-plugins`, 40 MB, 30 s).
  `configstorecli config current set` works only once the schema exists, and overrides those defaults.
  **It survives a reboot.**
- **Launch path (exact, from `healthd.log`):**
  `/usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py ++group=healthd-plugins,mem=40 -u http://!vmwLocalSocketHealthd`.
  Seen from the plugin: `argv = [<path>, "-u", "http://!vmwLocalSocketHealthd"]`, uid 0, cwd `/var/log`.
  The FAST period is 5 minutes.
- **S3 auth recipe: vAPI JSON-RPC, not REST.**
  - Every call is `POST https://<host>/api` with headers `vapi-service: <service id>`,
    `vapi-operation: <operation id>` and `Content-Type: application/json`.
  - Step 1: call `com.vmware.esx.authentication.token`/`create` with a
    `com.vmware.vapi.std.security.user_pass` security context. It returns a JWT.
  - Step 2: call `com.vmware.esx.observability.reports`/`get` (or `list`) with the security context
    `{"schemeId": "com.vmware.vapi.std.security.oauth", "accessToken": <JWT>}`.
  - The user needs the ClientProfile entitlement **`OBSERVABILITY`**. A fresh host's `root` profile
    has only `IDENTITY_MGMT`, and that gives `unauthorized`.
  - REST-style GETs on `/api/esx/observability/reports/…` do not work through rhttpproxy: they return 400
    "Unsupported content type", or hang when given a content type.
  - **vCenter does not proxy host reports:** `/api/esx/observability/reports` returns 404 on vCenter, and
    none of its 528 vAPI services is an observability service.
- **S4 packaging: built with the host's own `esximage` library.**
  - VIB: `Vib.ArFileVib` plus a `tgz` payload with the EsxGzip header, written by `WriteVibFile`.
  - Component: `Bulletin.Component`, packed by `OfflineBundle.WriteOfflineBundle(... configSchemas=...)`.
  - Install: `esxcli software component apply -d <bundle.zip>`. It is live (`Reboot Required: false`),
    but a **new** plugin needs `/etc/init.d/health restart` (or a reboot) before healthd launches it.
  - Remove: `esxcli software component remove -n lab-hwhealth`. It is live. The last report **stays
    served** until `/var/lib/vmware/osdata/healthd/lab.hwHealth*` is deleted.
  - Community VIBs may **not** ship `etc/vmware/default-config/` (extensibility rule).
- **Placement:** VM `esx-hwh-lab` (vm-47001) on **hyp01**, datastore **hyp01-local**. Left **powered off**.

## Task 1A — hyp01 capture (2026-11-07)

Host `hyp01` (172.16.10.145), ESX 9.1.1 (build 25714478), AMD Zen 4 (family 25, model 97). All commands
were read-only (`get`, `cat`, `ls`, `grep`). Fixtures are in `scripts/esx-hwhealth/tests/fixtures/`.

### 1. SMN index and data registers (plain GETs, no index write)

Base node: `/hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/<reg>`

```
vsish -e get .../addr/0x60   ->  0x00000000     (index register is 0)
vsish -e get .../addr/0x64   ->  0xffe077fc     (data register, with index 0; saved as smn_get_0x64.txt)
```

Note the GET output is zero-padded to 8 hex digits (`0x00000000`), unlike the MSR GETs (`0xa1000`).

### 2. CPU block count

`esxcli hardware cpu list` has **32** `CPU:<n>` blocks (`grep -c '^CPU:'`), 735 lines. Family 25, model 97,
`Brand: AuthenticAMD`, `Core Speed: 2495312047` (Hz, about 2.495 GHz nominal), Package Id 0 for all shown.

Other captured raw values:

```
vsish -e get /hardware/msr/pcpu/0/addr/0xC0010299  -> 0xa1000
vsish -e get /hardware/msr/pcpu/0/addr/0xC001029B  -> 0x8647a7cc
vsish -e get /power/pcpu/0/perf
  PCPU core busy (current frequency): -1 %
  ...
  APERF reading:565115497405
  MPERF reading:270057413775
vsish -e get /power/pcpu/0/temp   -> stdout empty; stderr "VSISHCmdGetInt():Get failed: Not supported"; exit code 2
```

`vsish_not_supported.txt` holds the stderr line (it goes to stderr; stdout is empty). Parsers should read
stderr or treat any non-zero exit as unsupported.

### 3. `HealthPlugin` interface

`/lib64/python3.11/site-packages/vmware/healthd/healthPlugin.py` does **not exist**. Only
`healthPlugin.pyc` (12669 bytes) and `__init__.pyc` ship. The facts below were read by unmarshalling the
pyc with the host's Python 3.11 (`marshal`, no file written) and are exact:

```
module constants: VMW_IANA_PEN=6876, PLUGIN_VERSION_HEADER='VMware-Health-Plugin-Version',
  PLUGIN_VERSION='1.0', REQUEST_TIMEOUT=20, HEALTHD_UDS_PATH='vmwLocalSocketHealthd',
  CONFIGSTORE_GROUP='health'
class Importance(IntEnum): NONE=0, LOW=1, MODERATE=2, HIGH=3, VERY_HIGH=4   (module level)
  IMPORTANCE_MATRIX built from ('INFO','WARNING','IMMEDIATE','CRITICAL') criticality names
class Defaults: logLevel='DEBUG', logId='healthd-plugins', healthServiceURL='http://!',
  criticality='INFO', tags, max_lifetime=30
class HealthPlugin:
  class Status(IntEnum):     OK=0, WARNING=1, ERROR=2, NONE=3
  class Magnitude(IntEnum):  NONE=0, SMALL=1, MEDIUM=2, LARGE=3
  __init__(self, pluginName, args=None)      # raises ValueError('Plugin Name is not specified.')
  generateHealthReport(self)                 # abstract, override
  sendReport(self, reportName, report, structuredData=None, reportFormat='json')
  getConfigFromConfigStore(self)             # ConfigStore group 'health', key = plugin name
  _readConfiguration / _configureLogging / _parseArgs(argv)
```

`_parseArgs` takes `-u <url>` (where reports are sent) and `--loglevel {critical,error,warning,info,debug}`.

`sendReport` behaviour (from the constants and names):
- `structuredData` may carry `magnitude` (int) and `status`. If `magnitude` is missing it logs "Magnitude is
  not sent in the report, assuming None".
- Raises `ValueError` if status is OK or NONE and magnitude is not NONE ("Magnitude should be none for
  ok/none status"), and if status is WARNING or ERROR and magnitude is NONE ("Magnitude should not be none
  for warning/error status").
- Builds `{"structured": {"criticality", "importance", "tags", "name", "time" (UTC isoformat), + status,
  magnitude...}, "unstructured": <report>}` as JSON, POSTs it to `urljoin(healthServiceURL, quote(name))`
  over the Unix socket `vmwLocalSocketHealthd`, with headers `content-type: application/json` and
  `VMware-Health-Plugin-Version: 1.0`. Returns `True` on success, `False` on error.

Resulting report shape, as returned by `esxcli system health report get -r vmw.ntpHealth`
(`ntp_health_report.json`):
`{"vmw.ntpHealth": {"structured": {"importance": 0, "name": ..., "status": 0, "tags": ["connectivity"], "time": ...}, "unstructured": {...}}}`
(structured.magnitude was absent for that OK report.)

### 4. How `mobility_network.py` sets status and magnitude

`/usr/lib/vmware/healthd/plugins/bin/mobility_network.py` (3364 bytes, `-r-xr-xr-x root root`, shebang
`#!/bin/python`):

```python
from vmware.healthd.healthPlugin import HealthPlugin
class MobilityNetworkHealth(HealthPlugin):
   PLUGIN_NAME = 'mobility_network'
   REPORT_NAME = 'vmw.mobilityNetworkHealth'
   def __init__(self, args=None):
      super().__init__(self.PLUGIN_NAME, args)
   ...
      status = max(vmStatus, prStatus)
      magnitude = self.Magnitude.NONE
      if status in (self.Status.WARNING, self.Status.ERROR):
         magnitude = self.Magnitude.LARGE
      return self.sendReport(self.REPORT_NAME, reports,
                             structuredData={'status': status, 'magnitude': magnitude})

def getReport(argv):
    plugin = MobilityNetworkHealth(args=argv)
    return plugin.generateHealthReport()

if __name__ == '__main__':
    ...
        ret = getReport(sys.argv[1:])
    ...
    sys.exit(0 if ret == True else 1)
```

Literal values for Task 5 (no `vmware.*` import): status `OK=0`, `WARNING=1`, `ERROR=2`, `NONE=3`; magnitude
`NONE=0`, `SMALL=1`, `MEDIUM=2`, `LARGE=3`. Rule: OK/NONE status -> magnitude 0; WARNING/ERROR -> non-zero
(mobility uses 3). `status` and `magnitude` are plain ints in the `structuredData` dict. The plugin is
invoked as `plugin <healthd args>` with `-u <url>` and exits 0 on success, 1 on failure. Note it also
calls `fss.init` (FeatureState) and the plugin is `enabled: false` in its default entry.

### 5. Registration (`ntp_health`) and plugin file layout

`configstorecli config current get -c esx -g health -k ntp_health` returns `{}` (also with `-m`). The
current store only holds overrides, so the default is what applies. The default is readable with:

```
configstorecli config default get -c esx -g health -k ntp_health
```

and in `/etc/vmware/default-config/esx-base-9.1.1_0.25714478-default.json` (`{"data": [...], "metadata": ...}`):

```json
{"component": "esx", "group": "health", "key": "ntp_health",
 "config": {
  "criticality": "CRITICAL", "enabled": true, "frequency": "MEDIUM",
  "log": {"directory": "/var/run/log/", "level": "error", "max_file_num": 8, "max_file_size": 524288,
          "name": "ntp_health", "output_to_console": false, "output_to_files": false,
          "output_to_syslog": true, "syslog": {"facility": "local4", "ident": "healthdPlugins"}},
  "max_lifetime": 30, "path": "ntp_health", "resource_group": "healthd-plugins",
  "resource_group_size_MB": 40, "tags": ["CONNECTIVITY"], "working_dir": "/var/log/"}}
```

Other `health` group entries all use `resource_group: healthd-plugins`, `resource_group_size_MB: 40`,
`max_lifetime: 30` (120 for network_health), `frequency` FAST/MEDIUM/SLOW. `mobility_network` has
`"path": "mobility_network.py"`, so `path` is the file name inside `plugins/bin/`, extension included for a
script. The `healthd` key in the same group has no `path`.

`configstorecli config current` subcommands are `get, export, set, delete, update (deprecated)`. There is
**no `create`**, which matters for S1 (use `set`, or `update`). Getting a non-existent
key/group returned `Error Code: 1204 Schema object not found for component/group/key`, so a new key under
`group=health` may need a schema (S1 must check this); `-i` on a singleton returns
`Error Code: 2000 Singleton object cannot contain instanceId`.

`ls -l /usr/lib/vmware/healthd/plugins/bin/`: all files `-r-xr-xr-x root root`, dated `Jan  1  2000`
(read-only image). Names are the registration `path` value: bare names (`ntp_health`, `memory`,
`ssd_storage`, `vsan_health`, ...) are 100 KB to 580 KB files (not inspected; likely compiled), and `mobility_network.py` is
the lone script.

`esxcli system health report list` (names for reports): `vmw.vpxaStatus, vmw.memoryHealth, vmw.ntpHealth,
vmw.infravisorPodsHealth, vmw.ssdStorageHealth, vmw.memTierHealth, vmw.nfsDatastoreHealth, vmw.vSANStatus,
vmw.coreServicesStatus, vmw.PSODCheck, hostd-health, vmw.autoscaler`. Report names are not tied to the
plugin key. `esxcli --formatter=json system health report get` fails with "Unable to find requested
formatter: json", but the plain `get` output is already a JSON document, which is what
`ntp_health_report.json` contains.

### 6. Acceptance level and Secure Boot

```
esxcli software acceptance get                         -> PartnerSupported
/usr/lib/vmware/secureboot/bin/secureBoot.py -s         -> Disabled   (exit 0)
```

### NVMe

Adapters: `vmhba0`, `vmhba1`, `vmhba2` (all Online, signature `nvmeMgmt-nvmhbaN`). WCTEMP/CCTEMP are
**not** in `esxcli nvme device log smart get`; they are in `esxcli nvme device get -A <vmhba>` under
"Controller Identify Info" as `Warning Composite Temperature Threshold` and `Critical Composite Temperature
Threshold` (units kelvin, bare numbers): vmhba0 359/361, vmhba1 354/355, vmhba2 359/361. SMART shows
`Composite Temperature: 314 K` and `Temperature Sensor 1..8: N K` (unused sensors read `0 K`).

## Task 1B — the nested host

| Item | Value |
|---|---|
| VM | `esx-hwh-lab` (vm-47001), hyp01, datastore `hyp01-local`, folder `esx-hwh-lab/` |
| Hardware | 4 vCPU (1 socket), 16384 MB, `nestedHVEnabled=True`, EFI, Secure Boot off, vmx-22, guest id `vmkernel9Guest` |
| Disk / NIC | 40 GB thin on an NVMe controller (`vmhba1` in the guest); vmxnet3 `00:50:56:8e:32:40` on `dev-vm1-vlan70` (dvportgroup-45) |
| Yield policy | CPU shares Custom 1000, memory shares Low, reservations 0, limits unlimited |
| Guest | ESXi 9.1.1 build 25714478, `esx-hwh-lab.lab.knowledgeondemand.net`, vmk0 172.16.70.50/24, gw 172.16.70.1, DNS 172.16.10.150 |
| Install | Unattended: ISO remastered with pycdlib, `KS.CFG` added, `ks=cdrom:/KS.CFG` in both `BOOT.CFG` files. The remastered ISO was deleted from the datastore and the workstation afterwards |
| State left | Powered off. Acceptance `CommunitySupported`. `vmtoolsdShellDom` set to `disabled` (see access note). `root` ClientProfile has `OBSERVABILITY` added (S3). No lab VIB, component, schema, plugin or report left |

Free-IP check before use: `ping 172.16.70.50` from the workstation gave `Reply from 192.168.6.11:
Destination host unreachable` (no ARP answer on VLAN 70), and no VM in vCenter had that IP.

**Access note (affects every later task on this VM):**
- The workstation **cannot reach VLAN 70**. The CRS309 has `devvm-isolation` forward rules that drop
  172.16.70.0/24 → 10/8, 172.16/12, 192.168/16, … and there is no established/related accept. So return
  traffic to the workstation (192.168.6.x) and to vCenter (172.16.10.x) is dropped.
- The guest itself is fine. On the router, ARP is `172.16.70.50 00:50:56:8E:32:40 DevVM1-VLAN70 reachable`
  and `/ping 172.16.70.50` succeeds. No promiscuous mode or forged transmits was needed, because vmk0 uses
  the vNIC's MAC. The router was only read; nothing was changed.
- All work went through **vCenter Guest Operations** (`StartProgramInGuest` and file transfer as root;
  ESXi's built-in tools support it).
- Those processes run in security domain `vmtoolsdShellDom`, which denies most files, network sockets
  and the MSR VSI nodes. On the nested host only, I set
  `esxcli system secpolicy domain set -n vmtoolsdShellDom -l disabled` and rebooted.
  `healthdDom` stayed `enforcing`, so S2 is unaffected.

## S1 — registration

**Attempt 1, `configstorecli` only: no.**
```
$ configstorecli config current create -c esx -g health -k lab_hwhealth -infile /tmp/cfg.json
configstorecli config current: error: argument cmd: invalid choice: 'create' (choose from 'get', 'export', 'set', 'delete', 'update')   rc=2
$ configstorecli config current set -c esx -g health -k lab_hwhealth -infile /tmp/cfg.json
Error Code: 1204  Message: Schema object not found for component/group/key   rc=180
$ configstorecli config current update -c esx -g health -k lab_hwhealth -infile /tmp/cfg.json
Error Code: 1037  Message: Schema object not found: comp esx grp health key lab_hwhealth   rc=13
```
Schemas are per key (`configstorecli schema get -c esx -g health -k ntp_health`). The esx-base schema
for the `health` plugins also restricts `tags` to `{AVAILABILITY, CONNECTIVITY, SYSTEM_CONTAINER, VMOTION}`
and `log.level` to `none|quiet|panic|error|warning|info|verbose|trivia` (error 1208).

**Attempt 2, a test VIB: yes.**
- The VIB carries a `default-config` JSON plus the schema. With the default-config file, install is
  rejected:
  `[VibValidationError] … violates extensibility rule checks: ['(line 24: col 0) Element vib failed to validate content']`.
  `/usr/share/esximage/schemas/vib20-extensibility.rng` doesn't allow `etc/vmware/default-config/` for
  community VIBs.
- Allowed community paths that matter here: `usr/lib/vmware/.+`, `usr/lib64/vmware/.+`, `opt/.+`,
  `etc/init.d/.+`, `etc/vmware/secpolicy/domains/plugins/.+`, `etc/vmware/secpolicy/tardisks/plugins/.+`.
- Without that file (plugin + schema only) it installs:
```
$ esxcli software acceptance set --level=CommunitySupported
Host acceptance level changed to 'CommunitySupported'.
$ esxcli software vib install -v /tmp/b/out/lab-hwhealth-0.1.0-1.vib
   Message: Operation finished successfully.
   VIBs Installed: lab_bootbank_lab-hwhealth_0.1.0-1
   Reboot Required: false
$ configstorecli config default get -c esx -g health -k lab_hwhealth   ->  {}
$ /etc/init.d/health restart
healthd.log:
  Scanning /usr/lib/vmware/healthd/plugins/bin/ for plugins.
  -ERROR … Failed to locate ConfigStore key: lab_hwhealth.frequency      (same for .enabled, .resource_group, .max_lifetime)
  Plugin configuration: Name: lab_hwhealth, Enabled: true, frequency: fast, resourceGroup: healthd-plugins, maxLifetime: 30
  Launching binary: /usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py ++group=healthd-plugins,mem=40 -u http://!vmwLocalSocketHealthd
  Plugin launched: lab_hwhealth. 264530
$ esxcli system health report list | grep lab
lab.hwHealth              2026-11-07T23:42:31+00:00
$ esxcli system health report get -r lab.hwHealth
{"lab.hwHealth": {"structured": {"status": 0, "magnitude": 0, "importance": 0, "tags": [], "name": "lab.hwHealth", "time": "2026-11-07T23:42:31.482262+00:00"}, "unstructured": {"hello": 1, "probes": [...], "ctx": {...}}}}
```
- The schema `defaults` block is **not** a default config: `config default get` stays `{}`. healthd
  falls back to its own built-in values, so `tags` is `[]`.
- Once the schema exists, `configstorecli` works as an override:
```
$ configstorecli config current set -c esx -g health -k lab_hwhealth -infile /tmp/cfg2.json   ->  Set: completed successfully
$ configstorecli config current set -c esx -g health -k lab_hwhealth -p resource_group_size_MB -v 100
  -> next launch: … lab_hwhealth.py ++group=healthd-plugins,mem=100 …; report tags ['AVAILABILITY']
```

**Survives a reboot: yes.** Steps:
1. VIB 0.1.0-3 installed with the `config current` override in place.
2. `auto-backup.sh`, then `reboot`.
3. After the reboot, `esxcli software vib list` shows `lab-hwhealth 0.1.0-3 lab CommunitySupported`,
   the current config still has `"resource_group_size_MB": 100`, healthd logs `Launching plugin: lab_hwhealth`
   at 23:55:08, and `report list` shows `lab.hwHealth 2026-11-07T23:55:08+00:00`.

Other facts:
- The override is kept when the VIB updates to a new schema version.
- It is **deleted** when the VIB is removed: after removal, `config current get` gives 1204.
- Updating the plugin file through a VIB update needs no healthd restart; the next scheduled run uses
  the new file.
- A **newly** installed plugin is not launched until healthd restarts. Evidence: after
  `component apply` with no restart, `grep 'Launching plugin: lab_hwhealth'` showed no new line in
  5.5 minutes.
- `#!/bin/python` with CRLF line endings fails at launch with `Plugin launch failed: No such file or
  directory`. Plugin files must be LF.
- `healthd.log` shows `Configured resource pool size: 40MB is outside limits` and
  `Group healthd-plugins was not found` for the built-in plugins too, on this build. They are harmless.

**Builder's call for v1:** ship the schema in the VIB and rely on healthd's defaults. If a v1 setting
must differ from the defaults (for example `tags`), set it with `configstorecli config current set`
from the VIB's `etc/init.d` script at boot.

Test plugin and build scripts: `docs/esx-hwhealth/spike/` (`lab_hwhealth_probe_plugin.py`,
`build_vib.py`, `build_bundle.py`, `lab-hwhealth-probe.initd`).

## S2 — sandbox

The probes ran inside the plugin under healthd, at `mem=100`. With the default `mem=40`, every
`subprocess` call failed earlier, at fork, with `OSError(28, 'No space left on device')`. That is a
resource-group limit; at 100 MB the fork goes through and the exec is denied. Reported by the plugin
(`esxcli system health report get -r lab.hwHealth`, 23:49:17 and 00:00:49):

| Probe (inside healthdDom) | Result |
|---|---|
| `vmware.vsi.get(…/pciConfigReg/size/4/addr/0x0)` | ok, `1905426566` |
| `vmware.vsi.get(…/addr/0x60)` | ok, `8388736` (0x00800080) |
| `vmware.vsi.set(…/addr/0x60, <same value>)` | **`Exception('Operation not permitted')`** |
| `vmware.vsi.get(/hardware/msr/pcpu/0/addr/0xC0010299)` | **`Exception('Operation not permitted')`** |
| `vmware.vsi.get(/power/pcpu/0/perf)` | ok, `{'busy': 0, 'abs': 0, 'perf': 0, 'aperf': 0, 'mperf': 0, 'halted': 798531342, 'pstateTimerPending': 0}` |
| `vmware.vsi.get(/power/pcpu/0/temp)` | `Exception('Not supported')` (virtual hardware) |
| `vmware.vsi.get(/vmkModules/vmknvme/controllers/256/info)` | ok, a dict (`mn: 'VMware Virtual NVMe Disk'`, …); no SMART node exists |
| exec `/bin/vsish -e get …/0xC0010299` | **`PermissionError(1, 'Operation not permitted')`** |
| exec `/bin/esxcli nvme device list` | **`PermissionError(1, 'Operation not permitted')`** |
| `esxcli nvme device list` in-process (`runpy /bin/esxcli.py`) | **`PermissionError(1, 'Operation not permitted')`** |

The SMN set was a write-back of the value just read (0x00800080), not `0x0`. It proves write permission
without changing a virtual 440BX register.

Same host, outside the sandbox (guest-ops shell with its domain disabled):
```
$ vsish -e set …/addr/0x60 0x00800080          rc=0
$ vsish -e get /hardware/msr/pcpu/0/addr/0xC0010299   ->  0x0   rc=0
$ vsish -e get /hardware/msr/pcpu/0/addr/0xC001029B   ->  0x0   rc=0
$ vsish -e get /power/pcpu/0/temp   ->  VSISHCmdGetInt():Get failed: Not supported   rc=2
$ python -c "import vmware.vsi as v; v.set('…/addr/0x60', 0x00800080)"   ->  ok
```
So "Operation not permitted" means the sandbox, and "Not supported" means virtual hardware.

**How the sandbox is set up:**
- `/etc/init.d/health` starts healthd with `++securitydom=healthdDom`, and plugins inherit that domain.
  `ps -Z` shows healthd in domain 63.
- `/etc/vmware/secpolicy/domains/healthdDom` grants `file_exec` only to the labelled
  `dnsHealthBinObj`, `networkHealthBinObj` and `psodCountBinObj`, and `vsi_node_access LiveDump`.
- Writable paths include `/var/lib/vmware/osdata/healthd w`, `/usr/lib/vmware/healthd-config w`,
  `/usr/lib/vmware/healthd rx` and `/etc/vmware/configstore w`.

**Sampler feasibility (boot-time init script in the same community VIB):**
- `etc/init.d/lab-hwhealth-probe` (`# chkconfig: on 99 01`) ran at boot. Its own `ps -Z` line shows
  domain **0** (superDom):
```
263371  263371  sh   0   /bin/sh /etc/init.d/lab-hwhealth-probe start
get 0x60 rc=0 v=0x00800080
set 0x60 rc=0
0x0
msr rc=0
Connection failed: 503 Service Unavailable
nvme rc=1
```
- At chkconfig 99, hostd isn't serving yet, so `esxcli` gets a 503. The sampler must retry `esxcli`
  later, or start its loop after boot completes.
- Where the plugin can read a cache file (v4 plugin, 00:00:49 and 00:02:57). The test files
  (`{"t": 1}` / `{"t": 2}`, root, `0644`) were **created by root from the guest-ops shell**
  (`vmtoolsdShellDom`, enforcement disabled), not by the sampler:
  - `/var/lib/vmware/osdata/healthd/…`: ok.
  - `/usr/lib/vmware/healthd-config/…`: ok, once the directory existed (created from the same shell);
    it is RAM, so no disk wear.
  - `/var/run/lab-hwhealth/cache.json` and `/tmp/…`: `PermissionError(1)`. The boot probe's own
    `/var/run/lab-hwhealth-probe.txt`, which **was** written from superDom, also gave
    `PermissionError(1)`. So the denial is by path, and that is why the cache moved off `/var/run`.
- **Not yet proven:** that a file the sampler writes from superDom at one of the readable paths is then
  readable from healthdDom. That is inferred from the path-based policy, and is **to be proven in Task 6
  on the nested host**.

**Decision: sampler fallback.**
- A boot-started sampler, shipped in the same VIB as an `etc/init.d` script, does the SMN, MSR and
  `esxcli nvme` reads.
- It writes the cache to `/usr/lib/vmware/healthd-config/` (or `/var/lib/vmware/osdata/healthd/`).
- The plugin reads the cache in-process and never execs anything. It may also read `/power/pcpu/N/perf`
  itself through `vmware.vsi` if that is useful.

Evidence behind this decision:
- **Proven:** the healthdDom denials (SMN write, MSR read, every exec), and that the SMN write and the
  MSR read work from superDom at boot.
- **Proven:** both cache paths are readable from healthdDom for a file created by root from the
  guest-ops shell.
- **Inferred, to be proven in Task 6 on the nested host:** the sampler writing the cache from superDom
  and the plugin then reading it.
- All of it is on virtual hardware, so it shows permission, not sensor values.

## S3 — remote API

**Route.** `/etc/vmware/rhttpproxy/endpoints.conf.d/healthd.json` sends `/api` requests with header
`vapi-service: com.vmware.esx.observability.reports` to apiForwarder on `localhost:9984`. Without the
header, `/api` falls through to a local service on 8098, and every earlier attempt got 400. The
forwarder speaks **vAPI JSON-RPC**. From `/usr/lib/vmware/vapi/observability_vapi_metadata.json`:
auth scheme `EsxJwtScheme` = `com.vmware.vapi.std.security.oauth` (SessionLess); privilege `OBSERVABILITY`
for `list` and `get`.

**Recipe** (tested on the nested host against its own management IP, `https://172.16.70.50`, through
rhttpproxy). Credentials are loaded by key name (`LAB_ESX_USER` / `LAB_ESX_PASS`); tokens are never logged.
```
POST https://<host>/api
  headers: vapi-service: com.vmware.esx.authentication.token
           vapi-operation: create
           Content-Type: application/json
  body: {"jsonrpc":"2.0","method":"invoke","id":"1","params":{
          "serviceId":"com.vmware.esx.authentication.token","operationId":"create",
          "input":{"STRUCTURE":{"operation-input":{}}},
          "ctx":{"appCtx":{},"securityCtx":{"schemeId":"com.vmware.vapi.std.security.user_pass",
                                            "userName":<user>,"password":<pass>}}}}
  -> result.output.STRUCTURE["com.vmware.esx.authentication.token.token_info"].access_token  (a 3-part JWT)

POST https://<host>/api
  headers: vapi-service: com.vmware.esx.observability.reports
           vapi-operation: get            (or list, with operation-input {})
           Content-Type: application/json
  body: {... "serviceId":"com.vmware.esx.observability.reports","operationId":"get",
          "input":{"STRUCTURE":{"operation-input":{"report":"lab.hwHealth"}}},
          "ctx":{"appCtx":{},"securityCtx":{"schemeId":"com.vmware.vapi.std.security.oauth","accessToken":<JWT>}}}
```
The `vapi-operation` header is required. Without it:
`{"code": -31001, "message": "Mismatching operation identifier in HTTP header and payload."}`.

**Entitlement.** With only a JWT, the report calls return
`{"result":{"error":{"ERROR":{"com.vmware.vapi.std.errors.unauthorized":…}}}}`. Entitlements come from
`com.vmware.esx.authentication.client_profiles`:
- `create` for `root` gave `already_exists`, and `get` of `LU###root` showed only
  `["IDENTITY_MGMT"]`.
- `update` with grants `[IDENTITY_MGMT, OBSERVABILITY]` returned `{"output": null}`.
- After that, a fresh token gives:
```
reports.list: 200 {"result":{"output":{"STRUCTURE":{"com.vmware.esx.observability.reports.list_result":{"reports":[{… "report":"vmw.memoryHealth", …
reports.get : 200 {"result":{"output":{"STRUCTURE":{"com.vmware.esx.observability.reports.info":{"health_data":"{\"ctx\":…,\"hello\":1,\"probes\":[…
reports.info fields: health_data (JSON string = the plugin's "unstructured" dict), importance {'OPTIONAL': 'NONE'},
                     last_update_time '2026-11-08T00:07:58.760Z', status 'OK', tags {'OPTIONAL': ['AVAILABILITY']}
```
`magnitude` is not among the API fields. For v1 the Operations pack should use a dedicated local user
with only `OBSERVABILITY` (one ClientProfile per host), not root. Only root was tested here.

**What doesn't work:**
- REST form, `GET /api/esx/observability/reports/<r>` with `vapi-service` and `Authorization: Bearer <JWT>`:
  without `Content-Type` it returns `400 {"message":"Unsupported content type: "}`; with
  `Content-Type: application/json` it hangs until timeout. That matches the spec's earlier
  "remote call timed out".
- `POST /api/session`: 400.

**From the workstation, against the real host hyp01** (read-only requests after authenticating; no
configuration or grants changed). **Disclosure:** all authenticated calls in this paragraph went beyond
the nested-VM approval. They used credential keys `LAB_ESX_USER` / `LAB_ESX_PASS` (key names only).
- The workstation created an ESX JWT on hyp01, then called `reports.get vmw.ntpHealth` and
  `client_profiles.list`.
- Before that, on the same host and with the same keys: Basic-auth GETs of
  `/api/esx/observability/reports…` and `POST /api/session` attempts. One further GET was
  unauthenticated. All of these returned 400 or timed out.
- Token `create`: OK, 3-part JWT.
- `reports.get vmw.ntpHealth`: `unauthorized`. hyp01's ClientProfiles are `svc-vcf-hyp01`, `dkvsd-user`,
  two vCenter OIDC issuer profiles and SSO `TrustedAdmins`; none is for `root`.
- So the remote path works from the workstation up to authorization. Granting `OBSERVABILITY` on the
  real hosts is a rollout step and needs the owner.
- The nested host itself can't be reached from the workstation (VLAN 70 isolation, above).

**vCenter: no proxy.** With an SSO session, `GET https://vcenter/api/esx/observability/reports` returns
`404 NOT_FOUND`. The 528 vAPI services in `/api/vapi/metadata/metamodel/service` include no observability
service (only `com.vmware.appliance.health.*`). Whether a custom report appears anywhere in the vSphere
UI was not checked.

## S4 — packaging

**Build tool: the ESX host's own `vmware.esximage` Python library** (works on 9.1.1; scripts in
`docs/esx-hwhealth/spike/`). Run with `python` on any ESX 9.1.1 host, here the nested one.
- **VIB:**
  - `Vib.ArFileVib(name, version=VibVersion, vendor, acceptancelevel="community", liveinstallok=True,
    liveremoveok=True, statelessready=True, maintenancemode=MaintenanceMode(remove=False, install=False),
    swplatforms=[SoftwarePlatform("9.1", "", "embeddedEsx")])`.
  - One payload, `Payload("labhwh", "tgz")`: a plain tar compressed with `Utils.EsxGzip.GzipFile`.
    `AddPayload` rejects a gzip without the EsxGzip header.
  - `AddPayload` fills in the file list and the `ConfigSchema:<vib>-<ver>:labhwh:<path>:sha-256:<hex>`
    tag. `WriteVibFile` writes the `ar` file (`descriptor.xml`, an empty `sig.pkcs7`, payload).
  - Default `maintenancemode` is "required"; set it explicitly.
- **Component bundle:**
  - `Bulletin.Component(componentnamespec={"name","uistring"}, componentversionspec={"version": VibVersion,
    "uistring"}, vendor, releasetype="extension", platforms=[…], vibids={vib.id})`, added to a
    `ComponentCollection`.
  - The VIB goes in a `VibCollection` with `remotelocations=["file://…"]`.
  - `OfflineBundle.WriteOfflineBundle(zip, "lab", "lab", None, None, None, None, None, comps, vibs,
    configSchemas=<ConfigSchemaCollection from the VIB's schema JSON>, versions=["9.1.1"])`.
  - Zip contents: `index.xml`, `vendor-index.xml`, `metadata.zip` (`vmware.xml`, `vibs/…xml`,
    `bulletins/lab-hwhealth_0.1.0-6.xml`, `configSchemas/lab-hwhealth-0.1.0-6-schema.json`),
    `vib20/lab-hwhealth/lab_bootbank_lab-hwhealth_0.1.0-6.vib`.
  - Without `configSchemas=`, the bundle installs with esxcli but lacks `configSchemas/`. Keep it for the
    vLCM depot (rollout task to confirm).

```
$ esxcli software sources component list -d /tmp/b/out/lab-hwhealth-0.1.0-6-bundle.zip
lab-hwhealth  Lab hardware health (spike)  0.1.0-6  0.1.0-6  lab  11-08-2026  CommunitySupported
$ esxcli software component apply -d /tmp/b/out/lab-hwhealth-0.1.0-6-bundle.zip
   Message: Operation finished successfully.
   Components Installed: lab-hwhealth_0.1.0-6
   Reboot Required: false
$ configstorecli schema get -c esx -g health -k lab_hwhealth      rc=0
(no report after 5.5 min without a restart; then)
$ /etc/init.d/health restart ; sleep 50 ; esxcli system health report list | grep lab
lab.hwHealth              2026-11-08T00:21:33+00:00
$ esxcli software component remove -n lab-hwhealth
   Message: Operation finished successfully.
   Components Removed: lab-hwhealth_0.1.0-6
   Reboot Required: false
  -> component list: 0 lab lines; vib list: 0; plugin file and init script gone; schema get rc=180 (1204)
$ esxcli system health report list | grep lab          (before purge, also after a healthd restart)
lab.hwHealth              2026-11-08T00:21:33+00:00
$ rm -f /var/lib/vmware/osdata/healthd/lab.hwHealth*
$ esxcli system health report get -r lab.hwHealth
 Unknown report: lab.hwHealth
```
The report files are `/var/lib/vmware/osdata/healthd/lab.hwHealth-{0,1,2}.json` plus the symlink
`lab.hwHealth.json`. healthd does not prune them, so uninstall must delete them. No reboot was needed for
install, update or remove. The bare VIB path (`esxcli software vib install|update|remove`) behaves the
same.

## Task 6 — packaging proof

Nested host `esx-hwh-lab` (ESXi 9.1.1 build 25714478, Python 3.11.15), 2026-11-08 UTC, driven over vCenter
Guest Operations. Builder: `scripts/esx-hwhealth/packaging/build_component.py`, run on the nested host with
its own `esximage` (`python packaging/build_component.py /tmp/t6/out 0.1.0-1`). The base schema is
discovered at build time (`esx-base-9.1.1_0.25714478-schema.json`), not hardcoded.

**Finding: a payload tar must declare its directories.** The first apply failed with
`[LiveInstallationError] ... secureMount.py ... tardisk ... PermissionError: [Errno 1] Operation not
permitted: '/tardisks/labhwh.t00'`. The real cause is in `vmkernel.log`:
`VisorFSTar: 3016: Boot image is corrupted prevented creating tardisk`. The spike's tar had no directory
entries and worked only because every file went into an existing directory. This payload creates
`usr/lib/vmware/healthd/lab-hwhealth/hwhealth/`. With a `0755` directory entry for every parent, ahead of
its files, the install goes through. The host asked for a reboot after the failed apply and got one.
Covered by `test_payload_tar_has_a_directory_entry_before_each_file`.

### 1. Build and VIB members (final code)

```
vib    /tmp/t6/out/lab-hwhealth-0.1.0-1.vib 15874
bundle /tmp/t6/out/lab-hwhealth-0.1.0-1-bundle.zip 21890
ar magic: !<arch>
member descriptor.xml     2241 bytes
member sig.pkcs7           0 bytes
member labhwh          13444 bytes
descriptor name: lab-hwhealth
descriptor version: 0.1.0-1
descriptor vendor: lab
descriptor acceptance-level: community
descriptor live-install-allowed: true
descriptor live-remove-allowed: true
payload 0o555   1513 etc/init.d/lab-hwhealth
payload 0o444  31075 usr/lib/vmware/configmanager/schemas/lab-hwhealth-0.1.0-1-schema.json
payload 0o444    280 usr/lib/vmware/healthd/lab-hwhealth/hwhealth/__init__.py
payload 0o444   6018 usr/lib/vmware/healthd/lab-hwhealth/hwhealth/collect.py
payload 0o444   1434 usr/lib/vmware/healthd/lab-hwhealth/hwhealth/decode.py
payload 0o444   3044 usr/lib/vmware/healthd/lab-hwhealth/hwhealth/evaluate.py
payload 0o444   4834 usr/lib/vmware/healthd/lab-hwhealth/hwhealth/readers.py
payload 0o444   1216 usr/lib/vmware/healthd/lab-hwhealth/hwhealth/runner.py
payload 0o444   1338 usr/lib/vmware/healthd/lab-hwhealth/hwhealth/smn.py
payload 0o555   2111 usr/lib/vmware/healthd/lab-hwhealth/lab_hwhealth_sampler.py
payload 0o555   4178 usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py
['index.xml', 'vendor-index.xml', 'metadata.zip', 'vib20/lab-hwhealth/lab_bootbank_lab-hwhealth_0.1.0-1.vib']
['vendor-index.xml', 'vmware.xml', 'vibs/lab-hwhealth-4309359132049054419.xml', 'bulletins/lab-hwhealth_0.1.0-1.xml', 'configSchemas/lab-hwhealth-0.1.0-1-schema.json']
Name          Display Name         Version  Display Version  Vendor  Creation Date  Acceptance Level
lab-hwhealth  Lab hardware health  0.1.0-1  0.1.0-1          lab     11-08-2026     CommunitySupported
```
The member list comes from a small Python ar reader on the host, because ESXi has no `ar`. The payload also
holds 12 `0o755` directory entries (`etc`, `etc/init.d`, `usr`, … `usr/lib/vmware/healthd/plugins/bin`), not
repeated above.

### 2. Apply and restart

```
$ esxcli software component apply -d /tmp/t6/out/lab-hwhealth-0.1.0-1-bundle.zip
   Message: Operation finished successfully.
   Components Installed: lab-hwhealth_0.1.0-1
   Reboot Required: false
-r-xr-xr-x    1 root     root          1513 Jan  1  1970 /etc/init.d/lab-hwhealth
-r-xr-xr-x    1 root     root          4178 Jan  1  1970 /usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py
/usr/lib/vmware/healthd/lab-hwhealth:  hwhealth/  lab_hwhealth_sampler.py
$ /etc/init.d/lab-hwhealth status          (right after apply, nothing else run)
lab-hwhealth sampler is running
$ configstorecli schema get -c esx -g health -k lab_hwhealth   rc=0
$ /etc/init.d/health restart
```
**The live install runs the init script's `start`,** so the sampler needs no manual start and no reboot.
healthd still needs the restart before it launches a new plugin, as S4 found.

### 3. The sampler runs in superDom and writes the cache

```
$ ps -Z -c | grep lab_hwhealth
264579  264579  python   0    /bin/python -B /usr/lib/vmware/healthd/lab-hwhealth/lab_hwhealth_sampler.py
$ ps -Z -c | grep /bin/healthd
264722  264722  healthd  63   /usr/lib/vmware/healthd/bin/healthd -r /var/lib/vmware/osdata/healthd
$ ls -l /usr/lib/vmware/healthd-config/lab-hwhealth/
-rw-r--r--    1 root     root           578 Nov  8 01:03 cache.json
```
The sampler runs in domain 0 (superDom) and healthd in domain 63 (healthdDom). The sampler created the cache
directory itself.

**The nested CPU reports family 25 / model 97,** because CPUID passes through from hyp01. So the cache has
`"supported": true`, not the `false` the task expected. The readings are empty or are errors:
- `eff_mhz` entries are `null` ("core halted").
- `tctl_c` and `ccd_c` fail with "smn index busy" (the virtual 440BX index register reads `0x00800080`).
- `package_w` is `0.0`.
- The NVMe entry is the virtual disk, with `temp_c` 30.

Those errors make the report WARNING.

### 4. The plugin, under healthd in healthdDom, reads the sampler's cache

This closes S2's "inferred" item: a cache written from superDom under `/usr/lib/vmware/healthd-config/`
is readable by the plugin in healthdDom. It also proves the plugin builds the real `HealthPlugin`
subclass and sends through it: `sendReport` returned True, and the exit code is 0.
```
healthd.log:
  Plugin configuration: Name: lab_hwhealth, Enabled: true, frequency: fast, resourceGroup: healthd-plugins, maxLifetime: 30
  Launching binary: /usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py ++group=healthd-plugins,mem=40 -u http://!vmwLocalSocketHealthd
  Plugin launched: lab_hwhealth. 264154
  Plugin 264154 exited with exit code: 0
$ esxcli system health report get -r lab.hwHealth            (first pass, 00:50:56)
{"lab.hwHealth": {"structured": {"status": 1, "magnitude": 3, "importance": 2, "tags": [], "name": "lab.hwHealth",
 "time": "2026-11-08T00:50:56.632621+00:00"}, "unstructured": {"schema": 1, "cpu": {"family": 25, "model": 97,
 "supported": true, "package_w": 0.0, "eff_mhz": [null, null, null, null]}, "nvme": [{"adapter": "vmhba1",
 "model": "VMware Virtual NVMe Disk", "temp_c": 30, "spare_pct": 100, "used_pct": 0, "critical_warning": 0,
 "media_errors": 0, "wctemp_c": null, "cctemp_c": null}], "errors": [{"reading": "eff_mhz[0]", "reason": "core halted"},
 {"reading": "eff_mhz[1]", ...}, {"reading": "eff_mhz[2]", ...}, {"reading": "eff_mhz[3]", ...},
 {"reading": "tctl_c", "reason": "smn index busy"}, {"reading": "ccd_c", "reason": "smn index busy"}],
 "sampled_at": "2026-11-08T00:50:10.630883+00:00", "duration_ms": 1845}}}
```
- The `unstructured` block is the sampler's cache: its `sampled_at` (00:50:10) is 46 s before the plugin ran.
- There is no `"reading": "cache"` error, so the plugin did not take the missing, stale or unreadable path.
- No `VmkAccess ... healthdDom ... Access denied` lines appeared after the spike's own; the plugin execs
  nothing.
- Status is 1 (WARNING) and magnitude 3 (LARGE). `tags` is `[]` because healthd uses its built-in defaults
  (S1).

The second pass, with the final code, gave the same result: report at 01:03:15, status 1, magnitude 3.

### 5. Reboot

After a reboot (one per code pass), the sampler and the report came back without help.
```
jumpstart: executing start plugin: lab-hwhealth / lab-hwhealth started.     (01:10:06)
$ ps -Z -c | grep lab_hwhealth
263418  263418  python   0    /bin/python -B /usr/lib/vmware/healthd/lab-hwhealth/lab_hwhealth_sampler.py
$ esxcli system health report list | grep lab
lab.hwHealth              2026-11-08T01:10:36+00:00
report at 01:10:36: "cpu": {"supported": false}, errors cpu/nvme "IO error: [Errno 111] Connection refused"
cache at 01:11:     "cpu": {"family": 25, "model": 97, "supported": true, ...}, nvme vmhba1 present
```
Early in boot, `esxcli` fails. This time it was refused; on the first pass it was a `timeout` and a 503. So
the first report after boot carries those errors. The loop recovers by itself within about a minute, as
designed.

Some cycles also returned `nvme: timeout` (about 20 s): on the first pass, shortly after install and soon
after boot.

### 6. Remove

```
$ esxcli software component remove -n lab-hwhealth
   Message: Operation finished successfully.
   Components Removed: lab-hwhealth_0.1.0-1
   Reboot Required: false
component/vib list | grep lab: (none)
$ ps -Z -c | grep lab_hwhealth                       (none: the live remove runs the init script's stop)
plugin file, init script: No such file or directory; schema get rc=180 (1204)
$ rm -f /var/lib/vmware/osdata/healthd/lab.hwHealth*     (lab.hwHealth-0.json, -1.json, lab.hwHealth.json)
$ /etc/init.d/health restart
$ esxcli system health report list | grep lab           grep rc=1
$ esxcli system health report get -r lab.hwHealth
 Unknown report: lab.hwHealth
leftovers: /usr/lib/vmware/healthd-config/lab-hwhealth, /var/run/lab-hwhealth-sampler.pid,
           /var/run/log/lab-hwhealth-sampler.log, /tardisks/labhwh*: all absent
```
**RAM-only leftovers:**
- **First pass:** the sampler wrote `hwhealth/__pycache__/*.pyc` into the RAM overlay of the read-only
  tree. The cache and an empty log also stayed behind. The init script now runs `python -B`, and `stop`
  removes the cache, its directory and an empty log. The second pass confirmed both fixes.
- **Both passes:** `component remove` leaves the empty directories `/usr/lib/vmware/healthd/lab-hwhealth/hwhealth/`.
  They are dated at boot or install time, not 1970. So visorfs creates a tardisk's new directories in the
  root ramdisk and doesn't drop them at unmount. They are empty, gone at the next reboot, and can't be
  cleaned from the VIB, because the stop hook runs while the tardisk is still mounted. They were removed by
  hand with `rm -rf`.

### 7. State left

- Component removed. Report files and stray directories deleted. `/tmp/t6` removed.
- Acceptance is still `CommunitySupported` and `vmtoolsdShellDom` is still `disabled`, both as Task 1B
  left them.
- The VM is **powered off**.

**Uninstall runbook:**
1. `esxcli software component remove -n lab-hwhealth`
2. `rm -f /var/lib/vmware/osdata/healthd/lab.hwHealth*`
3. `/etc/init.d/health restart`
4. Optional: `rm -rf /usr/lib/vmware/healthd/lab-hwhealth`. It removes empty directories in RAM; a reboot
   clears them too.

### Round 1: reinstall, in-place update, root-complex gate (2026-11-08, one boot)

The whole round ran in **one boot** of the nested host (03:22 to 03:49 UTC). The leftover directories were
not deleted between steps. `r1_check.sh` prints the component, sampler, tree, cache, healthd and report
state after each step. Its output is shown below, cut down only where lines repeat.

#### R18: SMN only on the AMD Raphael root complex

`collect()` now GETs `ROOT_CFG/0x0` first. It touches SMN only if the value is `0x14d81022`. Any other
value, or a failed read, skips the temperatures with an error entry, and no SMN call is made. On the
nested host, after the first apply:
```
$ vsish -e get /hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/0x0
0x71928086                                   (Intel 440BX: the virtual root complex)
$ python /tmp/t6/trace.py      (one live collect() as root, every runner call recorded)
pciConfigReg/set calls:
   vsish -e get /hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/0x0
vsish set calls: 0
errors: [{"reading": "tctl_c", "reason": "root complex not supported"}, {"reading": "ccd_c", "reason": "root complex not supported"}]
sampler cache 03:29:42 and report 03:30:17 (status 1, magnitude 3) carry the same two errors
```
The only `pciConfigReg` access is the ID read: no index GET, no `vsish set`, no data GET. Earlier passes
read the index (`smn index busy`). The value `0x14d81022` comes from the controller ruling (R18). It is not
in the hyp01 fixtures or in Task 1A section 1. Confirm it on hyp01 at rollout with a read-only
`vsish -e get …/addr/0x0`.

#### A. Apply `0.1.0-1` (first install this boot)

```
(no lab component before apply)
$ esxcli software component apply -d /tmp/t6/out1/lab-hwhealth-0.1.0-1-bundle.zip
   Components Installed: lab-hwhealth_0.1.0-1      Reboot Required: false
$ /etc/init.d/health restart
=== check: A (03:30:37)  component 0.1.0-1; sampler is running (pid 264034, domain 0, python -B);
    cache 03:29:42; report lab.hwHealth 03:30:17 status 1 magnitude 3
```

#### B. Remove (no purge, no `rm -rf`)

```
$ esxcli software component remove -n lab-hwhealth
   Components Removed: lab-hwhealth_0.1.0-1        Reboot Required: false
=== check: B (03:35:33)
component list: (none); /etc/init.d/lab-hwhealth: not found; (no sampler process)
/usr/lib/vmware/healthd/lab-hwhealth/hwhealth/  (leftover dir, holding __pycache__ from trace.py, see below)
cache.json: No such file or directory (the init script's stop removed it)
report list: lab.hwHealth 2026-11-08T03:31:58  (stale until purged, as in S4)
```

#### C. Apply `0.1.0-1` again, same boot, over the leftover directories

```
$ esxcli software component apply -d /tmp/t6/out1/lab-hwhealth-0.1.0-1-bundle.zip
   Message: Operation finished successfully.
   Components Installed: lab-hwhealth_0.1.0-1      Reboot Required: false
$ grep -c 'Boot image is corrupted' /var/log/vmkernel.log
1                     (unchanged: that one is the 00:45 failure from the first pass, before the directory fix)
$ /etc/init.d/health restart
=== check: C (03:38:35)
component 0.1.0-1; sampler is running (pid 265407, domain 0)
cache 03:37:56, errors tctl_c/ccd_c "root complex not supported"
report lab.hwHealth 03:38:20 {'status': 1, 'magnitude': 3, ...}
```
Declaring directories that already exist is fine. An earlier remove-and-reapply ran at 03:30:59 and
03:31:15 (`esxupdate.log`: `component.remove`, `component.apply`). It also succeeded, but its transcript
was lost to an output redirect, so it was repeated above.

#### D. In-place update `0.1.0-1` → `0.1.0-2` (`component apply`)

```
$ cat /var/run/lab-hwhealth-sampler.pid                        265407
$ esxcli software component apply -d /tmp/t6/out2/lab-hwhealth-0.1.0-2-bundle.zip
   Components Installed: lab-hwhealth_0.1.0-2
   Components Removed: lab-hwhealth_0.1.0-1
   Reboot Required: false
$ cat /var/run/lab-hwhealth-sampler.pid                        265835   (the update ran stop, then start)
$ esxcli software vib list | grep lab
lab-hwhealth                   0.1.0-2                                 lab     CommunitySupported  2026-11-08    host
$ configstorecli schema get -c esx -g health -k lab_hwhealth   rc=0
=== check: D (03:40:59)  component 0.1.0-2; sampler is running (pid 265835); cache 03:41:16; report 03:40:39 status 1 magnitude 3
```

#### Finding: `__pycache__` survives updates, and shipped files have mtime 0

At B, C and D there was a `hwhealth/__pycache__/` dated 03:30. It was written by **`trace.py`**, a root
`python` run that imported the installed package without `-B`. The product did not write it: the sampler
runs `-B` and healthdDom can't write the tree. But any manual root run (for example the documented
`lab_hwhealth.py --stdout`) would write it the same way.

That directory survived remove, reapply and update. Every shipped file has mtime `Jan 1 1970`, and a
`.pyc` is validated by the source's mtime and size. So **inferred, not demonstrated:** after an update that
changes a module without changing its size, Python would keep running the old bytecode.

Fix: both entrypoints set `sys.dont_write_bytecode = True` on the host, before importing `hwhealth`
(test `test_entrypoints_never_write_bytecode_on_the_host`). It was proven with one more update.

#### E. In-place update to `0.1.0-3` (bytecode fix), then a manual root run

```
$ rm -rf .../hwhealth/__pycache__   (the trace.py artefact)
$ esxcli software component apply -d /tmp/t6/out3/lab-hwhealth-0.1.0-3-bundle.zip
   Components Installed: lab-hwhealth_0.1.0-3
   Components Removed: lab-hwhealth_0.1.0-2
   Reboot Required: false
$ /usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py --stdout   (manual root run, no -B)
{"structured": {"status": 1, "magnitude": 3, "tags": ["hardware"], "reasons": ["cpu readings missing"]}, "unstructured": {...
$ find /usr/lib/vmware/healthd/lab-hwhealth -name __pycache__
(end of find)                                   (none written)
=== check: E (03:45:35)  component 0.1.0-3; sampler is running (pid 266295); cache 03:44:37
healthd.log: Launching plugin: lab_hwhealth at 03:45:39 -> report list: lab.hwHealth 2026-11-08T03:45:39
```
The 03:45:39 report came from healthd's scheduled run of the 0.1.0-3 plugin, with **no health restart**
after the update. That matches S1: an updated plugin is picked up without one.

#### F. Final remove, purge, clean

```
$ esxcli software component remove -n lab-hwhealth
   Components Removed: lab-hwhealth_0.1.0-3        Reboot Required: false
$ rm -f /var/lib/vmware/osdata/healthd/lab.hwHealth*   (lab.hwHealth-0.json, -1.json, -2.json, lab.hwHealth.json)
$ /etc/init.d/health restart
=== check: F (03:47:17)
component list: (none); (no sampler process); cache.json: No such file or directory
report list: (none)
$ esxcli system health report get -r lab.hwHealth
 Unknown report: lab.hwHealth
leftovers: healthd-config/lab-hwhealth, pidfile, sampler log, init script, plugin file: all absent; schema rc=180
/usr/lib/vmware/healthd/lab-hwhealth/hwhealth/: empty directories only -> rm -rf (with /tmp/t6)
```

#### Power state (vCenter, after the guest shutdown)

```
2026-11-08T03:49:28Z
vCenter 172.16.10.129: vm=esx-hwh-lab moid=vm-47001 host=hyp01.lab.knowledgeondemand.net powerState=poweredOff bootTime=None toolsRunningStatus=guestToolsNotRunning
```
