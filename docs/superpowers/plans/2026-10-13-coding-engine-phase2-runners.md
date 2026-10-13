# Coding engine — Phase 2: runner VMs on the Minisforum nodes

**Design:** `docs/superpowers/specs/2026-10-12-local-coding-engine-design.md` §3.2, §3.2.1 (Agent mode), §5 phase 2.
**Done when (design):** the 16-task set grades on a runner with the same results as on sandbox VM 176.

## Constraints that shape it

- **VLAN 10 has no internet** (lab memory), so runners cannot install packages where they run. The image is built
  and provisioned on the Proxmox side (internet in build mode), converted to a VMware disk, and imported onto each
  host's local datastore. Package updates mean a rebuild, not apt on the runner.
- **Base image: Debian 13 *generic* cloud image**, not the *genericcloud* one sandbox VM 176 uses: genericcloud's
  cloud kernel drops most hardware drivers, and ESXi guests need pvscsi/vmxnet3 (or e1000e). open-vm-tools for
  guest operations and IP reporting (the only control path, as for the llmbench workers).
- **Same grading stack as VM 176**, from the same script pieces: `/opt/wfpy` venv (its freeze builds the grader
  image), `wf-grader:1` (network-less Docker), shellcheck, ripgrep; plus PowerShell + Pester for the repo's VCF
  scripts. No opencode, no agent users: runners grade and execute, they do not host agents.
- **Deployment waits for memory headroom.** Each runner is 16 GB, tiering off + full reservation (design §3.2.1),
  so power-on needs 16 GB of free DRAM. During the VCF + agent-tier soak the hosts sit at 93-106 GB consumed of
  ~94 GB DRAM, which crash-hardening rule 6 forbids (the 2026-10-03 PSOD was a power-on allocation). Build and the
  parity test run on Proxmox now; host deployment follows the soak (Full -> Minimal, ~02:10Z 2026-10-14).

## Steps

1. **`scripts/files/wf-runner-provision.sh`** (runs inside the build VM): packages, `/opt/wfpy`, `wf-grader:1`,
   pwsh + Pester, open-vm-tools, cloud-init with the VMware datasource; ends with a JSON summary; cleans
   cloud-init state and machine-id for templating. Tests: syntax, required packages, the grader image tag shared
   with `wf-sandbox-provision.sh`.
2. **`scripts/79-vm-wf-runner-image.sh`** (host): VM 177 from the Debian 13 generic image on vmbr0 (DHCP, build
   only), run step 1 through the guest agent, then shut down.
3. **Grading parity on VM 177** before export: push the frozen W2 bundles + the harvested `lt-9-s38` patches, grade
   with the harness's Docker grader, compare pass/fail per task with the records graded on VM 176.
4. **Export**: zvol -> streamOptimized VMDK + OVF (vmx-22, pvscsi, vmxnet3 or e1000e, 8 vCPU, 16 GB).
5. **Deploy** (after the soak): govc `import.ovf` host-direct to `hypNN-local`, port group `SDDC-DPortGroup-VM-Mgmt`
   (ephemeral binding: works without vCenter), static IP via guestinfo, `sched.mem.enableTiering=FALSE` + full
   reservation, HA restart disabled, added to the vcf-lab `AgentTier` group so Full mode stops it.
6. **Parity on each runner**, then record results.
