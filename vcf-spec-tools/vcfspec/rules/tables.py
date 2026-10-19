"""Sizing tables.

Per-appliance rows are transcribed from VCF-Design-Studio (MIT,
github.com/mavlite/VCF-Design-Studio), whose APPLIANCE_DB and
DEPLOYMENT_PROFILES come from the VCF Planning and Preparation Workbook
static reference tables -- the 9.1 'simple' profile.

The *membership* of the list is not the workbook's. It is this lab's
approved 9.1.1 design (2026-09-24), which deploys a deliberately smaller
set than the workbook's default profile. Each deviation, and why:

- Operations Fleet Manager REMOVED. It is not a standalone appliance in
  9.1: Fleet Lifecycle became a service on the VCFMS Kubernetes cluster,
  so its 4/12/194 is already inside the VCFMS rows and counting it again
  double-counted it.
- Operations Collector REMOVED. Deliberately not deployed -- one vCenter
  and three hosts are collected by the Operations node directly.
- VCF Operations Medium -> Small (8/32 -> 4/16). Small is what the 9.1
  installer floors the Simple profile at.
- VCFMS worker nodes x3 -> x2. 9.1.1 right-sizes VCFMS for Day-0 rather
  than for the largest optional Day-N service, which drops one 12 vCPU /
  24 GB worker. This is automatic on FRESH 9.1.1 installs only; an
  environment upgraded from 9.1.0 keeps three workers until the KB 455842
  script is run, and that script refuses to run while Log Management or
  Real-time Metrics are installed.
- VCF License Server ADDED (2/4/50). New mandatory component in 9.1 that
  the transcribed profile predates.
- VCF Automation is absent because this design defers it. It is not free:
  adding it is +20 vCPU / +96 GB, and 20 is the floor -- the 16-vCPU
  figure circulating for 9.0 does not hold on 9.1.

Storage for "VCF Operations Small" keeps the Medium row's 274 GB. The
workbook figure for Small was not separately verified, and carrying the
larger number over is the conservative direction for a shortfall check.
"""
from __future__ import annotations

# (component, vcpu, ram_gb, storage_gb)
MANDATORY_STACK_9_1_1 = (
    ("vCenter Small", 4, 21, 694),
    ("NSX Manager Medium", 6, 24, 300),
    ("SDDC Manager", 4, 16, 914),
    ("vCLS x2", 2, 0.25, 4),
    ("VCF Operations Small", 4, 16, 274),
    ("VCF License Server", 2, 4, 50),
    ("VCFMS control node", 4, 10, 100),
    ("VCFMS worker nodes x2", 24, 48, 200),
)

# The vcpu column (row[1]) is transcribed for completeness with the source
# table but deliberately has no derived STACK_VCPU sum: _capacity_rules
# checks RAM and storage only, never vCPU count, because vCPU is routinely
# oversubscribed in a vSphere cluster (a 4:1 or higher ratio is normal),
# unlike RAM and storage, which cannot be oversubscribed without the
# workload actually failing. A raw "total physical cores >= stack vCPU
# demand" check would be checking the wrong thing -- and would also fail
# the bundled example (48 physical cores across 3 hosts vs a 50-vCPU
# mandatory stack), which is deployable in practice. See
# VCF-CAP-UNKNOWN-HARDWARE's catalogue entry for the corresponding fix-text
# correction: it used to promise a hardware.cores capacity check that this
# module has never implemented.
STACK_RAM_GB = sum(row[2] for row in MANDATORY_STACK_9_1_1)        # 139.25
STACK_STORAGE_GB = sum(row[3] for row in MANDATORY_STACK_9_1_1)    # 2536

AUTO_RAID_OVERHEAD = 1.5       # vSAN ESA Auto-RAID: RAID-5 (2+1) at 3-5 hosts
ESX_HOST_RAM_OVERHEAD_GB = 6   # reserved per host for the hypervisor
TB_TO_GB = 1000
VSP_POOL_MIN = 12
