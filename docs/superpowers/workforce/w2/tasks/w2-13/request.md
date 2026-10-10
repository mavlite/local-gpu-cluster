## Monitor: VRAM percentage is wrong when rocm-smi lists another "total" key first

`parse_rocm_vram_json` finds a card's total VRAM by looking for a key that *contains*
`VRAM Total Memory`. rocm-smi builds can list a used-memory label that also contains that phrase
before the real total. The total then gets read from the wrong key, and the VRAM check reports a
nonsensical percentage.

**Wanted:** the total comes from the `VRAM Total Memory (B)` value specifically, and used memory from
`VRAM Total Used Memory (B)`, whatever order or other keys the JSON has.
`scripts/files/tests/test_cluster_monitor.py` covers the parser.
