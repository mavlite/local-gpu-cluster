<#
.SYNOPSIS
    Proves a certificate's CDP and AIA are reachable and the CRL is current.

.DESCRIPTION
    Set-CaRevocationEndpoints.ps1 verifies the CA's CONFIGURATION. That is not
    the same claim as "an issued certificate carries a revocation URL that
    works", and the gap between those two is where the real failures live: a
    wrong flag prefix writes a value that reads back perfectly and still never
    reaches the certificate's CDP extension.

    So this reads the URLs out of a certificate the CA actually signed,
    resolves each host, fetches each URL, and checks the CRL parses and has not
    expired. Nothing here trusts the registry. This is the acceptance gate for
    the CDP/AIA configuration, and it is the ONLY check that can catch a wrong
    flag prefix.

    Three exit codes, and the third is the point:
      0  every URL resolved single-homed, fetched, and parsed
      1  a real failure -- a URL is missing, unreachable, or the CRL is stale
      2  INCONCLUSIVE -- this host could not look (no DNS, no route, no
         certificate), which says nothing about whether a lab consumer can

    WHAT "VALID CRL" DOES AND DOES NOT MEAN HERE. certutil -dump confirms the
    bytes parse as a CRL and exposes NextUpdate. It does NOT verify the
    signature or that the CRL was issued by this certificate's issuer. The
    claim made below is deliberately narrowed to what was actually checked.

    VANTAGE POINT MATTERS. Run on dns01 this proves the CA's output and the
    CRL's validity, but NOT that a lab consumer can reach the endpoint -- dns01
    is reaching itself. For the claim that usually matters, ask from inside the
    lab subnet:

        pct exec 159 -- curl -sS -o /dev/null -w '%{http_code}\n' \
            http://pki.knowledgeondemand.net/CertEnroll/<ca>.crl

    A workstation on a VPN using block-outside-dns cannot resolve anything
    except the VPN's own resolvers and will return 2 here however healthy the
    lab is.

.PARAMETER CertificateFile
    A .cer/.crt file to inspect.

.PARAMETER FromTlsEndpoint
    host:port to pull the live certificate from (e.g. dns01.knowledgeondemand.net:636).

.PARAMETER Thumbprint
    Thumbprint of a certificate in LocalMachine\My (use on dns01).

.PARAMETER RequireAia
    Require at least one caIssuers HTTP URL. On by default: a certificate with
    no fetchable issuer URL cannot be chained by a consumer that does not
    already hold the root. Pass -RequireAia:$false only when the root is known
    to be pre-distributed to every consumer.

.EXAMPLE
    .\Test-CaRevocationEndpoints.ps1 -FromTlsEndpoint dns01.knowledgeondemand.net:636
.EXAMPLE
    .\Test-CaRevocationEndpoints.ps1 -CertificateFile .\throwaway.cer
#>
[CmdletBinding(DefaultParameterSetName = 'Tls')]
param(
    [Parameter(ParameterSetName = 'File', Mandatory)][string]$CertificateFile,
    [Parameter(ParameterSetName = 'Tls')][string]$FromTlsEndpoint = 'dns01.knowledgeondemand.net:636',
    [Parameter(ParameterSetName = 'Store', Mandatory)][string]$Thumbprint,
    [bool]$RequireAia = $true,
    [int]$TimeoutSec = 15
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }
function Write-Warn { param([string]$m) Write-Host "  [!] $m"  -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "  [X] $m"  -ForegroundColor Red }

# Certificate subjects and extension text are attacker-influenceable in the
# general case and are printed to a terminal. Strip control characters so a
# crafted certificate cannot emit escape sequences.
function Format-Safe {
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    return ($Text -replace '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '?')
}

# ------------------------------------------------------------ get the cert --
Write-Step "Obtaining the certificate"
$cert = $null
switch ($PSCmdlet.ParameterSetName) {
    'File' {
        if (-not (Test-Path -LiteralPath $CertificateFile)) {
            Write-Fail "no such file: $CertificateFile"; exit 1
        }
        # .ProviderPath, not the PathInfo object: the X509Certificate2
        # constructor needs a filesystem string.
        $resolved = (Resolve-Path -LiteralPath $CertificateFile).ProviderPath
        $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $resolved
        Write-Ok "from file: $resolved"
    }
    'Store' {
        $cert = Get-ChildItem Cert:\LocalMachine\My |
                Where-Object { $_.Thumbprint -eq $Thumbprint } | Select-Object -First 1
        if (-not $cert) { Write-Fail "no certificate with thumbprint $Thumbprint in LocalMachine\My"; exit 1 }
        Write-Ok "from store: $(Format-Safe $cert.Subject)"
    }
    'Tls' {
        $parts = $FromTlsEndpoint.Split(':')
        if ($parts.Count -ne 2) { Write-Fail "FromTlsEndpoint must be host:port"; exit 1 }
        $h = $parts[0]; $prt = [int]$parts[1]
        $c = $null; $ssl = $null
        try {
            $c = New-Object Net.Sockets.TcpClient($h, $prt)
            # Accepting any certificate is deliberate and safe here: the point
            # is to READ the certificate's extensions. Nothing from this
            # connection is trusted, and no credential is ever sent over it.
            $ssl = New-Object Net.Security.SslStream($c.GetStream(), $false,
                [Net.Security.RemoteCertificateValidationCallback]{ param($a,$b,$d,$e) $true })
            $ssl.AuthenticateAsClient($h)
            $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $ssl.RemoteCertificate
            Write-Ok "from $FromTlsEndpoint : $(Format-Safe $cert.Subject)"
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
Write-Info "issuer : $(Format-Safe $cert.Issuer)"

# -------------------------------------------- pull the URLs from the cert --
Write-Step "URLs carried by the certificate"

function Get-MaximalUrls {
    param([string]$Text)
    # Windows renders a URL containing spaces TWICE: the decoded form, then the
    # percent-encoded form in parentheses, e.g.
    #     URL=http://h/a/My CA.crl (http://h/a/My%20CA.crl)
    # A match that stops at whitespace therefore also yields "http://h/a/My",
    # which is a strict prefix of the real URL. Without this the script reports
    # 404s against URLs the certificate never contained.
    #
    # A candidate is only dropped when the longer URL continues with a
    # percent-escape at the cut point -- that is the signature of this specific
    # rendering. A genuinely distinct URL that merely happens to be a prefix of
    # another (".../a.crl" beside ".../a.crl?x") is kept and tested.
    $raw = @([regex]::Matches($Text, 'https?://[^\s,()]+') |
             ForEach-Object { $_.Value.TrimEnd('.', ',', ';') } |
             Select-Object -Unique)
    $keep = @()
    foreach ($u in ($raw | Sort-Object { $_.Length } -Descending)) {
        $drop = $false
        foreach ($k in $keep) {
            if ($k.Length -gt $u.Length -and $k.StartsWith($u, [StringComparison]::OrdinalIgnoreCase)) {
                if ($k.Substring($u.Length) -match '^%[0-9A-Fa-f]{2}') { $drop = $true; break }
            }
        }
        if (-not $drop) { $keep += $u }
    }
    return @($keep)
}

function Get-CdpUrls {
    $ext = $cert.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.31' }
    Write-Info 'CDP (CRL Distribution Points) :'
    if (-not $ext) { Write-Warn '  no CDP extension present'; return @() }
    $text = $ext.Format($false)
    $urls = Get-MaximalUrls -Text $text
    if (-not $urls) { Write-Host "      (no http url; raw: $(Format-Safe $text))" -ForegroundColor DarkGray }
    foreach ($u in $urls) { Write-Host "      $(Format-Safe $u)" -ForegroundColor White }
    $ldap = @([regex]::Matches($text, 'ldap://[^\s,()]+')).Count
    if ($ldap) { Write-Info "  ($ldap ldap:// entry/entries -- Photon appliances cannot follow these)" }
    return @($urls)
}

function Get-AiaUrls {
    # AIA carries TWO different access methods and they are not interchangeable:
    #   1.3.6.1.5.5.7.48.2  caIssuers -- a fetchable CA certificate
    #   1.3.6.1.5.5.7.48.1  OCSP      -- a responder that answers POSTed OCSP
    #                                    requests and returns 400 to a bare GET
    # Classification is by OID ONLY. The Format() labels ("Certification
    # Authority Issuer", "On-line Certificate Status Protocol") are localised,
    # so matching them would misclassify on a non-English Windows and produce
    # false 400s. A block matching neither OID is reported and NOT fetched,
    # rather than optimistically treated as caIssuers.
    $ext = $cert.Extensions | Where-Object { $_.Oid.Value -eq '1.3.6.1.5.5.7.1.1' }
    Write-Info 'AIA (Authority Information Access) :'
    if (-not $ext) { Write-Warn '  no AIA extension present'; return @() }
    $text = $ext.Format($false)

    $issuerUrls = @(); $ocspUrls = @(); $unknown = @()
    $blocks = @([regex]::Split($text, '(?=Access Method)') | Where-Object { $_ -match '\S' })
    foreach ($b in $blocks) {
        $urls = Get-MaximalUrls -Text $b
        if (-not $urls) { continue }
        if     ($b -match '1\.3\.6\.1\.5\.5\.7\.48\.2') { $issuerUrls += $urls }
        elseif ($b -match '1\.3\.6\.1\.5\.5\.7\.48\.1') { $ocspUrls   += $urls }
        else                                            { $unknown    += $urls }
    }
    foreach ($u in $issuerUrls) { Write-Host "      $(Format-Safe $u)" -ForegroundColor White }
    foreach ($u in $ocspUrls)   { Write-Host "      $(Format-Safe $u)   [OCSP -- not fetched]" -ForegroundColor DarkGray }
    foreach ($u in $unknown)    { Write-Host "      $(Format-Safe $u)   [unknown access method -- not fetched]" -ForegroundColor Yellow }
    if ($ocspUrls) { Write-Info "  ($($ocspUrls.Count) OCSP url(s); a bare GET is not a valid OCSP request)" }
    if ($unknown)  { Write-Warn "  $($unknown.Count) url(s) under an unrecognised access method" }
    return @($issuerUrls)
}

$cdp = @(Get-CdpUrls)
$aia = @(Get-AiaUrls)

$hardFail = 0
if ($cdp.Count -eq 0) {
    Write-Fail "This certificate carries NO http CDP url."
    Write-Fail "Revocation checking cannot work for any consumer that cannot read ldap:///."
    Write-Fail "Cause is usually the http CRLPublicationURLs entry lacking the"
    Write-Fail "'include in the CDP extension of issued certificates' flag bit."
    $hardFail++
}
if ($RequireAia -and $aia.Count -eq 0) {
    Write-Fail "This certificate carries NO caIssuers http url (AIA)."
    Write-Fail "A consumer that does not already hold the root cannot build the chain."
    Write-Fail "Pass -RequireAia:`$false only if the root is pre-distributed everywhere."
    $hardFail++
}
if ($hardFail) { Write-Step "Result"; Write-Fail "$hardFail structural failure(s) -- nothing was fetched."; exit 1 }

# ------------------------------------------------------------ fetch them all --
# Each url is paired with WHAT IT SHOULD BE, taken from the extension it came
# from -- never guessed from the file extension. A url ending in neither .crl
# nor .crt used to be reported as "fetched" with no content check at all.
$targets = @()
foreach ($u in $cdp) { $targets += [pscustomobject]@{ Url = $u; Kind = 'crl';    From = 'CDP' } }
foreach ($u in $aia) { $targets += [pscustomobject]@{ Url = $u; Kind = 'cacert'; From = 'AIA' } }

Write-Step "Resolving and fetching $($targets.Count) url(s)"
$realFailure = 0
$inconclusive = 0

foreach ($t in $targets) {
    $u = $t.Url
    $uri = $null
    try { $uri = [Uri]$u } catch {
        Write-Fail "$(Format-Safe $u)"
        Write-Fail "    not a parseable URL"
        $realFailure++; continue
    }
    Write-Info "$($t.From) [$($t.Kind)] $(Format-Safe $u)"

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
            Write-Fail "    resolved with no address"
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
        Write-Ok "    fetched, $((Get-Item $tmp).Length) bytes"

        # A 200 carrying an IIS error page is still a failed fetch, so every
        # fetch is followed by a content check appropriate to what it should be.
        switch ($t.Kind) {
            'crl' {
                # certutil is used (not a .NET type) because Windows PowerShell
                # 5.1 has no CRL parser. Native stderr under
                # $ErrorActionPreference='Stop' throws NativeCommandError
                # before the exit code can be read, so relax it locally.
                $dump = $null; $rc = 0
                try {
                    $ErrorActionPreference = 'Continue'
                    $dump = & certutil -dump $tmp 2>&1
                    $rc = $LASTEXITCODE
                } finally { $ErrorActionPreference = 'Stop' }

                if ($rc -ne 0) {
                    Write-Fail "    certutil could not parse this as a CRL (served an error page?)"
                    $realFailure++
                } else {
                    $next = @($dump | Select-String -Pattern 'NextUpdate:') | Select-Object -First 1
                    if (-not $next) {
                        Write-Fail "    parsed, but no NextUpdate -- cannot confirm the CRL is current"
                        $realFailure++
                    } else {
                        $raw = ($next.Line -replace '.*NextUpdate:\s*', '').Trim()
                        $parsed = [datetime]::MinValue
                        if (-not [datetime]::TryParse($raw, [ref]$parsed)) {
                            # Unparseable date used to print [OK]. An expiry
                            # that cannot be read has not been checked.
                            Write-Fail "    NextUpdate '$(Format-Safe $raw)' could not be parsed -- expiry UNVERIFIED"
                            $realFailure++
                        } elseif ($parsed -lt (Get-Date)) {
                            Write-Fail "    CRL EXPIRED at $parsed -- strict validators hard-fail on this"
                            $realFailure++
                        } else {
                            Write-Ok "    CRL parsed, NextUpdate $parsed (signature NOT verified)"
                        }
                    }
                }
            }
            'cacert' {
                try {
                    $ca = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $tmp
                    Write-Ok "    CA certificate parsed: $(Format-Safe $ca.Subject)"
                } catch {
                    Write-Fail "    not parseable as a certificate (served an error page?)"
                    $realFailure++
                }
            }
            default {
                Write-Fail "    no content check exists for kind '$($t.Kind)' -- not verified"
                $realFailure++
            }
        }
    } catch {
        $msg = $_.Exception.Message.Split([Environment]::NewLine)[0]
        # 401 means /CertEnroll has authentication on it -- the exact bug the
        # hardening script used to introduce.
        if ($msg -match '401|Unauthorized') {
            Write-Fail "    401 Unauthorized -- /CertEnroll requires authentication."
            Write-Fail "    No consumer sends credentials for a CRL. It must be ANONYMOUS."
            $realFailure++
        } elseif ($msg -match '403') {
            Write-Fail "    403 -- is Require SSL set on /CertEnroll? It must NOT be."
            $realFailure++
        } else {
            Write-Fail "    fetch failed: $(Format-Safe $msg)"
            $realFailure++
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
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
Write-Ok "All $($targets.Count) url(s): single-homed, fetched, and content-checked"
Write-Info "CRL signatures were not verified -- that is Publish-LabRootTrust.ps1's chain check."
exit 0
