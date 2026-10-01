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

    Gates 3 and 4 are NEGATIVE assertions -- they claim something is refused
    or closed. For a negative gate, "the host could not be reached at all" is
    indistinguishable from "the desired state was achieved" unless that is
    checked explicitly, and an unreachable host would otherwise make both
    gates report a false PASS. So:

    - $CaHost is resolved exactly once, up front. If it does not resolve,
      that is reported as a single FATAL condition and every gate below is
      marked FAIL -- no individual gate is allowed to draw a conclusion from
      an unresolvable name.
    - Gate 3 additionally cross-checks with a Negotiate bind over 389 (a
      mechanism signing enforcement does not affect) before trusting a
      refused simple bind. If Negotiate also fails, the host was never really
      contacted and gate 3 cannot conclude anything -- it fails rather than
      crediting a transport error as "signing enforcement working". If
      Negotiate succeeds, the simple-bind failure is further checked against
      LdapException.ErrorCode to make sure it is a server-issued rejection
      (e.g. invalid credentials, 49) and not LDAP_SERVER_DOWN (81), a
      transport failure, before it counts as a refusal.
    - Gate 4 is three claims, not one, and it does NOT assert that port 80 is
      closed -- that would be backwards. Port 80 MUST be open, because it
      serves /CertEnroll where every lab consumer fetches the CRL and the CA
      certificate, and CDP/AIA are plain HTTP by design. The security property
      is that /certsrv never offers Basic over cleartext (it requires SSL, so
      an HTTP request gets 403 before any auth challenge), and that
      /CertEnroll is served WITHOUT credentials. A 401 from either is the
      failure. Each claim is only credited once the host is confirmed
      reachable by gate 1 or gate 3's Negotiate cross-check, and a missing
      HTTP reply is never read as a pass.

    Exit code is the number of failed gates (0 = all healthy). Nothing here
    writes to AD, the CA, or the host under test.

.PARAMETER CaHost
    FQDN of the CA / domain controller under test. Always a name, never an
    IP address: a certificate's SAN carries the FQDN, and testing by IP would
    pass against a certificate that a name-validating client would reject --
    the gate would lie in the most expensive possible way. This holds even
    when the FQDN does not resolve from the machine running this script --
    the fix for that is DNS, not silently falling back to an IP.

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
    [string]$CredFile = "$env:USERPROFILE\.vcflab\credentials.env",
    # Gate 4c needs a real object to fetch. Directory browsing is off on
    # /CertEnroll by default, so a bare GET of the directory cannot prove the
    # publication point serves anything. Supply the CRL's URL and the gate
    # becomes a real check; omit it and the gate honestly reports NOT VERIFIED
    # instead of passing on a 403 it did not understand.
    [string]$CrlUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.DirectoryServices.Protocols

# ------------------------------------------------------------------ helpers --
function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }

$script:failCount = 0
$script:unverifiedCount = 0
function Test-Gate {
    param([string]$Label, [bool]$Ok, [string]$Detail = '')
    if ($Ok) {
        Write-Host "  PASS  $Label" -ForegroundColor Green
    } else {
        Write-Host "  FAIL  $Label  $Detail" -ForegroundColor Red
        $script:failCount++
    }
}

# A third outcome, because this project keeps shipping gates that cannot tell
# "healthy" from "I could not look" and resolve the ambiguity as PASS. An
# unobservable gate is reported as its own thing and counted -- it is not a
# failure of the system under test, but it is NOT a pass either, and the exit
# code must not say everything is fine.
function Skip-Gate {
    param([string]$Label, [string]$Why)
    Write-Host "  ????  $Label  -- NOT VERIFIED: $Why" -ForegroundColor Yellow
    $script:unverifiedCount++
}

# LDAP_SERVER_DOWN. A transport failure, never a server-issued refusal.
$LDAP_SERVER_DOWN = 81

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
# (DOMAIN\user) is required for the simple (Basic) binds below.
$bindDn = 'knowledgeondemand\svc-vcf-ldap'

# --------------------------------------------------- resolve $CaHost, once --
# Every gate below depends on reaching $CaHost by name. Resolving it once, up
# front, means a name-resolution failure is reported as exactly what it is --
# not silently reinterpreted by a later gate as "refused" or "closed".
Write-Step "Resolving $CaHost"
$hostResolved = $false
$resolveErr = ''
try {
    $addrs = [System.Net.Dns]::GetHostAddresses($CaHost)
    if ($addrs.Count -gt 0) {
        $hostResolved = $true
        Write-Info ("{0} resolves to {1}" -f $CaHost, (($addrs | ForEach-Object { $_.ToString() }) -join ', '))
    }
} catch {
    $resolveErr = $_.Exception.Message.Split([Environment]::NewLine)[0]
}

if (-not $hostResolved) {
    $detail = "$CaHost does not resolve from this host: $resolveErr"
    Write-Host "  FATAL: $detail" -ForegroundColor Red
    Write-Info "Every gate depends on reaching $CaHost by name. None can be evaluated honestly against a name that does not resolve, so all are marked FAIL rather than reporting success against a host that was never contacted."
    Write-Host ""
    Test-Gate "636 accepts connections on $CaHost" $false $detail
    Test-Gate "simple bind over LDAPS reads the directory" $false $detail
    Test-Gate "389 refuses simple binds (signing enforcement intact)" $false $detail
    Test-Gate "4a port 80 is open (CDP/AIA reachable)" $false $detail
    Test-Gate "4b /certsrv requires SSL (403 over http)" $false $detail
    Test-Gate "4c CRL served anonymously over http" $false $detail

    Write-Host ""
    Write-Host ("  {0} gate(s) failed" -f $script:failCount) -ForegroundColor Red
    Write-Host ""
    exit $script:failCount
}

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

# Cross-check first: bind with Negotiate, a mechanism signing enforcement
# does not affect. If this also fails, $CaHost was never really contacted on
# 389 (or the credential is bad) and a failed simple bind below would prove
# nothing -- it would be a transport failure wearing a "refused" costume.
$negotiateOk = $false
$negotiateErr = ''
try {
    $idN = New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($CaHost, 389)
    $connN = New-Object System.DirectoryServices.Protocols.LdapConnection($idN)
    $connN.SessionOptions.ProtocolVersion = 3
    $connN.AuthType = [System.DirectoryServices.Protocols.AuthType]::Negotiate
    $connN.Timeout = [TimeSpan]::FromSeconds(20)
    $connN.Bind((New-Object System.Net.NetworkCredential('svc-vcf-ldap', $cred['AD_BIND_PASS'], 'knowledgeondemand')))
    $negotiateOk = $true
    $connN.Dispose()
} catch {
    $negotiateErr = $_.Exception.Message.Split([Environment]::NewLine)[0]
}

if (-not $negotiateOk) {
    Test-Gate "389 refuses simple binds (signing enforcement intact)" $false `
        "cannot verify -- a Negotiate bind to ${CaHost}:389 also failed, so the host was not confirmed reachable: $negotiateErr"
} else {
    $plainRefused = $false
    $plainDetail = ''
    $transportFailure = $false
    try {
        $id2 = New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($CaHost, 389)
        $conn2 = New-Object System.DirectoryServices.Protocols.LdapConnection($id2)
        $conn2.SessionOptions.ProtocolVersion = 3
        $conn2.AuthType = [System.DirectoryServices.Protocols.AuthType]::Basic
        $conn2.Timeout = [TimeSpan]::FromSeconds(20)
        $conn2.Bind((New-Object System.Net.NetworkCredential($bindDn, $cred['AD_BIND_PASS'])))
        $conn2.Dispose()
        # If Bind() did not throw, the server accepted an unsigned simple
        # bind -- signing enforcement is not in effect. FAIL.
    } catch [System.DirectoryServices.Protocols.LdapException] {
        $plainDetail = $_.Exception.Message.Split([Environment]::NewLine)[0]
        if ($_.Exception.ErrorCode -eq $LDAP_SERVER_DOWN) {
            # Negotiate just proved the host IS reachable and the credential
            # IS valid, so a server-down error here is an inconsistent
            # transport blip, not evidence of a server-issued refusal.
            $transportFailure = $true
        } else {
            $plainRefused = $true
        }
    } catch {
        # A non-LDAP exception (e.g. raw socket failure). Cannot attribute
        # this to a server-issued refusal either.
        $plainDetail = $_.Exception.Message.Split([Environment]::NewLine)[0]
        $transportFailure = $true
    }

    if ($transportFailure) {
        Test-Gate "389 refuses simple binds (signing enforcement intact)" $false `
            "not a server refusal, a transport failure: $plainDetail"
    } else {
        Test-Gate "389 refuses simple binds (signing enforcement intact)" $plainRefused $plainDetail
        if ($plainRefused) { Write-Info "refused as expected: $plainDetail" }
    }
}

# ------------------------- Gate 4 -- cleartext enrolment refused, CDP open --
# This gate used to assert "port 80 is closed", which is now exactly backwards.
# Port 80 MUST be open: it serves /CertEnroll, where every consumer in the lab
# fetches the CRL and the CA certificate, and CDP/AIA are plain HTTP by design
# (fetching them over HTTPS is circular -- validating the HTTPS certificate
# needs a CRL). A closed port 80 means revocation checking is broken fabric-wide.
#
# The security property was never "port 80 is closed". It is "web enrolment
# never accepts credentials over cleartext", which /CertSrv delivers by
# requiring SSL: IIS evaluates that before issuing an auth challenge, so an
# HTTP request gets 403 and is never offered Basic. So three claims, each
# measured separately:
#     4a  port 80 is OPEN              (CDP/AIA are reachable at all)
#     4b  http://.../certsrv  refused  (403/404, and specifically NOT 401)
#     4c  http://.../CertEnroll/ served WITHOUT credentials (not 401)
$hostConfirmedReachable = $ldapsUp -or $negotiateOk
if (-not $hostConfirmedReachable) {
    foreach ($g in @('4a port 80 is open (CDP/AIA reachable)',
                     '4b /certsrv requires SSL (403 over http)',
                     '4c CRL served anonymously over http')) {
        Test-Gate $g $false "cannot verify -- $CaHost was not confirmed reachable on any other port"
    }
} else {
    Write-Step "Gate 4 -- cleartext enrolment refused, CDP reachable"

    $httpUp = $false
    $tcp2 = New-Object System.Net.Sockets.TcpClient
    try { $httpUp = $tcp2.ConnectAsync($CaHost, 80).Wait(4000) } catch {} finally { $tcp2.Close() }
    Test-Gate "4a port 80 is open (CDP/AIA reachable)" $httpUp `
        $(if (-not $httpUp) { 'closed -- every certificate this CA issues has an unreachable CRL' } else { '' })

    # Returns the HTTP status AND whether the reply carried a WWW-Authenticate
    # header. Status is $null when no reply was obtained at all; $null is NOT a
    # status and must never be read as a passing one.
    function Get-HttpResult {
        param([string]$Url)
        $out = @{ Status = $null; HasAuthChallenge = $false }
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 8 `
                     -MaximumRedirection 0 -ErrorAction Stop
            $out.Status = [int]$r.StatusCode
            try { $out.HasAuthChallenge = [bool]$r.Headers['WWW-Authenticate'] } catch { }
            return $out
        } catch {
            $resp = $null
            try { $resp = $_.Exception.Response } catch { }
            if ($resp) {
                try { $out.Status = [int]$resp.StatusCode } catch { }
                # Guard every hop: a partial response object is common on error
                # paths, and under StrictMode a missing member throws.
                try {
                    $hdrs = $resp.Headers
                    if ($hdrs) { $out.HasAuthChallenge = [bool]$hdrs['WWW-Authenticate'] }
                } catch { }
            }
            return $out
        }
    }

    if ($httpUp) {
        # 4b. The property is "Require SSL is in force", and only 403 shows
        # that. An earlier version passed on anything except 401, which meant a
        # 200 -- /certsrv fully served over cleartext, the worst case -- passed,
        # as did a 404 on a host where web enrolment was never installed.
        $certsrv = Get-HttpResult "http://$CaHost/certsrv/"
        if ($null -eq $certsrv.Status) {
            Test-Gate "4b /certsrv requires SSL (403 over http)" $false `
                'no HTTP reply -- cannot tell a refusal from an unreachable service'
        } elseif ($certsrv.Status -eq 200) {
            Test-Gate "4b /certsrv requires SSL (403 over http)" $false `
                'HTTP 200 -- /certsrv is being SERVED over cleartext'
        } elseif ($certsrv.Status -eq 401) {
            Test-Gate "4b /certsrv requires SSL (403 over http)" $false `
                'HTTP 401 -- Basic auth offered over cleartext'
        } elseif ($certsrv.Status -ne 403) {
            Test-Gate "4b /certsrv requires SSL (403 over http)" $false `
                "HTTP $($certsrv.Status) -- expected 403; is web enrolment installed?"
        } elseif ($certsrv.HasAuthChallenge) {
            # Belt and braces on the ordering claim: a 403 that still carried
            # WWW-Authenticate would mean a challenge was emitted anyway.
            Test-Gate "4b /certsrv requires SSL (403 over http)" $false `
                '403 but WWW-Authenticate present -- a challenge was still offered'
        } else {
            Test-Gate "4b /certsrv requires SSL (403 over http)" $true
        }

        # 4c. Fetching the directory itself cannot prove the publication point
        # serves anything: directory browsing is off by default, so /CertEnroll/
        # answers 403.14 on a perfectly healthy CA -- and a 403.4 from an SSL
        # requirement mistakenly set on /CertEnroll looks identical while every
        # CRL fetch fails. Only fetching a real object settles it.
        if ($CrlUrl) {
            $crl = Get-HttpResult $CrlUrl
            if ($null -eq $crl.Status) {
                Test-Gate "4c CRL served anonymously over http" $false "no HTTP reply from $CrlUrl"
            } elseif ($crl.Status -eq 401) {
                Test-Gate "4c CRL served anonymously over http" $false `
                    'HTTP 401 -- authentication on /CertEnroll breaks revocation checking'
            } elseif ($crl.Status -ne 200) {
                Test-Gate "4c CRL served anonymously over http" $false "HTTP $($crl.Status) -- expected 200"
            } else {
                Test-Gate "4c CRL served anonymously over http" $true
            }
        } else {
            $enroll = Get-HttpResult "http://$CaHost/CertEnroll/"
            if ($null -ne $enroll.Status -and $enroll.Status -eq 401) {
                # 401 is unambiguous whatever else is true.
                Test-Gate "4c CRL served anonymously over http" $false `
                    'HTTP 401 -- authentication on /CertEnroll breaks revocation checking'
            } elseif ($null -ne $enroll.Status -and $enroll.Status -eq 200) {
                Test-Gate "4c CRL served anonymously over http" $true 'directory listing served'
            } else {
                $what = if ($null -eq $enroll.Status) { 'no reply' } else { "HTTP $($enroll.Status)" }
                Skip-Gate "4c CRL served anonymously over http" `
                    "$what from /CertEnroll/ proves nothing (directory browsing is off by default) -- pass -CrlUrl, or run Test-CaRevocationEndpoints.ps1"
            }
        }
    } else {
        Test-Gate "4b /certsrv requires SSL (403 over http)" $false 'port 80 closed'
        Test-Gate "4c CRL served anonymously over http"      $false 'port 80 closed'
    }
}

# ------------------------------------------------------------------ summary --
Write-Host ""
if ($script:unverifiedCount -gt 0) {
    Write-Host ("  {0} gate(s) NOT VERIFIED -- listed above; these are not passes" -f $script:unverifiedCount) -ForegroundColor Yellow
}
if ($script:failCount -gt 0) {
    Write-Host ("  {0} gate(s) failed" -f $script:failCount) -ForegroundColor Red
} else {
    Write-Host "  all gates passed" -ForegroundColor Green
}
Write-Host ""
exit $script:failCount
