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
    '(?m)^(\s+(?:HostApi|VsanFormation|VCenterApi|SddcManager|Nsx|Operations|VspControl|VspWorker|Appliance|HostShutdown)\s+=\s+)\d+',
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
function Note { param($m) $global:Log.Add($m) }

function New-FakeVM {
    param($Name,$Power='PoweredOn',$Cpu=4,$Ip='172.16.10.99',$Folder='Discovered virtual machine',
          $Tools='guestToolsRunning',$GuestState='Running',$VMHostName='hyp01.lab.knowledgeondemand.net')
    [pscustomobject]@{
        Name = $Name; PowerState = $Power; NumCpu = $Cpu
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
    [pscustomobject]@{ Name = $Server; IsConnected = $true }
}
function Disconnect-VIServer { param([Parameter(ValueFromRemainingArguments)]$a) }
function Get-VMHost {
    param($VM,[string]$Name,[Parameter(ValueFromRemainingArguments)]$a)
    [pscustomobject]@{ Name=$(if($Name){$Name}else{'stub-host.lab'}); ConnectionState='Connected' }
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
    if (-not $Name) { return $global:FakeVMs }
    if ($Name -match '\*') { return @($global:FakeVMs | Where-Object { $_.Name -like $Name }) }
    @($global:FakeVMs | Where-Object { $_.Name -eq $Name })
}
function Start-VM { param($VM,[switch]$Confirm,[string]$ErrorAction)
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
function Set-VMHost  { param([Parameter(ValueFromRemainingArguments)]$a) Note "MAINTENANCE" }
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

function Reset-Fleet {
    param([string]$Power='PoweredOff',[string]$ForeignPower='PoweredOn')
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
    $global:Log.Clear()
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
