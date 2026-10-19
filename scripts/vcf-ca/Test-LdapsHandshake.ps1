<#
.SYNOPSIS
    Reports whether the domain controller can actually complete an LDAPS
    handshake, and what certificate it presents.

.DESCRIPTION
    This is the CA project's before/after control. Run it before installing the
    CA to record the baseline, and again afterwards to prove the DC autoenrolled
    a usable certificate. Nothing else in the plan measures that end to end.

    It exists because a port check cannot answer the question. Measured on
    dns01 on 2026-09-30, BEFORE the CA existed:

        tcp/389 open, tcp/636 open, tcp/3268 open
        TLS 1.3 / 1.2 / 1.1 / 1.0 on 636 -> connection forcibly closed, every one

    LSASS binds 636 on every domain controller whether or not a certificate is
    available, so "636 is open" is true on a DC that cannot serve LDAPS at all.
    Trying all four protocol versions is what separates "no certificate" from
    "no protocol in common" -- a reset on one version is ambiguous, a reset on
    every version is not.

    After the CA is in place this should print HANDSHAKE OK with an issuer of
    the lab root CA, a subject naming the DC, and Server Authentication in the
    EKU. Anything less means autoenrolment did not deliver a usable cert,
    regardless of what certutil reports on the DC itself.

.PARAMETER Server
    Address to connect to. Defaults to the lab DC.

.PARAMETER SniName
    Name to send as SNI and to validate the certificate against.

.EXAMPLE
    .\Test-LdapsHandshake.ps1
.EXAMPLE
    .\Test-LdapsHandshake.ps1 -Server 172.16.10.150 -SniName DNS01.knowledgeondemand.net
#>
[CmdletBinding()]
param(
    [string]$Server  = '172.16.10.150',
    [string]$SniName = 'DNS01.knowledgeondemand.net',
    [int]$Port       = 636,
    [int]$TimeoutMs  = 10000
)

Set-StrictMode -Version Latest

Write-Host "`n=== LDAPS handshake against $Server`:$Port (SNI $SniName)" -ForegroundColor Cyan

# Reachability first, so a handshake failure is never confused with an
# unreachable host -- an unreachable DC must not read as "no certificate".
$tcp = New-Object Net.Sockets.TcpClient
try {
    $iar = $tcp.BeginConnect($Server, $Port, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
        Write-Host "  [FAIL] $Server`:$Port did not accept a connection -- host unreachable or port closed." -ForegroundColor Red
        Write-Host "         This is NOT evidence about the certificate." -ForegroundColor Red
        exit 2
    }
    $tcp.EndConnect($iar)
} catch {
    Write-Host "  [FAIL] cannot reach $Server`:$Port -- $($_.Exception.GetBaseException().Message)" -ForegroundColor Red
    Write-Host "         This is NOT evidence about the certificate." -ForegroundColor Red
    exit 2
} finally { $tcp.Close() }
Write-Host "  [ OK ] tcp/$Port accepts connections (this alone proves nothing about LDAPS)" -ForegroundColor DarkGray

$succeeded = $false
$chainErrors = $null
$presented = $null

foreach ($name in 'Tls13', 'Tls12', 'Tls11', 'Tls') {
    $proto = $null
    try { $proto = [Net.SecurityProtocolType]::$name }
    catch { Write-Host ("  {0,-6}: not supported by this .NET runtime" -f $name) -ForegroundColor DarkGray; continue }

    $c = $null; $ssl = $null
    try {
        $c = New-Object Net.Sockets.TcpClient($Server, $Port)
        # Accept any certificate: whether THIS machine trusts it is a separate
        # question from whether the DC can serve one at all. The chain result
        # is captured and reported rather than used to fail the handshake.
        $ssl = New-Object Net.Security.SslStream($c.GetStream(), $false,
            [Net.Security.RemoteCertificateValidationCallback]{
                param($sndr, $cert, $chain, $errors)
                $global:LdapsChainErrors = $errors
                return $true
            })
        $ssl.AuthenticateAsClient($SniName, $null, $proto, $false)
        $presented   = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
        $chainErrors = $global:LdapsChainErrors
        Write-Host ("  {0,-6}: HANDSHAKE OK" -f $name) -ForegroundColor Green
        $succeeded = $true
    } catch {
        Write-Host ("  {0,-6}: {1}" -f $name, $_.Exception.GetBaseException().Message) -ForegroundColor Yellow
    } finally {
        if ($ssl) { $ssl.Dispose() }
        if ($c)   { $c.Close() }
    }
}

if (-not $succeeded) {
    Write-Host "`n  [FAIL] No TLS version completed a handshake." -ForegroundColor Red
    Write-Host "         Every version resetting rules out a protocol mismatch:" -ForegroundColor Red
    Write-Host "         the DC has no usable LDAPS certificate." -ForegroundColor Red
    exit 1
}

Write-Host "`n=== certificate the DC presented"
Write-Host "  subject     : $($presented.Subject)"
Write-Host "  issuer      : $($presented.Issuer)"
Write-Host "  serial      : $($presented.SerialNumber)"
Write-Host "  thumbprint  : $($presented.Thumbprint)"
Write-Host "  valid       : $($presented.NotBefore.ToString('yyyy-MM-dd')) .. $($presented.NotAfter.ToString('yyyy-MM-dd'))"
Write-Host "  self-signed : $($presented.Subject -eq $presented.Issuer)"
Write-Host "  chain status: $(if ($chainErrors -eq [Net.Security.SslPolicyErrors]::None) { 'trusted by this machine' } else { $chainErrors })"

foreach ($e in $presented.Extensions) {
    if ($e.Oid.Value -eq '2.5.29.17') { Write-Host "  SAN         : $($e.Format($false))" }
    if ($e.Oid.Value -eq '2.5.29.37') { Write-Host "  EKU         : $($e.Format($false))" }
}

# Server Authentication is what makes the certificate usable for LDAPS. A cert
# that chains and names the host but lacks this EKU still will not serve.
$serverAuth = $presented.Extensions |
    Where-Object { $_.Oid.Value -eq '2.5.29.37' -and $_.Format($false) -match 'Server Authentication' }
if ($serverAuth) { Write-Host "`n  [ OK ] Server Authentication EKU present" -ForegroundColor Green }
else { Write-Host "`n  [WARN] Server Authentication EKU NOT found -- check the template's EKU" -ForegroundColor Yellow }

exit 0
