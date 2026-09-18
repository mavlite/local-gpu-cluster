"""Sizing tables.

Transcribed from VCF-Design-Studio (MIT, github.com/mavlite/VCF-Design-Studio),
whose APPLIANCE_DB and DEPLOYMENT_PROFILES come from the VCF Planning and
Preparation Workbook static reference tables. These are the 9.1 'simple' profile
values: the mandatory management stack for one instance.
"""
from __future__ import annotations

# (component, vcpu, ram_gb, storage_gb)
MANDATORY_STACK_9_1_1 = (
    ("vCenter Small", 4, 21, 694),
    ("NSX Manager Medium", 6, 24, 300),
    ("SDDC Manager", 4, 16, 914),
    ("Operations Fleet Manager", 4, 12, 194),
    ("vCLS x2", 2, 0.25, 4),
    ("VCF Operations Medium", 8, 32, 274),
    ("Operations Collector Medium", 8, 32, 274),
    ("VCFMS control node", 4, 10, 100),
    ("VCFMS worker nodes x3", 36, 72, 300),
)

STACK_VCPU = sum(row[1] for row in MANDATORY_STACK_9_1_1)          # 76
STACK_RAM_GB = sum(row[2] for row in MANDATORY_STACK_9_1_1)        # 219.25
STACK_STORAGE_GB = sum(row[3] for row in MANDATORY_STACK_9_1_1)    # 3054

AUTO_RAID_OVERHEAD = 1.5       # vSAN ESA Auto-RAID: RAID-5 (2+1) at 3-5 hosts
ESX_HOST_RAM_OVERHEAD_GB = 6   # reserved per host for the hypervisor
TB_TO_GB = 1000
VSP_POOL_MIN = 12
