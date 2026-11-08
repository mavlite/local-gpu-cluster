"""Build lab-hwhealth test VIB on an ESX 9.1.1 host using the host's own esximage.
Usage: python build_vib.py <plugin.py> <outdir> [version] [tag]"""
import datetime, io, json, os, sys, tarfile
from vmware.esximage import Vib, Version
from vmware.esximage.Utils import EsxGzip
plugin, out = sys.argv[1], sys.argv[2]
VER = sys.argv[3] if len(sys.argv) > 3 else "0.1.0-1"
TAG = sys.argv[4] if len(sys.argv) > 4 else "AVAILABILITY"
NAME, KEY = "lab-hwhealth", "lab_hwhealth"
META = {"version": 1, "vibname": NAME, "vibversion": VER}
base = "/usr/lib/vmware/configmanager/schemas/esx-base-9.1.1_0.25714478-schema.json"
ntp = [s for s in json.load(open(base))["data"] if s["group"] == "health" and s["key"] == "ntp_health"][0]
sch = json.loads(json.dumps(ntp)); sch["key"] = KEY; sch["defaults"] = {KEY: sch["defaults"]["ntp_health"]}
if TAG not in sch["properties"]["tags"]["items"].get("enum", [TAG]):
    sch["properties"]["tags"]["items"]["enum"].append(TAG)
cfg = {"criticality": "INFO", "enabled": True, "frequency": "FAST", "max_lifetime": 30,
       "log": {"directory": "/var/run/log/", "level": "info", "max_file_num": 8, "max_file_size": 524288,
               "name": KEY, "output_to_console": False, "output_to_files": False, "output_to_syslog": True,
               "syslog": {"facility": "local4", "ident": "healthdPlugins"}},
       "path": KEY + ".py", "resource_group": "healthd-plugins", "resource_group_size_MB": 40,
       "tags": [TAG], "working_dir": "/var/log/"}
sch["defaults"] = {KEY: dict(sch["defaults"][KEY], **cfg)}
files = {
    f"usr/lib/vmware/healthd/plugins/bin/{KEY}.py": (open(plugin, "rb").read(), 0o555),
    f"usr/lib/vmware/configmanager/schemas/{NAME}-{VER}-schema.json":
        (json.dumps({"data": [sch], "metadata": META}, indent=1).encode(), 0o444),
}
extra = os.path.join(os.path.dirname(os.path.abspath(plugin)), "lab-hwhealth-probe")
if os.path.exists(extra):
    files["etc/init.d/lab-hwhealth-probe"] = (open(extra, "rb").read(), 0o555)
os.makedirs(out, exist_ok=True)
tgz = os.path.join(out, "labhwh.tgz")
with open(tgz, "wb") as raw, EsxGzip.GzipFile(fileobj=raw, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
    for path, (data, mode) in sorted(files.items()):
        ti = tarfile.TarInfo(path); ti.size = len(data); ti.mode = mode; ti.mtime = 0; ti.uname = ti.gname = "root"
        tar.addfile(ti, io.BytesIO(data))
v = Vib.ArFileVib(name=NAME, version=Version.VibVersion.fromstring(VER), vendor="lab",
                  summary="Lab hardware health healthd plugin (spike)", description="lab.hwHealth test plugin",
                  releasedate=datetime.datetime.now(datetime.timezone.utc), acceptancelevel="community",
                  liveinstallok=True, liveremoveok=True, statelessready=True,
                  maintenancemode=Vib.MaintenanceMode(remove=False, install=False),
                  swplatforms=[Vib.SoftwarePlatform("9.1", "", "embeddedEsx")])
v.AddPayload(Vib.Payload("labhwh", "tgz"), tgz)
dst = os.path.join(out, f"{NAME}-{VER}.vib"); v.WriteVibFile(dst)
print("built", dst, os.path.getsize(dst), "swtags", v.swtags, "files", sorted(v.filelist))
