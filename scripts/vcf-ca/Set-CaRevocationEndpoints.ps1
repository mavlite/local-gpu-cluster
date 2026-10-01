<#
.SYNOPSIS
    Repoints the CA's CDP and AIA at a single-homed name. [OPERATOR], on dns01.

.DESCRIPTION
    Run this immediately after Install-LabCA.ps1 and BEFORE the CA issues
    anything. A certificate carries the CDP and AIA URLs configured at the
    moment it was signed; changing them afterwards does not repair a
    certificate already issued. The only remedy is reissuing it, so the window
    is: install -> here -> first issuance.

    THE PROBLEM. AD CS builds its HTTP CDP and AIA from the CA server's own DNS
    name. Here that is dns01.knowledgeondemand.net, which resolves to TWO
    addresses -- 172.16.10.150 (lab) and 192.168.6.197 (management). A VCF
    appliance that round-robins onto the management address cannot reach it, so
    CRL retrieval fails intermittently, and strict validators (VCF LCM among
    them) hard-fail on a CRL they cannot retrieve.

    WHAT THIS SCRIPT DOES, AND DELIBERATELY DOES NOT DO. It rewrites only the
    HOST of the existing http:// entries. Flag prefixes, paths and % tokens are
    preserved byte for byte.

    That restraint is the whole design. The flag prefixes control whether a URL
    reaches the CDP extension of issued certificates, and getting one wrong
    writes a value that reads back perfectly while every certificate the CA
    issues carries an unusable revocation URL -- unfixable after issuance. The
    published documentation for those bits is thin and secondary sources
    disagree: an archived Microsoft thread shows a working root CA using
    "10:http://..." and states the web location is the only one in the CDP
    extension of issued certificates, which puts the bit at 8, while other
    sources assert 2. Rather than bet an unfixable setting on resolving that,
    this script keeps whatever AD CS itself configured -- which is correct by
    construction, since those defaults already produce working certificates --
    and changes only the thing that is demonstrably wrong: the hostname.

    If an http entry is missing entirely, the script says so and stops rather
    than inventing one with a guessed flag. Add it through the CA MMC
    (certsrv.msc -> CA properties -> Extensions), where the checkbox
    "Include in the CDP extension of issued certificates" sets the correct bit
    without anyone doing bit arithmetic, then re-run.

    CDP AND AIA ARE PLAIN HTTP BY DESIGN. Serving them over HTTPS is circular:
    validating the HTTPS certificate requires fetching a CRL, which would
    require validating an HTTPS certificate. CRLs and CA certificates are
    signed, so the transport need not supply integrity. Do not "upgrade" these
    URLs to 443.

    CONFIGURATION VERIFIED IS NOT REVOCATION WORKING. This script's read-back
    compares what it wrote against what it meant to write, which cannot detect
    a wrong flag. The acceptance gate is Test-CaRevocationEndpoints.ps1 run
    against a real issued certificate. The completion message says so, and it
    is not optional.

    Dry run by default; pass -Apply to write.

.PARAMETER CdpHost
    DNS name to put in the CDP and AIA URLs. MUST resolve to exactly one
    address, reachable by every consumer that validates these certificates.

.PARAMETER ExpectedAddress
    The single address CdpHost must resolve to. Guards against repointing the
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

# Runs a native command with stderr captured, returning text and exit code.
# Native stderr under $ErrorActionPreference='Stop' raises NativeCommandError
# BEFORE the exit code can be read, so the preference is relaxed locally and
# restored in finally. Without this, a certutil warning line aborts the script.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $out = & $Exe @Arguments 2>&1
        return @{ Output = @($out); ExitCode = $LASTEXITCODE }
    } finally { $ErrorActionPreference = $prev }
}

# ------------------------------------------------------------- locate the CA --
Write-Step "Locating the CA"
$cfgRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services\CertSvc\Configuration'
if (-not (Test-Path -LiteralPath $cfgRoot)) {
    Write-Fail "AD CS is not installed on this host ($cfgRoot missing)."
    Write-Info "Run Install-LabCA.ps1 -Apply first."
    exit 1
}
$caName = (Get-ItemProperty -LiteralPath $cfgRoot -Name Active -ErrorAction Stop).Active
# -LiteralPath throughout: a CA name containing [ or ] is a wildcard to -Path.
$caKey = Join-Path $cfgRoot $caName
Write-Ok "CA: $caName"

# --------------------------------------------- the name must be single-homed --
# The entire reason this script exists is a name resolving to two addresses.
# Asserting the replacement does not share that defect is the one check that
# must never be skipped.
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
    Write-Fail "That is the exact defect this script exists to remove -- refusing to proceed."
    exit 1
}
if ($addrs[0] -ne $ExpectedAddress) {
    Write-Fail "$CdpHost resolves to $($addrs[0]), expected $ExpectedAddress."
    Write-Info "Pass -ExpectedAddress if that is deliberate."
    exit 1
}
Write-Ok "$CdpHost -> $($addrs[0])  (single-homed)"

# -------------------------------------------------- has it issued anything? --
# Certificates already signed carry the OLD urls and this script cannot fix
# them. "I could not query the CA database" is NOT the same as "nothing has
# been issued", and an earlier version printed the reassuring message for both.
Write-Step "Checking whether the CA has already issued certificates"
$res = Invoke-Native -Exe 'certutil' -Arguments @('-view', '-restrict', 'Disposition=20', '-out', 'RequestID')
if ($res.ExitCode -ne 0) {
    Write-Warn "Could not query the CA database (certutil exit $($res.ExitCode))."
    Write-Warn "Issuance state is UNKNOWN -- this is not a statement that nothing was issued."
    Write-Info "If certsvc is stopped, start it and re-run to get a real answer."
} else {
    $issuedRows = @($res.Output | Select-String -Pattern 'Row \d+:').Count
    if ($issuedRows -gt 0) {
        Write-Warn "$issuedRows certificate(s) have ALREADY been issued by this CA."
        Write-Warn "They carry the OLD CDP/AIA urls and this script cannot change them."
        Write-Warn "Reissue each one after this change, or their revocation urls stay wrong."
    } else {
        Write-Ok "CA database queried successfully: no issued certificates"
    }
}

# ------------------------------------------- rewrite the host, nothing else --
$targets = @(
    @{ Name = 'CRLPublicationURLs';    Label = 'CDP' }
    @{ Name = 'CACertPublicationURLs'; Label = 'AIA' }
)

<#
    Rewrites ONLY the authority of an http(s) publication entry, preserving the
    flag prefix, scheme, path and % tokens byte for byte. Returns a hashtable:
      Changed  -- $true if the entry was an http entry whose host differs
      IsHttp   -- $true if the entry is an http(s) entry at all
      Value    -- the entry to write (rewritten, or the original untouched)
      OldHost  -- the authority that was replaced, when IsHttp

    Entries that are not http(s) -- local paths, file://, ldap:/// -- are
    returned untouched. The match is textual rather than a [Uri] round-trip,
    because [Uri] re-encodes the % tokens (%1, %3%8%9) that AD CS expands at
    issuance time, which would corrupt every path it touched.

    The authority may itself be a token: AD CS writes http://%1/CertEnroll/...
    where %1 is the CA server's DNS name. Substituting it is the entire point.
#>
function Convert-PublicationUrlHost {
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Entry,
        [Parameter(Mandatory)][string]$NewHost
    )
    # rest is optional: an entry with no path ("2:http://host") must still be
    # recognised as http, or it would be silently left pointing at the old name.
    $pattern = '^(?<flags>\d+):(?<scheme>https?://)(?<authority>[^/]+)(?<rest>/.*)?$'
    $m = [regex]::Match($Entry, $pattern)
    if (-not $m.Success) {
        return @{ Changed = $false; IsHttp = $false; Value = $Entry; OldHost = $null }
    }
    $rebuilt = '{0}:{1}{2}{3}' -f $m.Groups['flags'].Value, $m.Groups['scheme'].Value,
                                  $NewHost, $m.Groups['rest'].Value
    return @{
        Changed = ($rebuilt -cne $Entry)
        IsHttp  = $true
        Value   = $rebuilt
        OldHost = $m.Groups['authority'].Value
    }
}

$plan = @()
$problems = 0
foreach ($t in $targets) {
    Write-Step "$($t.Label) -- $($t.Name)"
    $current = @((Get-ItemProperty -LiteralPath $caKey -Name $t.Name -ErrorAction SilentlyContinue).$($t.Name))
    if (-not $current -or $current.Count -eq 0) {
        Write-Fail "$($t.Name) is not set at all. That is not an AD CS default state."
        Write-Info "Configure it in certsrv.msc -> CA properties -> Extensions, then re-run."
        $problems++
        continue
    }

    $newVals = @()
    $httpSeen = 0
    foreach ($entry in $current) {
        $r = Convert-PublicationUrlHost -Entry $entry -NewHost $CdpHost
        if (-not $r.IsHttp) {
            # A local path, file:// or ldap:/// entry. Left exactly as found.
            Write-Info "keep   $entry"
            $newVals += $r.Value
            continue
        }
        $httpSeen++
        if ($r.Changed) {
            Write-Info "change $entry"
            Write-Host  "    ->  $($r.Value)" -ForegroundColor White
            Write-Info "       flag prefix preserved; only the host changed ($($r.OldHost) -> $CdpHost)"
        } else {
            Write-Ok "keep   $entry   (already points at $CdpHost)"
        }
        $newVals += $r.Value
    }

    if ($httpSeen -eq 0) {
        Write-Fail "No http:// entry in $($t.Name) -- there is nothing to repoint."
        Write-Fail "This script will NOT invent one: the flag prefix that puts a url into"
        Write-Fail "the extension of issued certificates is the one value that must not be"
        Write-Fail "guessed, and a wrong one cannot be fixed after issuance."
        Write-Info "Add it in certsrv.msc -> CA properties -> Extensions. For $($t.Label), tick"
        if ($t.Label -eq 'CDP') {
            Write-Info "  'Include in the CDP extension of issued certificates'"
        } else {
            Write-Info "  'Include in the AIA extension of issued certificates'"
        }
        Write-Info "then re-run this script to repoint the host."
        $problems++
        continue
    }
    $plan += @{ Name = $t.Name; Label = $t.Label; Entries = $newVals }
}

if ($problems) {
    Write-Step "Stopping"
    Write-Fail "$problems problem(s) above must be resolved first. Nothing was written."
    exit 1
}

if (-not $Apply) {
    Write-Host ""
    Write-Warn "DRY RUN -- nothing written. Pass -Apply to set these."
    Write-Host ""
    exit 0
}

# --------------------------------------------------------------------- write --
Write-Step "Writing"
foreach ($t in $plan) {
    try {
        Set-ItemProperty -LiteralPath $caKey -Name $t.Name -Value ([string[]]$t.Entries) `
            -Type MultiString -ErrorAction Stop
        Write-Ok "$($t.Name) written"
    } catch {
        Write-Fail "Could not write $($t.Name): $($_.Exception.Message)"
        exit 1
    }
}

# Read back and compare element by element. "Set-ItemProperty did not throw" is
# not evidence that the value on disk is the value intended. Note what this
# CANNOT catch: a wrong flag prefix. It compares intent against intent.
Write-Step "Verifying what is actually on disk"
$bad = 0
foreach ($t in $plan) {
    $back = @((Get-ItemProperty -LiteralPath $caKey -Name $t.Name -ErrorAction Stop).$($t.Name))
    if ($back.Count -ne $t.Entries.Count) {
        Write-Fail "$($t.Name): read back $($back.Count) entries, wrote $($t.Entries.Count)"
        $bad++
        continue
    }
    $mismatch = 0
    for ($i = 0; $i -lt $back.Count; $i++) {
        if ($back[$i] -cne $t.Entries[$i]) {
            Write-Fail "$($t.Name)[$i] mismatch:"
            Write-Fail "    wrote: $($t.Entries[$i])"
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
# still carries the old urls.
Write-Step "Restarting certsvc and publishing a fresh CRL"
try {
    Restart-Service certsvc -Force -ErrorAction Stop
    Write-Ok "certsvc restarted"
} catch {
    Write-Fail "Could not restart certsvc: $($_.Exception.Message)"
    exit 1
}

# certutil -CRL commonly answers "RPC server is unavailable" for a few seconds
# after a restart, while the CA finishes initialising. Retry rather than abort
# with the registry already written.
$published = $false
foreach ($attempt in 1..6) {
    $crl = Invoke-Native -Exe 'certutil' -Arguments @('-CRL')
    if ($crl.ExitCode -eq 0) { $published = $true; break }
    Write-Info "attempt $attempt/6: certutil -CRL exit $($crl.ExitCode), waiting for the CA to initialise"
    if ($attempt -lt 6) { Start-Sleep -Seconds 5 } else { $crl.Output | ForEach-Object { Write-Host "      $_" -ForegroundColor Red } }
}
if (-not $published) {
    Write-Fail "certutil -CRL did not succeed. The registry values ARE written."
    Write-Info "Publish by hand once the CA is up:  certutil -CRL"
    exit 1
}
Write-Ok "CRL published"

$crlFile = Get-ChildItem 'C:\Windows\system32\CertSrv\CertEnroll\*.crl' -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($crlFile) {
    Write-Ok "on disk: $($crlFile.Name)  ($($crlFile.LastWriteTime))"
    Write-Info "serve-test url: http://$CdpHost/CertEnroll/$($crlFile.Name)"
} else {
    Write-Warn "no .crl in CertEnroll -- IIS has nothing to serve"
}

Write-Step "NOT YET PROVEN -- do this before issuing anything real"
Write-Warn "The read-back above compared what was written against what was intended."
Write-Warn "It CANNOT detect a wrong flag prefix, which is the failure that matters."
Write-Host ""
Write-Info "1. Issue ONE throwaway certificate from this CA (any template)."
Write-Info "2. Run the end-to-end gate against it:"
Write-Info "     .\Test-CaRevocationEndpoints.ps1 -CertificateFile .\throwaway.cer"
Write-Info "   It reads the urls out of the certificate itself and fetches them, so a"
Write-Info "   flag that never reached the CDP extension shows up there and nowhere else."
Write-Info "3. Only once that exits 0, proceed to the template and rotation steps."
Write-Host ""
exit 0
