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

    CRITICAL: Port 80 removal happens FIRST, before enabling Basic auth, to prevent a
    cleartext window. Basic auth over cleartext exposes credentials on the wire. Disabling
    Windows auth and enabling Basic auth must happen together. Neither step is safe alone;
    all hardening steps must be applied as a unit.

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
Write-Step "Hardening /CertSrv and /CertEnroll authentication"
Write-Info "Configuring Basic auth + EPA for SDDC Manager compatibility"
Write-Info "Disabling Windows auth to enforce credentials"
Write-Host ""

# CRITICAL: Remove port 80 FIRST, before enabling Basic auth. This prevents a cleartext window
# where Basic auth is enabled but :80 is still bound. If any subsequent action fails, we have
# already closed the cleartext exposure rather than creating it.
$actions = @(
    @{
        what = 'Remove the HTTP (port 80) binding so Basic auth never crosses cleartext'
        do   = {
            Get-WebBinding -Name 'Default Web Site' -Protocol http |
                Where-Object { $_.bindingInformation -like '*:80:*' } |
                Remove-WebBinding
        }
    }
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
        what = 'Require Extended Protection for Authentication on /CertSrv (blocks ESC8 relay)'
        do   = {
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/authentication/basicAuthentication `
                -Name extendedProtection.tokenChecking -Value 'Require' `
                -PSPath 'IIS:\' `
                -Location 'Default Web Site/CertSrv'
        }
    }
    @{
        what = 'Enable Basic auth on /CertEnroll (if present)'
        do   = {
            try {
                Set-WebConfigurationProperty `
                    -Filter /system.webServer/security/authentication/basicAuthentication `
                    -Name enabled -Value $true `
                    -PSPath 'IIS:\' `
                    -Location 'Default Web Site/CertEnroll' `
                    -ErrorAction Stop
            } catch {
                Write-Warn "CertEnroll vdir not found; skipping CertEnroll auth configuration"
            }
        }
    }
    @{
        what = 'Disable Windows auth on /CertEnroll (if present)'
        do   = {
            try {
                Set-WebConfigurationProperty `
                    -Filter /system.webServer/security/authentication/windowsAuthentication `
                    -Name enabled -Value $false `
                    -PSPath 'IIS:\' `
                    -Location 'Default Web Site/CertEnroll' `
                    -ErrorAction Stop
            } catch {
                # Already warned above if CertEnroll missing; don't repeat
            }
        }
    }
    @{
        what = 'Require Extended Protection for Authentication on /CertEnroll (if present)'
        do   = {
            try {
                Set-WebConfigurationProperty `
                    -Filter /system.webServer/security/authentication/basicAuthentication `
                    -Name extendedProtection.tokenChecking -Value 'Require' `
                    -PSPath 'IIS:\' `
                    -Location 'Default Web Site/CertEnroll' `
                    -ErrorAction Stop
            } catch {
                # Already warned above if CertEnroll missing; don't repeat
            }
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

# Audit for pre-existing broad Allow rules that would bypass scoping.
# Windows Firewall evaluates all matching Allow rules with OR semantics; a scoped Allow
# does not narrow a broader pre-existing Allow. Task 1 commonly enables
# "World Wide Web Services (HTTP/HTTPS Traffic-In)" which allows 443 from anywhere.
# CRITICAL: Only TCP rules on port 443 are relevant. Rules with no port concept (ICMP, IGMP)
# must never be disabled. Get-NetFirewallPortFilter returns LocalPort='Any' for protocol-less
# rules; filter by protocol=TCP to avoid silencing ICMP on this domain controller.
Write-Info "Auditing for pre-existing broad Allow TCP rules on port 443"
$broadRules = @()
try {
    $allInboundRules = Get-NetFirewallRule -Direction Inbound -Action Allow -ErrorAction Stop
    foreach ($rule in $allInboundRules) {
        $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
        # Gate on protocol: only TCP rules can legitimately listen on port 443.
        # ICMP, IGMP, and other protocol-less rules must be skipped regardless of RemoteAddress.
        $proto = $port.Protocol
        if ($proto -ne 'TCP') {
            continue
        }
        if ($port.LocalPort -eq 443 -or $port.LocalPort -eq 'Any') {
            $addr = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
            if ($addr.RemoteAddress -eq 'Any' -or $addr.RemoteAddress -eq '0.0.0.0/0' -or $addr.RemoteAddress -eq '::/0') {
                $broadRules += $rule.DisplayName
            }
        }
    }
} catch {
    Write-Host "FATAL: Could not audit firewall rules" -ForegroundColor Red
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if ($broadRules.Count -gt 0) {
    Write-Host "  [!] Pre-existing Allow rules allow TCP 443 from anywhere, bypassing scoping:" -ForegroundColor Yellow
    foreach ($ruleName in $broadRules) {
        Write-Host "      - $ruleName" -ForegroundColor Yellow
    }
    Write-Host "  [!] These must be disabled to enforce scoping." -ForegroundColor Yellow
    if ($Apply) {
        Write-Info "Disabling pre-existing broad TCP 443 rules:"
        foreach ($ruleName in $broadRules) {
            Write-Info "  Disabling: $ruleName"
            try {
                Disable-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
                Write-Ok "Disabled: $ruleName"
            } catch {
                Write-Host "FATAL: Could not disable rule $ruleName" -ForegroundColor Red
                Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
                exit 1
            }
        }
    } else {
        Write-Warn "DRY RUN: pass -Apply to disable these rules"
        exit 1
    }
}

$allow = @($SddcManagerIp)
if ($AdminIp) {
    $allow += $AdminIp
}

# Remove the old scoped rule if it exists (idempotency). DisplayName is not unique, but
# we use it as the key. If re-running with different -AdminIp, remove the stale rule
# before creating a new one so the old IP does not persist.
Write-Info "Checking for existing scoped rule to remove (idempotency)"
$existingRule = Get-NetFirewallRule -DisplayName 'CertSrv HTTPS (scoped)' -Direction Inbound -ErrorAction SilentlyContinue
if ($existingRule) {
    Write-Host ("  {0} remove stale scoped rule (re-running with new -AdminIp?)" -f $(if($Apply) { 'APPLY ' } else { 'WOULD ' }))
    if ($Apply) {
        try {
            $existingRule | Remove-NetFirewallRule -ErrorAction Stop
            Write-Ok "Stale scoped rule removed"
        } catch {
            Write-Host "FATAL: Could not remove stale firewall rule" -ForegroundColor Red
            Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor Red
            exit 1
        }
    }
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
Write-Warn "IMPORTANT: /certsrv is now unreachable until Task 4 binds the HTTPS endpoint"
Write-Info "This is expected. /certsrv will be inaccessible for ~90-120 minutes due to"
Write-Info "GPO autoenrollment wait times. No consumer of /certsrv exists until Task 6."
Write-Host ""
