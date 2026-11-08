#!/bin/python
"""Build the lab-hwhealth VIB and its component offline bundle.

    python build_component.py <out_dir> <version>        # e.g. /tmp/out 0.1.0-1

build() runs **on an ESX 9.1.1 host**: the VIB and the bundle are written by the host's own
`vmware.esximage` library (spike S4), imported lazily inside build(). Everything else here is
pure and is tested on the workstation: the payload layout, the descriptor and component fields,
the ConfigStore schema and the init script.

Payload (R17):
  usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py           healthd scans this dir at start
  usr/lib/vmware/healthd/lab-hwhealth/hwhealth/*.py            the package (healthdDom rx tree)
  usr/lib/vmware/healthd/lab-hwhealth/lab_hwhealth_sampler.py  boot sampler, run by the init script
  etc/init.d/lab-hwhealth                                      starts/stops the sampler (superDom)
  usr/lib/vmware/configmanager/schemas/lab-hwhealth-<ver>-schema.json   ConfigStore esx/health/lab_hwhealth
Community VIBs may not ship etc/vmware/default-config/, so healthd runs the plugin on its
built-in defaults.
"""
import copy
import datetime
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

NAME = "lab-hwhealth"            # VIB and component name
KEY = "lab_hwhealth"             # healthd / ConfigStore key = plugin file stem
VENDOR = "lab"
ACCEPTANCE = "community"
REPORT = "lab.hwHealth"
PAYLOAD_NAME = "labhwh"
PLATFORM = ("9.1", "", "embeddedEsx")
ESX_VERSIONS = ["9.1.1"]
TEMPLATE_KEY = "ntp_health"      # the esx-base health schema entry the new key is derived from
DEFAULT_TAG = "AVAILABILITY"     # must be in the esx-base tags enum

SRC = Path(__file__).resolve().parent.parent
PLUGIN_PATH = f"usr/lib/vmware/healthd/plugins/bin/{KEY}.py"
PKG_DIR = f"usr/lib/vmware/healthd/{NAME}"
SAMPLER_PATH = f"{PKG_DIR}/lab_hwhealth_sampler.py"
INIT_PATH = f"etc/init.d/{NAME}"
SCHEMAS_DIR = Path("/usr/lib/vmware/configmanager/schemas")
PIDFILE = "/var/run/lab-hwhealth-sampler.pid"
SAMPLER_LOG = "/var/run/log/lab-hwhealth-sampler.log"
CACHE = "/usr/lib/vmware/healthd-config/lab-hwhealth/cache.json"   # = hwhealth.CACHE

EXEC, READ = 0o555, 0o444


def read_lf(path: Path) -> bytes:
    """Return the file's bytes; refuse CRLF (a CRLF shebang breaks the launch on ESX)."""
    data = Path(path).read_bytes()
    if b"\r" in data:
        raise ValueError(f"{path}: CRLF line endings; everything shipped must be LF")
    return data


def schema_install_path(version: str) -> str:
    return f"{SCHEMAS_DIR.as_posix().lstrip('/')}/{NAME}-{version}-schema.json"


def discover_base_schema(schemas_dir: Path = SCHEMAS_DIR) -> Path:
    """Find the host's esx-base schema file (its name carries the build number)."""
    found = sorted(Path(schemas_dir).glob("esx-base-*-schema.json"))
    if not found:
        raise FileNotFoundError(f"no esx-base-*-schema.json in {schemas_dir}")
    if len(found) > 1:
        raise ValueError(f"more than one esx-base schema in {schemas_dir}: {[p.name for p in found]}")
    return found[0]


def health_template(base_doc: dict) -> dict:
    """Pick the esx/health/ntp_health schema entry out of the esx-base schema document."""
    for entry in base_doc.get("data", []):
        if entry.get("group") == "health" and entry.get("key") == TEMPLATE_KEY:
            return entry
    raise ValueError(f"esx-base schema has no health/{TEMPLATE_KEY} entry to derive from")


def plugin_defaults() -> dict:
    return {
        "criticality": "INFO", "enabled": True, "frequency": "FAST", "max_lifetime": 30,
        "log": {"directory": "/var/run/log/", "level": "info", "max_file_num": 8,
                "max_file_size": 524288, "name": KEY, "output_to_console": False,
                "output_to_files": False, "output_to_syslog": True,
                "syslog": {"facility": "local4", "ident": "healthdPlugins"}},
        "path": f"{KEY}.py", "resource_group": "healthd-plugins", "resource_group_size_MB": 40,
        "tags": [DEFAULT_TAG], "working_dir": "/var/log/",
    }


def schema_doc(version: str, template: dict) -> dict:
    """ConfigStore schema for esx/health/lab_hwhealth, derived from the host's ntp_health entry."""
    entry = copy.deepcopy(template)
    entry["key"] = KEY
    base_defaults = entry.get("defaults", {}).get(TEMPLATE_KEY, {})
    entry["defaults"] = {KEY: {**base_defaults, **plugin_defaults()}}
    return {"data": [entry], "metadata": {"version": 1, "vibname": NAME, "vibversion": version}}


def schema_text(version: str, template: dict) -> str:
    return json.dumps(schema_doc(version, template), indent=1, sort_keys=True) + "\n"


def init_script() -> str:
    """The /etc/init.d script: runs in superDom, starts the sampler loop detached with a pidfile."""
    return f"""#!/bin/sh
# chkconfig: on 99 01
# {NAME}: boot sampler for the {REPORT} healthd plugin.
# Init scripts run in superDom, where the SMN write and the MSR reads are allowed. The sampler
# writes the cache that the plugin reads from inside healthdDom.
PYTHON=/bin/python
SAMPLER=/{SAMPLER_PATH}
PIDFILE={PIDFILE}
LOG={SAMPLER_LOG}
CACHE={CACHE}

running() {{
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}}

case "$1" in
  start)
    if running; then
      echo "{NAME} sampler already running"
      exit 0
    fi
    # -B: no __pycache__ in the RAM overlay of the read-only tree (it outlives a VIB remove)
    "$PYTHON" -B "$SAMPLER" </dev/null >/dev/null 2>"$LOG" &
    echo $! > "$PIDFILE"
    echo "{NAME} sampler started"
    ;;
  stop)
    if running; then
      pid="$(cat "$PIDFILE")"
      # TERM makes the sampler raise SystemExit, so its finally restores the SMN index. Wait
      # (about 5 s at most) for it to exit before removing its files; never SIGKILL it.
      kill -TERM "$pid"
      n=0
      while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 5 ]; do
        sleep 1
        n=$((n + 1))
      done
      if kill -0 "$pid" 2>/dev/null; then
        echo "{NAME} sampler $pid still running after TERM"
      fi
    fi
    # stop also runs at `component remove`: leave no cache, empty log or cache dir behind
    rm -f "$PIDFILE" "$CACHE"
    rmdir "$(dirname "$CACHE")" 2>/dev/null
    [ -s "$LOG" ] || rm -f "$LOG"
    echo "{NAME} sampler stopped"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    if running; then
      echo "{NAME} sampler is running"
      exit 0
    fi
    echo "{NAME} sampler is not running"
    exit 3
    ;;
  *)
    echo "Usage: $0 {{start|stop|restart|status}}"
    exit 1
    ;;
esac
exit 0
"""


def payload_files(version: str, template: dict) -> dict:
    """Install path (no leading /) -> (bytes, mode). Exactly what the VIB ships."""
    files = {
        PLUGIN_PATH: (read_lf(SRC / "lab_hwhealth.py"), EXEC),
        SAMPLER_PATH: (read_lf(SRC / "lab_hwhealth_sampler.py"), EXEC),
        INIT_PATH: (init_script().encode(), EXEC),
        schema_install_path(version): (schema_text(version, template).encode(), READ),
    }
    for src in sorted((SRC / "hwhealth").glob("*.py")):
        files[f"{PKG_DIR}/hwhealth/{src.name}"] = (read_lf(src), READ)
    return files


def stage(stage_dir: Path, version: str, template: dict) -> list:
    """Write the payload tree under stage_dir; return the sorted install paths."""
    files = payload_files(version, template)
    for rel, (data, mode) in files.items():
        dst = Path(stage_dir) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        dst.chmod(mode)
    return sorted(files)


def descriptor_fields(version: str) -> dict:
    return {
        "name": NAME, "version": version, "vendor": VENDOR, "acceptancelevel": ACCEPTANCE,
        "summary": "Lab hardware health healthd plugin",
        "description": f"{REPORT}: Zen 4 CPU temperature, power and clock, and NVMe health",
        "liveinstallok": True, "liveremoveok": True, "statelessready": True,
    }


def component_fields(version: str) -> dict:
    return {
        "name": NAME, "uistring": "Lab hardware health", "version": version, "vendor": VENDOR,
        "summary": f"{REPORT} healthd plugin and boot sampler",
        "description": descriptor_fields(version)["description"],
        "severity": "general", "urgency": "low", "category": "enhancement",
        "releasetype": "extension",
    }


def _parent_dirs(files: dict) -> list:
    dirs = set()
    for rel in files:
        parts = rel.split("/")[:-1]
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return sorted(dirs)


def write_tgz(path: Path, files: dict, gzip_cls) -> None:
    """Deterministic tar (sorted, mtime 0, root-owned) inside gzip_cls (EsxGzip on the host).

    Every parent directory gets its own entry ahead of its files: visorfs refuses a tardisk
    that puts files in directories it never declares.
    """
    with open(path, "wb") as raw, gzip_cls(fileobj=raw, mode="wb") as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for rel in _parent_dirs(files):
            info = tarfile.TarInfo(rel + "/")
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
            info.uname = info.gname = "root"
            tar.addfile(info)
        for rel in sorted(files):
            data, mode = files[rel]
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), mode, 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))


def build(out_dir: Path, version: str) -> tuple:
    """On an ESX host: write <out_dir>/lab-hwhealth-<ver>.vib and ...-bundle.zip; return both."""
    from vmware.esximage import Bulletin, ConfigSchema, OfflineBundle, Version, Vib, VibCollection
    from vmware.esximage.Utils import EsxGzip

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = health_template(json.loads(discover_base_schema().read_text(encoding="utf-8")))
    files = payload_files(version, template)
    platform = Vib.SoftwarePlatform(*PLATFORM)
    now = datetime.datetime.now(datetime.timezone.utc)
    vib_version = Version.VibVersion.fromstring(version)

    d = descriptor_fields(version)
    vib = Vib.ArFileVib(
        name=d["name"], version=vib_version, vendor=d["vendor"], summary=d["summary"],
        description=d["description"], releasedate=now, acceptancelevel=d["acceptancelevel"],
        liveinstallok=d["liveinstallok"], liveremoveok=d["liveremoveok"],
        statelessready=d["statelessready"],
        maintenancemode=Vib.MaintenanceMode(remove=False, install=False), swplatforms=[platform])
    vib_path = out_dir / f"{NAME}-{version}.vib"
    with tempfile.TemporaryDirectory() as tmp:
        tgz = Path(tmp) / f"{PAYLOAD_NAME}.tgz"
        write_tgz(tgz, files, EsxGzip.GzipFile)
        vib.AddPayload(Vib.Payload(PAYLOAD_NAME, "tgz"), str(tgz))
        vib.WriteVibFile(str(vib_path))

    packed = Vib.ArFileVib.FromFile(str(vib_path))
    packed.remotelocations = ["file://" + str(vib_path.resolve())]
    vibs = VibCollection.VibCollection()
    vibs.AddVib(packed)
    c = component_fields(version)
    comp = Bulletin.Component(
        componentnamespec={"name": c["name"], "uistring": c["uistring"]},
        componentversionspec={"version": vib_version, "uistring": version},
        vendor=c["vendor"], summary=c["summary"], description=c["description"],
        severity=c["severity"], urgency=c["urgency"], category=c["category"],
        releasetype=c["releasetype"], releasedate=now, platforms=[platform],
        vibids={packed.id})
    comps = Bulletin.ComponentCollection()
    comps.AddComponent(comp)
    schemas = ConfigSchema.ConfigSchemaCollection()
    schemas.AddFromJSON(schema_text(version, template))
    bundle_path = out_dir / f"{NAME}-{version}-bundle.zip"
    OfflineBundle.WriteOfflineBundle(str(bundle_path), VENDOR, VENDOR, None, None, None, None, None,
                                     comps, vibs, configSchemas=schemas, versions=ESX_VERSIONS)
    return vib_path, bundle_path


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: build_component.py <out_dir> <version>", file=sys.stderr)
        return 2
    vib_path, bundle_path = build(Path(argv[0]), argv[1])
    print(f"vib    {vib_path} {vib_path.stat().st_size}")
    print(f"bundle {bundle_path} {bundle_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
