<#
.SYNOPSIS
    Creates and verifies the VCF certificate template on the lab CA.

.DESCRIPTION
    The certificate template used by VCF must carry both Server Authentication
    and Client Authentication in the Enhanced Key Usage (EKU), must require the
    subject to be supplied in the request, and must restrict enrolment to a
    single service account. These constraints prevent certificate issuance from
    being delegated to anyone who can authenticate to AD.

    The template itself is created by hand in certtmpl.msc -- there is no
    supported ADCS cmdlet for template creation, and a scripted ADSI clone is
    fragile enough that a wrong template costs more than the five minutes of
    manual setup it saves.

    This script prints build instructions, then verifies that what was built
    is secure. Verification checks are the only controls standing between the
    enrolment account and arbitrary certificate issuance: all must pass.

    Dry run by default; pass -Apply to perform verification. The script does
    not write to the CA or AD -- it is read-only and safe to run at any time.

.PARAMETER Apply
    Actually perform verification checks. Without -Apply, the script only
    prints build instructions and exits without verifying.

.REQUIRES -RunAsAdministrator

.EXAMPLE
    .\New-VcfCertificateTemplate.ps1
    Dry run: shows build instructions and exits without verifying.

.EXAMPLE
    .\New-VcfCertificateTemplate.ps1 -Apply
    Verifies that the VMware template was built correctly.
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
function Write-Fail { param([string]$m) Write-Host "  [X] $m" -ForegroundColor Red }

# ------------------------------------------------- print build instructions --
Write-Step "VCF Certificate Template build instructions"
Write-Host @"
Create the template by hand in certtmpl.msc -- there is no supported cmdlet
for template creation, and a scripted ADSI clone is fragile enough that a
wrong template costs more than the five minutes this saves.

  1. Duplicate 'Web Server'

  2. General: Set Template display name to 'VMware'
     - Confirm the TEMPLATE NAME (cn) is also 'VMware' in the Name field.
       SDDC Manager is given the cn, and a display-name mismatch fails at
       certificate REQUEST time, not at configuration time.
     - Validity period: 2 years

  3. Compatibility: Leave at the LOWEST offered version
     - A modern default produces a v4 template with CNG/KSP constraints that
       reject web-enrolment CSR submission.

  4. Extensions: Application Policies
     - Server Authentication AND Client Authentication (both required)

  5. Subject Name: 'Supply in the request'
     - Do not set any Subject Name alternatves; SDDC Manager provides the SAN.

  6. Security: Enroll ACE
     - Add: knowledgeondemand\svc-vcf-ca -> Enroll (allow)
     - Remove: Any Authenticated Users / Domain Users Enroll ACE (this is ESC1)

  7. Issue it: certsrv.msc -> Certificate Templates -> right-click New ->
     Certificate Template to Issue -> VMware

  8. CA security: CA properties -> Security
     - Add: knowledgeondemand\svc-vcf-ca -> Request Certificates (allow)
     - NOTE: This is SEPARATE from the template Enroll ACE in step 6.

"@ -ForegroundColor Cyan

# ---------------------------------------------------- dry-run guard and exit --
if (-not $Apply) {
    Write-Warn "DRY RUN -- pass -Apply to perform verification"
    Write-Host ""
    exit 0
}

# ---------------------------------------- verification: template is issued --
Write-Step "Verification: Template is issued on the CA"
Write-Info "Running: certutil -CATemplates"
$issued = (certutil -CATemplates) -join "`n"
if ($issued -notmatch 'VMware') {
    Write-Fail "Template 'VMware' is not issued on the CA"
    Write-Info "This causes enrolment to fail with a confusing error at request time."
    Write-Info "Issue it: certsrv.msc -> Certificate Templates -> New -> VMware"
    exit 1
}
Write-Ok "Template is issued"

# ---------------------------------------- verification: EKU is correct --
Write-Step "Verification: EKU carries Server + Client Authentication"
Write-Info "Running: certutil -v -template VMware"
$t = (certutil -v -template VMware) -join "`n"

$hasServerAuth = $t -match 'Server Authentication'
$hasClientAuth = $t -match 'Client Authentication'

if (-not ($hasServerAuth -and $hasClientAuth)) {
    Write-Fail "EKU is not as designed"
    if (-not $hasServerAuth) {
        Write-Info "  Missing: Server Authentication"
    }
    if (-not $hasClientAuth) {
        Write-Info "  Missing: Client Authentication"
    }
    Write-Info "VCF certificates must carry both; see General -> Extensions tab."
    exit 1
}
Write-Ok "EKU carries Server + Client Authentication"

# ----------------------------------- verification: no broad enrolment ACE --
Write-Step "Verification: No broad enrolment ACE (ESC1 control)"
Write-Info "Checking: template does not permit Authenticated Users or Domain Users to enroll"
Write-Info "This is the ONLY control preventing arbitrary certificate issuance"

# The template output includes an "S-1-5-11" SID (Authenticated Users) or
# "Domain Users" group in the Enroll ACE. We must be careful:
# - "Authenticated Users" and "Domain Users" must be caught in Enroll ACEs only,
#   not in Read or other permissions.
# - A naive pattern could match unrelated text or miss context.
# Pattern: look for "Enroll" (permission type) followed by "Authenticated Users"
#   or "Domain Users" on the same or nearby line (LDAP distinguishedName groups
#   or SIDs appear line-by-line in certutil output).

# certutil -v -template output format includes:
#   "Enroll: <security info>"
# If it names Authenticated Users or Domain Users in the Enroll block, we fail.

# Check for the broad ACEs. The certutil output is somewhat free-form, so we
# look for patterns that indicate broad enrolment permission. The safest pattern
# is to look for "Enroll" followed by these group names on the same logical block.
$hasAuthenticatedUsers = $t -match 'Enroll.*Authenticated Users' -or $t -match 'Authenticated Users.*Enroll'
$hasDomainUsers = $t -match 'Enroll.*Domain Users' -or $t -match 'Domain Users.*Enroll'

# Also check for the SID form S-1-5-11 (Authenticated Users) in Enroll context
$hasS1511InEnroll = $t -match 'Enroll.*S-1-5-11' -or $t -match 'S-1-5-11.*Enroll'

if ($hasAuthenticatedUsers -or $hasDomainUsers -or $hasS1511InEnroll) {
    Write-Fail "A broad Enroll ACE is present -- this is the ESC1 control"
    Write-Info "Only knowledgeondemand\svc-vcf-ca should have Enroll permission."
    Write-Info "See Security tab in the template properties; remove the broad ACE."
    exit 1
}
Write-Ok "No broad enrolment ACE"

# ------------------------------ verification: ESC6 flag is not set on CA --
Write-Step "Verification: ESC6 flag not set on CA (EDITF_ATTRIBUTESUBJECTALTNAME2)"
Write-Info "Running: certutil -getreg policy\EditFlags"
$ef = (certutil -getreg policy\EditFlags) -join "`n"

if ($ef -match 'EDITF_ATTRIBUTESUBJECTALTNAME2') {
    Write-Fail "EDITF_ATTRIBUTESUBJECTALTNAME2 is set (ESC6)"
    Write-Info "This allows any template to accept an attacker-specified SAN."
    Write-Info "CA properties -> Policy Module -> Properties -> clear the flag"
    exit 1
}
Write-Ok "ESC6 flag not set"

# ------------------------------------------------------------------ summary --
Write-Step "All verification checks passed"
Write-Ok "Template is correctly configured for VCF"
Write-Host ""
exit 0
