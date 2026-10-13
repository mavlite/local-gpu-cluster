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
5. **Deploy** (`79 deploy` -> `scripts/files/wf-runner-esx.sh`): govc `import.vmdk` host-direct to `hypNN-local`,
   port group `SDDC-DPortGroup-VM-Mgmt` (ephemeral binding: works without vCenter), static IP via guestinfo,
   `sched.mem.enableTiering=FALSE`. **Done 2026-10-13, powered off:** wf-runner-01 hyp01 .208, -02 hyp02 .209,
   -03 hyp03 .210, all vmx-22. Learned on the way:
   - a vCenter-managed host refuses the memory reservation host-direct while vCenter is up (extraConfig is
     accepted), so `deploy` warns and **`79 power-on` sets the reservation** -- in Minimal mode, vCenter down;
   - power-on refuses without the reservation, without `enableTiering=FALSE`, or if the runner would take the host
     past 80% of DRAM, counting the larger of consumed memory and the memory of powered-on VMs (a VM started
     moments ago has not touched its pages yet). During the soak hyp01 had 75.8 GB committed: it would refuse;
   - credentials piped from PowerShell 5.1 gain a BOM on the first line: send a throwaway first line.

   **Soak exit order** (after ~02:10Z 2026-10-14): `Stop-VCFLab` (records the license sync) -> Minimal with the
   workers -> `79 power-on` each runner (sets the reservation once; it persists in the VMX) -> only then add the
   runners to the vcf-lab `LlmWorkers` list after the workers, so later `Start-VCFLab -Minimal` cycles (which power
   on host-direct without these guards) start them and Full mode stops them. HA restart priority "disabled" is a
   cluster override that needs vCenter: set it at the next Full window (a runner's disk is host-local, so HA could
   not restart it elsewhere meanwhile).
6. **Parity on each runner**, then record results.
