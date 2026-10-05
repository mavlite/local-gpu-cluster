<#
.SYNOPSIS
    Powers down the VCF lab gracefully, then optionally powers off the ESX
    hosts.

.DESCRIPTION
    Order:  developer VMs -> Operations Collector -> License Server ->
            VSP CONTROL PLANE -> VSP workers -> VCF Operations -> NSX ->
            SDDC Manager -> vCenter (last) -> ESX hosts.

    Two details in that order are deliberate and were learned the hard way:

    1. The VSP control plane is stopped BEFORE its workers. This is the reverse
       of the bring-up order and of the obvious reading. The supervisor is
       self-healing: with the control plane still alive it restarts workers as
       fast as you stop them. Observed 2026-09-27, where three workers had to
       be force-stopped and came back anyway; the second pass, with the control
       plane already down, stopped them gracefully first time.

    2. vCenter is never hard-killed by default. Its graceful shutdown can
       exceed any timeout you feel like guessing at, and a vCSA hard stop risks
       the embedded database. -Force permits a hard stop after a long ceiling.

    vSphere HA will restart management VMs when a host event occurs. Use
    -DisableHaFirst to turn host monitoring off for the duration; the script
    restores it only if it changed it.

    SCOPE: VCF components only -- vCenter, the VCF appliances, and VSP nodes.
    Developer VMs, containers and any other workload are never stopped, and are
    listed at the end as "left alone".

.PARAMETER IncludeHosts
    Also power off the ESX hosts after the VCF components are down.

    This is opt-in, and it REFUSES if any non-VCF VM is still running: a host
    cannot enter maintenance mode with a VM on it, and this script will not
    stop something it does not own in order to get there. Stop those VMs
    yourself, then re-run.

.PARAMETER DisableHaFirst
    Turn off vSphere HA host monitoring before shutting down, to stop HA
    resurrecting management VMs mid-sequence.

.PARAMETER Force
    Permit a hard power-off of vCenter if graceful shutdown exceeds the
    configured ceiling. Off by default, on purpose.

.EXAMPLE
    .\Stop-VCFLab.ps1
.EXAMPLE
    .\Stop-VCFLab.ps1 -IncludeHosts -DisableHaFirst
#>
[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPlainTextForPassword','CredentialPath',
    Justification='This is a filesystem path to the credential file, not a secret. Secrets are read from that file into PSCredential objects and never printed.')]
param(
    [switch]$IncludeHosts,
    [switch]$DisableHaFirst,
    [switch]$Force,
    [switch]$WhatIf,
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

$esxCred = Get-VCFLabCredentialObject -Map $cred -UserKey 'LAB_ESX_USER' -PassKey 'LAB_ESX_PASS'       -DefaultUser 'root'
$ssoCred = Get-VCFLabCredentialObject -Map $cred -UserKey $null          -PassKey 'VCF_SSO_ADMIN_PASS' -DefaultUser 'administrator@vsphere.local'

$started = Get-Date
Write-Host "VCF lab power-off  ::  $started" -ForegroundColor White
if ($WhatIf) { Write-Warn2 "WHATIF mode: no power state will change" }

$workers   = @(Get-LlmWorkers -Config $cfg)
$workerSet = @($workers | ForEach-Object VmName)
$workerGrace = if ($cfg.StopGraceSeconds.ContainsKey('LlmWorker')) { $cfg.StopGraceSeconds.LlmWorker } else { 120 }

$vc = $null
$vcDown = $false
try { $vc = Connect-VIServer -Server $cfg.VCenter.Ip -Credential $ssoCred -Force -ErrorAction Stop }
catch {
    # vCenter being down is NORMAL in Minimal mode. With -IncludeHosts the hosts
    # can still be shut down directly -- but only once they prove that no VCF
    # component is running, because those cannot be stopped gracefully without
    # vCenter's ordering and this script will not hard-stop them behind its back.
    $vcDown = $true
    Write-Warn2 "vCenter is not reachable -- treating the lab as Minimal and working host-direct."
    $vcf = Get-RunningVcfOnHosts -Config $cfg -EsxCredential $esxCred
    if (-not $IncludeHosts -and $vcf.Unreachable.Count -eq 0 -and $vcf.Running.Count -eq 0) {
        # Minimal mode: no VCF stack is running, so there is genuinely nothing
        # for a default Stop to do. That is success, not an error.
        Write-Ok "No VCF component is running (Minimal mode) -- nothing to stop. Hosts and LLM workers left as they are."
        Write-Info "To stop the LLM workers and power off the hosts, re-run with -IncludeHosts."
        exit 0
    }
    if ($vcf.Unreachable.Count -gt 0) {
        Write-Fail "Could not list VMs on: $($vcf.Unreachable -join ', '). Refusing to power off hosts blind."
        exit 1
    }
    if ($vcf.Running.Count -gt 0) {
        Write-Fail "VCF components are running but vCenter is not reachable to stop them in order:"
        foreach ($r in ($vcf.Running | Sort-Object Name)) { Write-Warn2 ("  running: {0}  on {1}" -f $r.Name, $r.Host) }
        Write-Info "Bring vCenter back (or stop these by hand), then re-run. Nothing has been changed."
        exit 1
    }
}
if (-not $vcDown) { Write-Ok "connected to vCenter" }

$haWasEnabled = $false
$failed       = @()
$managed      = @()
$foreignOn    = @()

# Everything from here to the hosts section needs vCenter. In Minimal mode it
# is down and there is no VCF stack running (proven above), so skip straight on.
if (-not $vcDown) {

# --- HA ------------------------------------------------------------------------
if ($DisableHaFirst -and -not $WhatIf) {
    Write-Step "vSphere HA"
    try {
        $cl = Get-Cluster -Name $cfg.Cluster -ErrorAction Stop
        $haWasEnabled = [bool]$cl.HAEnabled
        if ($haWasEnabled) {
            Set-Cluster -Cluster $cl -HAEnabled:$false -Confirm:$false | Out-Null
            Write-Ok "HA host monitoring disabled for the duration"
        } else { Write-Info "HA already disabled" }
    } catch { Write-Warn2 "Could not change HA: $($_.Exception.Message)" }
}

function Stop-Tier {
    param([string]$Label, [string[]]$Names, [int]$Grace)
    Write-Step $Label
    foreach ($n in $Names) {
        if (-not $n) { continue }
        $ok = Stop-LabVM -Name $n -GraceSeconds $Grace -AllowHardStop -WhatIfMode:$WhatIf
        if (-not $ok) { $script:failed += $n }
    }
}

# --- scope ----------------------------------------------------------------------
$managed = Get-VcfManagedNames -Config $cfg
Write-Step "Scope"
Write-Info ("managing: " + ($managed -join ', '))
$scope0 = Get-VmScope -Config $cfg -ManagedNames $managed
$foreignOn = @($scope0.Foreign | Where-Object { $_.PowerState -eq 'PoweredOn' -and $workerSet -notcontains $_.Name })
if ($foreignOn.Count -gt 0) {
    Write-Info ("leaving alone (running, not VCF): " + (($foreignOn | ForEach-Object Name) -join ', '))
}
$workersOn = @($scope0.Foreign | Where-Object { $_.PowerState -eq 'PoweredOn' -and $workerSet -contains $_.Name })
if ($workersOn.Count -gt 0) {
    $fate = if ($IncludeHosts) { 'stopped before the hosts go down' } else { 'left running (Minimal mode)' }
    Write-Info ("LLM workers, " + $fate + ": " + (($workersOn | ForEach-Object Name) -join ', '))
}

# --- tail appliances ------------------------------------------------------------
Stop-Tier -Label "Operations Collector" -Names @($cfg.Appliances.OpsCollector.VmName) -Grace $cfg.StopGraceSeconds.OpsCollector
Stop-Tier -Label "License Server"       -Names @($cfg.Appliances.License.VmName)      -Grace $cfg.StopGraceSeconds.License

# --- VSP: CONTROL PLANE FIRST, then workers -------------------------------------
$vsp = Get-VspNodes -Config $cfg
if ($vsp.All.Count -eq 0) { Write-Info "No VSP nodes present" }
else {
    Write-Info ("control plane: " + (($vsp.ControlPlane | ForEach-Object Name) -join ', '))
    Write-Info ("workers:       " + (($vsp.Workers      | ForEach-Object Name) -join ', '))
    Stop-Tier -Label "VSP control plane (BEFORE workers -- supervisor self-heals)" `
              -Names @($vsp.ControlPlane | ForEach-Object Name) -Grace $cfg.StopGraceSeconds.VspControl
    Stop-Tier -Label "VSP workers" `
              -Names @($vsp.Workers | ForEach-Object Name) -Grace $cfg.StopGraceSeconds.VspWorker

    # The supervisor has been seen restarting workers. Verify, then sweep once.
    Start-Sleep -Seconds 20
    $back = @(Get-VM -Name "$($cfg.Vsp.NamePrefix)*" -ErrorAction SilentlyContinue | Where-Object { $_.PowerState -eq 'PoweredOn' })
    if ($back.Count -gt 0 -and -not $WhatIf) {
        Write-Warn2 ("VSP nodes restarted themselves: " + (($back | ForEach-Object Name) -join ', ') + " -- stopping again")
        foreach ($b in $back) { Stop-LabVM -Name $b.Name -GraceSeconds 180 -AllowHardStop | Out-Null }
    }
}

# --- management appliances ------------------------------------------------------
# License window for Minimal mode: VCF 9 connected mode reports usage from
# Operations every 24 h, so only an Operations run of at least that long counts
# as a license refresh. Read its uptime BEFORE stopping it.
$opsUp = Get-VmUptimeHours -Name $cfg.Appliances.Operations.VmName
if ($null -ne $opsUp -and $opsUp -ge $script:LicenseSyncHours) {
    if ($WhatIf) { Write-Info ("WHATIF: would record a license sync (Operations up {0:N0} h)" -f $opsUp) }
    else {
        Write-LicenseStamp -Config $cfg
        Write-Ok ("Operations was up {0:N0} h -- license sync recorded for Minimal mode's window" -f $opsUp)
    }
} elseif ($null -ne $opsUp) {
    Write-Warn2 ("Operations was up only {0:N1} h (< {1} h): NOT recorded as a license sync." -f $opsUp, $script:LicenseSyncHours)
}
Stop-Tier -Label "VCF Operations" -Names @($cfg.Appliances.Operations.VmName)  -Grace $cfg.StopGraceSeconds.Operations
Stop-Tier -Label "NSX Manager"    -Names @($cfg.Appliances.Nsx.VmName)         -Grace $cfg.StopGraceSeconds.Nsx
Stop-Tier -Label "SDDC Manager"   -Names @($cfg.Appliances.SddcManager.VmName) -Grace $cfg.StopGraceSeconds.SddcManager

Write-Step "State before vCenter shuts down"
Get-VM | Sort-Object Name | ForEach-Object {
    $tag = if ($managed -contains $_.Name) { '[vcf]' } else { '[other]' }
    '  {0,-42} {1,-12} {2}' -f $_.Name, $_.PowerState, $tag
} | Write-Host

# Only VCF components block vCenter's shutdown. Foreign VMs are expected to be
# running -- they are out of scope, and vCenter going down does not stop them.
$managedStillOn = @(Get-VM | Where-Object {
    $_.PowerState -eq 'PoweredOn' -and $managed -contains $_.Name -and $_.Name -ne $cfg.VCenter.VmName })
if ($managedStillOn.Count -gt 0) {
    Write-Warn2 ("VCF components still running besides vCenter: " + (($managedStillOn | ForEach-Object Name) -join ', '))
    if (-not $Force) { Write-Fail "Refusing to shut down vCenter with VCF components running. Re-run with -Force to override."; exit 1 }
}

# --- vCenter, last --------------------------------------------------------------
# Shut down via its own host: once vCenter stops answering we still need a way
# to watch it, and we need host access for the power-off step anyway.
Write-Step "vCenter (last)"
$vcHost = $null
foreach ($h in $cfg.Hosts) {
    try {
        $c = Connect-VIServer -Server $h.Ip -Credential $esxCred -Force -ErrorAction Stop
        if (Get-VM -Server $c -Name $cfg.VCenter.VmName -ErrorAction SilentlyContinue) { $vcHost = $h; break }
        Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue
    } catch { }
}
if (-not $vcHost) { Write-Warn2 "Could not locate vCenter on a host; shutting down through vCenter itself" }
else { Write-Info "$($cfg.VCenter.VmName) is on $($vcHost.Short)" }

$grace = if ($Force) { $cfg.StopGraceSeconds.VCenterForce } else { $cfg.StopGraceSeconds.VCenter }
$ok = Stop-LabVM -Name $cfg.VCenter.VmName -GraceSeconds $grace -AllowHardStop:$Force -WhatIfMode:$WhatIf
if (-not $ok) {
    Write-Warn2 "vCenter is still shutting down. It is NOT being hard-killed (a vCSA hard stop risks its database)."
    Write-Warn2 "Wait for it, or re-run with -Force to permit a hard stop after $($cfg.StopGraceSeconds.VCenterForce)s."
    if ($IncludeHosts) { Write-Fail "Not powering off hosts while vCenter is running."; exit 1 }
}

}   # end: vCenter-dependent section (skipped when vCenter is down)

# Host-level classification needs no vCenter: VCF names by config + VSP prefix.
$isVcf = { param($n) Test-IsVcfName -Config $cfg -Name $n }

# --- hosts ----------------------------------------------------------------------
if (-not $IncludeHosts) {
    Write-Info "Hosts left running (pass -IncludeHosts to power them off)"
} elseif ($WhatIf) {
    Write-Info "WHATIF: would enter maintenance mode (vSAN noAction) and power off each host"
} else {
    Write-Step "ESX hosts"

    # vCenter was just shut down, so the session this script opened to it is
    # dead. Drop it before anything else runs a cmdlet without an explicit
    # -Server: PowerCLI falls back to the default server list, which still
    # holds that dead connection, and the call fails with
    #   "There was no endpoint listening at https://<vcsa>/sdk"
    # even though every host is perfectly healthy.
    Disconnect-VIServer -Server * -Confirm:$false -ErrorAction SilentlyContinue

    # The LLM workers are owned by these scripts, so they are stopped here --
    # via their hosts, which is the only path that exists in Minimal mode --
    # rather than being reported as blockers.
    if ($workers.Count -gt 0) {
        Write-Step "LLM workers"
        foreach ($w in $workers) {
            if (-not (Stop-HostVM -HostIp $w.HostIp -Name $w.VmName -EsxCredential $esxCred -GraceSeconds $workerGrace)) {
                $failed += $w.VmName
            }
        }
    }

    # A host cannot enter maintenance mode with any VM running on it. This
    # script will not stop a VM it does not own just to clear the way, so if
    # anything foreign is still up it refuses and names it. Ask each host
    # directly -- they are the only thing still answering, and they are the
    # authority on what is actually running on them.
    $blockers    = @()
    $unreachable = @()
    foreach ($h in $cfg.Hosts) {
        $c = $null
        try {
            $c = Connect-VIServer -Server $h.Ip -Credential $esxCred -Force -ErrorAction Stop
            foreach ($v in @(Get-VM -Server $c)) {
                if ($v.PowerState -eq 'PoweredOn' -and $managed -notcontains $v.Name -and -not (& $isVcf $v.Name)) {
                    $blockers += [pscustomobject]@{ Name = $v.Name; Host = $h.Short }
                }
            }
        } catch {
            $unreachable += $h.Short
        } finally {
            if ($c) { Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue }
        }
    }

    # A host that could not be queried is NOT a host known to be clear. Treating
    # "I could not look" as "nothing is running" would power off a host with
    # live VMs on it.
    if ($unreachable.Count -gt 0) {
        Write-Fail "Could not enumerate VMs on: $($unreachable -join ', ')"
        Write-Fail "Refusing to power off any host while another host's state is unknown."
        Write-Info "VCF components are already down. Check those hosts, then re-run with -IncludeHosts."
        exit 1
    }

    if ($blockers.Count -gt 0) {
        Write-Fail "Cannot power off hosts: non-VCF VMs are still running and this script will not stop them."
        foreach ($b in ($blockers | Sort-Object Name)) {
            Write-Warn2 ("  running: {0}  on {1}" -f $b.Name, $b.Host)
        }
        Write-Info "Stop those VMs yourself, then re-run with -IncludeHosts. VCF components are already down."
        exit 1
    }
    Write-Info "Whole-cluster shutdown uses vSAN mode noAction: there is nowhere to evacuate to."
    foreach ($h in $cfg.Hosts) {
        Write-Host "  --- $($h.Short)"
        try {
            $c = Connect-VIServer -Server $h.Ip -Credential $esxCred -Force -ErrorAction Stop
            $vmh = Get-VMHost -Server $c
            $left = @(Get-VM -Server $c | Where-Object { $_.PowerState -eq 'PoweredOn' })
            if ($left.Count -gt 0) {
                Write-Warn2 ("    still running: " + (($left | ForEach-Object Name) -join ', ') + " -- skipping this host")
                Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue
                $failed += $h.Short
                continue
            }
            if ($vmh.ConnectionState -ne 'Maintenance') {
                Set-VMHost -VMHost $vmh -State Maintenance -VsanDataMigrationMode NoDataMigration -Confirm:$false -ErrorAction Stop | Out-Null
                Write-Ok "    maintenance mode (NoDataMigration)"
            } else { Write-Info "    already in maintenance mode" }
            Stop-VMHost -VMHost $vmh -Confirm:$false -Force -ErrorAction Stop | Out-Null
            Write-Ok "    shutdown issued"
        } catch {
            Write-Fail "    $($h.Short): $($_.Exception.Message)"
            $failed += $h.Short
        }
        Start-Sleep -Seconds 10
    }

    Write-Step "Verifying hosts are down"
    Start-Sleep -Seconds 60
    foreach ($h in $cfg.Hosts) {
        $up = Test-TcpPort -ComputerName $h.Ip -Port 443 -TimeoutMs 4000
        if ($up) { Write-Warn2 "$($h.Short) still answering 443" } else { Write-Ok "$($h.Short) down" }
    }
}

if ($haWasEnabled) {
    Write-Info "HA was disabled by this run. It will need re-enabling after the next power-on."
}

# --- what we deliberately did not touch -----------------------------------------
# Reported from the pre-run snapshot: vCenter may be down by now, so this cannot
# be re-queried. These were running when the script started and were out of scope.
if ($foreignOn.Count -gt 0) {
    Write-Step "Left alone -- not VCF components"
    foreach ($f in ($foreignOn | Sort-Object Name)) { Write-Host ("  {0}" -f $f.Name) }
    Write-Info "These were running at the start of this run and were not touched."
}

Write-Step "Result"
if ($failed.Count -gt 0) { Write-Warn2 ("Items needing attention: " + ($failed -join ', ')) }
elseif ($vcDown) { Write-Ok "Minimal-mode shutdown complete (no VCF stack was running)" }
else { Write-Ok "VCF components shut down cleanly" }
Write-Host "Elapsed: $([int]((Get-Date) - $started).TotalMinutes) min" -ForegroundColor White

# $vc may already be gone if vCenter was shut down; this is best-effort.
Disconnect-AllViServers
