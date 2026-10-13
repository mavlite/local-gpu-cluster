"""i226-no-aspm: PCIe ASPM L1 off on the Intel I226 NIC, matched by vendor/device ID (outage 2026-10-13).

The Proxmox host's I226-V (igc) logged "PCIe link lost, device now detached" with ASPM L1 enabled and the host
stayed up with no network for 6.5 h. The script runs at every boot from i226-no-aspm.service; 40-host-config.sh
installs it. Tests drive the script against a fake sysfs tree (SYSFS_PCI_DEVICES) with a stub logger; fake device names use dashes because Windows cannot create the real
PCI names (which contain colons) -- the script matches by vendor/device ID, never by name."""
import os
import shutil
import subprocess

import pytest

FILES = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(FILES, "i226-no-aspm.sh")
ROOT = os.path.join(FILES, "..")


def device(root, name, vendor, dev, aspm="1", writable=True):
    d = root / name
    (d / "link").mkdir(parents=True)
    (d / "vendor").write_text(vendor + "\n")
    (d / "device").write_text(dev + "\n")
    f = d / "link" / "l1_aspm"
    f.write_text(aspm + "\n")
    if not writable:
        f.chmod(0o444)
    return f


def run(tmp_path, devices_root):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("sh not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "logger.out"
    (bin_dir / "logger").write_text(f'#!/bin/sh\necho "$@" >> "{log.as_posix()}"\n', newline="\n")
    (bin_dir / "logger").chmod(0o755)
    env = {"PATH": f"{bin_dir.as_posix()}:{os.environ.get('PATH', '/usr/bin:/bin')}",
           "SYSFS_PCI_DEVICES": devices_root.as_posix()}
    r = subprocess.run([sh, SCRIPT], capture_output=True, text=True, env=env)
    return r, (log.read_text() if log.exists() else "")


def test_i226_l1_aspm_is_turned_off_and_other_devices_are_untouched(tmp_path):
    root = tmp_path / "devices"
    nic = device(root, "0000-0f-00.0", "0x8086", "0x125c")
    gpu = device(root, "0000-03-00.0", "0x1002", "0x73a1")
    other_intel = device(root, "0000-10-00.0", "0x8086", "0x15f3")
    r, log = run(tmp_path, root)
    assert r.returncode == 0, r.stderr
    assert nic.read_text().strip() == "0"
    assert gpu.read_text().strip() == "1" and other_intel.read_text().strip() == "1"
    assert "0000-0f-00.0: l1_aspm=0" in log


def test_no_i226_present_is_logged_and_not_an_error(tmp_path):
    root = tmp_path / "devices"
    device(root, "0000-03-00.0", "0x1002", "0x73a1")
    r, log = run(tmp_path, root)
    assert r.returncode == 0 and "no I226 found" in log


def test_host_config_installs_and_enables_the_unit():
    src = open(os.path.join(ROOT, "40-host-config.sh"), encoding="utf-8").read()
    assert "phase_4_11_nic_aspm" in src.split("main() {", 1)[1]
    body = src[src.index("phase_4_11_nic_aspm() {"):]
    body = body[:body.index("\n}\n")]
    assert "i226-no-aspm.sh" in body and "i226-no-aspm.service" in body
    assert "systemctl enable --now i226-no-aspm.service" in body


def test_unit_runs_the_installed_script_at_boot():
    unit = open(os.path.join(FILES, "i226-no-aspm.service"), encoding="utf-8").read()
    assert "ExecStart=/usr/local/sbin/i226-no-aspm.sh" in unit and "WantedBy=multi-user.target" in unit
