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
    Fall back to copying over the network with Copy-DatastoreItem instead of
    using the host's VirtualDiskManager. Every byte round-trips through this
    workstation, so it is slower by a wide margin. Only useful if the disk
    manager refuses the copy.

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

<#
    Returns one of three states, never a bare boolean:
      ok       -- the server answered with an address
      noanswer -- the server answered, but with no address (a real DNS fault)
      error    -- the query could not be made at all (says nothing about the server)
    Collapsing the last two into "false" is what made the first version of the
    recovery check report a healthy DC as broken.
#>
function Get-DnsAnswer {
    param([Parameter(Mandatory)][string]$Server, [Parameter(Mandatory)][string]$Name)
    try {
        $r = Resolve-DnsName -Name $Name -Server $Server -Type A -DnsOnly -ErrorAction Stop
        # Under Set-StrictMode -Version Latest, reading a property an object
        # does not carry THROWS -- and that throw lands in this function's own
        # catch below, turning a real server answer into "could not ask". A
        # response legitimately contains records without IPAddress (SOA on an
        # empty name, CNAME chains), so test for the property before reading it.
        $rec = @($r | Where-Object {
                    $_.PSObject.Properties.Name -contains 'IPAddress' -and $_.IPAddress })
        if ($rec.Count) { return @{ State = 'ok';       Detail = $rec[0].IPAddress } }
        return               @{ State = 'noanswer'; Detail = 'replied with no address' }
    } catch {
        $m = $_.Exception.Message.Split([Environment]::NewLine)[0]
        # NXDOMAIN and NoData also throw, but they are the server ANSWERING --
        # authoritatively, that the name is absent. That is a real finding about
        # a working server, not an inability to look, so it must not be filed
        # with the timeouts.
        if ($m -match 'does not exist|No such host|no records|NoData') {
            return     @{ State = 'noanswer'; Detail = $m }
        }
        return         @{ State = 'error';    Detail = $m }
    }
}

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

    # Work out what will actually be copied here, in the dry run, rather than
    # at copy time -- the folder holds a great deal that must NOT come along.
    # Observed in this VM's folder while running: an 8.5 GB .vswp, a 86 MB
    # vmx-*.vswp, a .lck, a .vmx~ backup and twelve .scoreboard files, none of
    # which belong in a rollback copy and all of which ESXi recreates on power
    # on. An allowlist is the only safe filter: a denylist silently admits
    # whatever the next ESXi build decides to leave lying around.
    New-PSDrive -Name mgmtds -Location $ds -PSProvider VimDatastore -Root '\' -Scope Script | Out-Null
    try {
        $all = @(Get-ChildItem "mgmtds:\$SourceDir" | Where-Object { -not $_.PSIsContainer })
    } finally { Remove-PSDrive mgmtds -ErrorAction SilentlyContinue }
    if (-not $all) { Write-Fail "Nothing found in [$Datastore] $SourceDir"; exit 1 }

    # The descriptor .vmdk only. Its -flat pair IS listed by the datastore
    # browser -- contrary to what you might expect -- and must be excluded:
    # VirtualDiskManager copies descriptor and flat together as one disk, so
    # letting the flat match here would copy 60 GB a second time, on its own,
    # as something that is not a valid disk.
    $disks = @($all | Where-Object { $_.Name -match '\.vmdk$' -and $_.Name -notmatch '-(flat|delta|ctk|sesparse|rdm|rdmp)\.vmdk$' })
    $files = @($all | Where-Object { $_.Name -match '\.(vmx|vmxf|nvram|vmsd)$' })
    $items = $disks + $files

    $skipped = @($all | Where-Object { $_.Name -notin $items.Name -and $_.Name -notmatch '-flat\.vmdk$' })
    Write-Info ("copying {0} disk(s) and {1} config file(s); skipping {2} runtime file(s)" -f `
                $disks.Count, $files.Count, $skipped.Count)
    foreach ($d in $disks) { Write-Info "  disk   $($d.Name)" }
    foreach ($f in $files) { Write-Info "  config $($f.Name)" }
    if (-not $disks) { Write-Fail "No disk descriptor found -- refusing to make a copy with no disk."; exit 1 }
    if (-not ($files | Where-Object { $_.Name -match '\.vmx$' })) {
        Write-Fail "No .vmx found -- the copy would not be registerable."; exit 1
    }

    # Size the copy from the flat, not from Summary.Storage.Committed: that
    # figure includes the swap file we are deliberately not copying.
    $flatBytes = ($all | Where-Object { $_.Name -match '-flat\.vmdk$' } |
                  Measure-Object -Property Length -Sum).Sum
    $needGb = [math]::Ceiling((($flatBytes + ($files | Measure-Object -Property Length -Sum).Sum)) / 1GB)
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
    # The copy runs through the host's own VirtualDiskManager and FileManager,
    # so the bytes never leave the datastore and SSH is never opened. Both
    # managers are present on a standalone host (ha-vdiskmanager,
    # ha-nfc-file-manager) -- cloning through New-VM is NOT, because the clone
    # API lives in vCenter and this host has none.
    Write-Step "Copying"

    if ($UseDatastoreCopy) {
        Write-Info "copying over the network (slower; every byte round-trips through this workstation)"
        New-PSDrive -Name mgmtds -Location $ds -PSProvider VimDatastore -Root '\' -Scope Script | Out-Null
        try { Copy-DatastoreItem -Item "mgmtds:\$SourceDir" -Destination "mgmtds:\$target" -Recurse -Force }
        finally { Remove-PSDrive mgmtds -ErrorAction SilentlyContinue }
        Write-Ok "copied to $target"
    } else {
        $si  = Get-View ServiceInstance -Server $h
        $fm  = Get-View $si.Content.FileManager        -Server $h
        $vdm = Get-View $si.Content.VirtualDiskManager -Server $h

        $fm.MakeDirectory("[$Datastore] $target", $null, $true)
        Write-Ok "created [$Datastore] $target"

        foreach ($item in $items) {
            $src = "[$Datastore] $SourceDir/$($item.Name)"
            $dst = "[$Datastore] $target/$($item.Name)"
            # A disk is not a file: copying a .vmdk descriptor with the file
            # manager leaves its -flat behind and yields an unusable disk.
            if ($item.Name -match '\.vmdk$') {
                Write-Info "disk : $($item.Name)  (this is the slow one)"
                $t = Get-View $vdm.CopyVirtualDisk_Task($src, $null, $dst, $null, $null, $false) -Server $h
            } else {
                Write-Info "file : $($item.Name)"
                $t = Get-View $fm.CopyDatastoreFile_Task($src, $null, $dst, $null, $true) -Server $h
            }
            while ($t.Info.State -eq 'running' -or $t.Info.State -eq 'queued') {
                Start-Sleep -Seconds 5
                $t.UpdateViewData('Info')
            }
            if ($t.Info.State -ne 'success') {
                Write-Fail "copy of $($item.Name) failed: $($t.Info.Error.LocalizedMessage)"
                Write-Warn2 "DNS01 is still powered OFF. Bring it up before doing anything else."
                exit 1
            }
        }
        Write-Ok "copied host-locally to $target"
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
    #
    # But a probe that cannot run is not a failure of the thing probed. The
    # first version of this check reported "Lab DNS is not serving" after a
    # perfectly successful clone, because the workstation running it was on a
    # VPN using OpenVPN's block-outside-dns: that installs WFP filters
    # permitting port 53 ONLY to the VPN's own resolvers. Every other DNS
    # server -- the DC, 1.1.1.1, 8.8.8.8 -- then times out identically to a
    # dead service. WFP is invisible to Get-NetFirewallRule, so both ends
    # looked clean while the real cause sat on the client. So: establish
    # whether this host can resolve at all before saying anything about the DC.
    Write-Step "Verifying the directory actually answers"

    $dns = Get-DnsAnswer -Server $DcAddress -Name $DnsProbeName
    switch ($dns.State) {
        'ok' { Write-Ok "DNS resolves $DnsProbeName -> $($dns.Detail)" }
        'noanswer' {
            # The DC replied. That is a real answer about a real service.
            Write-Fail "The DC answered but returned no address for $DnsProbeName."
            Write-Warn2 "This IS a DNS fault -- the server is reachable and responding."
        }
        default {
            # Could not query. Before blaming the DC, find out whether this
            # host can query ANY resolver.
            $own = (Get-DnsClientServerAddress -AddressFamily IPv4 |
                    Where-Object { $_.ServerAddresses } |
                    Select-Object -First 1).ServerAddresses
            $control = if ($own) { Get-DnsAnswer -Server $own[0] -Name 'example.com' }
                       else      { @{ State = 'error'; Detail = 'no resolver configured' } }

            if ($control.State -eq 'ok') {
                Write-Fail "Lab DNS is not answering (this host CAN resolve via $($own[0]))."
                Write-Warn2 "Wait for it before starting the CA install -- every VCF"
                Write-Warn2 "component resolves its peers through this DC."
            } else {
                Write-Warn2 "INCONCLUSIVE -- this host cannot query any DNS server, so this"
                Write-Warn2 "says nothing about the DC. The control query also failed."
                Write-Info  "A VPN client with block-outside-dns does exactly this. Verify"
                Write-Info  "from inside the lab instead, e.g. on the Proxmox host:"
                Write-Info  "  pct exec 159 -- nslookup $DnsProbeName $DcAddress"
            }
        }
    }

    # Independent of DNS: AD DS itself. Its own fact, never folded into the
    # DNS verdict.
    if (Test-TcpPort -ComputerName $DcAddress -Port 389 -TimeoutMs 5000) { Write-Ok "LDAP 389 is listening" }
    else { Write-Warn2 "LDAP 389 is not listening yet" }

    Write-Step "Done"
    Write-Ok "rollback copy: [$Datastore] $target"
    Write-Info "To roll back: power off DNS01, register the copy's .vmx, and power that on."
    Write-Info "Keep it until the CA is proven working -- then delete it deliberately."
}
finally {
    Disconnect-VIServer -Server $h -Confirm:$false -ErrorAction SilentlyContinue
}
