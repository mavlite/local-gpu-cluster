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

    Dry run by default; pass -Apply to perform the installation.

.PARAMETER Apply
    Actually install ADCS. Without -Apply, the script only previews the
    configuration and exits without making changes.

.EXAMPLE
    .\Install-LabCA.ps1
    Dry run: shows CAPolicy.inf contents and exits without installing.

.EXAMPLE
    .\Install-LabCA.ps1 -Apply
    Installs ADCS with the lab CA configuration.
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ helpers --
function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }
function Write-Warn { param([string]$m) Write-Host "  [!] $m" -ForegroundColor Yellow }

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
    Write-Host ""
    exit 0
}

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
Write-Info "Copying to C:\Windows\CAPolicy.inf"
try {
    Copy-Item $inf 'C:\Windows\CAPolicy.inf' -Force -ErrorAction Stop
    Write-Ok "CAPolicy.inf deployed"
} catch {
    Write-Host "FATAL: Could not copy CAPolicy.inf to C:\Windows" -ForegroundColor Red
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
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
Write-Ok "CA installation complete. Run Test-LabCAHealth.ps1 to verify."
