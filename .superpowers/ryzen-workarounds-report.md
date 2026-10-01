# Consumer-AMD host workarounds — report

**Status:** done. All 452 tests pass (440 existing + 12 new); no existing test
was modified. Committed on `feat/esx-provisioning`.

**Commit:** `c1436c9` feat(vcfspec): consumer-AMD host workarounds for the ESX kickstart

**Test summary:** `cd vcf-spec-tools && python -m pytest -q` → `452 passed`.
Mutation-tested three new guards by breaking each in place and confirming the
suite reddened, then restored from backup and reran clean:
- opt-in check in `_workarounds_block` (forced always-on) → 2 tests failed
- block placement before the `%firstboot` reboot (moved after) → 2 tests failed
- `VCF-CAP-TIERING-NEEDS-WORKAROUND` guard in `_capacity_rules` (forced off) → 2 tests failed

**Before (no workaround requested — unchanged, byte-identical to pre-feature output):**
```
vim-cmd hostsvc/start_ssh

# ESX generates its certificates before the hostname is configured, ...
```

**After (`provisioning.hostWorkarounds: [consumer-amd]`, `hardware.cpuModel: 7945HX`):**
```
vim-cmd hostsvc/start_ssh

# Consumer-AMD host workarounds (provisioning.hostWorkarounds: consumer-amd).
# William Lam, VCF 9.1 comprehensive ESX configuration workarounds for lab
# deployments:
# https://williamlam.com/2026/05/vcf-9-1-comprehensive-esx-configuration-workarounds-for-lab-deployments.html
#
# NSX Edge/VNA deployment fails on consumer AMD: a DPDK vendor check rejects
# the real Ryzen brand string. The value below exists to satisfy that check,
# not to describe this host honestly -- it is not this host's real CPU. No
# reboot needed.
echo 'cpuid.brandstring = "AMD EPYC 7945HX"' >> /etc/vmware/config
# NVMe memory tiering cannot power on VMs on AMD Ryzen without this. Needs a
# reboot, served by the one below.
echo 'monitor_control.disable_apichv ="TRUE"' >> /etc/vmware/config
# Zen 4/5 entropy collection is slow; widen the kernel's entropy source
# count. Needs a reboot, served by the one below.
esxcli system settings kernel set -s entropySources -v 2
# vSAN's default compression (Zstd) costs more CPU on this hardware than
# LZ4. No reboot needed.
esxcli system settings advanced set -o /VSAN/Vsan2ZdomCompZstd -i 0

# ESX generates its certificates before the hostname is configured, ...
```
One reboot (the pre-existing cert-regen one) now serves `disable_apichv` and
`entropySources` too — confirmed by test that `esxcli system shutdown reboot`
appears exactly once.

**What changed:** `provision.py` (head/workaround/tail template split, new
`_workarounds_block`/`_GENERIC_EPYC_BRAND`); `catalogue.yaml`
(`VCF-CAP-TIERING-NEEDS-WORKAROUND`); `platform.py` (`_capacity_rules` now
flags `memoryTieringGb > 0` without `consumer-amd`); `v1.schema.json`
(`provisioning.hostWorkarounds` enum, `hardware.cpuModel`); `lab-3-host.yaml`
(all three hosts now request `consumer-amd`, declare `cpuModel: 7945HX`);
spec doc gets a new "Consumer-AMD host workarounds" section plus three
Acceptance bullets. `provision.py` stays pure — no file/network access added.

**Disagreement:** none technical. One judgment call worth flagging: I
included all four workarounds (not just the two load-bearing ones) under the
single `consumer-amd` flag, on the reading that the enum is meant to mean
"this is a lab Ryzen host," and splitting entropy/vSAN-compression into their
own opt-in values wasn't asked for. If you'd rather gate the two
lower-stakes settings separately, that's a small follow-up, not a rework.
