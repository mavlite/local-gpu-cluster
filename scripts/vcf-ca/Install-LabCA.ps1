<#
.SYNOPSIS
    Installs Active Directory Certificate Services (ADCS) with install-time
    configuration for the lab's root CA.

.DESCRIPTION
    This script installs ADCS with a root CA named 'knowledgeondemand-LabRoot-CA'
    configured with:
    - 10-year root certificate validity (install-time only)
    - 52-week CRL publication period (install-time only)
    - RSA 2048-bit keys with SHA-256 signatures (install-time only)
    - Web Enrolment role service for manual certificate requests
    - LoadDefaultTemplates disabled to prevent autoenrollment attack surface

    All settings marked "(install-time only)" cannot be changed after CA deployment
    without uninstalling the CA, rebuilding the machine, and re-rooting the entire
    trust fabric. Verify the final state with:
        certutil -cainfo | Select-String 'Validity|Name'
        certutil -getreg CA\CRLPeriodUnits

    THIS IS THE LAST ROLLBACK POINT IN THE ENTIRE PROJECT. Once the CA exists,
    dns01 must never be rolled back -- restoring an earlier image rolls the CA
    database back and reuses serial numbers that are already issued and already
    trusted elsewhere. So the moment BEFORE this script's -Apply is the final
    moment a restorable copy is usable. Take one now. The script requires a
    typed confirmation that you have.

    A VM SNAPSHOT IS NOT AVAILABLE for this VM. dns01 runs on the standalone
    management host, which has Software Memory Tiering enabled, and ESXi
    refuses snapshots there; snapshot-reverting a domain controller would risk
    USN rollback in any case. Take the copy with New-Dns01RollbackClone.ps1,
    which shuts the guest down cleanly and clones it host-locally.

    Dry run by default; pass -Apply to perform the installation.

.PARAMETER Confirmation
    Supply the literal string CONFIRM to skip the interactive prompt (for a
    console where Read-Host is not available). Any other value is refused.

.PARAMETER Apply
    Actually install ADCS. Without -Apply, the script only previews the
    configuration and exits without making changes.

.EXAMPLE
    .\Install-LabCA.ps1
    Dry run: shows CAPolicy.inf contents and exits without installing.

.EXAMPLE
    .\Install-LabCA.ps1 -Apply
    Clone dns01 first (New-Dns01RollbackClone.ps1), then installs ADCS with
    the lab CA configuration.
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Confirmation,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ helpers --
function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }
function Write-Warn { param([string]$m) Write-Host "  [!] $m" -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "  [X] $m" -ForegroundColor Red }

# --------------------------------------------------------- locate CAPolicy.inf --
$inf = Join-Path $PSScriptRoot 'CAPolicy.inf'
if (-not (Test-Path $inf)) {
    Write-Host "FATAL: CAPolicy.inf not found at $inf" -ForegroundColor Red
    exit 1
}
Write-Step "CAPolicy.inf found"
Write-Ok "Located at $inf"

# -------------------------------------------------------- preview the policy --
Write-Step "Configuration to be deployed"
Write-Info "The following will be copied to C:\Windows\CAPolicy.inf:"
Write-Host ""
Get-Content $inf | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
Write-Host ""

# ---------------------------------------------------- dry-run guard and exit --
if (-not $Apply) {
    Write-Warn "DRY RUN -- pass -Apply to perform the installation"
    Write-Warn "Before you do: take the rollback COPY of dns01 -- the last point at which you can."
    Write-Info "  .\New-Dns01RollbackClone.ps1 -Apply   (a VM snapshot is NOT possible on this host)"
    Write-Host ""
    exit 0
}

# ------------------------------------------- idempotency: already installed? --
# A re-run after a partial failure used to hit Install-AdcsCertificationAuthority
# on an already-configured CA and exit FATAL -- after having already overwritten
# C:\Windows\CAPolicy.inf with -Force and no backup. Short-circuit instead.
Write-Step "Checking whether ADCS is already installed"
$caFeature = $null
try {
    $caFeature = Get-WindowsFeature -Name ADCS-Cert-Authority -ErrorAction Stop
} catch {
    Write-Fail "FATAL: Could not query Windows features: $($_.Exception.Message)"
    exit 1
}
if ($caFeature -and $caFeature.Installed) {
    Write-Warn "ADCS-Cert-Authority is already Installed on this host."
    Write-Info "This script will NOT re-run the installation: Install-AdcsCertificationAuthority"
    Write-Info "fails on a configured CA, and re-copying CAPolicy.inf now would have no effect"
    Write-Info "anyway (it is read only at promotion time)."
    Write-Info "Verify the existing CA instead:"
    Write-Host ""
    Write-Host "    certutil -cainfo | Select-String 'Validity|Name'" -ForegroundColor DarkGray
    Write-Host "    certutil -getreg CA\CRLPeriodUnits" -ForegroundColor DarkGray
    Write-Host ""
    Write-Info "If the existing CA is wrong, removing it is a rebuild, not a re-run --"
    Write-Info "see the design doc's placement section before touching it."
    exit 0
}

# ------------------------------------------- last rollback point: confirm it --
Write-Step "LAST ROLLBACK POINT -- take the rollback COPY of dns01 NOW"
Write-Warn "Once this CA exists, dns01 must NEVER be rolled back: restoring an earlier"
Write-Warn "image rolls the CA database back and reuses serial numbers that are already"
Write-Warn "issued and already trusted elsewhere. Every later step in this project is"
Write-Warn "therefore forward-only. THIS is the last moment a copy of dns01 is usable."
Write-Host ""
Write-Warn "A VM SNAPSHOT IS NOT AVAILABLE here: the management host has Software Memory"
Write-Warn "Tiering enabled and ESXi refuses snapshots on it. Use the cold clone instead:"
Write-Info "  .\New-Dns01RollbackClone.ps1 -Apply"
Write-Host ""
Write-Info "Take the copy before answering. Then type CONFIRM to proceed."

$answer = $Confirmation
if (-not $answer) {
    $answer = Read-Host "Rollback copy taken? Type CONFIRM to install the CA"
}
if ($answer -cne 'CONFIRM') {
    Write-Fail "Not confirmed (got '$answer') -- nothing was changed."
    Write-Info "Re-run with -Apply once the rollback copy of dns01 exists."
    exit 1
}
Write-Ok "Confirmed"

# --------------------------------------------------------- install features --
Write-Step "Installing ADCS Windows Features"
Write-Info "Installing: ADCS-Cert-Authority, ADCS-Web-Enrollment"
try {
    Install-WindowsFeature -Name ADCS-Cert-Authority, ADCS-Web-Enrollment `
        -IncludeManagementTools -ErrorAction Stop | Out-Null
    Write-Ok "Features installed"
} catch {
    Write-Host "FATAL: Could not install ADCS features" -ForegroundColor Red
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------ copy CAPolicy to Windows --
Write-Step "Deploying CAPolicy.inf"

# Back up any pre-existing file before -Force overwrites it. Something else on
# this host may have put one there, and losing it silently is not acceptable on
# a domain controller.
$target = 'C:\Windows\CAPolicy.inf'
if (Test-Path $target) {
    $backup = "$target.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Write-Warn "An existing $target is present and will be overwritten."
    try {
        Copy-Item $target $backup -ErrorAction Stop
        Write-Ok "Backed up to $backup"
    } catch {
        Write-Fail "FATAL: Could not back up the existing CAPolicy.inf"
        Write-Fail "  Error: $($_.Exception.Message)"
        Write-Info "Refusing to overwrite a file that could not be backed up."
        exit 1
    }
}

Write-Info "Copying to $target"
try {
    Copy-Item $inf $target -Force -ErrorAction Stop
    Write-Ok "CAPolicy.inf deployed"
} catch {
    Write-Fail "FATAL: Could not copy CAPolicy.inf to C:\Windows"
    Write-Fail "  Error: $($_.Exception.Message)"
    exit 1
}

# --------------------------------------------------- configure the root CA --
Write-Step "Installing Enterprise Root CA"
Write-Info "CA Name: knowledgeondemand-LabRoot-CA"
Write-Info "Key Length: 2048-bit RSA"
Write-Info "Hash: SHA-256"
Write-Info "Validity: 10 years (from CAPolicy.inf)"

try {
    Install-AdcsCertificationAuthority `
        -CAType EnterpriseRootCA `
        -CACommonName 'knowledgeondemand-LabRoot-CA' `
        -KeyLength 2048 `
        -HashAlgorithmName SHA256 `
        -CryptoProviderName 'RSA#Microsoft Software Key Storage Provider' `
        -ValidityPeriod Years `
        -ValidityPeriodUnits 10 `
        -Force -ErrorAction Stop | Out-Null
    Write-Ok "Enterprise Root CA installed"
} catch {
    Write-Host "FATAL: Could not install ADCS Certification Authority" -ForegroundColor Red
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------- configure web enrolment --
Write-Step "Installing ADCS Web Enrollment role service"
try {
    Install-AdcsWebEnrollment -Force -ErrorAction Stop | Out-Null
    Write-Ok "Web Enrollment installed"
} catch {
    Write-Host "FATAL: Could not install Web Enrollment" -ForegroundColor Red
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------ verify the CA --
Write-Step "CA deployment complete"
Write-Info "Verify the installation before proceeding:"
Write-Host ""
Write-Host "    certutil -cainfo | Select-String 'Validity|Name'" -ForegroundColor DarkGray
Write-Host "    Expected: CA name 'knowledgeondemand-LabRoot-CA', validity '10 years'" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    certutil -getreg CA\CRLPeriodUnits" -ForegroundColor DarkGray
Write-Host "    Expected: CRLPeriodUnits = 52" -ForegroundColor DarkGray
Write-Host ""
Write-Info "These are install-time values and cannot be changed without rebuilding the CA."
Write-Host ""
Write-Warn "This CA has published NO certificate templates (LoadDefaultTemplates=0)."
Write-Warn "Nothing autoenrols yet -- not even this domain controller, so LDAPS will"
Write-Warn "NOT start until you publish 'Domain Controller Authentication' by hand."
Write-Info "Next: Set-CAWebEnrollmentHardening.ps1 -Apply, then"
Write-Info "      New-VcfCertificateTemplate.ps1 (section A) to publish the DC template."
Write-Host ""
Write-Ok "CA installation complete."
