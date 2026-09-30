<#
.SYNOPSIS
    Takes a cold clone of DNS01 as the rollback point before the CA install.

.DESCRIPTION
    Installing an Enterprise Root CA on dns01 is the one irreversible action in
    the certificate-authority plan: it writes CA, enrolment, AIA and NTAuth
    objects into the forest Configuration NC and pushes the root to every domain
    member's trust store. Uninstalling the role afterwards does not retract
    cached trust or clean NTAuth. This clone is the point you can return to.

    A VM snapshot was the original plan and is NOT possible here: DNS01 lives on
    the standalone management host, which runs with Software Memory Tiering
    enabled, and ESXi refuses snapshots on a tiered system --

        "Snapshots are not yet supported on a system with Software Memory
         Tiering enabled."

    Worth knowing regardless: snapshot-reverting a domain controller risks USN
    rollback. A cold copy taken with the guest shut down has no such problem,
    which is why this script insists on a clean shutdown rather than copying a
    running VM.

    THIS TAKES LAB DNS AND ACTIVE DIRECTORY DOWN for the duration of the copy.
    Nothing that resolves *.lab.knowledgeondemand.net will resolve while it
    runs. Run it when that is acceptable.

.PARAMETER Apply
    Actually shut down, copy and restart. Without it, this reports what it would
    do and changes nothing.

.PARAMETER UseDatastoreCopy
    Copy over the network with PowerCLI instead of running vmkfstools on the
    host. No SSH needed, but it pulls ~60 GB across the wire rather than copying
    locally on the datastore. Slower by a wide margin; use it if you would
    rather not enable SSH.

.EXAMPLE
    .\New-Dns01RollbackClone.ps1
.EXAMPLE
    .\New-Dns01RollbackClone.ps1 -Apply
#>
[CmdletBinding()]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPlainTextForPassword','CredentialPath',
    Justification='Filesystem path to the credential file, not a secret.')]
param(
    [string]$MgmtHost   = '192.168.6.167',
    [string]$VmName     = 'DNS01.knowledgeondemand.net',
    [string]$Datastore  = 'mgmt-datastore1',
    [string]$SourceDir  = 'DNS.knowledgeondemand.net',
    [string]$DcAddress    = '172.16.10.150',
    [string]$DnsProbeName = 'DNS01.knowledgeondemand.net',
    [int]$ShutdownTimeoutSeconds = 300,
    [string]$CredentialPath,
    [switch]$UseDatastoreCopy,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $(if ($CredentialPath) { $CredentialPath } else { $cfg.CredentialFile })
Disable-CertificateValidation
Initialize-PowerCLI | Out-Null

$esx = Get-VCFLabCredentialObject -Map $cred -UserKey 'MGMT_ESX_USER' -PassKey 'MGMT_ESX_PASS' -DefaultUser 'root'
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$target = "$SourceDir-rollback-$stamp"

Write-Step "Connecting to the management host"
$h = Connect-VIServer -Server $MgmtHost -Credential $esx -Force
Write-Ok "connected to $MgmtHost"

try {
    # ---------------------------------------------------------------- pre-flight
    Write-Step "Pre-flight"
    $vm = Get-VM -Server $h -Name $VmName -ErrorAction SilentlyContinue
    if (-not $vm) { Write-Fail "$VmName not found on $MgmtHost"; exit 1 }
    Write-Info "$VmName is $($vm.PowerState), tools=$($vm.ExtensionData.Guest.ToolsRunningStatus)"

    $ds = Get-Datastore -Server $h -Name $Datastore
    $needGb = [math]::Ceiling($vm.ExtensionData.Summary.Storage.Committed / 1GB)
    Write-Info ("copy needs about {0} GB; {1} GB free on {2}" -f $needGb, [int]$ds.FreeSpaceGB, $Datastore)
    if ($ds.FreeSpaceGB -lt ($needGb * 1.1)) {
        Write-Fail "Not enough free space for the clone with headroom."
        exit 1
    }

    if ($vm.PowerState -eq 'PoweredOn' -and
        $vm.ExtensionData.Guest.ToolsRunningStatus -ne 'guestToolsRunning') {
        Write-Fail "VMware Tools is not running, so a graceful shutdown cannot be requested."
        Write-Info  "Shut the guest down from its console, then re-run."
        exit 1
    }

    Write-Warn2 "This powers off your lab DNS and Active Directory for the duration."
    Write-Info  "source : [$Datastore] $SourceDir"
    Write-Info  "target : [$Datastore] $target"

    if (-not $Apply) {
        Write-Warn2 "DRY RUN -- nothing changed. Pass -Apply to proceed."
        exit 0
    }

    # ------------------------------------------------------------------ shutdown
    # A cold copy is the point. Copying a running DC's disk yields a crash-
    # consistent image of a database that hates crash consistency.
    Write-Step "Shutting the guest down cleanly"
    if ($vm.PowerState -eq 'PoweredOn') {
        Stop-VMGuest -VM $vm -Confirm:$false | Out-Null
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt $ShutdownTimeoutSeconds) {
            Start-Sleep -Seconds 10
            $vm = Get-VM -Server $h -Name $VmName
            if ($vm.PowerState -eq 'PoweredOff') { break }
            Write-Info ("waiting for shutdown ({0}s)" -f [int]$sw.Elapsed.TotalSeconds)
        }
        if ($vm.PowerState -ne 'PoweredOff') {
            Write-Fail "Guest did not shut down within $ShutdownTimeoutSeconds s."
            Write-Info  "It has NOT been hard-stopped. Finish the shutdown by hand and re-run."
            exit 1
        }
    }
    Write-Ok "powered off"

    # ---------------------------------------------------------------------- copy
    Write-Step "Copying"
    if ($UseDatastoreCopy) {
        # Pure PowerCLI: no SSH, but every byte crosses the network.
        New-PSDrive -Name mgmtds -Location $ds -PSProvider VimDatastore -Root '\' -Scope Script | Out-Null
        try {
            Copy-DatastoreItem -Item "mgmtds:\$SourceDir" -Destination "mgmtds:\$target" -Recurse -Force
            Write-Ok "copied over the network to $target"
        } finally { Remove-PSDrive mgmtds -ErrorAction SilentlyContinue }
    } else {
        # vmkfstools on the host is far faster -- the copy never leaves the
        # datastore. It needs SSH, which is off by default and is turned back
        # off afterwards regardless of outcome.
        $svc = Get-VMHostService -VMHost (Get-VMHost -Server $h) | Where-Object { $_.Key -eq 'TSM-SSH' }
        $sshWasRunning = $svc.Running
        if (-not $sshWasRunning) {
            Start-VMHostService -HostService $svc -Confirm:$false | Out-Null
            Write-Info "SSH enabled on the host (was off; will be restored)"
        }
        Write-Warn2 "Run this on $MgmtHost, then return here:"
        Write-Host ""
        Write-Host "  mkdir /vmfs/volumes/$Datastore/$target" -ForegroundColor White
        Write-Host "  cp /vmfs/volumes/$Datastore/$SourceDir/*.vmx  /vmfs/volumes/$Datastore/$target/" -ForegroundColor White
        Write-Host "  vmkfstools -i /vmfs/volumes/$Datastore/$SourceDir/$SourceDir.vmdk \" -ForegroundColor White
        Write-Host "             /vmfs/volumes/$Datastore/$target/$SourceDir.vmdk -d thin" -ForegroundColor White
        Write-Host ""
        Write-Info "The .vmx matters as much as the disk: without it the copy is not registerable."
        Read-Host "Press Enter once the copy has finished"

        if (-not $sshWasRunning) {
            Stop-VMHostService -HostService $svc -Confirm:$false | Out-Null
            Write-Ok "SSH returned to its previous state (off)"
        }
    }

    # ------------------------------------------------------------------- restart
    Write-Step "Bringing DNS01 back up"
    Start-VM -VM (Get-VM -Server $h -Name $VmName) -Confirm:$false | Out-Null
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $back = $false
    while ($sw.Elapsed.TotalSeconds -lt 600) {
        Start-Sleep -Seconds 15
        $v = Get-VM -Server $h -Name $VmName
        if ($v.ExtensionData.Guest.ToolsRunningStatus -eq 'guestToolsRunning') { $back = $true; break }
        Write-Info ("waiting for guest tools ({0}s)" -f [int]$sw.Elapsed.TotalSeconds)
    }
    if ($back) { Write-Ok "guest is up" } else { Write-Warn2 "guest tools not reporting yet; check the console" }

    # Tools running is not the same as AD answering. Prove the service, not the
    # VM -- and prove it with a query, not a port check. Measured on this DC:
    # 636 accepts TCP and then resets every TLS handshake, because there is no
    # certificate yet. A port probe on 636 would report a healthy LDAPS that
    # cannot complete a single handshake.
    Write-Step "Verifying the directory actually answers"
    $dnsOk = $false
    try {
        $ans = Resolve-DnsName -Name $DnsProbeName -Server $DcAddress -Type A -DnsOnly -ErrorAction Stop
        $ip  = ($ans | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
        if ($ip) { $dnsOk = $true; Write-Ok "DNS resolves $DnsProbeName -> $ip" }
        else     { Write-Warn2 "DNS replied but returned no address for $DnsProbeName" }
    } catch {
        Write-Warn2 "DNS query failed: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    }

    if (Test-TcpPort -ComputerName $DcAddress -Port 389 -TimeoutMs 5000) { Write-Ok "LDAP 389 is listening" }
    else { Write-Warn2 "LDAP 389 is not listening yet" }

    if (-not $dnsOk) {
        Write-Warn2 "Lab DNS is not serving yet. Wait for it before starting the CA install --"
        Write-Warn2 "every VCF component resolves its peers through this DC."
    }

    Write-Step "Done"
    Write-Ok "rollback copy: [$Datastore] $target"
    Write-Info "To roll back: power off DNS01, register the copy's .vmx, and power that on."
    Write-Info "Keep it until the CA is proven working -- then delete it deliberately."
}
finally {
    Disconnect-VIServer -Server $h -Confirm:$false -ErrorAction SilentlyContinue
}
