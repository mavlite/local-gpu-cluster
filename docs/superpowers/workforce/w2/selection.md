# W2 task set: selection record

This is the frozen real-repo task set for W3 (workforce spec §8; Plan D Task 7). It is disjoint from
W1, which uses w1-r1 `3d75bd4`, w1-r2 `9017c99`, w1-r3 `ad6e575` and the gate's T1, T2 and T4.

| ID | Commit | Kind | Size (tokens, est.) | Visible test |
|---|---|---|---|---|
| w2-01 | `79a2f69` | ops | 3,915 | `scripts/files/tests/test_host_config_zfs.py` |
| w2-02 | `817a624` | ops | 3,480 | `scripts/tools/gate/tests/test_gate_env.py` |
| w2-03 | `0085146` | ops | 5,517 | `scripts/files/tests/test_v620_fan.py` |
| w2-04 | `c4d8403` | router | 36,421 | `scripts/files/tests/test_alias_defaults.py` |
| w2-05 | `bc85434` | router | 3,616 | `scripts/files/tests/test_workforce_lane.py` |
| w2-06 | `abfa787` | router | 38,235 | `scripts/files/tests/test_web_fetch_guard.py` |
| w2-07 | `d547fbc` | router | 35,717 | `scripts/files/tests/test_tavily_cache.py` |
| w2-08 | `3259c5a` | rag | 16,216 | `scripts/rag/tests/test_source_interleave.py` |
| w2-09 | `f0eb0ac` | rag | 11,206 | `scripts/rag/tests/test_searchindex.py` |
| w2-10 | `19da0b2` | rag | 19,598 | `scripts/rag/tests/test_source_interleave.py` |
| w2-11 | `8b107e4` | monitor | 19,751 | `scripts/files/tests/test_cluster_monitor.py` |
| w2-12 | `5265c8d` | monitor | 19,209 | `scripts/files/tests/test_cluster_monitor.py` |
| w2-13 | `3828f7f` | monitor | 10,501 | `scripts/files/tests/test_cluster_monitor.py` |
| w2-14 | `467b5e6` | monitor | 2,296 | `scripts/files/tests/test_cluster_monitor.py` |
| w2-15 | `f5ae5b6` | monitor | 4,041 | `scripts/files/tests/test_cluster_monitor.py` |
| w2-16 | `2f51899` | sandbox | 1,418 | `scripts/files/tests/test_wf_conntrack_check.py` |

- **Sizes** are bytes divided by 3.5, counting the declared files at the parent plus the visible tests
  at the commit. Every task fits the 64K-token worker context; the largest is about 38K. The limit
  biases the set toward smaller tasks, and that bias is recorded as a limit of the experiment.
- **Ops tasks:** 3 (w2-01..03), all tested with fakes. The nested lab was dropped by the user on
  2026-10-07.
- **Replacements:** two, both in the ops slots, by the plan's rule (the next reserve of the same kind).
  - Primaries `1f51fa5` (nft braces) and `e553446` (memory-capped unit) passed local validation but
    failed in the Docker grader. Their visible test file, `test_scripts_static.py`, contains a
    PowerShell-parse test that **skips** where `pwsh` is absent (the Linux grader). The grader
    requires every expected test to pass, so a skip counts as a failure.
  - They were replaced by `817a624` (the gate window stops and restores `rag-refresh.timer`) and
    `0085146` (a deliberate fan-bridge stop is not a unit failure).
  - The grader image was not changed.
  - Reserves still unused: `f18bb47` and `5d942d7` (router), `2cd3ca5` (rag), and `e8cd1d8`,
    `81ebf59`, `c63a3f6`, `d7e2c38`, `e4a93e3` (monitor).
- **Frozen 2026-10-08.**
  - Manifest: `54c494ae38dcfd2adb7781257a65969c84a7986de5db988f17a770c2df7bb146`, identical locally and
    in `docker:wf-grader:1`.
  - Archive on the host: `/root/wf/w2-frozen.tgz` (bundles + refs), sha256
    `dcf1813f68d1eed17b65960c18905d7563e99ac0ea40e8444fc515e8cef4695a`.
- **Hidden tests** were each verified to fail at `<commit>~1` and pass at `<commit>`, with the commit's
  visible tests in place in both.
  - They target behaviour the request asks for that the visible tests don't pin.
  - Where a hidden test needs the visible test's helpers, it imports them with an explicit
    `sys.path` insert.
- **w2-13:** the commit's own visible test already passes on its parent, because the old substring
  lookup handles that key order. The hidden test supplies the look-alike label the commit
  disambiguates, so the task is not vacuous.
