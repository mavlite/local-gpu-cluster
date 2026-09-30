<#
.SYNOPSIS
    Hardens the CA's web enrolment endpoint before it becomes reachable.

.DESCRIPTION
    This script configures the /CertSrv web enrolment site to defend against ESC8
    (NTLM relay to /certsrv) by:
    - Enabling Basic authentication (SDDC Manager cannot speak Negotiate)
    - Disabling Windows authentication
    - Requiring Extended Protection for Authentication (EPA)
    - Removing the HTTP (port 80) binding to prevent cleartext credential leakage
    - Scoping inbound HTTPS to SDDC Manager and optional admin IP

    CRITICAL: The second and third steps (disable Windows auth and enable Basic auth)
    are inseparable. Basic auth over cleartext exposes credentials on the wire, which
    is why step four (removing port 80) is in the same script. Neither step is safe
    alone; all four must be applied as a unit.

    Dry run by default; pass -Apply to perform the hardening.

.PARAMETER SddcManagerIp
    IP address of the SDDC Manager host (default: 172.16.10.133).
    The firewall rule will allow HTTPS connections from this address.

.PARAMETER AdminIp
    Optional IP address of an admin workstation.
    If provided, the firewall rule will also allow HTTPS from this address.

.PARAMETER Apply
    Actually apply the hardening. Without -Apply, the script only previews the
    changes and exits without modifying anything.

.REQUIRES -RunAsAdministrator

.EXAMPLE
    .\Set-CAWebEnrollmentHardening.ps1
    Dry run: shows what would be changed and exits without making changes.

.EXAMPLE
    .\Set-CAWebEnrollmentHardening.ps1 -AdminIp 192.168.1.100 -Apply
    Hardens the web enrolment with admin access and applies all changes.
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$SddcManagerIp = '172.16.10.133',
    [string]$AdminIp,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module WebAdministration

# ------------------------------------------------------------------ helpers --
function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }
function Write-Warn { param([string]$m) Write-Host "  [!] $m" -ForegroundColor Yellow }

# ---------------------------------------------- IIS authentication hardening --
Write-Step "Hardening /CertSrv authentication"
Write-Info "Configuring Basic auth + EPA for SDDC Manager compatibility"
Write-Info "Disabling Windows auth to enforce credentials"
Write-Host ""

$actions = @(
    @{
        what = 'Enable Basic auth on /CertSrv (SDDC Manager cannot speak Negotiate)'
        do   = {
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/authentication/basicAuthentication `
                -Name enabled -Value $true `
                -PSPath 'IIS:\' `
                -Location 'Default Web Site/CertSrv'
        }
    }
    @{
        what = 'Disable Windows auth on /CertSrv'
        do   = {
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/authentication/windowsAuthentication `
                -Name enabled -Value $false `
                -PSPath 'IIS:\' `
                -Location 'Default Web Site/CertSrv'
        }
    }
    @{
        what = 'Require Extended Protection for Authentication (blocks ESC8 relay)'
        do   = {
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/authentication/basicAuthentication `
                -Name extendedProtection.tokenChecking -Value 'Require' `
                -PSPath 'IIS:\' `
                -Location 'Default Web Site/CertSrv'
        }
    }
    @{
        what = 'Remove the HTTP (port 80) binding so Basic auth never crosses cleartext'
        do   = {
            Get-WebBinding -Name 'Default Web Site' -Protocol http |
                Where-Object { $_.bindingInformation -like '*:80:*' } |
                Remove-WebBinding
        }
    }
)

foreach ($a in $actions) {
    Write-Host ("  {0} {1}" -f $(if($Apply) { 'APPLY ' } else { 'WOULD ' }), $a.what)
    if ($Apply) {
        try {
            & $a.do
        } catch {
            Write-Host "FATAL: Failed to apply: $($a.what)" -ForegroundColor Red
            Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
            exit 1
        }
    }
}

# ------------------------------------------------- firewall rule hardening --
Write-Step "Configuring firewall rules"

$allow = @($SddcManagerIp)
if ($AdminIp) {
    $allow += $AdminIp
}

Write-Host ("  {0} scope inbound 443 to: {1}" -f $(if($Apply) { 'APPLY ' } else { 'WOULD ' }), ($allow -join ', '))
if ($Apply) {
    try {
        New-NetFirewallRule `
            -DisplayName 'CertSrv HTTPS (scoped)' `
            -Direction Inbound `
            -Protocol TCP `
            -LocalPort 443 `
            -RemoteAddress $allow `
            -Action Allow `
            -ErrorAction Stop | Out-Null
        Write-Ok "Firewall rule created"
    } catch {
        Write-Host "FATAL: Could not create firewall rule" -ForegroundColor Red
        Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

# ------------------------------------------------- completion message --
if (-not $Apply) {
    Write-Warn "DRY RUN -- pass -Apply to apply hardening"
    Write-Host ""
    exit 0
}

Write-Step "Web enrolment hardening complete"
Write-Ok "All hardening steps applied successfully"
Write-Host ""
