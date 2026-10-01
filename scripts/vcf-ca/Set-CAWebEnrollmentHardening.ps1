<#
.SYNOPSIS
    Hardens the CA's web enrolment endpoint before it becomes reachable.

.DESCRIPTION
    This script configures the /CertSrv web enrolment site to defend against ESC8
    (NTLM relay to /certsrv) by:
    - Requiring SSL on /CertSrv so Basic auth is never offered over cleartext
    - Enabling Basic authentication (SDDC Manager cannot speak Negotiate)
    - DISABLING Windows (Negotiate/NTLM) authentication
    - Setting Extended Protection to Require on windowsAuthentication as
      defence in depth, in case anyone ever re-enables it
    - Scoping inbound HTTPS to SDDC Manager and an optional admin IP
    - Leaving /CertEnroll ANONYMOUS on port 80, and allowing port 80 from the
      lab subnet, so CDP and AIA are actually reachable

    TWO VIRTUAL DIRECTORIES, OPPOSITE TREATMENTS -- and this script used to get
    the second one backwards. /CertSrv authenticates, so it is the ESC8 surface
    and gets locked down. /CertEnroll does NOT authenticate: it is a static
    directory holding the CRL and the CA certificate, both signed objects, and
    there is no authentication there to relay. An earlier version of this
    script enabled Basic auth on /CertEnroll and removed the port 80 binding
    outright. Neither hardened anything, and together they broke revocation
    checking for the whole lab: a CRL fetch answers 401, and no consumer sends
    credentials when fetching a CRL.

    CDP AND AIA ARE PLAIN HTTP BY DESIGN, not by oversight. Serving them over
    HTTPS is circular -- validating the HTTPS certificate requires fetching a
    CRL, which would require validating an HTTPS certificate. The objects are
    signed, so the transport does not need to supply integrity.

    WHAT ACTUALLY CLOSES ESC8 HERE IS DISABLING NEGOTIATE. ESC8 is an NTLM
    relay against the web-enrolment endpoint; with Windows authentication off
    the endpoint never accepts NTLM, so there is no authentication to relay.
    Extended Protection for Authentication (EPA) is a CHANNEL-BINDING control
    for integrated auth -- it is meaningful only while Negotiate/NTLM is
    accepted. It is set here so that re-enabling windowsAuthentication later
    does not silently reopen the relay path, not because it is the control
    doing the work. EPA is NOT a property of basicAuthentication: the IIS
    basicAuthentication schema carries enabled/realm/defaultLogonDomain/
    logonMethod and nothing else, so writing extendedProtection there either
    throws or produces a 500.19 on /CertSrv.

    CRITICAL: Require-SSL on /CertSrv happens FIRST, before enabling Basic auth,
    to prevent a cleartext window. Basic auth over cleartext exposes credentials
    on the wire. IIS evaluates the SSL requirement BEFORE issuing an auth
    challenge, so an HTTP request to /CertSrv answers 403.4 and never sees a
    Basic prompt -- the same protection the old port-80 removal gave, without
    taking /CertEnroll down with it.

    RECOVERY: if any authentication step fails part-way, the script re-asserts
    windowsAuthentication enabled=$false on both virtual directories and still
    applies the firewall scoping before exiting non-zero. A half-applied run
    must never leave Negotiate enabled on an unscoped endpoint.

    Dry run by default; pass -Apply to perform the hardening.

.PARAMETER SddcManagerIp
    IP address of the SDDC Manager host (default: 172.16.10.133).
    The firewall rule will allow HTTPS connections from this address.

.PARAMETER AdminIp
    Optional IP address of an admin workstation.
    If provided, the firewall rule will also allow HTTPS from this address.

.PARAMETER CrlConsumerSubnet
    Subnet permitted to reach /CertEnroll on port 80 (default: 172.16.10.0/24).
    Deliberately broader than the 443 scope: every host that validates a
    certificate from this CA must fetch the CRL, not just SDDC Manager.

.PARAMETER DisableBroadRules
    Disable the pre-existing inbound Allow rules that explicitly permit TCP 443
    from any address. Without this switch the script only REPORTS them; it
    never disables a firewall rule on a domain controller unasked.

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

.EXAMPLE
    .\Set-CAWebEnrollmentHardening.ps1 -Apply -DisableBroadRules
    As above, and also disables the reported broad TCP 443 rules.
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$SddcManagerIp = '172.16.10.133',
    [string]$AdminIp,
    # Every consumer that validates a certificate issued by this CA has to
    # fetch the CRL and the CA certificate from /CertEnroll over HTTP --
    # vCenter, NSX, the ESXi hosts, Operations, SDDC Manager. That is the lab
    # subnet, not one host, so the port 80 rule is deliberately broader than
    # the 443 one.
    [string]$CrlConsumerSubnet = '172.16.10.0/24',
    [switch]$DisableBroadRules,
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
function Write-Fail { param([string]$m) Write-Host "  [X] $m" -ForegroundColor Red }

# Best-effort re-assertion of the one setting that must never be left on after a
# partial failure: Negotiate/NTLM enabled on the enrolment endpoint.
function Reset-WindowsAuthDisabled {
    foreach ($loc in @('Default Web Site/CertSrv', 'Default Web Site/CertEnroll')) {
        try {
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/authentication/windowsAuthentication `
                -Name enabled -Value $false `
                -PSPath 'IIS:\' -Location $loc -ErrorAction Stop
            Write-Ok "recovery: windowsAuthentication disabled on $loc"
        } catch {
            Write-Fail "recovery: could NOT disable windowsAuthentication on $loc -- $($_.Exception.Message)"
            Write-Fail "recovery: DO NOT leave this host reachable on 443 until that is fixed by hand"
        }
    }
}

# ---------------------------------------------- IIS authentication hardening --
Write-Step "Hardening /CertSrv and /CertEnroll authentication"
Write-Info "Disabling Windows auth -- THIS is what closes ESC8 (no NTLM, nothing to relay)"
Write-Info "Enabling Basic auth because SDDC Manager cannot speak Negotiate"
Write-Info "Setting EPA on windowsAuthentication as defence in depth only"
Write-Host ""

# CRITICAL: Remove port 80 FIRST, before enabling Basic auth. This prevents a cleartext window
# where Basic auth is enabled but :80 is still bound. If any subsequent action fails, we have
# already closed the cleartext exposure rather than creating it.
$actions = @(
    @{
        what = 'Require SSL on /CertSrv -- this is the cleartext control'
        do   = {
            # IIS evaluates the SSL requirement BEFORE issuing an authentication
            # challenge, so a plain HTTP request to /CertSrv gets 403.4 and never
            # sees a Basic prompt. Removing the port 80 binding also achieves
            # that -- which is what this script used to do -- but it takes
            # /CertEnroll down with it, and /CertEnroll is where every consumer
            # in the lab fetches the CRL and the CA certificate. Scope the
            # control to the vdir that actually needs it.
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/access `
                -Name sslFlags -Value 'Ssl' `
                -PSPath 'IIS:\' `
                -Location 'Default Web Site/CertSrv'
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
        what = 'Set Extended Protection = Require on /CertSrv windowsAuthentication (defence in depth)'
        do   = {
            # extendedProtection belongs to windowsAuthentication, NOT
            # basicAuthentication. Setting it here means that if anyone ever
            # re-enables Windows auth on this vdir, channel binding is already
            # required and the ESC8 relay does not come back with it.
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/authentication/windowsAuthentication `
                -Name extendedProtection.tokenChecking -Value 'Require' `
                -PSPath 'IIS:\' `
                -Location 'Default Web Site/CertSrv'
        }
    }
    @{
        what = 'Disable Windows auth on /CertSrv (the actual ESC8 control)'
        do   = {
            Set-WebConfigurationProperty `
                -Filter /system.webServer/security/authentication/windowsAuthentication `
                -Name enabled -Value $false `
                -PSPath 'IIS:\' `
                -Location 'Default Web Site/CertSrv'
        }
    }
    # ----------------------------------------------------------- /CertEnroll --
    # /CertEnroll is NOT an ESC8 surface. ESC8 relays NTLM to an endpoint that
    # AUTHENTICATES; /CertEnroll is a static directory of signed objects (the
    # CRL and the CA certificate) and accepts no credentials, so there is
    # nothing to relay to it. An earlier version of this script enabled Basic
    # auth here. That is not hardening -- it makes every CRL fetch answer 401,
    # and no consumer sends credentials when fetching a CRL, so it breaks
    # revocation checking for the entire lab.
    @{
        what = '/CertEnroll: anonymous auth ON (CRL and AIA fetches carry no credentials)'
        do   = {
            try {
                Set-WebConfigurationProperty `
                    -Filter /system.webServer/security/authentication/anonymousAuthentication `
                    -Name enabled -Value $true `
                    -PSPath 'IIS:\' `
                    -Location 'Default Web Site/CertEnroll' `
                    -ErrorAction Stop
            } catch {
                Write-Warn "CertEnroll vdir not found; skipping CertEnroll configuration"
            }
        }
    }
    @{
        what = '/CertEnroll: Basic auth OFF (a 401 on a CRL fetch breaks revocation checking)'
        do   = {
            try {
                Set-WebConfigurationProperty `
                    -Filter /system.webServer/security/authentication/basicAuthentication `
                    -Name enabled -Value $false `
                    -PSPath 'IIS:\' `
                    -Location 'Default Web Site/CertEnroll' `
                    -ErrorAction Stop
            } catch { }
        }
    }
    @{
        what = '/CertEnroll: Windows auth OFF'
        do   = {
            try {
                Set-WebConfigurationProperty `
                    -Filter /system.webServer/security/authentication/windowsAuthentication `
                    -Name enabled -Value $false `
                    -PSPath 'IIS:\' `
                    -Location 'Default Web Site/CertEnroll' `
                    -ErrorAction Stop
            } catch { }
        }
    }
    @{
        what = '/CertEnroll: do NOT require SSL -- CDP and AIA are plain HTTP by design'
        do   = {
            # Serving CDP/AIA over HTTPS is circular: validating the HTTPS
            # certificate requires fetching a CRL, which would require
            # validating an HTTPS certificate. CRLs and CA certificates are
            # signed, so the transport does not need to provide integrity.
            try {
                Set-WebConfigurationProperty `
                    -Filter /system.webServer/security/access `
                    -Name sslFlags -Value '' `
                    -PSPath 'IIS:\' `
                    -Location 'Default Web Site/CertEnroll' `
                    -ErrorAction Stop
            } catch { }
        }
    }
    @{
        what = 'Ensure the port 80 binding EXISTS -- CDP and AIA depend on it'
        do   = {
            $b = Get-WebBinding -Name 'Default Web Site' -Protocol http |
                 Where-Object { $_.bindingInformation -like '*:80:*' }
            if (-not $b) {
                New-WebBinding -Name 'Default Web Site' -Protocol http -Port 80 -IPAddress '*'
                Write-Ok "port 80 binding restored"
            } else {
                Write-Ok "port 80 binding already present"
            }
        }
    }
)

$authFailed = $false
foreach ($a in $actions) {
    Write-Host ("  {0} {1}" -f $(if($Apply) { 'APPLY ' } else { 'WOULD ' }), $a.what)
    if ($Apply -and -not $authFailed) {
        try {
            & $a.do
        } catch {
            Write-Fail "Failed to apply: $($a.what)"
            Write-Fail "  Error: $($_.Exception.Message)"
            $authFailed = $true
        }
    }
}

if ($authFailed) {
    Write-Step "Authentication hardening failed part-way -- recovering"
    Write-Warn "Re-asserting windowsAuthentication = disabled, then still scoping the firewall."
    Write-Warn "A half-applied run must not leave Negotiate enabled on an unscoped endpoint."
    Reset-WindowsAuthDisabled
}

# ------------------------------------------------- firewall rule hardening --
Write-Step "Configuring firewall rules"

# Audit for pre-existing broad Allow rules that would bypass scoping.
# Windows Firewall evaluates all matching Allow rules with OR semantics; a scoped Allow
# does not narrow a broader pre-existing Allow. Task 1 commonly enables
# "World Wide Web Services (HTTP/HTTPS Traffic-In)" which allows 443 from anywhere.
#
# SCOPE OF THIS AUDIT, deliberately narrow: only ENABLED inbound TCP Allow rules
# whose LocalPort EXPLICITLY contains 443. LocalPort='Any' is NOT included --
# that matched 38 rules on a real DC (Remote Assistance, Connected Devices, VS
# Code, node.exe and other program-scoped rules), none of which have anything to
# do with 443, and disabling them would cost remote management of this domain
# controller. Nothing here is ever disabled without -DisableBroadRules, and
# action is taken on rule Name (unique), never DisplayName (not unique).
Write-Info "Auditing for ENABLED inbound Allow rules that explicitly permit TCP 443 from anywhere"
$broadRules = @()
try {
    $allInboundRules = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction Stop
    foreach ($rule in $allInboundRules) {
        $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
        if (-not $port) { continue }
        # Gate on protocol: only TCP rules can legitimately listen on port 443.
        if ($port.Protocol -ne 'TCP') { continue }
        $localPorts = @($port.LocalPort | ForEach-Object { [string]$_ })
        if ($localPorts -notcontains '443') { continue }
        $addr = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
        if (-not $addr) { continue }
        $remotes = @($addr.RemoteAddress | ForEach-Object { [string]$_ })
        if (($remotes -contains 'Any') -or ($remotes -contains '0.0.0.0/0') -or ($remotes -contains '::/0')) {
            $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
            $svc = Get-NetFirewallServiceFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue
            $program = 'Any'
            if ($app -and $app.Program) { $program = [string]$app.Program }
            $service = 'Any'
            if ($svc -and $svc.Service) { $service = [string]$svc.Service }
            $broadRules += [pscustomobject]@{
                Name        = $rule.Name
                DisplayName = $rule.DisplayName
                Program     = $program
                Service     = $service
                Profile     = [string]$rule.Profile
            }
        }
    }
} catch {
    Write-Fail "FATAL: Could not audit firewall rules"
    Write-Fail "  Error: $($_.Exception.Message)"
    exit 1
}

$broadRemain = $false
if ($broadRules.Count -eq 0) {
    Write-Ok "No pre-existing rule explicitly allows TCP 443 from anywhere"
} else {
    Write-Warn "These ENABLED rules allow TCP 443 from any address and bypass the scoping below:"
    foreach ($r in $broadRules) {
        Write-Host ""
        Write-Host "      Name        : $($r.Name)"        -ForegroundColor Yellow
        Write-Host "      DisplayName : $($r.DisplayName)" -ForegroundColor Yellow
        Write-Host "      Program     : $($r.Program)"     -ForegroundColor Yellow
        Write-Host "      Service     : $($r.Service)"     -ForegroundColor Yellow
        Write-Host "      Profile     : $($r.Profile)"     -ForegroundColor Yellow
    }
    Write-Host ""
    if ($Apply -and $DisableBroadRules) {
        Write-Info "Disabling them by Name (-DisableBroadRules was passed):"
        foreach ($r in $broadRules) {
            try {
                Disable-NetFirewallRule -Name $r.Name -ErrorAction Stop
                Write-Ok "Disabled: $($r.Name) ($($r.DisplayName))"
            } catch {
                Write-Fail "Could not disable $($r.Name): $($_.Exception.Message)"
                $broadRemain = $true
            }
        }
    } else {
        $broadRemain = $true
        Write-Warn "NOT disabling anything. This script never disables a firewall rule on a"
        Write-Warn "domain controller unasked. Review the list above, then either re-run with"
        Write-Warn "-DisableBroadRules or disable the ones you agree with by hand:"
        foreach ($r in $broadRules) {
            Write-Host "      Disable-NetFirewallRule -Name '$($r.Name)'" -ForegroundColor DarkGray
        }
    }
}

$allow = @($SddcManagerIp)
if ($AdminIp) {
    $allow += $AdminIp
}

# Remove the old scoped rule if it exists (idempotency). Looked up by DisplayName
# but removed via the pipeline object, so the actual rule identity is preserved.
Write-Info "Checking for existing scoped rule to remove (idempotency)"
$existingRule = Get-NetFirewallRule -DisplayName 'CertSrv HTTPS (scoped)' -Direction Inbound -ErrorAction SilentlyContinue
if ($existingRule) {
    Write-Host ("  {0} remove stale scoped rule (re-running with new -AdminIp?)" -f $(if($Apply) { 'APPLY ' } else { 'WOULD ' }))
    if ($Apply) {
        try {
            $existingRule | Remove-NetFirewallRule -ErrorAction Stop
            Write-Ok "Stale scoped rule removed"
        } catch {
            Write-Fail "FATAL: Could not remove stale firewall rule"
            Write-Fail "  Error: $($_.Exception.Message)"
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
        Write-Fail "FATAL: Could not create firewall rule"
        Write-Fail "  Error: $($_.Exception.Message)"
        exit 1
    }
}

# --------------------------------------- port 80 for CDP/AIA (/CertEnroll) --
# What this exposes is a static directory of SIGNED objects. /CertSrv on the
# same port answers 403 because it requires SSL, so widening 80 does not widen
# the enrolment surface. Without this rule every certificate this CA issues
# carries a revocation URL nothing in the lab can reach, and strict validators
# -- VCF LCM among them -- hard-fail on a CRL they cannot retrieve.
$crlRuleName = 'CertEnroll HTTP CDP-AIA (lab subnet)'
Write-Info "Checking for existing CDP/AIA rule to remove (idempotency)"
$existingCrlRule = Get-NetFirewallRule -DisplayName $crlRuleName -Direction Inbound -ErrorAction SilentlyContinue
if ($existingCrlRule) {
    Write-Host ("  {0} remove stale CDP/AIA rule" -f $(if($Apply) { 'APPLY ' } else { 'WOULD ' }))
    if ($Apply) {
        try {
            $existingCrlRule | Remove-NetFirewallRule -ErrorAction Stop
            Write-Ok "Stale CDP/AIA rule removed"
        } catch {
            Write-Fail "FATAL: Could not remove stale CDP/AIA firewall rule"
            Write-Fail "  Error: $($_.Exception.Message)"
            exit 1
        }
    }
}

Write-Host ("  {0} allow inbound 80 from: {1}  (/CertEnroll only; /CertSrv requires SSL)" -f `
            $(if($Apply) { 'APPLY ' } else { 'WOULD ' }), $CrlConsumerSubnet)
if ($Apply) {
    try {
        New-NetFirewallRule `
            -DisplayName $crlRuleName `
            -Direction Inbound `
            -Protocol TCP `
            -LocalPort 80 `
            -RemoteAddress $CrlConsumerSubnet `
            -Action Allow `
            -ErrorAction Stop | Out-Null
        Write-Ok "CDP/AIA firewall rule created"
    } catch {
        Write-Fail "FATAL: Could not create the CDP/AIA firewall rule"
        Write-Fail "  Error: $($_.Exception.Message)"
        Write-Fail "Without it, every certificate this CA issues has an unreachable CRL."
        exit 1
    }
}

# ------------------------------------------------- completion message --
if (-not $Apply) {
    Write-Warn "DRY RUN -- pass -Apply to apply hardening"
    Write-Host ""
    exit 0
}

if ($authFailed) {
    Write-Step "Web enrolment hardening INCOMPLETE"
    Write-Fail "Authentication hardening failed part-way; recovery ran and the firewall was scoped."
    Write-Fail "Fix the error above and re-run before exposing /certsrv."
    Write-Host ""
    exit 1
}

if ($broadRemain) {
    Write-Step "Web enrolment hardening applied, but scoping is NOT yet effective"
    Write-Fail "Broad TCP 443 rules listed above still allow 443 from any address."
    Write-Fail "Until they are disabled, the 'CertSrv HTTPS (scoped)' rule narrows nothing."
    Write-Host ""
    exit 1
}

Write-Step "Web enrolment hardening complete"
Write-Ok "All hardening steps applied successfully"
Write-Warn "IMPORTANT: /certsrv is unreachable until YOU bind a certificate to IIS on 443."
Write-Warn "NO SCRIPT DOES THIS. It is a manual step -- README step 5, 'Bind the DC's"
Write-Warn "certificate to IIS on 443', which also depends on the DC having autoenrolled"
Write-Warn "first (~90-120 min GPO cycle, or force it with certutil -pulse)."
Write-Info "That is expected. No consumer of /certsrv exists until Register-VcfCA.ps1."
Write-Host ""
exit 0
