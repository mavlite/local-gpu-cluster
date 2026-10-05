<#
    Shared helpers for Start-VCFLab.ps1 / Stop-VCFLab.ps1.

    Credential handling: values are read from the credential file into
    SecureString / PSCredential and are never written to the console, a
    transcript, a log file, or a command line. Nothing here interpolates a
    secret into a string that gets printed.
#>

Set-StrictMode -Version Latest

function Import-VCFLabConfig {
    param([string]$Path = (Join-Path $PSScriptRoot 'VCFLab.Config.psd1'))
    if (-not (Test-Path $Path)) { throw "Config not found: $Path" }
    $cfg = Import-PowerShellDataFile -Path $Path
    $cfg['CredentialFile'] = Resolve-VCFLabCredentialPath -Config $cfg -ScriptRoot $PSScriptRoot
    Assert-VCFLabConfig -Config $cfg
    $cfg
}

<#
    Fail fast on the optional sections, at load, with a message that names the
    problem -- under StrictMode a missing key otherwise surfaces later as a bare
    "property cannot be found" from deep inside a power sequence.
#>
function Assert-VCFLabConfig {
    param([Parameter(Mandatory)][hashtable]$Config)
    $problems = New-Object System.Collections.Generic.List[string]
    if ($Config.ContainsKey('LlmWorkers') -and $Config.LlmWorkers) {
        $hostShorts = @($Config.Hosts | ForEach-Object { $_.Short })
        $seen = @{}
        foreach ($w in @($Config.LlmWorkers)) {
            if ($w -isnot [hashtable] -or -not $w.ContainsKey('VmName') -or -not $w.VmName -or
                -not $w.ContainsKey('Host') -or -not $w.Host) {
                $problems.Add("LlmWorkers: every entry needs VmName and Host"); continue
            }
            if ($seen.ContainsKey($w.VmName)) { $problems.Add("LlmWorkers: $($w.VmName) is listed twice") }
            $seen[$w.VmName] = $true
            if ($hostShorts -notcontains $w.Host) { $problems.Add("LlmWorkers: $($w.VmName) names host '$($w.Host)', not one of $($hostShorts -join ', ')") }
            # A worker that LOOKS like a VCF component would be counted as one by
            # the "is the VCF stack down?" check, and Minimal would refuse forever.
            if (Test-IsVcfName -Config $Config -Name $w.VmName) { $problems.Add("LlmWorkers: $($w.VmName) collides with a VCF component name or the VSP prefix") }
        }
    }
    if ($Config.ContainsKey('LicenseWindow')) {
        $lw = $Config.LicenseWindow
        if ($lw -isnot [hashtable] -or -not $lw.ContainsKey('WarnDays') -or -not $lw.ContainsKey('RefuseDays')) {
            $problems.Add("LicenseWindow needs WarnDays and RefuseDays")
        } elseif (-not (0 -lt $lw.WarnDays -and $lw.WarnDays -lt $lw.RefuseDays -and $lw.RefuseDays -lt 180)) {
            $problems.Add("LicenseWindow must satisfy 0 < WarnDays < RefuseDays < 180 (VCF 9 licenses lapse at 180 days)")
        }
    }
    if ($problems.Count -gt 0) { throw ("VCFLab.Config.psd1 is invalid:`n  " + ($problems -join "`n  ")) }
}

<#
    Works out where credentials.env lives.

    A .psd1 holds static data only -- no variable expansion -- so the configured
    value may be relative, absolute, or absent. Blindly joining it to the user
    profile produced paths like C:\Users\X\C:\Users\X\... when someone set an
    absolute path, so each candidate is tested rather than assumed.

    Search order:
      1. the configured value, if it is already rooted
      2. next to the scripts        (the natural layout when copied to a host)
      3. under the user profile     (.vcflab\credentials.env)
      4. credentials.env next to the scripts
#>
function Resolve-VCFLabCredentialPath {
    param([hashtable]$Config, [string]$ScriptRoot)

    $rel = $null
    if ($Config.ContainsKey('CredentialFileRelative') -and $Config.CredentialFileRelative) {
        $rel = [string]$Config.CredentialFileRelative
    } elseif ($Config.ContainsKey('CredentialFile') -and $Config.CredentialFile) {
        $rel = [string]$Config.CredentialFile
    }

    # Precedence: an explicit rooted path wins outright, then anything beside
    # the scripts (the "I copied this folder to a host" case), then the profile.
    # Script-adjacent beats the profile deliberately -- a credentials.env you
    # placed next to the scripts is a more specific intent than a leftover in
    # your profile, and silently preferring the profile copy would mean running
    # with credentials you did not mean to use.
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($rel -and [System.IO.Path]::IsPathRooted($rel)) {
        $candidates.Add($rel)
    } else {
        if ($rel) { $candidates.Add((Join-Path $ScriptRoot $rel)) }
        $candidates.Add((Join-Path $ScriptRoot 'credentials.env'))
        if ($rel) { $candidates.Add((Join-Path $env:USERPROFILE $rel)) }
        $candidates.Add((Join-Path $env:USERPROFILE '.vcflab\credentials.env'))
    }

    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path } }

    throw ("Credential file not found. Tried:`n  " + (($candidates | Select-Object -Unique) -join "`n  ") +
           "`nPass an explicit path with -CredentialPath, or set CredentialFileRelative in VCFLab.Config.psd1.")
}

function Write-Step   { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok     { param([string]$m) Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Write-Info   { param([string]$m) Write-Host "  [info] $m" -ForegroundColor Gray }
function Write-Warn2  { param([string]$m) Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Write-Fail   { param([string]$m) Write-Host "  [FAIL] $m" -ForegroundColor Red }

<#
    Reads the credential file. Tolerant of both "NAME=value" and "NAME = value"
    -- the file mixes the two, and the spaced form is not shell-sourceable,
    which has caused parse failures before.
#>
function Import-VCFLabCredential {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path $Path)) { throw "Credential file not found: $Path" }
    $acl = Get-Acl $Path
    $everyone = $acl.Access | Where-Object { $_.IdentityReference -match 'Everyone|Users' }
    if ($everyone) { Write-Warn2 "Credential file has broad ACLs: $Path" }

    $map = @{}
    foreach ($line in (Get-Content -Path $Path -ErrorAction Stop)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        if ($k) { $map[$k] = $v }
    }
    $map
}

function Get-VCFLabCredentialObject {
    param([hashtable]$Map, [string]$UserKey, [string]$PassKey, [string]$DefaultUser)
    $u = if ($UserKey -and $Map.ContainsKey($UserKey) -and $Map[$UserKey]) { $Map[$UserKey] } else { $DefaultUser }
    if (-not $Map.ContainsKey($PassKey)) { throw "Credential key '$PassKey' missing from credential file" }
    $sec = ConvertTo-SecureString -String $Map[$PassKey] -AsPlainText -Force
    New-Object System.Management.Automation.PSCredential($u, $sec)
}

function Initialize-PowerCLI {
    if (-not (Get-Module -ListAvailable -Name VMware.VimAutomation.Core)) {
        throw "PowerCLI is not installed. Install-Module VMware.PowerCLI -Scope CurrentUser"
    }
    Import-Module VMware.VimAutomation.Core -ErrorAction Stop | Out-Null
    # Lab uses self-signed certs; do not prompt, do not participate in CEIP.
    Set-PowerCLIConfiguration -InvalidCertificateAction Ignore -Scope Session -Confirm:$false | Out-Null
    Set-PowerCLIConfiguration -ParticipateInCEIP $false -Scope Session -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Disconnect-AllViServers
}

<#
    Drops any existing PowerCLI sessions. A cached session can make a wrong
    password look correct, so sessions are always established explicitly.

    $global:DefaultVIServers does not exist until PowerCLI has connected at
    least once, and under Set-StrictMode -Version Latest a bare reference to it
    is a terminating error -- hence Get-Variable rather than direct access.
#>
function Disconnect-AllViServers {
    $v = Get-Variable -Name DefaultVIServers -Scope Global -ErrorAction SilentlyContinue
    if (-not $v -or -not $v.Value) { return }
    foreach ($s in @($v.Value)) {
        if ($s) { Disconnect-VIServer -Server $s -Confirm:$false -ErrorAction SilentlyContinue }
    }
}

<#
    Sends a Wake-on-LAN magic packet.

    Format: 6 x 0xFF followed by the target MAC repeated 16 times, as a UDP
    datagram to the broadcast address (ports 9 and 7 are both conventional).

    Magic packets are layer 2 and do not route: this must be sent from a host on
    the same segment as the target NIC, or to that segment's directed broadcast
    with the intervening switch configured to forward it. In this lab the mgmt
    ESX host is dual-homed onto the lab VLAN and stays up when the cluster is
    down, which makes it a natural injector.

    WoL is power-ON only. It cannot power off, reset, or recover a host that is
    hung -- for that you need switched power (a PDU) or hands on the chassis.
#>
function Send-WakeOnLan {
    param(
        [Parameter(Mandatory)][string]$MacAddress,
        [string]$Broadcast = '255.255.255.255',
        [int[]]$Ports = @(9, 7)
    )
    $clean = ($MacAddress -replace '[:\-\.\s]', '').Trim()
    if ($clean.Length -ne 12 -or $clean -notmatch '^[0-9A-Fa-f]{12}$') {
        throw "Invalid MAC '$MacAddress' (expected 12 hex digits, e.g. 98:B7:85:23:A4:CE)"
    }
    $mac = for ($i = 0; $i -lt 12; $i += 2) { [Convert]::ToByte($clean.Substring($i, 2), 16) }

    $packet = New-Object byte[] 102
    for ($i = 0; $i -lt 6; $i++) { $packet[$i] = 0xFF }
    for ($r = 0; $r -lt 16; $r++) { [Array]::Copy($mac, 0, $packet, 6 + ($r * 6), 6) }

    $sent = 0
    foreach ($p in $Ports) {
        $udp = New-Object System.Net.Sockets.UdpClient
        try {
            $udp.EnableBroadcast = $true
            $udp.Connect([System.Net.IPAddress]::Parse($Broadcast), $p)
            [void]$udp.Send($packet, $packet.Length)
            $sent++
        } catch {
            Write-Warn2 "WoL to $Broadcast`:$p failed: $($_.Exception.Message)"
        } finally { $udp.Close() }
    }
    $sent -gt 0
}

function Test-TcpPort {
    param([string]$ComputerName, [int]$Port, [int]$TimeoutMs = 4000)
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect($ComputerName, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $c.EndConnect($iar); $true
    } catch { $false } finally { $c.Close() }
}

<#
    Generic gate. Polls a scriptblock returning [bool] until it is true or the
    timeout expires. Every tier advance in these scripts goes through this, so
    that "it is up" always means a functional check passed -- not that a VM
    powered on, and not that a port opened.
#>
function Wait-Gate {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Test,
        [int]$TimeoutSeconds = 900,
        [int]$PollSeconds = 15
    )
    # A gate that swallows its errors spins in silence. An NSX gate once waited
    # the full 1800s while NSX had been STABLE the whole time, because the test
    # threw and the exception went nowhere. Gates set $script:LastGateDetail;
    # this surfaces it while waiting and on failure.
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $lastErr = ''
    $script:LastGateDetail = ''
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $ok = $false
        try { $ok = [bool](& $Test) } catch { $lastErr = $_.Exception.Message }
        if ($ok) {
            $d = if ($script:LastGateDetail) { " ($script:LastGateDetail)" } else { '' }
            Write-Ok ("{0} after {1}s{2}" -f $Name, [int]$sw.Elapsed.TotalSeconds, $d)
            return $true
        }
        Start-Sleep -Seconds $PollSeconds
        if ([int]$sw.Elapsed.TotalSeconds % 60 -lt $PollSeconds) {
            $why = if ($lastErr) { "error: $lastErr" } elseif ($script:LastGateDetail) { $script:LastGateDetail } else { 'no detail reported' }
            Write-Info ("waiting on {0} ({1}s) -- {2}" -f $Name, [int]$sw.Elapsed.TotalSeconds, $why)
        }
    }
    $why = if ($lastErr) { "last error: $lastErr" } elseif ($script:LastGateDetail) { "last state: $script:LastGateDetail" } else { 'no detail reported' }
    Write-Fail ("{0} did not pass within {1}s -- {2}" -f $Name, $TimeoutSeconds, $why)
    $false
}

function Invoke-LabRest {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [string]$Method = 'GET',
        [hashtable]$Headers,
        $Body,
        [pscredential]$BasicCredential,
        [int]$TimeoutSec = 30
    )
    $p = @{ Uri = $Uri; Method = $Method; TimeoutSec = $TimeoutSec; ErrorAction = 'Stop' }
    if ($Headers) { $p.Headers = $Headers }
    if ($null -ne $Body) { $p.Body = ($Body | ConvertTo-Json -Depth 5); $p.ContentType = 'application/json' }
    if ($BasicCredential) {
        $pair = "{0}:{1}" -f $BasicCredential.UserName, $BasicCredential.GetNetworkCredential().Password
        $b64  = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
        # ContainsKey, not dot access: under Set-StrictMode -Version Latest a
        # hashtable's MISSING key throws on dot access exactly as a missing
        # object property does ("The property 'Headers' cannot be found on this
        # object"). So -BasicCredential without -Headers threw here rather than
        # building an auth header, and the throw surfaced from the caller's
        # catch block as an unrelated-looking error. Confirmed by execution.
        if (-not $p.ContainsKey('Headers')) { $p.Headers = @{} }
        $p.Headers['Authorization'] = "Basic $b64"
    }
    # PS 5.1 has no -SkipCertificateCheck; the callback below is session-scoped.
    Invoke-RestMethod @p
}

function Disable-CertificateValidation {
    if (-not ("TrustAllCertsPolicy" -as [type])) {
        Add-Type -TypeDefinition @"
using System.Net; using System.Security.Cryptography.X509Certificates;
public class TrustAllCertsPolicy : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint sp, X509Certificate c, WebRequest r, int p) { return true; }
}
"@
    }
    [System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCertsPolicy
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
}

# ---------------------------------------------------------------- gates -----

function Test-VCenterApi {
    param([string]$Server, [pscredential]$Credential)
    try {
        $c = Connect-VIServer -Server $Server -Credential $Credential -ErrorAction Stop -Force
        $null = Get-VMHost -Server $c -ErrorAction Stop
        $true
    } catch { $false }
}

function Test-SddcManagerApi {
    param([string]$Ip, [pscredential]$Credential)
    try {
        $body = @{ username = $Credential.UserName; password = $Credential.GetNetworkCredential().Password }
        $r = Invoke-LabRest -Uri "https://$Ip/v1/tokens" -Method POST -Body $body -TimeoutSec 30
        [bool]$r.accessToken
    } catch { $false }
}

<#
    NSX readiness.

    Checks three things that actually carry state:
      * control_cluster_status and mgmt_cluster_status both STABLE
      * every group in detailed_cluster_status STABLE, with all members UP
      * transport nodes reporting success

    It deliberately does NOT use /api/v1/node/services. That endpoint returns
    service entries with no runtime state at all -- every entry lacks a
    service_status property -- so a "no service is down" test against it passes
    vacuously whatever NSX is doing. An earlier version of this check, and its
    shell equivalent, both reported "29/29 services running" while reading a
    field that does not exist. Per-service state needs /node/services/<n>/status
    one call at a time, which is not worth 29 round trips when the group detail
    already carries the answer.

    $LastNsxGateDetail is set on every call so a failing gate can say why.
#>
function Test-NsxCluster {
    param([string]$Ip, [pscredential]$Credential, [int]$ExpectedTransportNodes = 3)
    $script:LastGateDetail = ''
    try {
        $h = @{}
        $pair = "{0}:{1}" -f $Credential.UserName, $Credential.GetNetworkCredential().Password
        $h['Authorization'] = "Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))

        $s = Invoke-LabRest -Uri "https://$Ip/api/v1/cluster/status" -Headers $h -TimeoutSec 30
        $ctl = $s.control_cluster_status.status
        $mgt = $s.mgmt_cluster_status.status
        if ($ctl -ne 'STABLE' -or $mgt -ne 'STABLE') {
            $script:LastGateDetail = "control=$ctl mgmt=$mgt"
            return $false
        }

        $badGroups = @()
        $groups = @()
        if ($s.PSObject.Properties.Name -contains 'detailed_cluster_status' -and $s.detailed_cluster_status) {
            if ($s.detailed_cluster_status.PSObject.Properties.Name -contains 'groups') {
                $groups = @($s.detailed_cluster_status.groups)
            }
        }
        foreach ($g in $groups) {
            $gs = $g.group_status
            if ($gs -and $gs -ne 'STABLE') { $badGroups += "$($g.group_type)=$gs"; continue }
            foreach ($mem in @($g.members)) {
                if ($mem.member_status -and $mem.member_status -ne 'UP') {
                    $badGroups += "$($g.group_type)/$($mem.member_fqdn)=$($mem.member_status)"
                }
            }
        }
        if ($badGroups.Count -gt 0) {
            $script:LastGateDetail = "groups not ready: " + ($badGroups -join ', ')
            return $false
        }

        $tn = Invoke-LabRest -Uri "https://$Ip/api/v1/transport-nodes/state" -Headers $h -TimeoutSec 30
        $ok = @($tn.results | Where-Object { $_.state -eq 'success' }).Count
        if ($ok -lt $ExpectedTransportNodes) {
            $script:LastGateDetail = "transport nodes success=$ok of $ExpectedTransportNodes"
            return $false
        }

        $script:LastGateDetail = "control+mgmt STABLE, $($groups.Count) groups OK, $ok transport nodes"
        $true
    } catch {
        $script:LastGateDetail = "error: $($_.Exception.Message)"
        $false
    }
}

function Test-OperationsApi {
    param([string]$Ip, [pscredential]$Credential)
    try {
        $body = @{ username = $Credential.UserName; password = $Credential.GetNetworkCredential().Password }
        $r = Invoke-LabRest -Uri "https://$Ip/suite-api/api/auth/token/acquire" -Method POST -Body $body `
                            -Headers @{ Accept = 'application/json' } -TimeoutSec 30
        [bool]$r.token
    } catch { $false }
}

function Get-VMGuestIp {
    param($Vm)
    try { (Get-VM -Name $Vm.Name -ErrorAction Stop).Guest.IPAddress | Where-Object { $_ -and $_ -notmatch ':' } | Select-Object -First 1 }
    catch { $null }
}

function Test-VmGuestUp {
    param([string]$Name)
    try {
        $v = Get-VM -Name $Name -ErrorAction Stop
        if ($v.PowerState -ne 'PoweredOn') { return $false }
        $ip = $v.Guest.IPAddress | Where-Object { $_ -and $_ -notmatch ':' } | Select-Object -First 1
        [bool]($v.Guest.State -eq 'Running' -and $ip)
    } catch { $false }
}

# ------------------------------------------------------------- power ops ----

<#
    Compare each host's own view of its VMs against vCenter's.

    vpxa is the agent that reports VM state up to vCenter, and it can wedge
    while still looking healthy: the host stays Connected, esxcli through vpxa
    answers, and only the VM runtime state stops flowing. vCenter then plans
    against a stale object and power-on fails with

        The object 'vpx.vmprov.PowerOnVm:vpx.vmprov.PowerOnVm' has already
        been deleted or has not been completely created

    which reads like a broken VM and is not. The host is the authority; vCenter
    is a cache. Comparing the two is the only cheap way to catch it.

    LIMIT worth knowing: this can only see drift that has something to disagree
    about. On a cold start where every VM is off on both sides, a wedged vpxa is
    invisible here -- it shows up on the first power-on instead, which is why
    Start-LabVM repairs on failure rather than relying on this check alone.

    Returns one object per host: Short, Drift (count), Details, Checked.
#>
function Get-HostInventoryDrift {
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][pscredential]$EsxCredential
    )
    $vcView = @{}
    foreach ($v in (Get-VM -ErrorAction SilentlyContinue)) { $vcView[$v.Name] = $v.PowerState }

    foreach ($h in $Config.Hosts) {
        $details = New-Object System.Collections.Generic.List[string]
        $checked = 0
        $hc = $null
        try {
            $hc = Connect-VIServer -Server $h.Ip -Credential $EsxCredential -Force -ErrorAction Stop
            foreach ($hv in (Get-VM -Server $hc -ErrorAction Stop)) {
                $checked++
                if (-not $vcView.ContainsKey($hv.Name)) {
                    $details.Add("$($hv.Name): on the host but absent from vCenter")
                    continue
                }
                if ($vcView[$hv.Name] -ne $hv.PowerState) {
                    $details.Add("$($hv.Name): host=$($hv.PowerState) vCenter=$($vcView[$hv.Name])")
                }
            }
        } catch {
            $details.Add("could not query the host: $($_.Exception.Message -replace '\s+', ' ')")
        } finally {
            if ($hc) { Disconnect-VIServer -Server $hc -Confirm:$false -ErrorAction SilentlyContinue }
        }
        [pscustomobject]@{
            Short   = $h.Short
            Name    = $h.Name
            Ip      = $h.Ip
            Checked = $checked
            Drift   = $details.Count
            Details = $details
        }
    }
}

<#
    Restart vpxa on one host and wait for vCenter to agree with it again.

    Restart-VMHostService THROWS on success here: vpxa drops the management
    connection as it restarts, and PowerCLI surfaces that as "An error occurred
    while communicating with the remote host". Treating that as a failure is
    wrong -- observed 2026-09-29, where the restart worked and the resync landed
    about twenty seconds later. So the exception is swallowed and the outcome is
    judged by polling the actual state, never by the cmdlet's return.

    Running VMs are not disturbed; vpxa is a management agent, not the
    hypervisor.
#>
function Repair-HostVpxa {
    param(
        [Parameter(Mandatory)][string]$HostName,
        [Parameter(Mandatory)][pscredential]$EsxCredential,
        [hashtable]$Config,
        [int]$TimeoutSeconds = 240
    )
    $short = ($HostName -split '\.')[0]
    Write-Info "$short : restarting vpxa to resync vCenter's inventory"

    $vmh = Get-VMHost -Name $HostName -ErrorAction SilentlyContinue
    if (-not $vmh) {
        # Name may be an IP, or vCenter may know it by FQDN; try a loose match.
        $vmh = Get-VMHost -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -like "$short*" } | Select-Object -First 1
    }
    if (-not $vmh) { Write-Fail "$short : not found in vCenter; cannot restart vpxa"; return $false }

    try {
        Get-VMHostService -VMHost $vmh -ErrorAction Stop |
            Where-Object { $_.Key -eq 'vpxa' } |
            Restart-VMHostService -Confirm:$false -ErrorAction Stop | Out-Null
    } catch {
        # Expected: the agent cuts the connection mid-call. Not a failure.
        Write-Info "$short : vpxa dropped the connection while restarting (expected)"
    }

    if (-not $Config) { Write-Info "$short : vpxa restart issued"; return $true }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        Start-Sleep -Seconds 10
        $one = @{ Hosts = @($Config.Hosts | Where-Object { $_.Name -eq $HostName -or $_.Short -eq $short }) }
        if ($one.Hosts.Count -eq 0) { break }
        $d = @(Get-HostInventoryDrift -Config $one -EsxCredential $EsxCredential)
        if ($d.Count -gt 0 -and $d[0].Drift -eq 0 -and $d[0].Checked -gt 0) {
            Write-Ok "$short : vCenter agrees with the host again after $([int]$sw.Elapsed.TotalSeconds)s"
            return $true
        }
    }
    Write-Warn2 "$short : still out of sync after $TimeoutSeconds s; check the host by hand"
    $false
}

<#
    Pre-flight: report inventory drift and repair it before anything depends on
    vCenter being right. Every downstream gate reads VM state from vCenter, so a
    stale view does not just fail a power-on -- it hangs guest-IP gates for their
    full timeout while the node is actually up and healthy.
#>
function Confirm-HostInventorySync {
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][pscredential]$EsxCredential,
        [switch]$ReportOnly
    )
    $drifted = @()
    foreach ($r in (Get-HostInventoryDrift -Config $Config -EsxCredential $EsxCredential)) {
        if ($r.Drift -eq 0) {
            Write-Ok "$($r.Short) : vCenter agrees with the host ($($r.Checked) VMs)"
            continue
        }
        Write-Warn2 "$($r.Short) : $($r.Drift) of $($r.Checked) VMs disagree with vCenter"
        foreach ($d in $r.Details) { Write-Host "         $d" -ForegroundColor Yellow }
        $drifted += $r
    }
    if ($drifted.Count -eq 0) { return $true }
    if ($ReportOnly) {
        Write-Info "reporting only; restart vpxa on the affected hosts to resync"
        return $false
    }
    $ok = $true
    foreach ($r in $drifted) {
        if (-not (Repair-HostVpxa -HostName $r.Name -EsxCredential $EsxCredential -Config $Config)) { $ok = $false }
    }
    $ok
}

<#
    Graceful guest shutdown with an optional hard-stop fallback.

    GraceSeconds = 0 means wait indefinitely and NEVER hard stop. That is the
    default for vCenter: a vCSA hard-kill risks its embedded database, and a
    fixed timeout on the one VM where patience matters has bitten us before.
#>
function Stop-LabVM {
    param(
        [Parameter(Mandatory)][string]$Name,
        [int]$GraceSeconds = 240,
        [switch]$AllowHardStop,
        [switch]$WhatIfMode
    )
    $vm = Get-VM -Name $Name -ErrorAction SilentlyContinue
    if (-not $vm) { Write-Info "$Name : not in inventory, skipping"; return $true }
    if ($vm.PowerState -ne 'PoweredOn') { Write-Info "$Name : already $($vm.PowerState)"; return $true }
    if ($WhatIfMode) { Write-Info "$Name : WHATIF would shut down"; return $true }

    $toolsOk = $false
    try { $toolsOk = ($vm.Guest.ExtensionData.ToolsRunningStatus -eq 'guestToolsRunning') } catch {}

    if ($toolsOk) {
        Write-Info "$Name : guest shutdown requested"
        Stop-VMGuest -VM $vm -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    } else {
        Write-Warn2 "$Name : no VMware Tools -- hard power off"
        Stop-VM -VM $vm -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Start-Sleep -Seconds 5
        return ((Get-VM -Name $Name).PowerState -eq 'PoweredOff')
    }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        Start-Sleep -Seconds 10
        $cur = Get-VM -Name $Name -ErrorAction SilentlyContinue
        if (-not $cur -or $cur.PowerState -eq 'PoweredOff') {
            Write-Ok "$Name : off gracefully after $([int]$sw.Elapsed.TotalSeconds)s"; return $true
        }
        if ($GraceSeconds -gt 0 -and $sw.Elapsed.TotalSeconds -ge $GraceSeconds) { break }
        if ($GraceSeconds -eq 0 -and $sw.Elapsed.TotalSeconds % 120 -lt 10) {
            Write-Info "$Name : still shutting down ($([int]$sw.Elapsed.TotalSeconds)s) -- no hard stop configured"
        }
    }

    if (-not $AllowHardStop) {
        Write-Warn2 "$Name : still running after $([int]$sw.Elapsed.TotalSeconds)s. NOT hard-stopping (use -Force to allow)."
        return $false
    }
    Write-Warn2 "$Name : grace expired -- hard power off"
    Stop-VM -VM (Get-VM -Name $Name) -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 6
    ((Get-VM -Name $Name -ErrorAction SilentlyContinue).PowerState -eq 'PoweredOff')
}

<#
    Power a VM on, falling back to its host when vCenter cannot do it.

    Observed 2026-09-29: vCenter's inventory for a host went stale after a cold
    boot. Every power-on of a VM on that host failed with

        The object 'vpx.vmprov.PowerOnVm:vpx.vmprov.PowerOnVm' has already
        been deleted or has not been completely created

    while the host, the VM and vSAN were all healthy -- esxcli through vpxa
    answered, the host was Connected and out of maintenance mode, and vSAN
    reported 93 objects healthy and none inaccessible. Connecting straight to
    the host and powering the VM on there worked first time, and vCenter went
    on reporting the VM as PoweredOff while it was up and serving traffic.

    So: try vCenter, and on failure go to the host that holds the VM. The host
    is the authority on whether a VM is running; vCenter is a cache that can be
    wrong. -EsxCredential enables the fallback, and without it the failure is
    reported as before.
#>
function Start-LabVM {
    param(
        [Parameter(Mandatory)][string]$Name,
        [pscredential]$EsxCredential,
        [hashtable]$Config,
        [switch]$RepairInventory,
        [switch]$WhatIfMode
    )
    $vm = Get-VM -Name $Name -ErrorAction SilentlyContinue
    if (-not $vm) { Write-Warn2 "$Name : not in inventory"; return $false }
    if ($vm.PowerState -eq 'PoweredOn') { Write-Info "$Name : already powered on"; return $true }
    if ($WhatIfMode) { Write-Info "$Name : WHATIF would power on"; return $true }

    $hostName = $null
    try { $hostName = $vm.VMHost.Name } catch { }

    try {
        Start-VM -VM $vm -Confirm:$false -ErrorAction Stop | Out-Null
        Write-Info "$Name : power on issued"
        return $true
    } catch {
        $msg = ($_.Exception.Message -replace '\s+', ' ')
        Write-Warn2 "$Name : vCenter could not power it on -- $msg"
    }

    if (-not $EsxCredential) {
        Write-Fail "$Name : no ESX credential supplied, cannot fall back to the host"
        return $false
    }
    if (-not $hostName) {
        Write-Fail "$Name : vCenter did not say which host holds it; cannot fall back"
        return $false
    }

    Write-Info "$Name : falling back to $(($hostName -split '\.')[0]) directly"
    $hc = $null
    $started = $false
    try {
        $hc = Connect-VIServer -Server $hostName -Credential $EsxCredential -Force -ErrorAction Stop
        $hvm = Get-VM -Server $hc -Name $Name -ErrorAction Stop
        if ($hvm.PowerState -eq 'PoweredOn') {
            Write-Ok "$Name : already running on the host (vCenter's view is stale)"
        } else {
            Start-VM -VM $hvm -Server $hc -Confirm:$false -ErrorAction Stop | Out-Null
            Write-Ok "$Name : powered on via the host"
        }
        $started = $true
    } catch {
        Write-Fail "$Name : host fallback also failed -- $(($_.Exception.Message -replace '\s+', ' '))"
    } finally {
        if ($hc) { Disconnect-VIServer -Server $hc -Confirm:$false -ErrorAction SilentlyContinue }
    }
    if (-not $started) { return $false }

    # Reaching here is proof that vCenter's inventory for this host is stale:
    # the host powered the VM on and vCenter could not. Repair it now rather
    # than leaving it -- every later gate reads guest IPs FROM vCenter, so a
    # stale view does not merely fail a power-on, it hangs those gates for
    # their full timeout while the node is up and healthy.
    if ($RepairInventory) {
        $null = Repair-HostVpxa -HostName $hostName -EsxCredential $EsxCredential -Config $Config
    } else {
        Write-Warn2 "vCenter's view of $(($hostName -split '\.')[0]) is stale; restart vpxa there or later gates will hang"
    }
    $true
}

<#
    The set of VM names these scripts are allowed to touch: vCenter, the VCF
    appliances, and VSP nodes. Everything else on the cluster is out of scope.

    VSP nodes are matched by prefix AND folder, so a non-VCF VM that merely
    happens to start with "platform-" is not swept up.
#>
function Get-VcfManagedNames {
    param([hashtable]$Config)
    $names = New-Object System.Collections.Generic.List[string]
    $names.Add($Config.VCenter.VmName)
    foreach ($k in $Config.Appliances.Keys) { $names.Add($Config.Appliances[$k].VmName) }
    foreach ($v in (Get-VM -Name "$($Config.Vsp.NamePrefix)*" -ErrorAction SilentlyContinue)) {
        $inFolder = $true
        try { $inFolder = ($v.Folder -and $v.Folder.Name -eq $Config.Vsp.Folder) } catch { $inFolder = $true }
        if ($inFolder) { $names.Add($v.Name) }
    }
    $names | Where-Object { $_ } | Select-Object -Unique
}

<#
    Splits every powered-on VM into "ours" and "not ours", so the scripts can
    act on the first and report the second. Used to prove a host is genuinely
    clear before maintenance mode, and to say plainly what was left running.
#>
function Get-VmScope {
    param([hashtable]$Config, [string[]]$ManagedNames)
    $all = Get-VM -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Managed   = @($all | Where-Object { $ManagedNames -contains $_.Name })
        Foreign   = @($all | Where-Object { $ManagedNames -notcontains $_.Name })
    }
}

<#
    Discovers VSP control-plane vs worker nodes by vCPU count. The supervisor
    destroys and recreates workers on its own, so names are not stable -- this
    was observed twice on 2026-09-27 (bn2r2 destroyed, 8n62z created).
#>
function Get-VspNodes {
    param([hashtable]$Config)
    $all = Get-VM -Name "$($Config.Vsp.NamePrefix)*" -ErrorAction SilentlyContinue
    $cp  = @($all | Where-Object { $_.NumCpu -eq $Config.Vsp.ControlPlaneVcpu } | Sort-Object Name)
    $wk  = @($all | Where-Object { $_.NumCpu -ne $Config.Vsp.ControlPlaneVcpu } | Sort-Object Name)
    [pscustomobject]@{ ControlPlane = $cp; Workers = $wk; All = $all }
}

# ------------------------------------------------------- LLM workers ----------
# Minimal mode runs the hosts and the CPU LLM workers with every VCF component
# OFF, vCenter included. So everything below talks to the hosts directly: the
# workers sit on local datastores and an ephemeral-binding port group, which a
# host can attach without vCenter.

<#
    The configured LLM workers, each resolved to the host that holds it.
    An older config without LlmWorkers yields an empty list, not an error --
    under StrictMode a missing hashtable key throws, so it is checked first.
#>
function Get-LlmWorkers {
    param([Parameter(Mandatory)][hashtable]$Config)
    if (-not $Config.ContainsKey('LlmWorkers') -or -not $Config.LlmWorkers) { return @() }
    foreach ($w in @($Config.LlmWorkers)) {
        $h = @($Config.Hosts | Where-Object { $_.Short -eq $w.Host })
        if ($h.Count -ne 1) { throw "LlmWorkers: $($w.VmName) names host '$($w.Host)', which is not in Hosts" }
        [pscustomobject]@{ VmName = [string]$w.VmName; HostShort = [string]$h[0].Short; HostIp = [string]$h[0].Ip }
    }
}

<#
    Is this VM name a VCF component? Answerable without vCenter, which is the
    point: on a host-direct connection there is no folder to check, so VSP
    nodes are matched by prefix (and the configured names as a fallback).
#>
function Test-IsVcfName {
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][string]$Name)
    if ($Name -eq $Config.VCenter.VmName) { return $true }
    foreach ($k in $Config.Appliances.Keys) { if ($Name -eq $Config.Appliances[$k].VmName) { return $true } }
    if ($Name -like "$($Config.Vsp.NamePrefix)*") { return $true }
    ($Config.Vsp.KnownControlPlane + $Config.Vsp.KnownWorkers) -contains $Name
}

<#
    Ask every host which VCF components it is RUNNING. Used where vCenter
    cannot be: before Minimal starts workers beside a stack that is still up,
    and before a vCenter-less -IncludeHosts powers hosts off. A host that could
    not be queried is reported, never assumed clear.
#>
function Get-RunningVcfOnHosts {
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][pscredential]$EsxCredential,
        [string[]]$Allow = @()
    )
    $running = @(); $unreachable = @()
    foreach ($h in $Config.Hosts) {
        $c = $null
        try {
            $c = Connect-VIServer -Server $h.Ip -Credential $EsxCredential -Force -ErrorAction Stop
            foreach ($v in @(Get-VM -Server $c -ErrorAction Stop)) {
                if ($v.PowerState -eq 'PoweredOn' -and $Allow -notcontains $v.Name -and
                    (Test-IsVcfName -Config $Config -Name $v.Name)) {
                    $running += [pscustomobject]@{ Name = $v.Name; Host = $h.Short }
                }
            }
        } catch {
            $unreachable += $h.Short
        } finally {
            if ($c) { Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue }
        }
    }
    [pscustomobject]@{ Running = $running; Unreachable = $unreachable }
}

function Start-HostVM {
    param(
        [Parameter(Mandatory)][string]$HostIp,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][pscredential]$EsxCredential,
        [switch]$WhatIfMode
    )
    $c = $null
    try {
        $c = Connect-VIServer -Server $HostIp -Credential $EsxCredential -Force -ErrorAction Stop
        # The connection itself already succeeded (Stop above), so an empty
        # result here genuinely means "not registered on this host".
        $vm = @(Get-VM -Server $c -Name $Name -ErrorAction SilentlyContinue)
        if ($vm.Count -eq 0) { Write-Fail "$Name : not registered on $HostIp"; return $false }
        if ($vm[0].PowerState -eq 'PoweredOn') { Write-Info "$Name : already powered on"; return $true }
        if ($WhatIfMode) { Write-Info "$Name : WHATIF would power on via its host"; return $true }
        Start-VM -VM $vm[0] -Server $c -Confirm:$false -ErrorAction Stop | Out-Null
        Write-Ok "$Name : powered on via its host"
        $true
    } catch {
        Write-Fail "$Name : power-on via $HostIp failed -- $(($_.Exception.Message -replace '\s+', ' '))"
        $false
    } finally {
        if ($c) { Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue }
    }
}

<#
    Running, Tools up, and an IPv4 address -- read from the HOST, because in
    Minimal mode there is no vCenter to ask (Test-VmGuestUp reads vCenter).
#>
function Test-HostVmGuestUp {
    param(
        [Parameter(Mandatory)][string]$HostIp,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][pscredential]$EsxCredential
    )
    $c = $null
    try {
        $c = Connect-VIServer -Server $HostIp -Credential $EsxCredential -Force -ErrorAction Stop
        $v = @(Get-VM -Server $c -Name $Name -ErrorAction Stop)
        if ($v.Count -eq 0 -or $v[0].PowerState -ne 'PoweredOn') { return $false }
        $ip = $v[0].Guest.IPAddress | Where-Object { $_ -and $_ -notmatch ':' } | Select-Object -First 1
        [bool]($v[0].Guest.State -eq 'Running' -and $ip)
    } catch { $false }
    finally { if ($c) { Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue } }
}

<#
    Guest shutdown via the host, hard stop after the grace period. Workers are
    stateless inference servers, so a hard stop after grace is acceptable here
    -- unlike vCenter, which is never hard-killed by default.
#>
function Stop-HostVM {
    param(
        [Parameter(Mandatory)][string]$HostIp,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][pscredential]$EsxCredential,
        [int]$GraceSeconds = 120,
        [switch]$WhatIfMode
    )
    $c = $null
    try {
        $c = Connect-VIServer -Server $HostIp -Credential $EsxCredential -Force -ErrorAction Stop
        $vm = @(Get-VM -Server $c -Name $Name -ErrorAction SilentlyContinue)
        if ($vm.Count -eq 0) { Write-Info "$Name : not registered on $HostIp, skipping"; return $true }
        if ($vm[0].PowerState -ne 'PoweredOn') { Write-Info "$Name : already $($vm[0].PowerState)"; return $true }
        if ($WhatIfMode) { Write-Info "$Name : WHATIF would shut down via its host"; return $true }

        $toolsOk = $false
        try { $toolsOk = ($vm[0].Guest.ExtensionData.ToolsRunningStatus -eq 'guestToolsRunning') }
        catch { Write-Info "$Name : VMware Tools status unreadable ($($_.Exception.Message -replace '\s+', ' ')) -- treating as not running" }
        if ($toolsOk) {
            Write-Info "$Name : guest shutdown requested via its host"
            Stop-VMGuest -VM $vm[0] -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
            $sw = [Diagnostics.Stopwatch]::StartNew()
            while ($sw.Elapsed.TotalSeconds -lt $GraceSeconds) {
                $cur = @(Get-VM -Server $c -Name $Name -ErrorAction SilentlyContinue)
                if ($cur.Count -eq 0 -or $cur[0].PowerState -eq 'PoweredOff') {
                    Write-Ok "$Name : off gracefully after $([int]$sw.Elapsed.TotalSeconds)s"; return $true
                }
                Start-Sleep -Seconds 5
            }
            Write-Warn2 "$Name : still running after ${GraceSeconds}s -- hard power off"
        } else {
            Write-Warn2 "$Name : no VMware Tools -- hard power off"
        }
        # It may have finished shutting down between the last poll and now.
        $now = @(Get-VM -Server $c -Name $Name -ErrorAction SilentlyContinue)
        if ($now.Count -eq 0 -or $now[0].PowerState -eq 'PoweredOff') { Write-Ok "$Name : off"; return $true }
        Stop-VM -VM $now[0] -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Start-Sleep -Seconds 5
        $after = @(Get-VM -Server $c -Name $Name -ErrorAction SilentlyContinue)
        ($after.Count -eq 0 -or $after[0].PowerState -eq 'PoweredOff')
    } catch {
        Write-Fail "$Name : shutdown via $HostIp failed -- $(($_.Exception.Message -replace '\s+', ' '))"
        $false
    } finally {
        if ($c) { Disconnect-VIServer -Server $c -Confirm:$false -ErrorAction SilentlyContinue }
    }
}

# ------------------------------------------------------- license window -------
# VCF 9 licenses must be refreshed at least every 180 days or they are treated
# as expired (hosts disconnect from vCenter, workloads cannot start). In
# connected mode VCF Operations reports usage every 24 h, so a refresh needs
# Operations UP for a day -- a Full start alone proves nothing. The stamp is
# therefore written by Stop-VCFLab, and only when it finds Operations has been
# up for at least LicenseSyncHours before stopping it. Minimal reads its age and
# fails CLOSED: missing, unreadable or future-dated all refuse.

$script:LicenseSyncHours = 24

function Get-LicenseStampPath {
    param([Parameter(Mandatory)][hashtable]$Config)
    Join-Path (Split-Path -Parent $Config.CredentialFile) 'last-license-sync.txt'
}

function Write-LicenseStamp {
    param([Parameter(Mandatory)][hashtable]$Config)
    (Get-Date).ToUniversalTime().ToString('o') | Set-Content -Path (Get-LicenseStampPath -Config $Config) -Encoding utf8
}

<#
    State: Missing | Unreadable | Future | Ok, with Days set only for Ok.
    Never collapses a bad stamp into "no stamp" -- the caller decides, and
    Minimal refuses on anything but Ok.
#>
function Get-LicenseStampAge {
    param([Parameter(Mandatory)][hashtable]$Config)
    $p = Get-LicenseStampPath -Config $Config
    if (-not (Test-Path $p)) { return [pscustomobject]@{ State = 'Missing'; Days = $null; Detail = $p } }
    try {
        $t = [datetime]::Parse((Get-Content -Path $p -Raw).Trim(), $null,
                               [Globalization.DateTimeStyles]::RoundtripKind)
    } catch {
        return [pscustomobject]@{ State = 'Unreadable'; Days = $null; Detail = "$p -- $($_.Exception.Message -replace '\s+', ' ')" }
    }
    $days = [int][math]::Floor(((Get-Date).ToUniversalTime() - $t.ToUniversalTime()).TotalDays)
    if ($days -lt 0) { return [pscustomobject]@{ State = 'Future'; Days = $days; Detail = "$p is dated $($t.ToUniversalTime().ToString('u'))" } }
    [pscustomobject]@{ State = 'Ok'; Days = $days; Detail = $p }
}

# Hours since a VM booted, read from vCenter; $null when unknown.
function Get-VmUptimeHours {
    param([Parameter(Mandatory)][string]$Name)
    try {
        $vm = Get-VM -Name $Name -ErrorAction Stop
        if ($vm.PowerState -ne 'PoweredOn') { return $null }
        $boot = $vm.ExtensionData.Runtime.BootTime
        if (-not $boot) { return $null }
        ((Get-Date).ToUniversalTime() - ([datetime]$boot).ToUniversalTime()).TotalHours
    } catch { $null }
}
