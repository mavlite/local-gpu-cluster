"""ESX hardware health: Zen 4 temperature, RAPL power and APERF/MPERF clock decoding."""

SCHEMA = 1
REPORT_NAME = "lab.hwHealth"
# Written by the sampler (superDom), read by the plugin (healthdDom). RAM, not disk.
CACHE = "/usr/lib/vmware/healthd-config/lab-hwhealth/cache.json"
