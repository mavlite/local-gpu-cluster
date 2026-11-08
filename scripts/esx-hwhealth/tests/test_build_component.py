"""Workstation tests for the pure parts of packaging/build_component.py.

build() itself needs the ESX host's esximage library and is proven on the nested host (Task 6).
The module is loaded by file path: a package named `packaging` would shadow the PyPI one.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_component", ROOT / "packaging" / "build_component.py")
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)

VERSION = "0.1.0-1"
PKG = "usr/lib/vmware/healthd/lab-hwhealth"

# Shaped like the ntp_health entry of the host's esx-base schema (only the parts the builder edits).
TEMPLATE = {
    "component": "esx", "group": "health", "key": "ntp_health", "type": "object",
    "properties": {"tags": {"type": "array",
                            "items": {"type": "string",
                                      "enum": ["AVAILABILITY", "CONNECTIVITY"]}}},
    "defaults": {"ntp_health": {"enabled": True, "frequency": "MEDIUM", "path": "ntp_health",
                                "tags": ["CONNECTIVITY"]}},
}
BASE_DOC = {"data": [{"component": "esx", "group": "health", "key": "memory"}, TEMPLATE],
            "metadata": {"version": 1}}


def _expected_paths():
    package = sorted(f"{PKG}/hwhealth/{p.name}" for p in (ROOT / "hwhealth").glob("*.py"))
    return sorted([
        "usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py",
        f"{PKG}/lab_hwhealth_sampler.py",
        "etc/init.d/lab-hwhealth",
        f"usr/lib/vmware/configmanager/schemas/lab-hwhealth-{VERSION}-schema.json",
        *package,
    ])


def test_payload_file_list_is_exactly_plugin_package_sampler_init_and_schema(tmp_path):
    paths = bc.stage(tmp_path, VERSION, TEMPLATE)
    assert paths == _expected_paths()
    assert f"{PKG}/hwhealth/__init__.py" in paths
    on_disk = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert on_disk == paths


def test_nothing_shipped_under_default_config_or_pycache(tmp_path):
    paths = bc.stage(tmp_path, VERSION, TEMPLATE)
    assert not [p for p in paths if "default-config" in p or "__pycache__" in p]


def test_staged_files_match_sources_and_use_lf(tmp_path):
    bc.stage(tmp_path, VERSION, TEMPLATE)
    plugin = tmp_path / "usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py"
    assert plugin.read_bytes() == (ROOT / "lab_hwhealth.py").read_bytes()
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert b"\r" not in f.read_bytes(), f


def test_crlf_source_is_refused(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"#!/bin/python\r\nprint(1)\r\n")
    with pytest.raises(ValueError, match="CRLF"):
        bc.read_lf(bad)


def test_same_version_staged_twice_is_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert bc.stage(a, VERSION, TEMPLATE) == bc.stage(b, VERSION, TEMPLATE)
    for rel in bc.stage(tmp_path / "c", VERSION, TEMPLATE):
        assert (a / rel).read_bytes() == (b / rel).read_bytes()


def test_modes_entrypoints_executable_package_read_only():
    files = bc.payload_files(VERSION, TEMPLATE)
    assert files["usr/lib/vmware/healthd/plugins/bin/lab_hwhealth.py"][1] == 0o555
    assert files[f"{PKG}/lab_hwhealth_sampler.py"][1] == 0o555
    assert files["etc/init.d/lab-hwhealth"][1] == 0o555
    assert files[f"{PKG}/hwhealth/__init__.py"][1] == 0o444


def test_descriptor_values():
    d = bc.descriptor_fields(VERSION)
    assert d["name"] == "lab-hwhealth"
    assert d["version"] == VERSION
    assert d["vendor"] == "lab"
    assert d["acceptancelevel"] == "community"
    assert d["liveinstallok"] is True and d["liveremoveok"] is True


def test_component_values():
    c = bc.component_fields(VERSION)
    assert c["name"] == "lab-hwhealth" and c["version"] == VERSION and c["vendor"] == "lab"
    assert c["releasetype"] == "extension"


def test_schema_file_name_and_key():
    assert bc.schema_install_path(VERSION) == (
        f"usr/lib/vmware/configmanager/schemas/lab-hwhealth-{VERSION}-schema.json")
    doc = json.loads(bc.schema_text(VERSION, TEMPLATE))
    (entry,) = doc["data"]
    assert entry["component"] == "esx" and entry["group"] == "health"
    assert entry["key"] == "lab_hwhealth"
    assert list(entry["defaults"]) == ["lab_hwhealth"]
    assert entry["defaults"]["lab_hwhealth"]["path"] == "lab_hwhealth.py"
    assert doc["metadata"] == {"version": 1, "vibname": "lab-hwhealth", "vibversion": VERSION}


def test_schema_does_not_mutate_template():
    before = json.dumps(TEMPLATE, sort_keys=True)
    bc.schema_text(VERSION, TEMPLATE)
    assert json.dumps(TEMPLATE, sort_keys=True) == before


def test_health_template_is_picked_from_base_doc():
    assert bc.health_template(BASE_DOC) == TEMPLATE
    with pytest.raises(ValueError, match="ntp_health"):
        bc.health_template({"data": []})


def test_base_schema_is_discovered_not_hardcoded(tmp_path):
    (tmp_path / "esx-base-9.9.9_0.1-schema.json").write_text("{}")
    (tmp_path / "other-schema.json").write_text("{}")
    assert bc.discover_base_schema(tmp_path) == tmp_path / "esx-base-9.9.9_0.1-schema.json"
    assert "25714478" not in (ROOT / "packaging" / "build_component.py").read_text()


def test_base_schema_discovery_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        bc.discover_base_schema(tmp_path)
    (tmp_path / "esx-base-1-schema.json").write_text("{}")
    (tmp_path / "esx-base-2-schema.json").write_text("{}")
    with pytest.raises(ValueError, match="esx-base"):
        bc.discover_base_schema(tmp_path)


def test_init_script_shape():
    text = bc.init_script()
    lines = text.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert "# chkconfig: on 99 01" in lines
    for verb in ("start)", "stop)", "status)"):
        assert verb in text
    assert "PIDFILE=/var/run/" in text
    assert f"/{PKG}/lab_hwhealth_sampler.py" in text
    assert "\r" not in text


def test_entrypoints_add_install_dir_to_sys_path():
    import lab_hwhealth
    import lab_hwhealth_sampler
    assert lab_hwhealth.INSTALL_DIR == lab_hwhealth_sampler.INSTALL_DIR == "/" + PKG


def test_payload_tar_has_a_directory_entry_before_each_file(tmp_path):
    # visorfs refuses a tardisk whose files sit in directories the tar never declares
    # ("Boot image is corrupted prevented creating tardisk", nested host, Task 6).
    import gzip
    import tarfile
    tgz = tmp_path / "p.tgz"
    files = bc.payload_files(VERSION, TEMPLATE)
    bc.write_tgz(tgz, files, gzip.GzipFile)
    with tarfile.open(tgz) as t:
        members = t.getmembers()
    seen = set()
    for m in members:
        parent = m.name.rstrip("/").rpartition("/")[0]
        if parent:
            assert parent in seen, f"{m.name} before its directory {parent}"
        if m.isdir():
            assert m.mode == 0o755
            seen.add(m.name.rstrip("/"))
    assert sorted(m.name for m in members if m.isfile()) == sorted(files)
    assert f"{PKG}/hwhealth" in seen and all(m.mtime == 0 for m in members)


def test_init_script_leaves_nothing_behind():
    # Nested proof, first pass: after `component remove` the sampler's __pycache__, the cache and
    # an empty log stayed in RAM. -B stops the bytecode writes; stop removes the rest.
    text = bc.init_script()
    assert '"$PYTHON" -B "$SAMPLER"' in text
    stop = text.split("  stop)")[1].split(";;")[0]
    assert 'rm -f "$PIDFILE" "$CACHE"' in stop
    assert "CACHE=/usr/lib/vmware/healthd-config/lab-hwhealth/cache.json" in text
    assert '[ -s "$LOG" ] || rm -f "$LOG"' in stop


def test_init_cache_path_matches_the_package():
    import hwhealth
    assert bc.CACHE == hwhealth.CACHE
