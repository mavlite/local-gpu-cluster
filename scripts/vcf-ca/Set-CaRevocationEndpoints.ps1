<#
.SYNOPSIS
    Points the CA's CDP and AIA at a single-homed HTTP name. [OPERATOR], on dns01.

.DESCRIPTION
    Run this immediately after Install-LabCA.ps1 and BEFORE the CA issues
    anything. A certificate carries the CDP and AIA URLs that were configured
    at the moment it was signed; changing them afterwards does not repair a
    certificate already issued. There is no retroactive fix except reissuing,
    so the window is: install -> here -> first issuance.

    TWO PROBLEMS WITH THE DEFAULTS, both specific to this lab.

    1. AD CS builds its HTTP CDP and AIA from the CA server's own DNS name.
       Here that is dns01.knowledgeondemand.net, which resolves to TWO
       addresses -- 172.16.10.150 (lab) and 192.168.6.197 (management). A VCF
       appliance that round-robins onto 192.168.6.197 cannot reach it, so CRL
       retrieval fails intermittently, and strict validators (VCF LCM among
       them) hard-fail on a CRL they cannot retrieve. This sets both to
       pki.knowledgeondemand.net, an A record pointing only at the lab address.

    2. The default URLs are plain HTTP on port 80, which is CORRECT -- and the
       hardening script used to remove the port 80 binding outright. That is
       reconciled in Set-CAWebEnrollmentHardening.ps1, which now requires SSL
       on /CertSrv (the ESC8 surface) and leaves /CertEnroll anonymous on port
       80. Do not "fix" these URLs to HTTPS: validating an HTTPS certificate
       requires fetching a CRL, which would require validating an HTTPS
       certificate. CRLs and CA certificates are signed objects, so the
       transport does not need to supply integrity.

    WHY THE REGISTRY AND NOT certutil -setreg: these values are REG_MULTI_SZ
    and contain % tokens and backslashes. Passing a multi-line value with %
    through certutil means quoting it correctly in two layers, and a
    mis-escaped value here is baked into every certificate the CA ever issues.
    Writing the multi-string directly is exact, and it can be read back and
    compared element by element -- which this script does before reporting
    success.

    Dry run by default; pass -Apply to write.

.PARAMETER CdpHost
    DNS name used in the CDP and AIA URLs. MUST resolve to exactly one
    address, reachable by every consumer that validates these certificates.

.PARAMETER ExpectedAddress
    The single address CdpHost must resolve to. Guards against pointing the
    CDP at the wrong interface.

.PARAMETER Apply
    Actually write the values, restart certsvc and publish a fresh CRL.

.EXAMPLE
    .\Set-CaRevocationEndpoints.ps1
.EXAMPLE
    .\Set-CaRevocationEndpoints.ps1 -Apply
#>
[CmdletBinding()]
param(
    [string]$CdpHost = 'pki.knowledgeondemand.net',
    [string]$ExpectedAddress = '172.16.10.150',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }
function Write-Warn { param([string]$m) Write-Host "  [!] $m"  -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "  [X] $m"  -ForegroundColor Red }

# ------------------------------------------------------------- locate the CA --
Write-Step "Locating the CA"
$cfgRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services\CertSvc\Configuration'
if (-not (Test-Path $cfgRoot)) {
    Write-Fail "AD CS is not installed on this host ($cfgRoot missing)."
    Write-Info "Run Install-LabCA.ps1 -Apply first."
    exit 1
}
$caName = (Get-ItemProperty -Path $cfgRoot -Name Active -ErrorAction Stop).Active
$caKey  = Join-Path $cfgRoot $caName
Write-Ok "CA: $caName"

# --------------------------------------------- the name must be single-homed --
# The entire reason this script exists is a name that resolves to two
# addresses. Asserting the replacement does not share that defect is the one
# check that must never be skipped.
Write-Step "Verifying $CdpHost resolves to exactly one address"
try {
    $addrs = @(Resolve-DnsName -Name $CdpHost -Type A -DnsOnly -ErrorAction Stop |
               Where-Object { $_.PSObject.Properties.Name -contains 'IPAddress' -and $_.IPAddress } |
               ForEach-Object { $_.IPAddress })
} catch {
    Write-Fail "$CdpHost does not resolve: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    Write-Info "Create it first: scripts\vcf-dns\Set-VCFLabDnsRecord.ps1 -Apply"
    exit 1
}
if ($addrs.Count -ne 1) {
    Write-Fail "$CdpHost resolves to $($addrs.Count) addresses: $($addrs -join ', ')"
    Write-Fail "A CDP/AIA name MUST be single-homed or CRL fetches fail intermittently."
    Write-Fail "That is the exact defect this script exists to avoid -- refusing to proceed."
    exit 1
}
if ($addrs[0] -ne $ExpectedAddress) {
    Write-Fail "$CdpHost resolves to $($addrs[0]), expected $ExpectedAddress."
    Write-Info "Pass -ExpectedAddress if that is deliberate."
    exit 1
}
Write-Ok "$CdpHost -> $($addrs[0])  (single-homed)"

# -------------------------------------------------- warn if already issuing --
# Certificates already signed carry the OLD URLs and this script cannot fix
# them. Say so plainly rather than let it pass silently.
Write-Step "Checking whether the CA has already issued certificates"
$issuedRows = $null
try {
    $view = & certutil -view -restrict "Disposition=20" -out "RequestID" 2>&1
    $issuedRows = @($view | Select-String -Pattern 'Row \d+:').Count
} catch { }
if ($null -ne $issuedRows -and $issuedRows -gt 0) {
    Write-Warn "$issuedRows certificate(s) have ALREADY been issued by this CA."
    Write-Warn "They carry the OLD CDP/AIA URLs and this script cannot change them."
    Write-Warn "Reissue each one after this change, or their revocation URLs stay wrong."
} else {
    Write-Ok "No issued certificates found -- this is the right moment for this change"
}

# ---------------------------------------------------------------- the values --
# Flag prefixes are documented AD CS values, not guesses:
#   CRLPublicationURLs    1 = publish the CRL to this location
#                         2 = publish to Active Directory
#                         8 = include in the CDP extension of issued certificates
#   CACertPublicationURLs 1 = publish the CA certificate to this location
#                         2 = include in the AIA extension of issued certificates
#
# Exactly ONE http entry carries flag 8, and no ldap:/// entry does. Windows
# clients are perfectly happy with an http CDP, while the lab's Photon-based
# appliances cannot follow ldap:/// at all -- so one uniform http CDP is both
# simpler and the only form every consumer here can actually use. LDAP
# publication is kept (flag 2) because certutil -dcinfo verify looks there.
$crlUrls = @(
    '1:C:\Windows\system32\CertSrv\CertEnroll\%3%8%9.crl'
    '2:ldap:///CN=%7%8,CN=%2,CN=CDP,CN=Public Key Services,CN=Services,%6%10'
    "8:http://$CdpHost/CertEnroll/%3%8%9.crl"
)
$aiaUrls = @(
    '1:C:\Windows\system32\CertSrv\CertEnroll\%1_%3%4.crt'
    "2:http://$CdpHost/CertEnroll/%1_%3%4.crt"
)
$targets = @(
    @{ Name = 'CRLPublicationURLs';    Values = $crlUrls }
    @{ Name = 'CACertPublicationURLs'; Values = $aiaUrls }
)

Write-Step "Current values"
foreach ($t in $targets) {
    $cur = (Get-ItemProperty -Path $caKey -Name $t.Name -ErrorAction SilentlyContinue).$($t.Name)
    Write-Info "$($t.Name) :"
    if ($cur) { foreach ($v in $cur) { Write-Host "      $v" -ForegroundColor DarkGray } }
    else      { Write-Host "      (not set)" -ForegroundColor DarkGray }
}

Write-Step "New values"
foreach ($t in $targets) {
    Write-Info "$($t.Name) :"
    foreach ($v in $t.Values) { Write-Host "      $v" -ForegroundColor White }
}

if (-not $Apply) {
    Write-Host ""
    Write-Warn "DRY RUN -- nothing written. Pass -Apply to set these."
    Write-Info "Then prove it end to end with Test-CaRevocationEndpoints.ps1, which reads"
    Write-Info "the URLs out of a real issued certificate and fetches them."
    Write-Host ""
    exit 0
}

# --------------------------------------------------------------------- write --
Write-Step "Writing"
foreach ($t in $targets) {
    try {
        Set-ItemProperty -Path $caKey -Name $t.Name -Value ([string[]]$t.Values) `
            -Type MultiString -ErrorAction Stop
        Write-Ok "$($t.Name) written"
    } catch {
        Write-Fail "Could not write $($t.Name): $($_.Exception.Message)"
        exit 1
    }
}

# Read back and compare element by element. "Set-ItemProperty did not throw" is
# not evidence that the value on disk is the value intended.
Write-Step "Verifying what is actually on disk"
$bad = 0
foreach ($t in $targets) {
    $back = @((Get-ItemProperty -Path $caKey -Name $t.Name -ErrorAction Stop).$($t.Name))
    if ($back.Count -ne $t.Values.Count) {
        Write-Fail "$($t.Name): read back $($back.Count) entries, wrote $($t.Values.Count)"
        $bad++
        continue
    }
    $mismatch = 0
    for ($i = 0; $i -lt $back.Count; $i++) {
        if ($back[$i] -cne $t.Values[$i]) {
            Write-Fail "$($t.Name)[$i] mismatch:"
            Write-Fail "    wrote: $($t.Values[$i])"
            Write-Fail "    read : $($back[$i])"
            $mismatch++
        }
    }
    if ($mismatch) { $bad += $mismatch } else { Write-Ok "$($t.Name) matches exactly" }
}
if ($bad) {
    Write-Fail "On-disk values do not match what was intended -- do NOT issue anything yet."
    exit 1
}

# ------------------------------------------------ restart, then publish CRL --
# The CA reads these at service start, and a CRL published before the restart
# still carries the old URLs.
Write-Step "Restarting certsvc and publishing a fresh CRL"
try {
    Restart-Service certsvc -Force -ErrorAction Stop
    Write-Ok "certsvc restarted"
} catch {
    Write-Fail "Could not restart certsvc: $($_.Exception.Message)"
    exit 1
}

$crlOut = & certutil -CRL 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "certutil -CRL failed:"
    $crlOut | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
    exit 1
}
Write-Ok "CRL published"

$crlFile = Get-ChildItem 'C:\Windows\system32\CertSrv\CertEnroll\*.crl' -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($crlFile) { Write-Ok "on disk: $($crlFile.Name)  ($($crlFile.LastWriteTime))" }
else { Write-Warn "no .crl in CertEnroll -- IIS has nothing to serve" }

Write-Step "Done"
Write-Warn "NOT yet proven. This verified the configuration, not the result."
Write-Info "Run Test-CaRevocationEndpoints.ps1 once a certificate exists: it reads the"
Write-Info "URLs out of the real certificate and fetches them over the network."
Write-Host ""
exit 0
