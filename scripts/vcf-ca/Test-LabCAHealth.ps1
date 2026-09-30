<#
.SYNOPSIS
    Verification gates for the lab CA. Read-only, safe to run at any time.

.DESCRIPTION
    Four gates prove whether the lab CA is actually doing its job, not merely
    that it was installed:

    1. LDAPS (636) accepts a TCP connection at all.
    2. A simple bind over LDAPS succeeds and can read the directory. This is
       what VCF Operations does, and it is the entire reason this CA exists.
       AuthType is deliberately Basic (a simple bind), never Negotiate --
       Negotiate succeeds over plaintext 389 with no certificate involved at
       all and would prove nothing about LDAPS.
    3. Plaintext LDAP (389) still REFUSES simple binds. Windows Server 2025's
       LDAP signing enforcement is correct lab behaviour, and this CA project
       must never weaken it. This gate protects that invariant, not the CA.
    4. Port 80 on the CA host stays closed -- certsrv is HTTPS-only.

    Exit code is the number of failed gates (0 = all healthy). Nothing here
    writes to AD, the CA, or the host under test.

.PARAMETER CaHost
    FQDN of the CA / domain controller under test. Always a name, never an
    IP address: a certificate's SAN carries the FQDN, and testing by IP would
    pass against a certificate that a name-validating client would reject --
    the gate would lie in the most expensive possible way.

.PARAMETER BaseDn
    Base distinguished name searched to confirm the bind actually reads the
    directory, not just that Bind() returned without throwing.

.PARAMETER RootFile
    PEM of the lab root CA, reserved for a future chain-validation gate. Not
    yet used by any gate below.

.PARAMETER CredFile
    Path to the credentials file. Defaults to %USERPROFILE%\.vcflab\credentials.env.
    Lines are KEY=VALUE; keys may be padded with trailing spaces for alignment
    ("KEY    =value"), so both sides are trimmed.

.REQUIRES nothing -- this script is read-only and needs no elevation.

.EXAMPLE
    .\Test-LabCAHealth.ps1
    Runs all four gates against dns01.knowledgeondemand.net.

.EXAMPLE
    .\Test-LabCAHealth.ps1 -CaHost dns02.knowledgeondemand.net
    Runs the same gates against a different domain controller.
#>
[CmdletBinding()]
param(
    [string]$CaHost   = 'dns01.knowledgeondemand.net',
    [string]$BaseDn   = 'DC=knowledgeondemand,DC=net',
    [string]$RootFile,
    [string]$CredFile = "$env:USERPROFILE\.vcflab\credentials.env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.DirectoryServices.Protocols

# ------------------------------------------------------------------ helpers --
function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }

$script:failCount = 0
function Test-Gate {
    param([string]$Label, [bool]$Ok, [string]$Detail = '')
    if ($Ok) {
        Write-Host "  PASS  $Label" -ForegroundColor Green
    } else {
        Write-Host "  FAIL  $Label  $Detail" -ForegroundColor Red
        $script:failCount++
    }
}

# ----------------------------------------------------------- load credentials --
Write-Step "Loading bind credentials"
if (-not (Test-Path $CredFile)) {
    Write-Host "FATAL: credentials file not found at $CredFile" -ForegroundColor Red
    exit 1
}
$cred = @{}
foreach ($line in Get-Content $CredFile) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith('#') -or $t -notmatch '=') { continue }
    $k, $v = $t -split '=', 2
    $cred[$k.Trim()] = $v.Trim()
}
if (-not $cred.ContainsKey('AD_BIND_PASS') -or -not $cred['AD_BIND_PASS']) {
    Write-Host "FATAL: AD_BIND_PASS not found or empty in $CredFile" -ForegroundColor Red
    exit 1
}
Write-Info "credentials loaded from $CredFile"

# svc-vcf-ldap has no userPrincipalName set, so UPN-form auth
# (svc-vcf-ldap@knowledgeondemand.net) cannot work. Down-level NetBIOS form
# (DOMAIN\user) is required.
$bindDn = 'knowledgeondemand\svc-vcf-ldap'

# --------------------------------------------------- Gate 1 -- 636 is open --
Write-Step "Gate 1 -- LDAPS port reachability"
$ldapsUp = $false
$tcp = New-Object System.Net.Sockets.TcpClient
try { $ldapsUp = $tcp.ConnectAsync($CaHost, 636).Wait(5000) } catch {} finally { $tcp.Close() }
Test-Gate "636 accepts connections on $CaHost" $ldapsUp

# ----------------------------------------- Gate 2 -- simple bind over LDAPS --
Write-Step "Gate 2 -- simple bind over LDAPS reads the directory"
$bindOk = $false
$bindErr = ''
try {
    $id = New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($CaHost, 636)
    $conn = New-Object System.DirectoryServices.Protocols.LdapConnection($id)
    $conn.SessionOptions.ProtocolVersion = 3
    $conn.SessionOptions.SecureSocketLayer = $true
    $conn.Timeout = [TimeSpan]::FromSeconds(20)
    # Deliberately Basic (a simple bind), not Negotiate. Negotiate would
    # succeed over plaintext 389 with no certificate at all and would tell us
    # nothing about whether LDAPS actually works.
    $conn.AuthType = [System.DirectoryServices.Protocols.AuthType]::Basic
    $conn.Bind((New-Object System.Net.NetworkCredential($bindDn, $cred['AD_BIND_PASS'])))

    $req = New-Object System.DirectoryServices.Protocols.SearchRequest(
        $BaseDn,
        '(sAMAccountName=svc-vcf-ldap)',
        [System.DirectoryServices.Protocols.SearchScope]::Subtree,
        @('distinguishedName'))
    $resp = $conn.SendRequest($req)
    $bindOk = ($resp.Entries.Count -ge 1)
    $conn.Dispose()
} catch {
    $bindErr = $_.Exception.Message.Split([Environment]::NewLine)[0]
}
Test-Gate "simple bind over LDAPS reads the directory" $bindOk $bindErr

# ------------------------------------- Gate 3 -- 389 still refuses binds --
Write-Step "Gate 3 -- plaintext LDAP still refuses simple binds"
$plainRefused = $false
$plainDetail = ''
try {
    $id2 = New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($CaHost, 389)
    $conn2 = New-Object System.DirectoryServices.Protocols.LdapConnection($id2)
    $conn2.SessionOptions.ProtocolVersion = 3
    $conn2.AuthType = [System.DirectoryServices.Protocols.AuthType]::Basic
    $conn2.Timeout = [TimeSpan]::FromSeconds(20)
    $conn2.Bind((New-Object System.Net.NetworkCredential($bindDn, $cred['AD_BIND_PASS'])))
    $conn2.Dispose()
    # If Bind() did not throw, the server accepted an unsigned simple bind --
    # signing enforcement is not in effect. That is a FAIL for this gate.
} catch {
    $plainRefused = $true
    $plainDetail = $_.Exception.Message.Split([Environment]::NewLine)[0]
}
Test-Gate "389 refuses simple binds (signing enforcement intact)" $plainRefused
if ($plainRefused) { Write-Info "refused as expected: $plainDetail" }

# --------------------------------------- Gate 4 -- certsrv is HTTPS-only --
Write-Step "Gate 4 -- port 80 is closed on the CA host"
$httpUp = $false
$tcp2 = New-Object System.Net.Sockets.TcpClient
try { $httpUp = $tcp2.ConnectAsync($CaHost, 80).Wait(4000) } catch {} finally { $tcp2.Close() }
Test-Gate "port 80 is closed on $CaHost" (-not $httpUp)

# ------------------------------------------------------------------ summary --
Write-Host ""
if ($script:failCount -gt 0) {
    Write-Host ("  {0} gate(s) failed" -f $script:failCount) -ForegroundColor Red
} else {
    Write-Host "  all gates passed" -ForegroundColor Green
}
Write-Host ""
exit $script:failCount
