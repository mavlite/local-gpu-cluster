# IP pool rule report

**Status:** done, committed on `feat/esx-provisioning`.
**Commit SHA:** a2e2a41

**Tests:** `cd vcf-spec-tools && python -m pytest -q` -> 509 passed (was 498; +11 new). All three guards (missing-pool, too-small, .0/.255 exclusion) mutation-tested individually by neutralizing each in turn and confirming the suite reddens, then restored.

**New codes:**
- `VCF-NET-IP-POOL-MISSING` (error) -- vmotion/vsan network has no pool.
- `VCF-NET-IP-POOL-TOO-SMALL` (error) -- pool's usable addresses (excluding .0/.255) < host count.

**Files:** `vcfspec/rules/network.py` (`_ip_pool_rules`, `_usable_pool_size`, `_count_congruent`), `vcfspec/rules/catalogue.yaml`, `vcfspec/examples/lab-3-host.yaml` (vmotion/vsan pools .20-.30), `tests/test_rules_network.py`.

**Disagreements/notes:**
- Schema already fully specified `pool.start`/`pool.end` (optional) -- no schema change was needed; item 2 in the brief was already satisfied.
- Used a closed-form count instead of iterating addresses to exclude .0/.255, since pool bounds aren't bounded in size and iterating a full /0 range would hang.
- Left `source_url` empty on both catalogue entries: the evidence is a live Installer API response, not a stable docs page, and I didn't want to fabricate a URL.
