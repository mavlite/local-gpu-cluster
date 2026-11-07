# ESX hardware health: spike findings

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

## S1 — registration

TODO (later dispatch, nested ESX VM).

## S2 — sandbox

TODO (later dispatch, nested ESX VM).

## S3 — remote API

TODO (later dispatch, nested ESX VM).

## S4 — packaging

TODO (later dispatch, nested ESX VM).
