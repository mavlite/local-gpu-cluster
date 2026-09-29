# VCF lab power control

Controlled power-on and power-off for the three-host VCF 9.1.1 lab.

```powershell
.\Start-VCFLab.ps1                 # bring up the VCF stack
.\Start-VCFLab.ps1 -SkipInventoryRepair   # report inventory drift, do not fix it
.\Stop-VCFLab.ps1                  # shut down the VCF stack, leave hosts running
.\Stop-VCFLab.ps1 -IncludeHosts    # also power off the ESX hosts
.\Stop-VCFLab.ps1 -WhatIf          # dry run
```

Requires PowerCLI (`Install-Module VMware.PowerCLI -Scope CurrentUser`) and the
credential file at `%USERPROFILE%\.vcflab\credentials.env`. Credentials are read
into `PSCredential` objects and never printed, logged, or passed on a command
line.

## Scope: VCF components only

These scripts power **only** VCF components. The managed set is computed at run
time from the config:

- `VCenter.VmName`
- every `Appliances.*.VmName` — SDDC Manager, NSX, Operations, License Server,
  Operations Collector
- VSP nodes matching `Vsp.NamePrefix` inside `Vsp.Folder`

Anything else on the cluster — developer VMs, containers, appliances, one-off
workloads — is never started, never stopped, and never migrated. Both scripts
print the managed set at the start and list everything else under **"left
alone"**. There is deliberately no flag to widen the scope: if you want a
non-VCF VM powered, do it yourself so the decision is visible.

### The one place this bites: `-IncludeHosts`

A host cannot enter maintenance mode while any VM is running on it. "Only touch
VCF components" and "power off the hosts" are therefore incompatible whenever
something foreign is still up.

`Stop-VCFLab.ps1 -IncludeHosts` resolves that by **refusing**: it names every
running non-VCF VM and the host it sits on, then exits 1. The VCF components are
already down at that point, so you stop those VMs yourself and re-run. It will
not stop a VM it does not own in order to clear the way, and it will not
silently skip the hosts either.

Host power-off is opt-in for the same reason — the default is a VCF-stack
shutdown with the hosts left running.

## Order

| # | Power-on | Gate before advancing |
|---|----------|-----------------------|
| 0 | ESX hosts (**manual** — no BMC) | authenticated API on all three |
| 1 | — | vSAN reports 3 members on every host |
| 2 | vCenter | authenticated API call succeeds |
| 2a | — | every host's VM power states match vCenter's |
| 3 | SDDC Manager | `POST /v1/tokens` returns a token |
| 4 | NSX Manager | cluster STABLE **and** all service groups up **and** transport nodes success |
| 5 | VCF Operations | `suite-api` auth returns a token |
| 6 | VSP control plane | guest IPs **and then** 6443 listening |
| 7 | VSP workers | guest IPs |
| 8 | License Server | guest IP |
| 9 | Operations Collector | guest IP |

Power-off is the reverse **with one deliberate exception** — see below.

## Six things these scripts encode, learned the hard way

**1. The VSP control plane is stopped BEFORE its workers.**
This reverses the bring-up order and looks wrong. The supervisor is
self-healing: with the control plane alive it restarts workers as fast as you
stop them. On 2026-09-27 three workers were force-stopped and came straight
back; the second pass, with the control plane already down, stopped them
gracefully first time. `Stop-VCFLab.ps1` also re-checks afterwards and sweeps
any node that reappeared.

**2. vCenter is never hard-killed by default.**
Its graceful shutdown can exceed any timeout you care to guess at. A vCSA hard
stop risks the embedded database. `StopGraceSeconds.VCenter = 0` means "wait
indefinitely"; `-Force` permits a hard stop after `VCenterForce` seconds. A
fixed 420s timeout hard-killed a vCSA once — hence the default.

**3. A powered-on VM is not a running service, and an open port is not
readiness.**
`rhttpproxy` answers 443 long before `hostd` is up, and VSP control-plane nodes
report guest IPs roughly a minute before 6443 listens. Every tier advance goes
through `Wait-Gate` with a functional test.

**4. vSAN evacuation mode differs by intent.**
Whole-cluster shutdown uses **`NoDataMigration`** — there is nowhere to evacuate
to when every host is going down. Single-host maintenance uses
**`EnsureAccessibility`**; `FullDataMigration` *fails* on three hosts at FTT=1
because there is no fourth host to hold the copy.

**5. A wedged vpxa looks like a broken VM.**
On 2026-09-29 every power-on on one host failed with *"The object
`vpx.vmprov.PowerOnVm` has already been deleted or has not been completely
created"*. Nothing was actually broken: the host was Connected and out of
maintenance mode, esxcli **through vpxa** answered, vSAN had 93 objects healthy
and none inaccessible, and the VM was green with 61 GB free on the host. vpxa
had stopped reporting VM state, so vCenter planned against stale objects — it
insisted a VM was PoweredOff while the host had it running with a boot time and
NSX was serving traffic on it.

Three things now encode that:

- **Step 2a** compares each host's own view against vCenter's before anything
  downstream reads VM state, and restarts vpxa where they disagree.
- `Start-LabVM` falls back to powering a VM on **via its host** when vCenter
  refuses, then repairs that host — reaching the fallback is *proof* the
  inventory is stale.
- `Restart-VMHostService` **throws on success** (vpxa drops the connection as it
  restarts). The code swallows that and judges the outcome by polling real
  state. A reconnect (`Set-VMHost -State Connected`) does *not* fix it; the vpxa
  restart does, in about twenty seconds. Rebooting hosts is never necessary.

Step 2a cannot see a wedge on a fully cold cluster, where every VM is off on
both sides and there is nothing to disagree about — that case surfaces on the
first power-on, which is why the fallback exists too. `-SkipInventoryRepair`
reports without touching anything.

Why this matters beyond one failed power-on: every later gate reads guest IPs
**from vCenter**, so a stale view does not just fail a power-on, it burns each
gate's full timeout on nodes that are up and healthy.

**6. VSP node names are not stable.**
The supervisor destroys and recreates workers by itself (`bn2r2` destroyed,
`8n62z` created, on the same day). Control plane vs worker is therefore
discovered by **vCPU count**, not by name. The names in the config are a
fallback only.

## Other behaviour worth knowing

- **vSphere HA restarts management VMs on host events.** It resurrected `ops`
  and `platform-pfnmx` twice during maintenance. Use `-DisableHaFirst` on
  shutdown to turn host monitoring off for the duration; re-enable it yourself
  afterwards (the script tells you it changed it).
- **DRS placement.** If DRS is Manual nothing will place or balance VMs;
  `Start-VCFLab.ps1` warns at the end. If DRS is fully automated it will move
  VMs mid-sequence, including onto a host you are about to work on.
- **Hosts cannot be powered on remotely** unless the onboard Realtek RTL8125 is
  cabled and its MAC is set in `Hosts[].WolMac` — the cabled Intel X520 10GbE
  NICs do not support WoL at all. With no MAC configured, power-on is a physical
  action; the script waits and says so. WoL is power-ON only.
- **hyp03 does not always power down fully on a software reboot** — it has been
  seen answering ICMP with every TCP port refused, needing a chassis power
  cycle. A *shutdown* works; a *reboot* is the unreliable one.

## Files

| File | Purpose |
|------|---------|
| `VCFLab.Config.psd1` | topology, timeouts, grace periods, scope — edit here |
| `VCFLab.Common.ps1` | gates, credential loading, scope resolution, power helpers |
| `Start-VCFLab.ps1` | power-on |
| `Stop-VCFLab.ps1` | power-off |
| `tests/Test-VCFLabScripts.ps1` | stub harness — runs both scripts with no live lab |
| `tests/stub-overrides.ps1` | the network/sleep stubs the harness appends to a copy of `VCFLab.Common.ps1` |

## Testing without a lab

```powershell
.\tests\Test-VCFLabScripts.ps1     # exit code = number of failures
```

It copies the scripts to a sandbox under `%TEMP%`, appends `stub-overrides.ps1`
to the copied `VCFLab.Common.ps1`, and runs both scripts against fake VMs.
Nothing reaches the network and the real credential file is never read — the
sandbox gets its own dummy one.

Two things about that harness are worth knowing before you extend it:

- **Overrides must be appended to the copied `VCFLab.Common.ps1`**, not defined
  in the harness. Each script dot-sources `Common` into its own script scope,
  and those definitions shadow anything a parent scope defined — so a stub in
  the harness silently loses and the run calls the live lab.
- **Stub state must be `$global:`, not `$script:`.** Called from inside
  `Start-VCFLab.ps1`, a stub resolves `$script:` against *that* script's scope,
  where the variable does not exist; under the script's `Set-StrictMode
  -Version Latest` that throws, the throw is swallowed by the gate's own
  try/catch, and it surfaces as an ordinary gate timeout.

What it asserts: the VSP control plane stops before its workers; neither script
ever touches a non-VCF VM; `-IncludeHosts` refuses (and names the blockers)
while a foreign VM runs; and it powers the hosts off once the way is clear.

## Status

Syntax-validated, and the scope and ordering invariants pass under the stub
harness above (9/9). **The power sequences have not yet been run end to end
against a live lab** — they were written immediately
after a manual shutdown, so the gates and ordering reflect verified behaviour but
the PowerCLI calls themselves are untested in anger. Run `-WhatIf` first, and
watch the first real run.
