# NVMe health thresholds research (hyp01 drives)

Research of 2026-11-08 behind the NVMe rows of the spec's Thresholds table
(`docs/superpowers/specs/2026-11-06-esx-hardware-health-design.md`). Section 8 is the
proposal. The owner adopted it with simplifications: every known Critical Warning bit is
ERROR, and the composite-temperature-time counters are not graded. **The spec is the
authority.**

Scope: fixtures in scripts/esx-hwhealth/tests/fixtures cover hyp01 only (vmhba0..2). Nothing in them indicates hyp02/hyp03 models.
Research limits: Crucial/Micron product pages and Samsung datasheets were blocked (403) or lacked specs in this session. Manufacturer-sourced numbers below are therefore mostly via StorageReview/FunkyKit/CDRLabs quoting the spec sheet. Items marked UNVERIFIED were not confirmed.

## 1. Fixture facts (firmware-reported)

| Adapter | Model | FW | WCTEMP | CCTEMP | Avail Spare Thr | Used | Spare | Comp temp | Media err | Err log entries |
|---|---|---|---|---|---|---|---|---|---|---|
| vmhba0 | CT4000P310SSD8 (P310 4TB) | V8CR010 | 359 K = 85.85 C | 361 K = 87.85 C | 5 % | 1 % | 100 % | 314 K (40.85 C) | 0 | 0 |
| vmhba1 | SAMSUNG MZVLB512HAJQ-000L7 | 5L2QEXA7 | 354 K = 80.85 C | 355 K = 81.85 C | 10 % | 11 % | 100 % | 314 K | 0 | 0x1db = 475 |
| vmhba2 | CT1000P310SSD2 (P310 1TB) | V8CR001 | 359 K = 85.85 C | 361 K = 87.85 C | 5 % | 0 % | 100 % | 311 K (37.85 C) | 0 | 0 |

Derived: vmhba1 has 0xd281 = 53,889 power-on hours (~6.2 yr), ~50 TB written (0x5d5f884 data units x 512,000 B). vmhba0 ~16.7 TB written, vmhba2 ~1.95 TB (0x39f9f6 = 3,799,542 units). Neither SMART dump has a Warning/Critical Composite Temperature Time above 0.

Important identity note: the model string MZVLB512HAJQ is the original **PM981** 512GB (the PM981a is MZVLB512HBJQ). Treat it as PM981 (Polaris, V-NAND TLC, FW 5L2Q...). The P310 4TB is a 2280 (CT4000P310SSD8); the 1TB is 2230.

Key observation: the drives' own WCTEMP/CCTEMP are very different:
- P310: 86 / 88 C (a 2 C gap). Throttling begins far below this (see 2), so WCTEMP is NOT an early-warning level; it is near the death line.
- Samsung PM981: 81 / 82 C (1 C gap).
With "WARNING >= WCTEMP else 70 C", the spec as written would not warn until 86 C on P310 and 81 C on Samsung, i.e. after throttling has been active for ~5-15 C.

## 2. Temperature

Operating range
- P310: Operating temperature 0 to 70 C. Source: Neowin deal listing for the 4TB ("Operating Temperature: 0°C to 70°C"), not confirmed against Crucial's own sheet (Crucial site blocked). Whether it is composite or case temp is not stated in anything I could read. UNVERIFIED which sensor.
- PM981: datasheet listings (SHI / directmacro mirrors of the Samsung brochure) say operating 0 to 70 C, storage -40 to 85 C. The directmacro PDF itself 403'd; this came from search-result summaries. Sensor not stated; Samsung client SSD sheets generally refer to the SMART composite/"drive" temperature, but I could not confirm.

Throttling measurements
- P310 2TB 2230 (CDRLabs): "When the P310's temperature reached about 81 ºC, Adaptive Thermal Protection would kick in and throttle its performance." https://cdrlabs.com/reviews/crucial-p310-2tb-pcie-gen4-nvme-2230-m2-solid-state-drive/adaptive-thermal-protection-and-final-thoughts.html
- Other reviews: OC Inside (1TB 2230) says throttling around 70 C (from search summary, not read); ServeTheHome 2TB reached 82 C; FunkyKit "Under load, the P310 series keeps temperatures up to 77°C"; StorageReview says only that it throttles "when necessary to prevent overheating" (no number). Tom's Hardware: no temperature number found in readable text.
- No source gave TMT1/TMT2 for P310. Firmware reports WCTEMP 359 K (86 C), so throttling at ~81 C in review sits below WCTEMP (the controller throttles via adaptive thermal protection before the SMART warning temp).
- PM981 512GB (Tom's Hardware, via search summary): throttled during extended sequential read after ~109 s / ~207 GB; no temperature number found. Firmware WCTEMP 81 C / CCTEMP 82 C sits right at typical throttling territory for this controller. TMT values not found.

## 3. Endurance
- P310 1TB: 220 TBW; 2TB: 440 TBW; warranty 5 years; MTTF 1.5 million hours. Quote (StorageReview): "the 1TB model rated at 220TBW and the 2TB model rated at 440TBW"; "The mean time to failure (MTTF) is 1.5 million hours." https://www.storagereview.com/review/crucial-p310-ssd-review (5-year limited warranty per its spec table.)
- P310 4TB: 800 TBW per a Neowin listing ("Endurance: Up to 800 TBW"). UNVERIFIED vs Crucial; consistent with 220/440 scaling (~200 TBW/TB).
- PM981 512GB: MTBF 1.5 million hours (listing summaries). TBW: search found 150/300/600 TBW for the PM981 family capacities (256/512/1TB), which would make 512GB = 300 TBW, but the page did not map them explicitly -> UNVERIFIED; warranty is OEM-channel (none from Samsung to retail buyers).
- Percentage Used semantics (libnvme doc quoting NVMe base spec): "A value of 100 indicates that the estimated endurance of the NVM in the NVM subsystem has been consumed". The spec allows values >100 (up to 255). It is a vendor's wear estimate; drives continue to work past 100 but the vendor no longer guarantees them. Sanity check on fixtures: PM981 ~50 TB written -> 11 % used vs 300 TBW -> 17 % expected, so the controller's number is a reasonable-to-slightly-optimistic measure.

## 4. Available Spare
- Fixture thresholds: P310 5 %, Samsung 10 % (drive's own). All three at 100 %.
- NVMe semantics: Available Spare = "normalized percentage (0% to 100%) of the remaining spare capacity available"; "When the Available Spare falls below the threshold indicated in this field, an asynchronous event completion may occur." Critical Warning bit 0 is set when spare "has fallen below the threshold". https://manpages.ubuntu.com/manpages/noble/man2/nvme_smart_log.2.html
- Consequence: the drive's own threshold is the manufacturer's error line. Fixed ≤10 % / ≤20 % would be wrong for the P310 whose threshold is 5 %, and ERROR at 10 % would equal the Samsung threshold. Better: ERROR when spare < drive's threshold (== bit 0) and WARNING when spare <= threshold + margin, or when spare < 100 % at all on a young drive (any decline from 100 is notable; judgment).

## 5. Critical Warning byte (NVMe base spec; bit text from libnvme types.h enum nvme_smart_crit)
https://raw.githubusercontent.com/linux-nvme/libnvme/master/src/nvme/types.h
- Bit 0: "the available spare capacity has fallen below the threshold."
- Bit 1: "a temperature is either greater than or equal to an over temperature threshold; or less than or equal to an under temperature threshold." (Fixture shows this as "Temperature Warning".)
- Bit 2: "the NVM subsystem reliability has been degraded due to significant media related errors or any internal error that degrades NVM subsystem reliability."
- Bit 3: "all of the media has been placed in read only mode." (not set if due to namespace write-protect)
- Bit 4: "the volatile memory backup device has failed. This field is only valid if the controller has a volatile memory backup solution."
- Bit 5: "the Persistent Memory Region has become read-only or unreliable." (NVMe 1.4+; P310 reports version 1.4 but 0 PMR support in the fixtures; Samsung is 1.2 and has no bit 5.)
- Bits reflect current state, not history: "Bits in this field represent the current associated state and are not persistent."
- Operational grading (netdata guide, practitioner not vendor): bit0 ticket / replacement signal; bit1 ticket, page if sustained (critical comp time increasing); bit2 ticket, page only with rising media errors, and some vendors trip it preemptively; bit3 page, unconditional; bit4 ticket but risk escalation; bit5 ticket only if PMR in use. https://www.netdata.cloud/guides/nvme/nvme-critical-warning-nonzero/
- Fixture shows only bits 0-4 as booleans (no PMR line) so bit 5 is not exposed by this esxcli output; nothing to grade there.

## 6. Media errors / error log
- Media and Data Integrity Errors: "the number of occurrences where the controller detected an unrecovered data integrity error." Kingston doc: includes uncorrectable ECC, CRC failure, LBA tag mismatch. https://media.kingston.com/support/pdf/ssd-smart-attribute.pdf
- Error Information log entries: "the number of Error Information log entries over the life of the controller." It counts all errors, including benign host-caused ones (invalid field, unsupported commands, aborted). Fixture: PM981 has 475 entries with 0 media errors -> clearly benign/normal; a non-zero count by itself must not alert. No vendor guidance found for Micron or Samsung on either field.
- Practitioner guidance (netdata): treat growth from baseline as the signal, not absolute value.

## 7. What SMART can and cannot predict
- Predicts: wear-out (Percentage Used, spare), sustained thermal abuse (comp temp time counters), controller-declared degradation (bits 0/2/3).
- Does not predict: sudden controller/firmware/power-related death, NAND/PMIC/thermal-solder or PCIe link faults. Netdata: "SMART is self-reported; treat it as one witness, not the verdict"; bit 2 trip points are vendor-defined; bits can be transient and miss polling; spare can deplete non-linearly near end of life.
- In this lab, memory-tiering NVMe drives failed with 0 media errors and no warnings beforehand (memory notes: tier_drive_failure_root_cause, tier_apst_disabled). That is consistent with the above: SMART gave no leading indicator. Hence thresholds should be framed as "catches wear and overheating", not "predicts failure". Memory tiering is a heavy-write, steady-load use that makes Percentage Used and temperature the useful signals; also watch Unsafe Shutdowns and Power Cycles deltas (not graded below).

## 8. Proposed thresholds

| Reading | WARNING | ERROR | Basis |
|---|---|---|---|
| Composite temp (all) | >= 70 C (min(WCTEMP, 70)) | >= WCTEMP (P310 86 C; PM981 81 C), or CCTEMP if lower than 85 | 70 C = vendor operating max (0-70 C) for both models; P310 throttles ~81 C (CDRLabs) and PM981 near 81 C WCTEMP. Judgment: ERROR at WCTEMP rather than CCTEMP because the CCTEMP is only 1-2 C above WCTEMP and by then the drive is already throttling. Sampling at 40 C idle leaves wide margin. |
| Warning/Critical Composite Temp Time | WCTEMP time > 0 min | CCTEMP time > 0 min | Judgment; counters are cumulative, so alert on increase since last poll, not absolute. |
| Percentage Used | >= 80 % | >= 100 % | Spec: 100 = estimated endurance consumed (NVMe). 80 % is judgment (replace-planning lead time). Drive keeps working past 100 so ERROR is "out of warranty envelope", not "failed". Today: 0/1/11 %. |
| Available Spare | <= drive threshold + 10 points (P310 15 %, PM981 20 %) or < 100 % while Percentage Used < 50 (early sign) | < drive's Available Spare Threshold (equivalent to bit 0) | NVMe spec for ERROR; margin is judgment. Falling spare faster than wear is anomalous. |
| CW bit 0 spare below threshold | -- | ERROR | NVMe: spare fell below threshold; replacement signal. (Could be WARNING if you treat as ticket-only; I recommend ERROR because the vendor has declared it.) |
| CW bit 1 temperature | WARNING | ERROR if also Critical Comp Time rising or comp temp >= CCTEMP | Netdata grading; bit 1 triggers at WCTEMP (over temp) or under temp. |
| CW bit 2 reliability degraded | WARNING if media errors flat | ERROR if media errors > 0 / rising | Netdata: only combination with rising media errors confirms degradation; vendors set it preemptively. |
| CW bit 3 read-only | -- | ERROR | Drive refuses writes. Unconditional. |
| CW bit 4 volatile backup failed | WARNING | ERROR if the drive has power-loss protection that matters (enterprise) | Consumer P310/PM981 have no volatile backup, bit invalid there ("only valid if the controller has a volatile memory backup solution"); a set bit on these drives would be suspicious -> ERROR is simpler. Judgment. |
| CW bit 5 PMR | WARNING | -- | Not exposed by current esxcli output; not used on these drives. |
| Media errors | -- | ERROR if > 0 (or increased since baseline) | Spec defines it as unrecovered data integrity errors; no benign cause. Judgment on strictness; fixtures all 0. |
| Error log entries | none (informational) | none | PM981 has 475 with 0 media errors: benign. Only alert on growth combined with media errors. |

Keep "any CW bit set -> at least WARNING" as a floor so unknown bits are never silent.

## 9. Not found
- Crucial/Micron official P310 datasheet (operating-temp definition, 4TB TBW, TMT1/TMT2): blocked.
- Samsung PM981 datasheet TBW/temperature text: blocked (403); TBW 300 for 512GB inferred.
- Tom's Hardware / TechPowerUp throttle point text: pages truncated or 403.
- Any vendor guidance on media error counts or error log entries.
- hyp02/hyp03 models: no indication in fixtures; the list above covers hyp01 only.
