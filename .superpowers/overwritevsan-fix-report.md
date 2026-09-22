# --overwritevsan fix report

**Status:** fixed and committed.
**Commit SHA:** 0de77383299f08833c8073924361bcfd5c8b1439

**Test summary:** `cd vcf-spec-tools && python -m pytest -q` -> 498 passed (496 baseline + 2 new). Mutation test performed: reverted `overwritevsan_opt` to unconditional `" --overwritevsan"`, reran `test_provision.py` -> 3 tests failed as expected, then restored the fix and reconfirmed 498 passing.

**Before:**
`install --disk={boot_disk} --overwritevmfs --overwritevsan`

**After:**
`install --disk={boot_disk} --overwritevmfs{overwritevsan_opt}`
where `overwritevsan_opt` is `" --overwritevsan"` only when `hardware.bootDiskClaimedByVsan` is `true` (default `false`), else `""`. Added that boolean to `vcfspec/schemas/inventory/v1.schema.json` (hardware object) with a description covering both failure modes and the verbatim ESX 9.1.1 error. Updated the `_KICKSTART_HEAD` block comment in `vcfspec/provision.py`, the design spec (`docs/superpowers/specs/2026-09-21-esx-provisioning-design.md`), and `tests/test_provision.py` (fixed the now-wrong assertion, added `test_overwritevsan_is_absent_by_default` and `test_overwritevsan_is_present_when_the_boot_disk_was_a_vsan_member`).

**`--overwritevmfs` finding:** could not confirm a symmetric failure mode from documentation. Broadcom's script-command reference (and community kickstart docs going back to 5.x/7.0/8.0) describe it only as "required to overwrite an existing VMFS datastore" — nothing documents it erroring on a disk with *no* VMFS, and long-standing community kickstarts commonly pair `--overwritevmfs --novmfsondisk` on disks that may or may not already carry VMFS, implying it's safe when absent. I did not find (or have access to reproduce against real hardware) a documented failure analogous to the vSAN case, so per the constraint I left it unconditional rather than guessing.

**Disagree/flag:** nothing to disagree with. One judgment call worth noting: I named the field `hardware.bootDiskClaimedByVsan` rather than something like `hardware.bootDiskIsVsanRebuild`, to mirror the disk's actual state (matching `vsanDevice`'s naming style) rather than the operator's intent — seemed more durable if reused elsewhere later.
