"""Wrap a VIB into a component offline bundle using the host's esximage (ESX 9.1.1).
Usage: python build_bundle.py <vib> <out.zip> <component-version>"""
import datetime, os, sys, tarfile
from vmware.esximage import Bulletin, ConfigSchema, OfflineBundle, Version, Vib, VibCollection
vibpath, dst, cver = sys.argv[1], sys.argv[2], sys.argv[3]
vib = Vib.ArFileVib.FromFile(vibpath)
vib.remotelocations = ["file://" + os.path.abspath(vibpath)]
vibs = VibCollection.VibCollection(); vibs.AddVib(vib)
comp = Bulletin.Component(
    componentnamespec={"name": vib.name, "uistring": "Lab hardware health (spike)"},
    componentversionspec={"version": Version.VibVersion.fromstring(cver), "uistring": cver},
    vendor="lab", summary="lab.hwHealth healthd plugin (spike)", description="lab.hwHealth test plugin",
    severity="general", urgency="low", category="enhancement", releasetype="extension",
    releasedate=datetime.datetime.now(datetime.timezone.utc),
    platforms=[Vib.SoftwarePlatform("9.1", "", "embeddedEsx")], vibids={vib.id})
comps = Bulletin.ComponentCollection(); comps.AddComponent(comp)
schemas = ConfigSchema.ConfigSchemaCollection()
with tarfile.open(os.path.join(os.path.dirname(os.path.abspath(vibpath)), "labhwh.tgz")) as t:
    for m in t.getmembers():
        if m.name.startswith("usr/lib/vmware/configmanager/schemas/"):
            schemas.AddFromJSON(t.extractfile(m).read().decode())
OfflineBundle.WriteOfflineBundle(dst, "lab", "lab", None, None, None, None, None, comps, vibs,
                                 configSchemas=schemas, versions=["9.1.1"])
print("bundle", dst, os.path.getsize(dst), "component", comp.id, "vib", vib.id)
