#!/bin/python
# S1/S2 spike test plugin v2: {"hello": 1} plus sensor-access probes run under healthd.
import io, os, runpy, subprocess, sys, contextlib
from vmware.healthd.healthPlugin import HealthPlugin
B = "/hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/"
def probe(name, fn):
    try:
        v = fn(); return {"probe": name, "ok": True, "value": repr(v)[:300]}
    except BaseException as e:
        return {"probe": name, "ok": False, "exc": repr(e)[:300]}
def esxcli(*args):
    out = io.StringIO(); sys.argv = ["esxcli"] + list(args)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try: runpy.run_path("/bin/esxcli.py", run_name="__main__")
        except SystemExit as e: code = e.code
        else: code = 0
    return {"rc": code, "out": out.getvalue()[-300:]}
def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return {"rc": p.returncode, "out": p.stdout[-200:], "err": p.stderr[-200:]}
class T(HealthPlugin):
    def generateHealthReport(self):
        import vmware.vsi as vsi
        argv = list(sys.argv)
        idx = {}
        probes = [probe("vsi get pci 0x0", lambda: vsi.get(B + "0x0")),
                  probe("vsi get pci 0x60", lambda: idx.setdefault("v", vsi.get(B + "0x60"))),
                  probe("vsi set pci 0x60 (same value)", lambda: vsi.set(B + "0x60", idx.get("v", 0))),
                  probe("vsi get msr 0xC0010299", lambda: vsi.get("/hardware/msr/pcpu/0/addr/0xC0010299")),
                  probe("vsi get /power/pcpu/0/perf", lambda: vsi.get("/power/pcpu/0/perf")),
                  probe("vsi get /power/pcpu/0/temp", lambda: vsi.get("/power/pcpu/0/temp")),
                  probe("vsi get vmknvme controller info", lambda: vsi.get("/vmkModules/vmknvme/controllers/256/info")),
                  probe("exec vsish get msr", lambda: run(["/bin/vsish", "-e", "get", "/hardware/msr/pcpu/0/addr/0xC0010299"])),
                  probe("exec esxcli nvme device list", lambda: run(["/bin/esxcli", "nvme", "device", "list"])),
                  probe("in-process esxcli nvme device list", lambda: esxcli("nvme", "device", "list"))]
        for path in ("/var/run/lab-hwhealth-probe.txt", "/var/run/lab-hwhealth/cache.json",
                     "/usr/lib/vmware/healthd-config/lab-hwhealth-cache.json",
                     "/var/lib/vmware/osdata/healthd/lab-hwhealth-cache.json", "/tmp/lab-hwhealth-cache.json"):
            probes.append(probe("read " + path, lambda p=path: open(p).read()[:60]))
        sys.argv = argv
        ctx = {"argv": argv, "uid": os.getuid(), "cwd": os.getcwd()}
        return self.sendReport("lab.hwHealth", {"hello": 1, "v": 4, "probes": probes, "ctx": ctx},
                               structuredData={"status": 0, "magnitude": 0})
if __name__ == "__main__":
    sys.exit(0 if T("lab_hwhealth", args=sys.argv[1:]).generateHealthReport() else 1)
