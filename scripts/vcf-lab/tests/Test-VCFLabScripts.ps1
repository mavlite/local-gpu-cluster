<#
    Runs Start-VCFLab.ps1 / Stop-VCFLab.ps1 against stubbed PowerCLI cmdlets.

    Purpose: catch strict-mode violations, null references and ordering bugs
    without needing a live vCenter or a working PowerCLI install. It does NOT
    validate real PowerCLI behaviour -- only that the scripts' own logic runs.

    Invariants asserted:
      1. shutdown stops the VSP control plane BEFORE its workers
      2. neither script ever touches a non-VCF VM
      3. -IncludeHosts REFUSES while a non-VCF VM is running
      4. -IncludeHosts proceeds once only VCF VMs were running
#>
param([string]$ScriptDir = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- sandbox ---
# The scripts dot-source VCFLab.Common.ps1 into their OWN script scope, and
# those definitions shadow anything this harness defines in a parent scope.
# So network helpers cannot be stubbed from here -- a run against the real
# ScriptDir would call the live lab. Work on a copy with overrides appended.
$sandbox = Join-Path $env:TEMP 'vcflab-stub-sandbox'
if (Test-Path $sandbox) { Remove-Item $sandbox -Recurse -Force }
$null = New-Item -ItemType Directory -Path $sandbox
Copy-Item (Join-Path $ScriptDir '*.ps1')  $sandbox
Copy-Item (Join-Path $ScriptDir '*.psd1') $sandbox
Add-Content -Path (Join-Path $sandbox 'VCFLab.Common.ps1') `
            -Value (Get-Content (Join-Path $PSScriptRoot 'stub-overrides.ps1') -Raw) -Encoding utf8

# Dummy credentials inside the sandbox, so the real credential file is never
# read by a test run. CredentialFileRelative resolves against the script dir
# first, so this wins.
$null = New-Item -ItemType Directory -Path (Join-Path $sandbox '.vcflab')
@'
LAB_ESX_USER=root
LAB_ESX_PASS=stub
VCF_SSO_ADMIN_PASS=stub
VCF_NSX_ADMIN_PASS=stub
OPERATIONS_ADMIN_USER=admin
OPERATIONS_ADMIN_PASS=stub
'@ | Set-Content -Path (Join-Path $sandbox '.vcflab\credentials.env') -Encoding utf8

# Start-Sleep is a no-op in a stub run, so a gate that never passes busy-spins
# for its full real-clock timeout. Shrink every timeout in the sandbox config.
$cfgPath = Join-Path $sandbox 'VCFLab.Config.psd1'
$cfgText = Get-Content $cfgPath -Raw
$cfgText = [regex]::Replace($cfgText,
    '(?m)^(\s+(?:HostApi|VsanFormation|VCenterApi|SddcManager|Nsx|Operations|VspControl|VspWorker|Appliance|HostShutdown|LlmWorker)\s+=\s+)\d+',
    '${1}5')
Set-Content -Path $cfgPath -Value $cfgText -Encoding utf8


$ScriptDir = $sandbox

# ------------------------------------------------------------------ stubs ---
# Stub state is GLOBAL, not $script:. A stub called from inside
# Start-VCFLab.ps1 resolves $script: against THAT script's scope, not this
# harness's -- so $script:FakeVMs there is unset, and under the child's
# Set-StrictMode -Version Latest that throws. The throw lands inside the
# gate's own try/catch and is reported as a plain gate failure, which is
# exactly as misleading as it sounds.
$global:FakeVMs = @()
$global:Log     = New-Object System.Collections.Generic.List[string]

# Per-host connection state, keyed the way the scripts address hosts: by IP,
# because both scripts connect with `Connect-VIServer -Server $h.Ip`.
#
# This exists because the previous Get-VMHost stub hard-coded
# ConnectionState='Connected' and the Set-VMHost stub logged the bare string
# "MAINTENANCE" whatever state it was asked for. Entering and exiting were
# therefore indistinguishable to every test, and a cold start that never left
# maintenance mode was invisible to the whole suite.
$global:HostState     = @{}
$global:HostIpByFqdn  = @{}

# Populated HERE, after the declarations above -- doing it up in the sandbox
# section ran before `$global:HostIpByFqdn = @{}` and was silently wiped by it.
# The fakes key host state by IP (that is how the scripts connect) while a VM
# names its host by FQDN, so the mapping is read from the config rather than
# duplicated, and a config change cannot quietly decouple the two.
$sandboxCfg = Import-PowerShellDataFile -Path $cfgPath
foreach ($h in $sandboxCfg.Hosts) { $global:HostIpByFqdn[[string]$h.Name] = [string]$h.Ip }
$global:SandboxHostIps = @($sandboxCfg.Hosts | ForEach-Object { [string]$_.Ip })
function Note { param($m) $global:Log.Add($m) }

function New-FakeVM {
    param($Name,$Power='PoweredOn',$Cpu=4,$Ip='172.16.10.99',$Folder='Discovered virtual machine',
          $Tools='guestToolsRunning',$GuestState='Running',$VMHostName='hyp01.lab.knowledgeondemand.net',
          $BootTime=(Get-Date).ToUniversalTime().AddHours(-48))
    [pscustomobject]@{
        Name = $Name; PowerState = $Power; NumCpu = $Cpu
        # Stop-VCFLab reads Operations' boot time to decide whether its run was
        # long enough (24 h) to count as a VCF license sync.
        ExtensionData = [pscustomobject]@{ Runtime = [pscustomobject]@{ BootTime = $BootTime } }
        # Start-LabVM's host fallback reads VMHost.Name to know where to go.
        # Without this the fallback cannot run and the wedge test is vacuous.
        VMHost = [pscustomobject]@{ Name = $VMHostName }
        Folder = [pscustomobject]@{ Name = $Folder }
        Guest = [pscustomobject]@{
            IPAddress = @($Ip); State = $GuestState
            ExtensionData = [pscustomobject]@{ ToolsRunningStatus = $Tools }
        }
    }
}

function Get-Module { param([switch]$ListAvailable,[string[]]$Name)
    [pscustomobject]@{ Name = 'VMware.VimAutomation.Core'; Version = '13.1.0' } }
function Import-Module { param([Parameter(ValueFromRemainingArguments)]$a) }
function Set-PowerCLIConfiguration { param([Parameter(ValueFromRemainingArguments)]$a) }
function Connect-VIServer {
    param([string]$Server,$Credential,[switch]$Force,[string]$ErrorAction)
    # Minimal mode leaves vCenter down for days; Stop-VCFLab has to cope.
    if ($global:VCenterDown -and $Server -eq '172.16.10.129') {
        throw "Could not resolve the requested VC server. (stub: vCenter is down)"
    }
    if ($global:UnreachableHosts -contains $Server) { throw "stub: host $Server unreachable" }
    [pscustomobject]@{ Name = $Server; IsConnected = $true }
}
$global:UnreachableHosts = @()
$global:FailStart = @()
function Disconnect-VIServer { param([Parameter(ValueFromRemainingArguments)]$a) }
function Get-VMHost {
    param($VM,[string]$Name,$Server,[Parameter(ValueFromRemainingArguments)]$a)
    $key = if ($Server -and $Server.Name) { [string]$Server.Name }
           elseif ($Name)                 { [string]$Name }
           else                           { 'stub-host.lab' }
    $state = if ($global:HostState.ContainsKey($key)) { $global:HostState[$key] } else { 'Connected' }
    [pscustomobject]@{ Name = $key; ConnectionState = $state }
}
function Get-VMHostService { param([Parameter(ValueFromRemainingArguments)]$a)
    ,@([pscustomobject]@{ Key='vpxa'; Running=$true; Policy='on' }) }
function Restart-VMHostService {
    param([Parameter(ValueFromPipeline)]$Service,[Parameter(ValueFromRemainingArguments)]$a)
    process {
        Note 'VPXA-RESTART'
        # Faithful to the real cmdlet: it throws on success, because vpxa drops
        # the connection as it restarts. A test that let it return cleanly would
        # never catch a regression in the code that swallows this.
        throw 'An error occurred while communicating with the remote host.'
    }
}
function Get-VM {
    param([string]$Name,$Server,[string]$ErrorAction)
    # Host-aware: a connection to a HOST sees only the VMs registered on that
    # host, as the real thing does. Without this, a worker started or stopped
    # through the WRONG host would still pass, and a VCF VM would be reported
    # "running" on every host at once.
    $pool = $global:FakeVMs
    if ($Server -and $Server.Name -and $global:SandboxHostIps -contains [string]$Server.Name) {
        $pool = @($global:FakeVMs | Where-Object { $global:HostIpByFqdn[[string]$_.VMHost.Name] -eq [string]$Server.Name })
    }
    if (-not $Name) { return $pool }
    if ($Name -match '\*') { return @($pool | Where-Object { $_.Name -like $Name }) }
    @($pool | Where-Object { $_.Name -eq $Name })
}
function Start-VM { param($VM,$Server,[switch]$Confirm,[string]$ErrorAction)
    # Faithful to ESXi: a host in maintenance mode refuses to power on a VM. A
    # stub that happily started VMs anyway would let a fix that exits
    # maintenance mode too LATE still pass, which is the ordering mistake most
    # worth catching here.
    $hv = $null
    try { $hv = [string]$VM.VMHost.Name } catch { }
    if ($hv) {
        $ip = if ($global:HostIpByFqdn.ContainsKey($hv)) { $global:HostIpByFqdn[$hv] } else { $hv }
        if ($global:HostState.ContainsKey($ip) -and $global:HostState[$ip] -eq 'Maintenance') {
            Note "REFUSED-MAINT $($VM.Name)"
            throw "The operation is not allowed in the current state. The host is in maintenance mode."
        }
    }
    if ($global:FailStart -contains $VM.Name) { Note "STARTFAIL $($VM.Name)"; throw "stub: power-on of $($VM.Name) failed" }
    Note "START $($VM.Name)"; ($global:FakeVMs | Where-Object Name -eq $VM.Name) | ForEach-Object { $_.PowerState='PoweredOn' } }
function Stop-VM { param($VM,[switch]$Confirm,[string]$ErrorAction)
    Note "HARDSTOP $($VM.Name)"; ($global:FakeVMs | Where-Object Name -eq $VM.Name) | ForEach-Object { $_.PowerState='PoweredOff' } }
function Stop-VMGuest { param($VM,[switch]$Confirm,[string]$ErrorAction)
    Note "GUESTSTOP $($VM.Name)"; ($global:FakeVMs | Where-Object Name -eq $VM.Name) | ForEach-Object { $_.PowerState='PoweredOff' } }
function Get-EsxCli {
    # .Invoke() must be a real METHOD, not a ScriptBlock property: a property
    # returns a collection and [int]$st.SubClusterMemberCount then yields 0,
    # which silently fails the vSAN gate and spins it to its full timeout.
    param([Parameter(ValueFromRemainingArguments)]$a)
    $get = New-Object psobject
    $get | Add-Member -MemberType ScriptMethod -Name Invoke -Value { [pscustomobject]@{ SubClusterMemberCount = 3 } }
    [pscustomobject]@{ vsan = [pscustomobject]@{ cluster = [pscustomobject]@{ get = $get } } }
}
function Get-Cluster { param([Parameter(ValueFromRemainingArguments)]$a)
    [pscustomobject]@{ Name='lab01-cluster-001'; HAEnabled=$true; DrsAutomationLevel='FullyAutomated' } }
function Set-Cluster { param([Parameter(ValueFromRemainingArguments)]$a) Note "Set-Cluster" }
function Set-VMHost {
    # No [Parameter()] attribute anywhere in this param block, deliberately:
    # a single [Parameter(...)] makes the function ADVANCED, PowerShell then
    # supplies -ErrorAction as a common parameter, and the explicit
    # [string]$ErrorAction below collides with it --
    #   "A parameter with the name 'ErrorAction' was defined multiple times"
    # which surfaced as "could not exit maintenance mode" and, in the shutdown
    # path, as hosts never powering off at all.
    param($VMHost,[string]$State,$VsanDataMigrationMode,[switch]$Confirm,[string]$ErrorAction)
    $key = if ($VMHost -and $VMHost.Name) { [string]$VMHost.Name } else { 'stub-host.lab' }
    if ($State) { $global:HostState[$key] = $State }
    # Record the TARGET state, so entering and exiting maintenance are
    # distinguishable in the log.
    Note "SETHOSTSTATE $key $State"
}
function Stop-VMHost { param([Parameter(ValueFromRemainingArguments)]$a) Note "HOSTOFF" }

# make gates resolve instantly
function Test-TcpPort { param([Parameter(ValueFromRemainingArguments)]$a) $true }
function Start-Sleep  { param([Parameter(ValueFromRemainingArguments)]$a) }

# REST gates always pass
function Invoke-LabRest { param([Parameter(ValueFromRemainingArguments)]$a)
    [pscustomobject]@{
        accessToken='x'; token='x'
        control_cluster_status=[pscustomobject]@{status='STABLE'}
        mgmt_cluster_status   =[pscustomobject]@{status='STABLE'}
        detailed_cluster_status=[pscustomobject]@{
            overall_status='STABLE'
            groups=@(
                [pscustomobject]@{ group_type='MANAGER';    status='STABLE' },
                [pscustomobject]@{ group_type='POLICY';     status='STABLE' },
                [pscustomobject]@{ group_type='CONTROLLER'; status='STABLE' })
        }
        results=@(
            [pscustomobject]@{ state='success' },
            [pscustomobject]@{ state='success' },
            [pscustomobject]@{ state='success' })
    } }

$script:Foreign = @('devvm01','devvm02','devvm03','truenas-backup')
# LLM worker VMs live on LOCAL datastores, so each is pinned to the host named
# here -- the same pairing as LlmWorkers in VCFLab.Config.psd1.
$script:Workers = [ordered]@{
    'llmbench01' = 'hyp02.lab.knowledgeondemand.net'
    'llmbench02' = 'hyp01.lab.knowledgeondemand.net'
    'llmbench03' = 'hyp03.lab.knowledgeondemand.net'
}
$script:VcfNames = @('vcsa','sddc-manager','nsxa','ops','licsrv','opscollector',
                     'platform-dlq9c','platform-rptxg','platform-62t8n','platform-pfnmx','platform-c5t7j','platform-8n62z')
$global:VCenterDown = $false
$script:StampPath = Join-Path $sandbox '.vcflab\last-license-sync.txt'
function Set-Stamp { param([int]$DaysAgo)
    (Get-Date).ToUniversalTime().AddDays(-$DaysAgo).ToString('o') | Set-Content -Path $script:StampPath -Encoding utf8 }

function Reset-Fleet {
    param([string]$Power='PoweredOff',[string]$ForeignPower='PoweredOn',[string]$WorkerPower='PoweredOff')
    $f = 'vcf-management-services'
    $global:FakeVMs = @(
        (New-FakeVM 'vcsa'           $Power 4  '172.16.10.129' $f)
        (New-FakeVM 'sddc-manager'   $Power 4  '172.16.10.133' $f)
        (New-FakeVM 'nsxa'           $Power 6  '172.16.10.132' $f)
        (New-FakeVM 'ops'            $Power 2  '172.16.10.122' $f)
        (New-FakeVM 'licsrv'         $Power 2  '172.16.10.134' $f)
        (New-FakeVM 'opscollector'   $Power 4  '172.16.10.123' $f)
        (New-FakeVM 'platform-dlq9c' $Power 4  '172.16.10.162' $f)
        (New-FakeVM 'platform-rptxg' $Power 4  '172.16.10.165' $f)
        (New-FakeVM 'platform-62t8n' $Power 4  '172.16.10.166' $f)
        (New-FakeVM 'platform-pfnmx' $Power 10 '172.16.10.164' $f)
        (New-FakeVM 'platform-c5t7j' $Power 10 '172.16.10.167' $f)
        (New-FakeVM 'platform-8n62z' $Power 10 '172.16.10.163' $f)
        (New-FakeVM 'devvm01'        $ForeignPower 8 '172.16.70.10')
        (New-FakeVM 'devvm02'        $ForeignPower 8 '172.16.71.10')
        (New-FakeVM 'devvm03'        $ForeignPower 8 '172.16.72.10')
        (New-FakeVM 'truenas-backup' $ForeignPower 2 '172.16.10.200')
    )
    $i = 205
    foreach ($w in $script:Workers.Keys) {
        $global:FakeVMs += New-FakeVM -Name $w -Power $WorkerPower -Cpu 16 -Ip "172.16.10.$i" -VMHostName $script:Workers[$w]
        $i++
    }
    # Hosts start out of maintenance unless a test says otherwise, so the
    # existing cases keep their previous meaning.
    $global:HostState = @{}
    $global:VCenterDown = $false
    $global:UnreachableHosts = @()
    $global:FailStart = @()
    $global:Log.Clear()
}
function Get-Started { @($global:Log | Where-Object { $_ -like 'START *' } | ForEach-Object { $_ -replace '^START ','' }) }
function Get-Stopped { @($global:Log | Where-Object { $_ -match '^(GUESTSTOP|HARDSTOP) ' } | ForEach-Object { ($_ -split ' ',2)[1] }) }
function Invoke-Lab { param([string]$Script,[hashtable]$Arg = @{})
    # Records the exit code in $global:LastExit, so a refusal test can tell an
    # intended `exit 1` from a script that crashed early (-1 here).
    $o = @()
    $global:LASTEXITCODE = 0
    try { $o = & (Join-Path $ScriptDir $Script) @Arg -ErrorAction Stop *>&1; $global:LastExit = $LASTEXITCODE }
    catch { $o += "SCRIPT ERROR: $($_.Exception.Message)"; $global:LastExit = -1 }
    $o | Select-Object -Last 6 | ForEach-Object { "   $_" } | Write-Host
    ($o | ForEach-Object { "$_" }) -join "`n"
}

function Get-Touched {
    @($global:Log | Where-Object { $_ -match '^(START|GUESTSTOP|HARDSTOP) ' } |
        ForEach-Object { ($_ -split ' ',2)[1] })
}

Push-Location $ScriptDir
$script:fail = 0
function Check { param($Label,$Cond)
    if ($Cond) { Write-Host "   PASS  $Label" -ForegroundColor Green }
    else { Write-Host "   FAIL  $Label" -ForegroundColor Red; $script:fail++ } }

# ---------------------------------------------------------------- Start -----
Write-Host "`n########## Start-VCFLab.ps1 ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOn'
try { & (Join-Path $ScriptDir 'Start-VCFLab.ps1') -ErrorAction Stop *>&1 | Select-Object -Last 6 | ForEach-Object { "   $_" } | Write-Host }
catch { Write-Host "   SCRIPT ERROR: $($_.Exception.Message)" -ForegroundColor Red; $script:fail++ }
$startOrder = @($global:Log | Where-Object { $_ -like 'START *' } | ForEach-Object { $_ -replace '^START ','' })
Write-Host "   power-on order: $($startOrder -join ' -> ')" -ForegroundColor Gray
Check "start touches no non-VCF VM" (@(Get-Touched | Where-Object { $script:Foreign -contains $_ }).Count -eq 0)
Check "start powers on vcsa" ($startOrder -contains 'vcsa')

# ------------------------------------------------- Stop (hosts left alone) --
Write-Host "`n########## Stop-VCFLab.ps1 (default: hosts left up) ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOn'
try { & (Join-Path $ScriptDir 'Stop-VCFLab.ps1') -ErrorAction Stop *>&1 | Select-Object -Last 6 | ForEach-Object { "   $_" } | Write-Host }
catch { Write-Host "   SCRIPT ERROR: $($_.Exception.Message)" -ForegroundColor Red; $script:fail++ }
$stopOrder = @($global:Log | Where-Object { $_ -like 'GUESTSTOP *' -or $_ -like 'HARDSTOP *' } |
                ForEach-Object { $_ -replace '^(GUESTSTOP|HARDSTOP) ','' })
Write-Host "   shutdown order: $($stopOrder -join ' -> ')" -ForegroundColor Gray
Check "shutdown touches no non-VCF VM" (@(Get-Touched | Where-Object { $script:Foreign -contains $_ }).Count -eq 0)
Check "no host powered off without -IncludeHosts" (-not ($global:Log -contains 'HOSTOFF'))

$cp = @('platform-dlq9c','platform-rptxg','platform-62t8n')
$wk = @('platform-pfnmx','platform-c5t7j','platform-8n62z')
$lastCp  = ($cp | ForEach-Object { [array]::IndexOf($stopOrder,$_) } | Measure-Object -Maximum).Maximum
$firstWk = ($wk | ForEach-Object { [array]::IndexOf($stopOrder,$_) } | Where-Object { $_ -ge 0 } | Measure-Object -Minimum).Minimum
Check "control plane stops before workers (lastCP=$lastCp firstWorker=$firstWk)" ($lastCp -ge 0 -and $firstWk -ge 0 -and $lastCp -lt $firstWk)

# ------------------------------------ Stop -IncludeHosts with foreign VMs ---
Write-Host "`n########## Stop-VCFLab.ps1 -IncludeHosts (foreign VMs running) ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOn'
$out = @()
try { $out = & (Join-Path $ScriptDir 'Stop-VCFLab.ps1') -IncludeHosts -ErrorAction Stop *>&1 }
catch { Write-Host "   SCRIPT ERROR: $($_.Exception.Message)" -ForegroundColor Red; $script:fail++ }
$out | Select-Object -Last 8 | ForEach-Object { "   $_" } | Write-Host
Check "refuses rather than stopping non-VCF VMs" (@(Get-Touched | Where-Object { $script:Foreign -contains $_ }).Count -eq 0)
Check "no host powered off while foreign VMs run" (-not ($global:Log -contains 'HOSTOFF'))
Check "names the blockers" (($out -join "`n") -match 'devvm01')

# --------------------------------------- Stop -IncludeHosts, VCF-only lab ---
Write-Host "`n########## Stop-VCFLab.ps1 -IncludeHosts (nothing foreign running) ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOff'
try { & (Join-Path $ScriptDir 'Stop-VCFLab.ps1') -IncludeHosts -ErrorAction Stop *>&1 | Select-Object -Last 6 | ForEach-Object { "   $_" } | Write-Host }
catch { Write-Host "   SCRIPT ERROR: $($_.Exception.Message)" -ForegroundColor Red; $script:fail++ }
Check "still never touches a powered-off foreign VM" (@(Get-Touched | Where-Object { $script:Foreign -contains $_ }).Count -eq 0)
Check "hosts powered off when the way is clear" ($global:Log -contains 'HOSTOFF')

# --- cold start after a -IncludeHosts shutdown: hosts are STILL in maintenance -
# Stop-VCFLab enters maintenance mode before powering hosts off, and ESXi
# PERSISTS maintenance mode across a reboot. So the state a cold start actually
# meets is: hosts up, in maintenance, no VMs running. Observed live on
# 2026-10-02 -- all three hosts Maintenance, 0 powered-on VMs, every VCF
# appliance unreachable.
Write-Host "`
########## Start-VCFLab.ps1 (hosts left in maintenance by a prior shutdown) ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
foreach ($ip in $global:SandboxHostIps) { $global:HostState[$ip] = 'Maintenance' }
try { & (Join-Path $ScriptDir 'Start-VCFLab.ps1') -ErrorAction Stop *>&1 | Select-Object -Last 6 | ForEach-Object { "   $_" } | Write-Host }
catch { Write-Host "   SCRIPT ERROR: $($_.Exception.Message)" -ForegroundColor Red }
$stillMaint = @($global:SandboxHostIps | Where-Object { $global:HostState[$_] -eq 'Maintenance' })
$exited     = @($global:Log | Where-Object { $_ -match '^SETHOSTSTATE \S+ Connected$' })
Check "cold start takes every host OUT of maintenance (still in: $($stillMaint.Count))" ($stillMaint.Count -eq 0)
Check "cold start records an explicit exit per host (got $($exited.Count))" ($exited.Count -ge $global:SandboxHostIps.Count)
# The consequence, not just the call: nothing can start while a host is in
# maintenance, so vCenter coming up is the proof the exit happened in time.
Check "vcsa powers on despite the prior maintenance state" (@($global:Log | Where-Object { $_ -eq 'START vcsa' }).Count -gt 0)
Check "no power-on was refused for maintenance mode" (@($global:Log | Where-Object { $_ -like 'REFUSED-MAINT *' }).Count -eq 0)
Check "maintenance exit still touches no non-VCF VM" (@(Get-Touched | Where-Object { $script:Foreign -contains $_ }).Count -eq 0)

# =========================== Minimal mode (hosts + LLM workers) ===============
# Day-to-day the VCF stack is OFF; only the hosts and the CPU LLM workers run.
# The workers sit on local datastores and an ephemeral-binding port group, so
# they need neither vSAN nor vCenter -- the script powers them on host-direct.
# Refusals assert the EXIT CODE as well as "nothing happened": a script that
# crashed early also starts nothing, and must not pass as a refusal.

$workerNames = @($script:Workers.Keys)
function Get-WorkersStarted { @(Get-Started | Where-Object { $workerNames -contains $_ }) }

Write-Host "`n########## Start-VCFLab.ps1 -Mode Full (default) leaves LLM workers alone ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
Remove-Item $script:StampPath -ErrorAction SilentlyContinue
$null = Invoke-Lab 'Start-VCFLab.ps1'
Check "full mode exits 0" ($global:LastExit -eq 0)
Check "full mode starts no LLM worker" ((Get-WorkersStarted).Count -eq 0)
Check "a Full start alone records NO license sync (Operations must run 24 h)" (-not (Test-Path $script:StampPath))

Write-Host "`n########## Start-VCFLab.ps1 -Mode Full while LLM workers run ##########" -ForegroundColor Magenta
# Workers pin 24 GB of DRAM per host; with the management stack up that pushes
# vcsa/nsxa/sddc-manager onto the consumer tier drives (ops review C2). Full
# must refuse -- not warn after the fact.
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
$out = Invoke-Lab 'Start-VCFLab.ps1'
Check "Full refuses while workers run (exit 1)" ($global:LastExit -eq 1)
Check "  ...before powering on any VCF component" (@(Get-Started | Where-Object { $script:VcfNames -contains $_ }).Count -eq 0)
Check "  ...without touching the workers" ((Get-Stopped).Count -eq 0)
Check "  ...and names -StopLlmWorkers" ($out -match 'StopLlmWorkers')

Write-Host "`n########## Start-VCFLab.ps1 -Mode Full -StopLlmWorkers ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
$null = Invoke-Lab 'Start-VCFLab.ps1' @{ StopLlmWorkers = $true }
$stopped = Get-Stopped
Check "-StopLlmWorkers stops every worker first" (@($workerNames | Where-Object { $stopped -notcontains $_ }).Count -eq 0)
Check "  ...then the Full start proceeds (vcsa on, exit 0)" ($global:LastExit -eq 0 -and (Get-Started) -contains 'vcsa')
$logArr = @($global:Log); $firstStart = -1; $lastWorkerStop = -1
for ($i = 0; $i -lt $logArr.Count; $i++) {
    if ($firstStart -lt 0 -and $logArr[$i] -like 'START *') { $firstStart = $i }
    if ($logArr[$i] -match '^(GUESTSTOP|HARDSTOP) llmbench') { $lastWorkerStop = $i }
}
Check "  ...workers are down before anything starts" ($lastWorkerStop -ge 0 -and $firstStart -gt $lastWorkerStop)

Write-Host "`n########## Start-VCFLab.ps1 -Mode Minimal (cold, hosts in maintenance) ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
foreach ($ip in $global:SandboxHostIps) { $global:HostState[$ip] = 'Maintenance' }
Set-Stamp -DaysAgo 1
$out = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal' }
$st = Get-Started
Check "minimal exits 0" ($global:LastExit -eq 0)
foreach ($w in $workerNames) { Check "minimal powers on $w" ($st -contains $w) }
Check "minimal starts no VCF component" (@($st | Where-Object { $script:VcfNames -contains $_ }).Count -eq 0)
Check "minimal takes every host out of maintenance" (@($global:SandboxHostIps | Where-Object { $global:HostState[$_] -eq 'Maintenance' }).Count -eq 0)
Check "minimal had no power-on refused for maintenance" (@($global:Log | Where-Object { $_ -like 'REFUSED-MAINT *' }).Count -eq 0)
Check "minimal touches no non-VCF, non-worker VM" (@(Get-Touched | Where-Object { $script:Foreign -contains $_ }).Count -eq 0)
Check "a fresh license sync does NOT warn (negative control)" ($out -notmatch 'Plan a Full run')

Write-Host "`n########## Start-VCFLab.ps1 -Mode Minimal -WhatIf ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
foreach ($ip in $global:SandboxHostIps) { $global:HostState[$ip] = 'Maintenance' }
Set-Stamp -DaysAgo 1
$null = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal'; WhatIf = $true }
Check "minimal -WhatIf exits 0" ($global:LastExit -eq 0)
Check "minimal -WhatIf powers nothing on" ((Get-Started).Count -eq 0)
Check "minimal -WhatIf leaves maintenance mode alone" (@($global:Log | Where-Object { $_ -like 'SETHOSTSTATE *' }).Count -eq 0)

Write-Host "`n########## Start-VCFLab.ps1 -Mode Minimal while the VCF stack runs ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOff'
foreach ($ip in $global:SandboxHostIps) { $global:HostState[$ip] = 'Maintenance' }
Set-Stamp -DaysAgo 1
$out = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal' }
Check "refuses with exit 1" ($global:LastExit -eq 1)
Check "starts no worker beside a running VCF stack" ((Get-WorkersStarted).Count -eq 0)
Check "never stops VCF components itself" ((Get-Stopped).Count -eq 0)
Check "refuses BEFORE touching maintenance mode" (@($global:Log | Where-Object { $_ -like 'SETHOSTSTATE *' }).Count -eq 0)
Check "says to run Stop-VCFLab first" ($out -match 'Stop-VCFLab')

Write-Host "`n########## Start-VCFLab.ps1 -Mode Minimal with a host unreachable ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
Set-Stamp -DaysAgo 1
$global:UnreachableHosts = @($global:SandboxHostIps[0])
$null = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal' }
Check "an unqueryable host is never assumed clear (non-zero exit)" ($global:LastExit -ne 0)
Check "  ...and no worker is started" ((Get-WorkersStarted).Count -eq 0)
$global:UnreachableHosts = @()

Write-Host "`n########## Start-VCFLab.ps1 -Mode Minimal, one worker fails to power on ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
Set-Stamp -DaysAgo 1
$global:FailStart = @('llmbench02')
$out = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal' }
Check "a failed worker makes the run exit 1" ($global:LastExit -eq 1)
Check "  ...the other workers still start" (((Get-WorkersStarted) -contains 'llmbench01') -and ((Get-WorkersStarted) -contains 'llmbench03'))
Check "  ...and the failed one is named" ($out -match 'Workers not up: .*llmbench02')

Write-Host "`n########## Start-VCFLab.ps1 -Mode Minimal -WithVCenter ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
Set-Stamp -DaysAgo 1
$null = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal'; WithVCenter = $true }
$st = Get-Started
Check "-WithVCenter exits 0" ($global:LastExit -eq 0)
Check "-WithVCenter starts vcsa" ($st -contains 'vcsa')
Check "-WithVCenter still starts the workers" (@($workerNames | Where-Object { $st -notcontains $_ }).Count -eq 0)
Check "-WithVCenter starts no other VCF component" (@($st | Where-Object { $script:VcfNames -contains $_ -and $_ -ne 'vcsa' }).Count -eq 0)

Write-Host "`n########## Start-VCFLab.ps1 -Mode Minimal, license window (fails closed) ##########" -ForegroundColor Magenta
$cases = @(
    @{ Label = '160 days old';  Prep = { Set-Stamp -DaysAgo 160 }; Why = 'lapse' }
    @{ Label = 'missing';       Prep = { Remove-Item $script:StampPath -ErrorAction SilentlyContinue }; Why = 'No license sync is recorded' }
    @{ Label = 'unreadable';    Prep = { 'not-a-date' | Set-Content -Path $script:StampPath -Encoding utf8 }; Why = 'cannot be read' }
    @{ Label = 'future-dated';  Prep = { Set-Stamp -DaysAgo -10 }; Why = 'in the future' }
)
foreach ($c in $cases) {
    Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
    & $c.Prep
    $out = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal' }
    Check "stamp $($c.Label): refuses with exit 1" ($global:LastExit -eq 1)
    Check "stamp $($c.Label): nothing started" ((Get-Started).Count -eq 0)
    Check "stamp $($c.Label): says why" ($out -match [regex]::Escape($c.Why))
}
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
Remove-Item $script:StampPath -ErrorAction SilentlyContinue
$null = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal'; IgnoreLicenseWindow = $true }
Check "-IgnoreLicenseWindow overrides a missing stamp" ($global:LastExit -eq 0 -and (Get-WorkersStarted).Count -eq $workerNames.Count)
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
Set-Stamp -DaysAgo 40
$out = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal' }
Check "40 days: still starts the workers" ($global:LastExit -eq 0 -and (Get-WorkersStarted).Count -eq $workerNames.Count)
Check "40 days: warns to plan a Full run" ($out -match 'Plan a Full run')

Write-Host "`n########## Stop-VCFLab.ps1 (default) with LLM workers running ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
Remove-Item $script:StampPath -ErrorAction SilentlyContinue
$null = Invoke-Lab 'Stop-VCFLab.ps1'
Check "default stop leaves the LLM workers running (full -> minimal)" (@(Get-Stopped | Where-Object { $workerNames -contains $_ }).Count -eq 0)
Check "default stop still stops the VCF stack" ((Get-Stopped) -contains 'nsxa')
Check "Operations up 48 h: license sync recorded" (Test-Path $script:StampPath)

Write-Host "`n########## Stop-VCFLab.ps1 (default), Operations up only 1 h ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOff'
($global:FakeVMs | Where-Object Name -eq 'ops').ExtensionData.Runtime.BootTime = (Get-Date).ToUniversalTime().AddHours(-1)
Remove-Item $script:StampPath -ErrorAction SilentlyContinue
$null = Invoke-Lab 'Stop-VCFLab.ps1'
Check "Operations up 1 h: NO license sync recorded" (-not (Test-Path $script:StampPath))

Write-Host "`n########## Stop-VCFLab.ps1 -WhatIf ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
Remove-Item $script:StampPath -ErrorAction SilentlyContinue
$null = Invoke-Lab 'Stop-VCFLab.ps1' @{ WhatIf = $true; IncludeHosts = $true }
Check "stop -WhatIf stops nothing" ((Get-Stopped).Count -eq 0)
Check "stop -WhatIf powers off no host" (-not ($global:Log -contains 'HOSTOFF'))
Check "stop -WhatIf writes no license stamp" (-not (Test-Path $script:StampPath))

Write-Host "`n########## Stop-VCFLab.ps1 -IncludeHosts with LLM workers running ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOn' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
$null = Invoke-Lab 'Stop-VCFLab.ps1' @{ IncludeHosts = $true }
Check "-IncludeHosts stops every LLM worker" (@($workerNames | Where-Object { (Get-Stopped) -notcontains $_ }).Count -eq 0)
Check "-IncludeHosts treats workers as owned, not blockers (hosts go down)" ($global:Log -contains 'HOSTOFF')

Write-Host "`n########## Stop-VCFLab.ps1 from Minimal mode (vCenter down) ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
$global:VCenterDown = $true
$out = Invoke-Lab 'Stop-VCFLab.ps1'
Check "default stop in Minimal: nothing to do, exit 0" ($global:LastExit -eq 0 -and $out -match 'nothing to stop')
Check "  ...workers and hosts untouched" ((Get-Stopped).Count -eq 0 -and -not ($global:Log -contains 'HOSTOFF'))

Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
$global:VCenterDown = $true
$null = Invoke-Lab 'Stop-VCFLab.ps1' @{ IncludeHosts = $true }
Check "vCenter down: workers stopped via their hosts" (@($workerNames | Where-Object { (Get-Stopped) -notcontains $_ }).Count -eq 0)
Check "vCenter down: hosts still powered off" ($global:Log -contains 'HOSTOFF')

Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff' -WorkerPower 'PoweredOn'
($global:FakeVMs | Where-Object Name -eq 'nsxa').PowerState = 'PoweredOn'
$global:VCenterDown = $true
$out = Invoke-Lab 'Stop-VCFLab.ps1' @{ IncludeHosts = $true }
Check "vCenter down + a VCF VM still up: exit 1" ($global:LastExit -eq 1)
Check "  ...no host powered off" (-not ($global:Log -contains 'HOSTOFF'))
Check "  ...nothing stopped behind vCenter's back" ((Get-Stopped).Count -eq 0)
Check "  ...the VCF VM is named on its own host only" ($out -match 'nsxa\s+on hyp01' -and $out -notmatch 'nsxa\s+on hyp0[23]')
$global:VCenterDown = $false

Write-Host "`n########## config validation (fails at load, names the problem) ##########" -ForegroundColor Magenta
$badCfg = Join-Path $sandbox 'bad.psd1'
$t = (Get-Content $cfgPath -Raw) -replace "@\{ VmName = 'llmbench03'; Host = 'hyp03' \}", "@{ VmName = 'llmbench01'; Host = 'hyp03' }"
Set-Content -Path $badCfg -Value $t -Encoding utf8
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
$out = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal'; ConfigPath = $badCfg }
Check "duplicate worker rejected at load" ($out -match 'listed twice' -and (Get-Started).Count -eq 0)
$t = (Get-Content $cfgPath -Raw) -replace 'RefuseDays\s*=\s*\d+', 'RefuseDays = 200'
Set-Content -Path $badCfg -Value $t -Encoding utf8
$out = Invoke-Lab 'Start-VCFLab.ps1' @{ Mode = 'Minimal'; ConfigPath = $badCfg }
Check "RefuseDays beyond 180 rejected at load" ($out -match 'RefuseDays < 180')

# --- 7. vCenter cannot power on: host fallback + vpxa repair -----------------
Write-Host "`n########## Start-VCFLab.ps1 (vCenter power-on wedged) ##########" -ForegroundColor Magenta
Reset-Fleet -Power 'PoweredOff' -ForeignPower 'PoweredOff'
# vCenter's Start-VM fails for everything; the per-host connection still works.
# $global:HostSideStart records that the fallback path actually ran.
$global:HostSideStart = New-Object System.Collections.Generic.List[string]
function Start-VM {
    param($VM,$Server,[switch]$Confirm,[string]$ErrorAction)
    if ($Server -and $Server.Name -ne '172.16.10.129') {
        $global:HostSideStart.Add($VM.Name)
        Note "HOSTSTART $($VM.Name)"
        ($global:FakeVMs | Where-Object Name -eq $VM.Name) | ForEach-Object { $_.PowerState='PoweredOn' }
        return
    }
    throw "The object 'vpx.vmprov.PowerOnVm:vpx.vmprov.PowerOnVm' has already been deleted or has not been completely created"
}
try { & (Join-Path $ScriptDir 'Start-VCFLab.ps1') -ErrorAction Stop *>&1 | Select-Object -Last 4 | ForEach-Object { "   $_" } | Write-Host }
catch { Write-Host "   SCRIPT ERROR: $($_.Exception.Message)" -ForegroundColor Red; $script:fail++ }
Check "falls back to the host when vCenter refuses" ($global:HostSideStart.Count -gt 0) "no host-side start recorded"
Check "restarts vpxa after a fallback" ($global:Log -contains 'VPXA-RESTART')
Check "the throw from Restart-VMHostService is not fatal" ($global:HostSideStart.Count -gt 1) "stopped after the first VM"
Check "still no non-VCF VM touched" (@(Get-Touched | Where-Object { $script:Foreign -contains $_ }).Count -eq 0)

Pop-Location
Write-Host "`n########## failures: $($script:fail) ##########" -ForegroundColor $(if($script:fail){'Red'}else{'Green'})
exit $script:fail
