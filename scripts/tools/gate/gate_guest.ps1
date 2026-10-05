<#
    Phase-1 gate: run a command in, upload to, or download from an llmbenchNN worker VM through
    vSphere GuestOperations. The workers have no SSH; this is the only control path.

      gate_guest.ps1 -VmName llmbench01 -Exec 'bash /opt/gate/worker_serve.sh status'
      gate_guest.ps1 -VmName llmbench01 -Put  C:\path\worker_serve.sh -Remote /tmp/worker_serve.sh
      gate_guest.ps1 -VmName llmbench01 -Fetch /var/log/gate/server-....log -OutFile C:\path\x.log

    Connects to vCenter when it answers, otherwise straight to the worker's ESXi host (Minimal
    mode has vCenter off). Transfer URLs are rewritten to the host IP because lab DNS does not
    resolve from the workstation over the VPN. Invoke-VMScript is not used for the same reason.

    Needs the vcf-lab scripts (branch feat/9-1-1-right-sized-capacity) for config and credentials:
    -VCFLabRoot or $env:VCFLAB_ROOT. The guest password is LLM_BENCH_GUEST_PASS in the vcf-lab
    credentials file; it is never printed or passed on a command line.
#>
[CmdletBinding(DefaultParameterSetName = 'Exec')]
param(
    [Parameter(Mandatory)][ValidatePattern('^llmbench0[1-3]$')][string]$VmName,
    [Parameter(Mandatory, ParameterSetName = 'Exec')][string]$Exec,
    [Parameter(ParameterSetName = 'Exec')][int]$TimeoutSec = 120,
    [Parameter(Mandatory, ParameterSetName = 'Put')][string]$Put,
    [Parameter(Mandatory, ParameterSetName = 'Put')][string]$Remote,
    [Parameter(Mandatory, ParameterSetName = 'Fetch')][string]$Fetch,
    [Parameter(Mandatory, ParameterSetName = 'Fetch')][string]$OutFile,
    [string]$VCFLabRoot = $env:VCFLAB_ROOT
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $VCFLabRoot -or -not (Test-Path (Join-Path $VCFLabRoot 'VCFLab.Common.ps1'))) {
    throw 'Set -VCFLabRoot (or $env:VCFLAB_ROOT) to the scripts/vcf-lab directory.'
}
. (Join-Path $VCFLabRoot 'VCFLab.Common.ps1')
$cfg  = Import-VCFLabConfig -Path (Join-Path $VCFLabRoot 'VCFLab.Config.psd1')
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
if (-not $cred.ContainsKey('LLM_BENCH_GUEST_PASS')) { throw "LLM_BENCH_GUEST_PASS missing from $($cfg.CredentialFile)" }
Disable-CertificateValidation
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Initialize-PowerCLI

if (Test-TcpPort -ComputerName $cfg.VCenter.Ip -Port 443 -TimeoutMs 3000) {
    $sso = Get-VCFLabCredentialObject -Map $cred -UserKey $null -PassKey 'VCF_SSO_ADMIN_PASS' -DefaultUser 'administrator@vsphere.local'
    $vc = Connect-VIServer -Server $cfg.VCenter.Ip -Credential $sso -Force
    $vm = Get-VM -Server $vc -Name $VmName
    $hostIp = ($cfg.Hosts | Where-Object { $_.Name -eq (Get-VMHost -Server $vc -Id $vm.VMHostId).Name }).Ip
} else {
    $wk = @(Get-LlmWorkers -Config $cfg | Where-Object { $_.VmName -eq $VmName })
    if ($wk.Count -ne 1) { throw "vCenter is down and $VmName is not a configured LlmWorker" }
    $esx = Get-VCFLabCredentialObject -Map $cred -UserKey 'LAB_ESX_USER' -PassKey 'LAB_ESX_PASS' -DefaultUser 'root'
    $hostIp = $wk[0].HostIp
    $vc = Connect-VIServer -Server $hostIp -Credential $esx -Force
    $vm = Get-VM -Server $vc -Name $VmName
}

try {
    $auth = New-Object VMware.Vim.NamePasswordAuthentication
    $auth.Username = 'bench'
    $auth.Password = $cred['LLM_BENCH_GUEST_PASS']
    $auth.InteractiveSession = $false
    $gom = Get-View -Server $vc $vc.ExtensionData.Content.GuestOperationsManager
    $pm  = Get-View -Server $vc $gom.ProcessManager
    $fm  = Get-View -Server $vc $gom.FileManager
    $moref = $vm.ExtensionData.MoRef

    function Get-GuestBytes([string]$Path) {
        $info = $fm.InitiateFileTransferFromGuest($moref, $auth, $Path)
        $url = $info.Url -replace '^https://[^/]+/', "https://$hostIp/"
        $c = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 300).Content
        if ($c -is [string]) { $c = [Text.Encoding]::UTF8.GetBytes($c) }
        # Unary comma: a returned byte[] is otherwise unrolled into object[] by the pipeline.
        return ,[byte[]]$c
    }

    switch ($PSCmdlet.ParameterSetName) {
        'Put' {
            $bytes = [IO.File]::ReadAllBytes($Put)
            $attr = New-Object VMware.Vim.GuestFileAttributes
            $url = $fm.InitiateFileTransferToGuest($moref, $auth, $Remote, $attr, [long]$bytes.Length, $true)
            $url = $url -replace '^https://[^/]+/', "https://$hostIp/"
            $null = Invoke-WebRequest -Uri $url -Method Put -Body $bytes -ContentType 'application/octet-stream' -UseBasicParsing -TimeoutSec 300
            Write-Output ("uploaded {0} bytes -> {1}:{2}" -f $bytes.Length, $VmName, $Remote)
        }
        'Fetch' {
            $c = Get-GuestBytes $Fetch
            if ($c -is [string]) { $c = [Text.Encoding]::UTF8.GetBytes($c) }
            [IO.File]::WriteAllBytes($OutFile, $c)
            Write-Output ("fetched {0} bytes <- {1}:{2}" -f $c.Length, $VmName, $Fetch)
        }
        'Exec' {
            $out = "/tmp/gate_$([guid]::NewGuid().ToString('N').Substring(0, 12)).out"
            $spec = New-Object VMware.Vim.GuestProgramSpec
            $spec.ProgramPath = '/bin/bash'
            $spec.Arguments = "-c '( $($Exec -replace "'", "'\''") ) > $out 2>&1'"
            $procId = $pm.StartProgramInGuest($moref, $auth, $spec)
            $deadline = (Get-Date).AddSeconds($TimeoutSec)
            $p = $null
            while ((Get-Date) -lt $deadline) {
                $p = @($pm.ListProcessesInGuest($moref, $auth, @($procId)))[0]
                if ($p -and $p.EndTime) { break }
                Start-Sleep -Seconds 2
            }
            if (-not $p -or -not $p.EndTime) { throw "guest command still running after $TimeoutSec s (pid $procId)" }
            $text = Get-GuestBytes $out
            if ($text -is [byte[]]) { $text = [Text.Encoding]::UTF8.GetString($text) }
            Write-Output $text
            if ($p.ExitCode -ne 0) { throw "guest command exited $($p.ExitCode)" }
        }
    }
} finally {
    Disconnect-VIServer -Server $vc -Confirm:$false -ErrorAction SilentlyContinue
}
