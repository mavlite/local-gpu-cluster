<#
.SYNOPSIS
    Powers on the VCF lab in the documented order, gating each tier on a
    functional check.

.DESCRIPTION
    Order:  ESX hosts (manual) -> vSAN quorum -> vCenter -> SDDC Manager ->
            NSX -> VCF Operations -> VSP control plane -> VSP workers ->
            License Server -> Operations Collector -> developer VMs.

    Every tier advances only when the tier below it genuinely answers. A VM
    being PoweredOn proves nothing, and neither does an open port: rhttpproxy
    serves 443 well before hostd is ready, and VSP control-plane nodes report
    guest IPs roughly a minute before 6443 listens.

    The ESX hosts have no BMC. This script cannot power them on -- it waits
    for them and tells you what to do.

    SCOPE: VCF components only -- vCenter, the VCF appliances, and VSP nodes --
    plus, in Minimal mode, the configured LlmWorkers. Developer VMs, containers
    and any other workload are never touched, and are listed at the end as
    "left alone". If you want one started, start it yourself, so the decision
    is visible.

.PARAMETER Mode
    Full (default): the whole VCF stack, as above. LLM workers are left alone --
    running them beside the full stack pushes the management VMs onto the
    memory tier.

    Minimal: day-to-day running. ESX hosts only, out of maintenance mode, then
    the LlmWorkers powered on host-direct. vCenter, NSX, SDDC Manager,
    Operations and VSP stay OFF. The workers need neither vSAN nor vCenter
    (local datastores, ephemeral-binding port group). Refuses if VCF
    components are still running -- run Stop-VCFLab.ps1 first, which leaves
    the hosts and workers up. Also refuses -- failing closed -- when the
    recorded license sync is missing, unreadable, future-dated or older than
    LicenseWindow.RefuseDays (VCF 9 licenses lapse 180 days after a refresh).
    Stop-VCFLab records a sync when VCF Operations had been up 24 h or more.

.PARAMETER WithVCenter
    Minimal only: also start vCenter (for the UI), but nothing else.

.PARAMETER StopLlmWorkers
    Full only: shut the LLM workers down (host-direct, graceful) before the
    stack starts. Without it, Full REFUSES while any worker runs: each pins
    24 GB of DRAM, which pushes the management VMs onto the consumer tier
    drives -- the failure class behind the 2026-10-04 outage.

.PARAMETER IgnoreLicenseWindow
    Minimal only: start even though the last Full start is past RefuseDays.

.PARAMETER WhatIf
    Report what would happen without changing power state.

.EXAMPLE
    .\Start-VCFLab.ps1
.EXAMPLE
    .\Start-VCFLab.ps1 -Mode Minimal
#>
[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPlainTextForPassword','CredentialPath',
    Justification='This is a filesystem path to the credential file, not a secret. Secrets are read from that file into PSCredential objects and never printed.')]
param(
    [ValidateSet('Full','Minimal')][string]$Mode = 'Full',
    [switch]$WithVCenter,
    [switch]$StopLlmWorkers,
    [switch]$IgnoreLicenseWindow,
    [switch]$WhatIf,
    [switch]$SkipInventoryRepair,
    [string]$ConfigPath,
    [string]$CredentialPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'VCFLab.Common.ps1')

$cfg  = if ($ConfigPath) { Import-VCFLabConfig -Path $ConfigPath } else { Import-VCFLabConfig }
$cred = Import-VCFLabCredential -Path $(if ($CredentialPath) { $CredentialPath } else { $cfg.CredentialFile })
Disable-CertificateValidation
Initialize-PowerCLI

$esxCred  = Get-VCFLabCredentialObject -Map $cred -UserKey 'LAB_ESX_USER'        -PassKey 'LAB_ESX_PASS'        -DefaultUser 'root'
$ssoCred  = Get-VCFLabCredentialObject -Map $cred -UserKey $null                 -PassKey 'VCF_SSO_ADMIN_PASS'  -DefaultUser 'administrator@vsphere.local'
$nsxCred  = Get-VCFLabCredentialObject -Map $cred -UserKey $null                 -PassKey 'VCF_NSX_ADMIN_PASS'  -DefaultUser 'admin'
$opsCred  = Get-VCFLabCredentialObject -Map $cred -UserKey 'OPERATIONS_ADMIN_USER' -PassKey 'OPERATIONS_ADMIN_PASS' -DefaultUser 'admin'

$started = Get-Date
Write-Host "VCF lab power-on ($Mode)  ::  $started" -ForegroundColor White
$minimal = ($Mode -eq 'Minimal')
if ($WithVCenter -and -not $minimal) { Write-Warn2 "-WithVCenter only applies to -Mode Minimal; Full starts vCenter anyway" }

# --- license window (Minimal) ----------------------------------------------------
# Minimal keeps VCF Operations down, and VCF 9 licenses lapse after 180 days
# without a refresh -- hosts then disconnect from vCenter and workloads cannot
# start. Check before touching anything, so the refusal costs nothing.
if ($minimal) {
    Write-Step "License window"
    $lw = if ($cfg.ContainsKey('LicenseWindow')) { $cfg.LicenseWindow } else { @{ WarnDays = 30; RefuseDays = 150 } }
    $age = Get-LicenseStampAge -Config $cfg
    # Fail CLOSED. "I cannot tell when the license was last refreshed" is not a
    # reason to assume it is fine -- the cost of being wrong is every workload
    # refusing to start.
    $refuse = $null
    switch ($age.State) {
        'Missing'    { $refuse = "No license sync is recorded on this machine ($($age.Detail))." }
        'Unreadable' { $refuse = "The license sync record cannot be read: $($age.Detail)" }
        'Future'     { $refuse = "The license sync record is dated in the future ($($age.Detail)); check this machine's clock." }
        'Ok' {
            if ($age.Days -ge $lw.RefuseDays) {
                $refuse = "The last recorded license sync was $($age.Days) days ago. VCF 9 licenses lapse 180 days after the last refresh."
            } elseif ($age.Days -ge $lw.WarnDays) {
                Write-Warn2 "Last license sync $($age.Days) days ago. Plan a Full run before day $($lw.RefuseDays) and keep VCF Operations up 24 h."
            } else {
                Write-Ok "last license sync $($age.Days) days ago (refresh window OK)"
            }
        }
    }
    if ($refuse) {
        if ($IgnoreLicenseWindow) {
            Write-Warn2 $refuse
            Write-Warn2 "Continuing because -IgnoreLicenseWindow was given."
        } else {
            Write-Fail $refuse
            Write-Fail "Run .\Start-VCFLab.ps1 (Full), leave VCF Operations up for 24 h so it can sync the license,"
            Write-Fail "then .\Stop-VCFLab.ps1 -- it records the sync when it finds Operations was up that long."
            Write-Info "-IgnoreLicenseWindow overrides this (e.g. after confirming the license in VCF Operations)."
            exit 1
        }
    }
}

# --- 0. hosts -----------------------------------------------------------------
# No BMC. Wake-on-LAN can power a host ON if its onboard RTL8125 is cabled and
# BIOS has WoL enabled with ErP/Deep Sleep off; otherwise this is a physical act.
Write-Step "0. ESX hosts"
$down = @($cfg.Hosts | Where-Object { -not (Test-TcpPort -ComputerName $_.Ip -Port 443 -TimeoutMs 3000) })
if ($down.Count -gt 0) {
    Write-Info ("not responding: " + (($down | ForEach-Object Short) -join ', '))
    $wakeable = @($down | Where-Object { $_.WolMac })
    if ($wakeable.Count -gt 0 -and -not $WhatIf) {
        Write-Step "0a. Wake-on-LAN"
        foreach ($h in $wakeable) {
            if (Send-WakeOnLan -MacAddress $h.WolMac -Broadcast $cfg.WolBroadcast -Ports $cfg.WolPorts) {
                Write-Ok "magic packet sent to $($h.Short) ($($h.WolMac)) via $($cfg.WolBroadcast)"
            }
        }
        Write-Info "WoL is power-ON only; it cannot recover a hung host."
    }
    $noMac = @($down | Where-Object { -not $_.WolMac })
    if ($noMac.Count -gt 0) {
        Write-Warn2 ("no WolMac configured for: " + (($noMac | ForEach-Object Short) -join ', ') +
                     " -- power these on at the chassis")
    }
}
foreach ($h in $cfg.Hosts) {
    $ok = Wait-Gate -Name "$($h.Short) management API" -TimeoutSeconds $cfg.GateTimeoutSeconds.HostApi -Test {
        if (-not (Test-TcpPort -ComputerName $h.Ip -Port 443)) { return $false }
        try {
            $c = Connect-VIServer -Server $h.Ip -Credential $esxCred -Force -ErrorAction Stop
            $null = Get-VMHost -Server $c -ErrorAction Stop
            Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue
            $true
        } catch { $false }
    }
    if (-not $ok) { Write-Fail "Aborting: $($h.Short) never came up. Power it on at the chassis."; exit 1 }
}

# --- 1. vSAN ------------------------------------------------------------------
# Staggered host boots have produced a three-way partition where each host was
# MASTER of its own 1-member cluster. Do not proceed until quorum is real.
Write-Step "1. vSAN quorum"
# Minimal without vCenter does not need vSAN (the workers are on local
# datastores), so it only takes a short look rather than the full wait.
$vsanTimeout = if ($minimal -and -not $WithVCenter) { [math]::Min(120, $cfg.GateTimeoutSeconds.VsanFormation) } else { $cfg.GateTimeoutSeconds.VsanFormation }
$ok = Wait-Gate -Name "vSAN reports 3 members on every host" -TimeoutSeconds $vsanTimeout -Test {
    foreach ($h in $cfg.Hosts) {
        try {
            $c = Connect-VIServer -Server $h.Ip -Credential $esxCred -Force -ErrorAction Stop
            $esxcli = Get-EsxCli -VMHost (Get-VMHost -Server $c) -V2 -ErrorAction Stop
            $st = $esxcli.vsan.cluster.get.Invoke()
            Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue
            if ([int]$st.SubClusterMemberCount -ne $cfg.Hosts.Count) { return $false }
        } catch { return $false }
    }
    $true
}
if (-not $ok) {
    # The LLM workers live on local datastores, so a vSAN problem does not stop
    # them. Anything that boots from vSAN -- vCenter included -- needs it.
    if ($minimal -and -not $WithVCenter) {
        Write-Warn2 "vSAN did not form. Continuing: the LLM workers use local datastores, not vSAN."
        Write-Warn2 "Investigate the partition before the next Full start."
    } else { Write-Fail "vSAN did not form. Check for a partition before continuing."; exit 1 }
}

# --- M1. Minimal: the VCF stack must already be down -------------------------
# Checked BEFORE anything changes on the hosts, so a refusal leaves them exactly
# as found. Minimal never stops VCF components itself -- that is Stop-VCFLab's
# job, with its ordering and grace periods -- and starting workers beside a
# running stack is the memory squeeze Minimal exists to avoid.
$workers = @(Get-LlmWorkers -Config $cfg)
if ($minimal) {
    Write-Step "M1. VCF components must be down"
    $allow = if ($WithVCenter) { @($cfg.VCenter.VmName) } else { @() }
    $vcf = Get-RunningVcfOnHosts -Config $cfg -EsxCredential $esxCred -Allow $allow
    if ($vcf.Unreachable.Count -gt 0) {
        Write-Fail "Could not list VMs on: $($vcf.Unreachable -join ', ') -- cannot prove the VCF stack is down."
        exit 1
    }
    if ($vcf.Running.Count -gt 0) {
        Write-Fail "VCF components are still running; Minimal mode will not start workers beside them:"
        foreach ($r in ($vcf.Running | Sort-Object Name)) { Write-Warn2 ("  running: {0}  on {1}" -f $r.Name, $r.Host) }
        Write-Info "Run .\Stop-VCFLab.ps1 first (it stops the VCF stack and leaves hosts and workers up), then re-run."
        exit 1
    }
    Write-Ok "no VCF component running"
}

# --- F0. Full: the LLM workers must be off ------------------------------------
# Checked before anything changes on the hosts. A worker pins 24 GB of DRAM per
# host; with the management stack up that squeezes vcsa/nsxa/sddc-manager onto
# the consumer tier drives (ops review C2). Warning after the stack was up was
# too late, so Full refuses -- or, with -StopLlmWorkers, stops them first.
if (-not $minimal) {
    Write-Step "F0. LLM workers must be off for Full mode"
    $lw = Get-RunningLlmWorkers -Config $cfg -EsxCredential $esxCred
    if ($lw.Unreachable.Count -gt 0) {
        Write-Fail "Could not check LLM workers on: $($lw.Unreachable -join ', ') -- cannot prove they are off."
        exit 1
    }
    if ($lw.Running.Count -eq 0) {
        Write-Ok "no LLM worker running"
    } elseif (-not $StopLlmWorkers) {
        Write-Fail ("LLM workers are running: " + (($lw.Running | ForEach-Object { "$($_.VmName) on $($_.HostShort)" }) -join ', '))
        Write-Fail "Full mode will not start the management stack beside them (each pins 24 GB of DRAM)."
        Write-Info "Re-run with -StopLlmWorkers to shut them down first, or stop them yourself."
        exit 1
    } else {
        $grace = if ($cfg.StopGraceSeconds.ContainsKey('LlmWorker')) { $cfg.StopGraceSeconds.LlmWorker } else { 120 }
        foreach ($w in $lw.Running) {
            if (-not (Stop-HostVM -HostIp $w.HostIp -Name $w.VmName -EsxCredential $esxCred -GraceSeconds $grace -WhatIfMode:$WhatIf)) {
                Write-Fail "$($w.VmName) did not stop -- not starting the stack beside it."
                exit 1
            }
        }
        if (-not $WhatIf) {
            $still = Get-RunningLlmWorkers -Config $cfg -EsxCredential $esxCred
            if ($still.Running.Count -gt 0 -or $still.Unreachable.Count -gt 0) {
                Write-Fail "LLM workers still running or unverifiable after the stop -- holding."
                exit 1
            }
            Write-Ok "LLM workers stopped"
        }
    }
}

# --- 1a. exit maintenance mode -------------------------------------------------
# Stop-VCFLab.ps1 puts every host into maintenance mode before powering it off,
# and ESXi PERSISTS maintenance mode across a reboot. Nothing used to clear it
# on the way back up, so a cold start met hosts that were powered on, healthy,
# vSAN-complete -- and refusing every single power-on with
#   "The operation is not allowed in the current state."
# Observed 2026-10-02: all three hosts in Maintenance, zero VMs running, every
# VCF appliance unreachable. The old failure signature was a full-length
# vCenter gate timeout, because step 2 discards Start-LabVM's result, so the
# refused power-on was never reported.
#
# Deliberately AFTER the vSAN gate: taking hosts out of maintenance while vSAN
# is still partitioned would let VMs power on against a datastore that is not
# actually healthy. Maintenance mode does not stop a host participating in the
# vSAN cluster, so the gate above passes either way.
Write-Step "1a. Exit maintenance mode"
$stuck = @()
foreach ($h in $cfg.Hosts) {
    $c = $null
    try {
        $c = Connect-VIServer -Server $h.Ip -Credential $esxCred -Force -ErrorAction Stop
        $vmh = Get-VMHost -Server $c
        if ($vmh.ConnectionState -ne 'Maintenance') {
            Write-Info "$($h.Short) : not in maintenance mode"
        } elseif ($WhatIf) {
            Write-Info "$($h.Short) : WHATIF would exit maintenance mode"
        } else {
            Set-VMHost -VMHost $vmh -State Connected -Confirm:$false -ErrorAction Stop | Out-Null

            # Read it back. "The call returned" is not "the host left
            # maintenance mode", and every later tier depends on this having
            # actually happened -- a silent no-op here reappears as an
            # inexplicable power-on failure several steps downstream.
            $left = $false
            for ($i = 0; $i -lt 6; $i++) {
                if ((Get-VMHost -Server $c).ConnectionState -ne 'Maintenance') { $left = $true; break }
                Start-Sleep -Seconds 5
            }
            if ($left) { Write-Ok "$($h.Short) : exited maintenance mode" }
            else {
                Write-Fail "$($h.Short) : still in maintenance mode after the exit was accepted"
                $stuck += $h.Short
            }
        }
    } catch {
        Write-Fail "$($h.Short) : could not exit maintenance mode -- $(($_.Exception.Message -replace '\s+', ' '))"
        $stuck += $h.Short
    } finally {
        if ($c) { Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue }
    }
}
if ($stuck.Count -gt 0) {
    Write-Fail "Still in maintenance mode: $($stuck -join ', ')"
    Write-Fail "No VM can power on while its host is in maintenance mode, so stopping here"
    Write-Fail "rather than timing out on every gate below."
    exit 1
}

# --- M. Minimal: LLM workers, host-direct -------------------------------------
# Everything here talks to the hosts; vCenter is not required and usually not
# running. The workers' disks are on local datastores and their port group uses
# ephemeral binding, so the host can attach their NICs without vCenter.
if ($minimal) {
    Write-Step "M2. LLM workers"
    if ($workers.Count -eq 0) { Write-Warn2 "No LlmWorkers configured in VCFLab.Config.psd1 -- nothing to start." }
    $failedWorkers = @()
    foreach ($w in $workers) {
        if (-not (Start-HostVM -HostIp $w.HostIp -Name $w.VmName -EsxCredential $esxCred -WhatIfMode:$WhatIf)) {
            $failedWorkers += $w.VmName
        }
    }
    if (-not $WhatIf) {
        $gateTimeout = if ($cfg.GateTimeoutSeconds.ContainsKey('LlmWorker')) { $cfg.GateTimeoutSeconds.LlmWorker } else { 600 }
        foreach ($w in ($workers | Where-Object { $failedWorkers -notcontains $_.VmName })) {
            $ok = Wait-Gate -Name "$($w.VmName) guest up (Tools + IPv4, read from $($w.HostShort))" -TimeoutSeconds $gateTimeout -Test {
                Test-HostVmGuestUp -HostIp $w.HostIp -Name $w.VmName -EsxCredential $esxCred
            }
            if (-not $ok) { $failedWorkers += $w.VmName }
        }
    }

    if (-not $WithVCenter) {
        Write-Step "Summary -- Minimal"
        foreach ($w in $workers) {
            $state = if ($failedWorkers -contains $w.VmName) { 'NOT UP' } else { 'up' }
            Write-Host ('  {0,-16} on {1,-6} {2}' -f $w.VmName, $w.HostShort, $state)
        }
        Write-Info "vCenter, NSX, SDDC Manager, Operations and VSP are intentionally OFF."
        Write-Host "`nMinimal power-on finished in $([int]((Get-Date) - $started).TotalMinutes) min" -ForegroundColor White
        if ($failedWorkers.Count -gt 0) { Write-Fail "Workers not up: $($failedWorkers -join ', ')"; exit 1 }
        exit 0
    }
}

# --- 2. vCenter ---------------------------------------------------------------
Write-Step "2. vCenter"
# This loop used to end in `catch { }`, which turned every possible cause --
# a refused connection, a dead session, a permissions error -- into the single
# unhelpful line "Could not find vcsa on any host." It also never reset $c
# between iterations, so when Connect-VIServer threw, the Get-VM below ran
# against the PREVIOUS host's already-disconnected session and quietly found
# nothing. Report what actually happened, per host.
$vcHost  = $null
$probe   = @()
foreach ($h in $cfg.Hosts) {
    $c = $null                   # never inherit the previous host's session
    try {
        $c = Connect-VIServer -Server $h.Ip -Credential $esxCred -Force -ErrorAction Stop
        $found = @(Get-VM -Server $c -Name $cfg.VCenter.VmName -ErrorAction SilentlyContinue)
        if ($found.Count -gt 0) {
            $vcHost = $h
            Write-Info "$($cfg.VCenter.VmName) is registered on $($h.Short)"
            # -EsxCredential and -Config so a wedged vCenter power-on can fall
            # back to the host, exactly as every tier below this one does, and
            # the RESULT is checked rather than piped to Out-Null: a refused
            # power-on here used to be invisible and resurfaced as a
            # full-length vCenter gate timeout with no stated cause.
            if (-not (Start-LabVM -Name $cfg.VCenter.VmName -EsxCredential $esxCred -Config $cfg -WhatIfMode:$WhatIf)) {
                Write-Fail "$($cfg.VCenter.VmName) could not be powered on; not waiting on the gate below."
                exit 1
            }
            break
        }
        # Record what this host DOES hold, so a miss is self-diagnosing.
        $probe += [pscustomobject]@{
            Host  = $h.Short
            State = 'connected'
            Names = (@(Get-VM -Server $c -ErrorAction SilentlyContinue | ForEach-Object Name) -join ', ')
        }
    } catch {
        $probe += [pscustomobject]@{
            Host  = $h.Short
            State = "ERROR: $(($_.Exception.Message -replace '\s+', ' '))"
            Names = ''
        }
    } finally {
        if ($c) { Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue }
    }
}
if (-not $vcHost) {
    Write-Fail "Could not find $($cfg.VCenter.VmName) on any host."
    foreach ($p in $probe) {
        Write-Warn2 "  $($p.Host): $($p.State)"
        if ($p.Names) { Write-Info  "    registered there: $($p.Names)" }
    }
    Write-Info "If the VM is listed above under a different name, VCenter.VmName in"
    Write-Info "VCFLab.Config.psd1 does not match the inventory. If a host errored,"
    Write-Info "that error is the real cause -- it used to be discarded silently."
    exit 1
}

if ($WhatIf) { Write-Info 'WHATIF: stopping before the vCenter gate'; exit 0 }

$ok = Wait-Gate -Name "vCenter API authenticating" -TimeoutSeconds $cfg.GateTimeoutSeconds.VCenterApi -Test {
    Test-VCenterApi -Server $cfg.VCenter.Ip -Credential $ssoCred
}
if (-not $ok) { Write-Fail "vCenter never came up."; exit 1 }
$vc = Connect-VIServer -Server $cfg.VCenter.Ip -Credential $ssoCred -Force
Write-Ok "connected to vCenter $($cfg.VCenter.Ip)"

# --- 2a. inventory sync --------------------------------------------------------
# Everything past this point reads VM state FROM vCenter -- the managed-set
# resolution below, and every guest-IP gate after it. vpxa can wedge while the
# host still looks Connected and esxcli still answers, leaving vCenter planning
# against stale objects: power-on then fails with "vpx.vmprov.PowerOnVm ... has
# not been completely created", and guest-IP gates wait out their full timeout
# on nodes that are actually up. Catch it here, before it costs half an hour.
#
# This cannot see a wedge on a truly cold cluster, where every VM is off on both
# sides and there is nothing to disagree about. Start-LabVM repairs on the first
# power-on failure for that case.
Write-Step "2a. Host inventory sync"
if (-not (Confirm-HostInventorySync -Config $cfg -EsxCredential $esxCred -ReportOnly:$SkipInventoryRepair)) {
    if ($SkipInventoryRepair) {
        Write-Warn2 "Continuing with a stale inventory because -SkipInventoryRepair was given."
        Write-Warn2 "Expect power-on failures and gate timeouts on the affected hosts."
    } else {
        Write-Fail "Could not bring vCenter's inventory back in step with the hosts."
        Write-Info "Restart vpxa by hand on the hosts named above, then re-run."
        exit 1
    }
}

if ($minimal) {
    # -WithVCenter: vCenter is up for the UI; everything else stays down.
    Write-Step "Summary -- Minimal with vCenter"
    Write-Ok "vCenter and the LLM workers are up; NSX, SDDC Manager, Operations and VSP are intentionally OFF."
    if ($failedWorkers.Count -gt 0) { Write-Fail "Workers not up: $($failedWorkers -join ', ')" }
    Write-Host "`nMinimal power-on finished in $([int]((Get-Date) - $started).TotalMinutes) min" -ForegroundColor White
    Disconnect-VIServer -Server $vc -Confirm:$false -ErrorAction SilentlyContinue
    if ($failedWorkers.Count -gt 0) { exit 1 }
    exit 0
}

# --- scope ---------------------------------------------------------------------
$managed = Get-VcfManagedNames -Config $cfg
Write-Step "Scope"
Write-Info ("VCF components this script will manage: " + ($managed -join ', '))
$scope = Get-VmScope -Config $cfg -ManagedNames $managed
if ($scope.Foreign.Count -gt 0) {
    Write-Info ("left alone (not VCF): " + (($scope.Foreign | ForEach-Object Name) -join ', '))
}

# --- 3. SDDC Manager ----------------------------------------------------------
# SDDC Manager comes up BEFORE NSX. Getting this backwards is easy and wrong.
Write-Step "3. SDDC Manager"
Start-LabVM -Name $cfg.Appliances.SddcManager.VmName -EsxCredential $esxCred -Config $cfg -RepairInventory:(-not $SkipInventoryRepair) | Out-Null
$ok = Wait-Gate -Name "SDDC Manager API issuing tokens" -TimeoutSeconds $cfg.GateTimeoutSeconds.SddcManager -Test {
    Test-SddcManagerApi -Ip $cfg.Appliances.SddcManager.Ip -Credential $ssoCred
}
if (-not $ok) { Write-Fail "SDDC Manager did not come up; holding."; exit 1 }

# --- 4. NSX -------------------------------------------------------------------
Write-Step "4. NSX Manager"
Start-LabVM -Name $cfg.Appliances.Nsx.VmName -EsxCredential $esxCred -Config $cfg -RepairInventory:(-not $SkipInventoryRepair) | Out-Null
$ok = Wait-Gate -Name "NSX cluster STABLE, all services running, transport nodes success" `
                -TimeoutSeconds $cfg.GateTimeoutSeconds.Nsx -Test {
    Test-NsxCluster -Ip $cfg.Appliances.Nsx.Ip -Credential $nsxCred -ExpectedTransportNodes $cfg.Hosts.Count
}
if (-not $ok) { Write-Fail "NSX did not fully converge; holding."; exit 1 }

# --- 5. VCF Operations --------------------------------------------------------
Write-Step "5. VCF Operations"
Start-LabVM -Name $cfg.Appliances.Operations.VmName -EsxCredential $esxCred -Config $cfg -RepairInventory:(-not $SkipInventoryRepair) | Out-Null
$ok = Wait-Gate -Name "VCF Operations authenticating" -TimeoutSeconds $cfg.GateTimeoutSeconds.Operations -Test {
    Test-OperationsApi -Ip $cfg.Appliances.Operations.Ip -Credential $opsCred
}
if (-not $ok) { Write-Fail "VCF Operations did not come up; holding."; exit 1 }

# --- 6. VSP control plane -----------------------------------------------------
Write-Step "6. VSP control plane"
$vsp = Get-VspNodes -Config $cfg
if ($vsp.ControlPlane.Count -eq 0) { Write-Warn2 "No control-plane nodes found by vCPU; falling back to configured names"
    $vsp = [pscustomobject]@{
        ControlPlane = @($cfg.Vsp.KnownControlPlane | ForEach-Object { Get-VM -Name $_ -ErrorAction SilentlyContinue })
        Workers      = @($cfg.Vsp.KnownWorkers      | ForEach-Object { Get-VM -Name $_ -ErrorAction SilentlyContinue })
    }
}
Write-Info ("control plane: " + (($vsp.ControlPlane | ForEach-Object Name) -join ', '))
Write-Info ("workers:       " + (($vsp.Workers      | ForEach-Object Name) -join ', '))
foreach ($n in $vsp.ControlPlane) { Start-LabVM -Name $n.Name -EsxCredential $esxCred -Config $cfg -RepairInventory:(-not $SkipInventoryRepair) | Out-Null }

$ok = Wait-Gate -Name "control-plane guests reporting IPs" -TimeoutSeconds $cfg.GateTimeoutSeconds.VspControl -Test {
    -not ($vsp.ControlPlane | Where-Object { -not (Test-VmGuestUp -Name $_.Name) })
}
if (-not $ok) { Write-Fail "VSP control plane never reported IPs."; exit 1 }

# Second gate: an IP is NOT readiness. 6443 has been observed to open ~60s later.
$ok = Wait-Gate -Name "control plane serving 6443" -TimeoutSeconds $cfg.GateTimeoutSeconds.VspControl -Test {
    $ips = $vsp.ControlPlane | ForEach-Object { (Get-VM -Name $_.Name).Guest.IPAddress | Where-Object { $_ -notmatch ':' } | Select-Object -First 1 }
    @($ips | Where-Object { $_ -and (Test-TcpPort -ComputerName $_ -Port 6443) }).Count -ge 1
}
if (-not $ok) { Write-Fail "VSP control plane never served 6443; not starting workers."; exit 1 }

# --- 7. VSP workers -----------------------------------------------------------
Write-Step "7. VSP workers"
foreach ($n in $vsp.Workers) { Start-LabVM -Name $n.Name -EsxCredential $esxCred -Config $cfg -RepairInventory:(-not $SkipInventoryRepair) | Out-Null }
Wait-Gate -Name "worker guests reporting IPs" -TimeoutSeconds $cfg.GateTimeoutSeconds.VspWorker -Test {
    -not ($vsp.Workers | Where-Object { -not (Test-VmGuestUp -Name $_.Name) })
} | Out-Null

# --- 8/9. tail appliances -----------------------------------------------------
Write-Step "8. License Server"
Start-LabVM -Name $cfg.Appliances.License.VmName -EsxCredential $esxCred -Config $cfg -RepairInventory:(-not $SkipInventoryRepair) | Out-Null
Wait-Gate -Name "licsrv guest up" -TimeoutSeconds $cfg.GateTimeoutSeconds.Appliance -Test {
    Test-VmGuestUp -Name $cfg.Appliances.License.VmName } | Out-Null

Write-Step "9. Operations Collector (last)"
Start-LabVM -Name $cfg.Appliances.OpsCollector.VmName -EsxCredential $esxCred -Config $cfg -RepairInventory:(-not $SkipInventoryRepair) | Out-Null
Wait-Gate -Name "opscollector guest up" -TimeoutSeconds $cfg.GateTimeoutSeconds.Appliance -Test {
    Test-VmGuestUp -Name $cfg.Appliances.OpsCollector.VmName } | Out-Null

# --- summary ------------------------------------------------------------------
Write-Step "Summary -- VCF components"
Get-VM | Where-Object { $managed -contains $_.Name } | Sort-Object Name | ForEach-Object {
    $ip = $_.Guest.IPAddress | Where-Object { $_ -and $_ -notmatch ':' } | Select-Object -First 1
    '  {0,-42} {1,-12} {2}' -f $_.Name, $_.PowerState, $ip
} | Write-Host

$after = Get-VmScope -Config $cfg -ManagedNames $managed
if ($after.Foreign.Count -gt 0) {
    Write-Step "Left alone -- not VCF components, untouched by this script"
    $after.Foreign | Sort-Object Name | ForEach-Object {
        '  {0,-42} {1}' -f $_.Name, $_.PowerState
    } | Write-Host
}

$workersOn = @($workers | Where-Object { $wk = $_; @(Get-VM -Name $wk.VmName -ErrorAction SilentlyContinue | Where-Object { $_.PowerState -eq 'PoweredOn' }).Count -gt 0 })
if ($workersOn.Count -gt 0) {
    Write-Warn2 ("LLM workers are running beside the full stack: " + (($workersOn | ForEach-Object VmName) -join ', '))
    Write-Warn2 "Their reserved RAM pushes management VMs onto the memory tier. Stop them, or use -Mode Minimal."
}

# No license stamp here: a Full start proves nothing about the license until
# VCF Operations has been up long enough to report usage. Stop-VCFLab records
# the sync when it finds Operations has been up for 24 h.
Write-Info "To refresh the VCF license window for Minimal mode, leave VCF Operations up for 24 h before Stop-VCFLab."

Write-Host "`nPower-on finished in $([int]((Get-Date) - $started).TotalMinutes) min" -ForegroundColor White
Write-Warn2 "If DRS is set to Manual, nothing will place or balance VMs. Check before relying on automation."
Disconnect-VIServer -Server $vc -Confirm:$false -ErrorAction SilentlyContinue
