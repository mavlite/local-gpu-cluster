"""hardware.bootDiskVmfs: reinstalling ESX without destroying the local datastore.

The lab's boot NVMe carries EFI, both bootbanks, OSDATA *and* the hostNN-local
VMFS datastore, which holds VM disks. The default `install --overwritevmfs`
wipes that datastore. A rebuild after an OSDATA corruption must not, so the
operator can declare `bootDiskVmfs: preserve`.

Every behaviour pinned here was read from the ESX 9.1.1.0 (build 25714478)
installer itself (weasel scriptedinstall + systemStorage/autoPartition.py),
not from docs:
- `--preservevmfs` and `--overwritevmfs` are mutually exclusive in the grammar.
- With `--preservevmfs` the installer rewrites EFI, bootbanks and OSDATA and
  re-creates the VMFS partition at its exact old start/end sectors and label.
- If it finds no VMFS on the disk (a damaged partition table) that is only a
  *warning*, and the install carries on to create a fresh datastore over the
  space. `paranoid` turns warnings into errors, so it is what makes
  "preserve" a guarantee rather than a hope.
"""
from __future__ import annotations

import copy
import re

import pytest

from vcfspec.provision import render_provisioning

from .test_provision import ISO_BOOT_CFG


def _install_line(kickstart: str) -> str:
    return [ln for ln in kickstart.splitlines() if ln.startswith("install ")][0]


_PARANOID = re.compile(r"^\s*paranoid\b", re.M)


def _preserving(inventory, value="preserve", host=0):
    doc = copy.deepcopy(inventory)
    doc["hosts"][host]["hardware"]["bootDiskVmfs"] = value
    return render_provisioning(doc, ISO_BOOT_CFG)


def test_the_default_still_overwrites_and_is_not_paranoid(inventory):
    out = render_provisioning(inventory, ISO_BOOT_CFG)
    ks = out.kickstarts["ks-esx01.cfg"]
    line = _install_line(ks)
    assert "--overwritevmfs" in line
    assert "--preservevmfs" not in line
    assert not _PARANOID.search(ks)


def test_preserve_emits_preservevmfs_and_never_overwritevmfs(inventory):
    out = _preserving(inventory)
    assert out.findings == ()
    line = _install_line(out.kickstarts["ks-esx01.cfg"])
    assert "--preservevmfs" in line
    assert "--overwritevmfs" not in line   # the grammar rejects both together
    assert "--novmfsondisk" not in line    # and this one with --preservevmfs


def test_preserve_targets_only_the_boot_disk(inventory):
    """The installer writes only the --disk target. Neither the vSAN device
    nor the tier device may appear on the install line in any form."""
    out = _preserving(inventory)
    hw = inventory["hosts"][0]["hardware"]
    line = _install_line(out.kickstarts["ks-esx01.cfg"])
    assert f"--disk={inventory['hosts'][0]['bootDisk']} " in line
    for device in (hw.get("vsanDevice"), hw.get("memoryTieringDevice")):
        if device:
            assert device.rsplit("/", 1)[-1] not in line
    assert "--firstdisk" not in line


def test_preserve_makes_the_install_paranoid(inventory):
    """Without `paranoid`, a VMFS the installer cannot see is a warning and
    the install formats a new datastore over the old one."""
    out = _preserving(inventory)
    lines = out.kickstarts["ks-esx01.cfg"].splitlines()
    assert "paranoid" in lines
    assert lines.index("paranoid") < lines.index(_install_line("\n".join(lines)))


def test_preserve_skips_the_datastore1_rename(inventory):
    """A preserved datastore keeps its label (esx01-local); there is no
    datastore1 to rename, so the command would only fail noisily."""
    out = _preserving(inventory)
    ks = out.kickstarts["ks-esx01.cfg"]
    assert "vim-cmd hostsvc/datastore/rename" not in ks
    assert "was preserved by the install" in ks
    # The rest of %firstboot still follows the skipped rename.
    assert "esxcli system coredump file set -s -e true" in ks


def test_preserve_is_per_host(inventory):
    out = _preserving(inventory)
    neighbour = out.kickstarts["ks-esx02.cfg"]
    assert "--overwritevmfs" in _install_line(neighbour)
    assert not _PARANOID.search(neighbour)
    assert "vim-cmd hostsvc/datastore/rename datastore1 esx02-local" in neighbour


@pytest.mark.parametrize("value", ["preserv", "Preserve", "PRESERVE", "",
                                   "overwrite ", None, True])
def test_an_unknown_value_withholds_the_kickstart_rather_than_overwriting(
        inventory, value):
    """A typo must never fall back to the destructive default."""
    out = _preserving(inventory, value=value)
    assert "ks-esx01.cfg" not in out.kickstarts
    assert any(f.code == "VCF-PROV-BOOT-VMFS-MODE" for f in out.findings)
    assert "ks-esx02.cfg" in out.kickstarts


def test_preserve_contradicts_a_boot_disk_claimed_by_vsan(inventory):
    """A boot disk vSAN claimed carries no VMFS to keep: --overwritevsan
    reclaims it, the installer then finds no datastore, and `paranoid`
    aborts. Say so at render time instead of at the console."""
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["bootDiskVmfs"] = "preserve"
    doc["hosts"][0]["hardware"]["bootDiskClaimedByVsan"] = True
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert "ks-esx01.cfg" not in out.kickstarts
    assert any(f.code == "VCF-PROV-BOOT-VMFS-MODE" for f in out.findings)
