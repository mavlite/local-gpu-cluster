<#
.SYNOPSIS
    Proves a certificate's CDP and AIA are reachable and the CRL is current.

.DESCRIPTION
    Set-CaRevocationEndpoints.ps1 verifies the CA's CONFIGURATION. That is not
    the same claim as "an issued certificate carries a revocation URL that
    works", and the gap between those two is where the real failures live: a
    wrong flag prefix writes a value that reads back perfectly and still never
    reaches the certificate's CDP extension.

    So this reads the URLs out of a certificate that the CA actually signed,
    resolves each host, fetches each URL, and checks the CRL parses and has
    not expired. Nothing here trusts the registry.

    Three exit codes, and the third is the point:
      0  every URL resolved, fetched, and the CRL is current
      1  a real failure -- a URL is missing, unreachable, or the CRL is stale
      2  INCONCLUSIVE -- this host could not look (no DNS, no route), which
         says nothing about whether a lab consumer can

    VANTAGE POINT MATTERS. Run on dns01 this proves the CA's output and the
    CRL's validity, but NOT that a lab consumer can reach the endpoint -- dns01
    is reaching itself. The claim you usually want is the lab one, so verify
    from inside the lab subnet:

        pct exec 159 -- curl -sS -o /dev/null -w '%{http_code}\n' \
            http://pki.knowledgeondemand.net/CertEnroll/<ca>.crl

    Note that a workstation on a VPN using block-outside-dns cannot resolve
    anything except the VPN's own resolvers, and will return 2 here however
    healthy the lab is.

.PARAMETER CertificateFile
    A .cer/.crt file to inspect.

.PARAMETER FromTlsEndpoint
    host:port to pull the live certificate from (e.g. dns01.knowledgeondemand.net:636).

.PARAMETER Thumbprint
    Thumbprint of a certificate in LocalMachine\My (use on dns01).

.EXAMPLE
    .\Test-CaRevocationEndpoints.ps1 -FromTlsEndpoint dns01.knowledgeondemand.net:636
.EXAMPLE
    .\Test-CaRevocationEndpoints.ps1 -CertificateFile .\issued.cer
#>
[CmdletBinding(DefaultParameterSetName = 'Tls')]
param(
    [Parameter(ParameterSetName = 'File', Mandatory)][string]$CertificateFile,
    [Parameter(ParameterSetName = 'Tls')][string]$FromTlsEndpoint = 'dns01.knowledgeondemand.net:636',
    [Parameter(ParameterSetName = 'Store', Mandatory)][string]$Thumbprint,
    [int]$TimeoutSec = 15
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }
function Write-Warn { param([string]$m) Write-Host "  [!] $m"  -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "  [X] $m"  -ForegroundColor Red }

# ------------------------------------------------------------ get the cert --
Write-Step "Obtaining the certificate"
$cert = $null
switch ($PSCmdlet.ParameterSetName) {
    'File' {
        if (-not (Test-Path $CertificateFile)) { Write-Fail "no such file: $CertificateFile"; exit 1 }
        $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 (Resolve-Path $CertificateFile)
        Write-Ok "from file: $CertificateFile"
    }
    'Store' {
        $cert = Get-ChildItem Cert:\LocalMachine\My |
                Where-Object { $_.Thumbprint -eq $Thumbprint } | Select-Object -First 1
        if (-not $cert) { Write-Fail "no certificate with thumbprint $Thumbprint in LocalMachine\My"; exit 1 }
        Write-Ok "from store: $($cert.Subject)"
    }
    'Tls' {
        $parts = $FromTlsEndpoint.Split(':')
        if ($parts.Count -ne 2) { Write-Fail "FromTlsEndpoint must be host:port"; exit 1 }
        $h = $parts[0]; $prt = [int]$parts[1]
        $c = $null; $ssl = $null
        try {
            $c = New-Object Net.Sockets.TcpClient($h, $prt)
            $ssl = New-Object Net.Security.SslStream($c.GetStream(), $false,
                [Net.Security.RemoteCertificateValidationCallback]{ param($a,$b,$d,$e) $true })
            $ssl.AuthenticateAsClient($h)
            $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $ssl.RemoteCertificate
            Write-Ok "from $FromTlsEndpoint : $($cert.Subject)"
        } catch {
            # Could not obtain the certificate at all. That is not a statement
            # about its CDP.
            Write-Warn "could not retrieve a certificate from $FromTlsEndpoint --"
            Write-Warn "  $($_.Exception.GetBaseException().Message)"
            Write-Info "INCONCLUSIVE: nothing was learned about CDP or AIA."
            exit 2
        } finally {
            if ($ssl) { $ssl.Dispose() }
            if ($c) { $c.Close() }
        }
    }
}
Write-Info "issuer : $($cert.Issuer)"

# -------------------------------------------- pull the URLs from the cert --
# These come from the certificate, not from the CA's registry. A flag prefix
# that never made it into the extension is invisible to a config read-back and
# obvious here.
Write-Step "URLs carried by the certificate"
function Get-MaximalUrls {
    param([string]$Text)
    # Windows renders a URL containing spaces TWICE: the decoded form, then the
    # percent-encoded form in parentheses, e.g.
    #     URL=http://h/a/My CA.crl (http://h/a/My%20CA.crl)
    # A match that stops at whitespace therefore yields "http://h/a/My" as well
    # as the real URL -- and the truncated one is always a strict prefix of the
    # real one. Keep only maximal candidates. Without this the script reports
    # 404s against URLs the certificate never contained.
    $raw = @([regex]::Matches($Text, 'https?://[^\s,()]+') |
             ForEach-Object { $_.Value.TrimEnd('.', ',', ';') } |
             Select-Object -Unique)
    $keep = @()
    foreach ($u in ($raw | Sort-Object { $_.Length } -Descending)) {
        $isPrefixOfKept = $false
        foreach ($k in $keep) {
            if ($k.Length -gt $u.Length -and $k.StartsWith($u, [StringComparison]::OrdinalIgnoreCase)) {
                $isPrefixOfKept = $true; break
            }
        }
        if (-not $isPrefixOfKept) { $keep += $u }
    }
    return @($keep)
}

function Get-CdpUrls {
    $ext = $cert.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.31' }
    Write-Info 'CDP (CRL Distribution Points) :'
    if (-not $ext) { Write-Warn '  no CDP extension present'; return @() }
    $text = $ext.Format($false)
    $urls = Get-MaximalUrls -Text $text
    if (-not $urls) { Write-Host "      (no http url; raw: $text)" -ForegroundColor DarkGray }
    foreach ($u in $urls) { Write-Host "      $u" -ForegroundColor White }
    $ldap = @([regex]::Matches($text, 'ldap://[^\s,()]+')).Count
    if ($ldap) { Write-Info "  ($ldap ldap:// entry/entries -- Photon appliances cannot follow these)" }
    return $urls
}

function Get-AiaUrls {
    # AIA carries TWO different access methods and they are not interchangeable:
    #   1.3.6.1.5.5.7.48.2  caIssuers -- a fetchable CA certificate
    #   1.3.6.1.5.5.7.48.1  OCSP      -- a responder that answers POSTed OCSP
    #                                    requests and returns 400 to a bare GET
    # Fetching an OCSP URL with Invoke-WebRequest and calling the 400 a failure
    # is a bug in the test, not a finding about the CA.
    $ext = $cert.Extensions | Where-Object { $_.Oid.Value -eq '1.3.6.1.5.5.7.1.1' }
    Write-Info 'AIA (Authority Information Access) :'
    if (-not $ext) { Write-Warn '  no AIA extension present'; return @() }
    $text = $ext.Format($false)

    $issuerUrls = @()
    $ocspUrls   = @()
    # Each access method begins its own block in the formatted output.
    $blocks = @([regex]::Split($text, '(?=Access Method)') | Where-Object { $_ -match 'Access Method' })
    if (-not $blocks) { $blocks = @($text) }   # unexpected shape: treat as caIssuers
    foreach ($b in $blocks) {
        $urls = Get-MaximalUrls -Text $b
        if ($b -match '1\.3\.6\.1\.5\.5\.7\.48\.1|On-line Certificate Status') { $ocspUrls += $urls }
        else { $issuerUrls += $urls }
    }
    foreach ($u in $issuerUrls) { Write-Host "      $u" -ForegroundColor White }
    foreach ($u in $ocspUrls) { Write-Host "      $u   [OCSP -- not fetched]" -ForegroundColor DarkGray }
    if ($ocspUrls) { Write-Info "  ($($ocspUrls.Count) OCSP responder url(s); a bare GET is not a valid OCSP request)" }
    return $issuerUrls
}

$cdp = Get-CdpUrls
$aia = Get-AiaUrls

if (-not $cdp) {
    Write-Fail "This certificate carries NO http CDP url."
    Write-Fail "Revocation checking cannot work for any consumer that cannot read ldap:///."
    Write-Fail "Cause is usually a CRLPublicationURLs entry missing flag 8."
    exit 1
}

# ------------------------------------------------------------ fetch them all --
Write-Step "Resolving and fetching"
$realFailure = 0
$inconclusive = 0

foreach ($u in ($cdp + $aia)) {
    $uri = [Uri]$u
    Write-Info $u

    # Single-homed check: the defect that started all of this.
    try {
        $ips = @(Resolve-DnsName -Name $uri.Host -Type A -DnsOnly -ErrorAction Stop |
                 Where-Object { $_.PSObject.Properties.Name -contains 'IPAddress' -and $_.IPAddress } |
                 ForEach-Object { $_.IPAddress })
        if ($ips.Count -eq 1) { Write-Ok "    resolves to $($ips[0])" }
        elseif ($ips.Count -gt 1) {
            Write-Fail "    resolves to $($ips.Count) addresses: $($ips -join ', ')"
            Write-Fail "    A CDP/AIA host must be single-homed; consumers will round-robin."
            $realFailure++
        } else {
            Write-Warn "    resolved with no address"
            $realFailure++
        }
    } catch {
        Write-Warn "    cannot resolve $($uri.Host) from this host"
        Write-Info "    INCONCLUSIVE for this url -- verify from inside the lab"
        $inconclusive++
        continue
    }

    $tmp = Join-Path $env:TEMP ("cdp_" + [IO.Path]::GetRandomFileName())
    try {
        Invoke-WebRequest -Uri $u -OutFile $tmp -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop | Out-Null
        $len = (Get-Item $tmp).Length
        Write-Ok "    fetched, $len bytes"

        # A 200 carrying an IIS error page is still a failed fetch. For a CRL,
        # make certutil parse it and check it has not expired -- a stale CRL is
        # exactly what strict validators hard-fail on.
        if ($u -match '\.crl$') {
            $dump = & certutil -dump $tmp 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Fail "    certutil could not parse this as a CRL (served an error page?)"
                $realFailure++
            } else {
                $next = ($dump | Select-String -Pattern 'NextUpdate:' | Select-Object -First 1)
                if ($next) {
                    $raw = ($next.Line -replace '.*NextUpdate:\s*', '').Trim()
                    $parsed = [datetime]::MinValue
                    if ([datetime]::TryParse($raw, [ref]$parsed)) {
                        if ($parsed -lt (Get-Date)) {
                            Write-Fail "    CRL EXPIRED at $parsed -- strict validators hard-fail on this"
                            $realFailure++
                        } else {
                            Write-Ok "    valid CRL, NextUpdate $parsed"
                        }
                    } else { Write-Ok "    valid CRL, NextUpdate $raw" }
                } else { Write-Warn "    parsed, but no NextUpdate found" }
            }
        } elseif ($u -match '\.crt$') {
            try {
                $ca = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $tmp
                Write-Ok "    valid CA certificate: $($ca.Subject)"
            } catch {
                Write-Fail "    not parseable as a certificate (served an error page?)"
                $realFailure++
            }
        }
    } catch {
        $msg = $_.Exception.Message.Split([Environment]::NewLine)[0]
        # 401 here means /CertEnroll has authentication on it -- the exact bug
        # the hardening script used to introduce.
        if ($msg -match '401|Unauthorized') {
            Write-Fail "    401 Unauthorized -- /CertEnroll requires authentication."
            Write-Fail "    No consumer sends credentials for a CRL. It must be ANONYMOUS."
            $realFailure++
        } elseif ($msg -match '403') {
            Write-Fail "    403 -- is Require SSL set on /CertEnroll? It must NOT be."
            $realFailure++
        } else {
            Write-Fail "    fetch failed: $msg"
            $realFailure++
        }
    } finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
}

Write-Step "Result"
if ($realFailure) {
    Write-Fail "$realFailure real failure(s). Do not rotate any certificate until these pass:"
    Write-Fail "a resource reissued with an unreachable CRL partitions the management plane."
    exit 1
}
if ($inconclusive) {
    Write-Warn "$inconclusive url(s) INCONCLUSIVE from this host -- nothing was proven about them."
    Write-Info "Re-run from inside the lab subnet before trusting the result."
    exit 2
}
Write-Ok "Every CDP and AIA url resolved single-homed, fetched, and parsed"
exit 0
