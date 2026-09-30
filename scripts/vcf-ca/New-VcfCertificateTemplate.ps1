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
Write-Step "Verification: Only knowledgeondemand\svc-vcf-ca has Enroll (ESC1 control)"
Write-Info "This is the ONLY control preventing arbitrary certificate issuance"

# The requirement is strict: ONLY knowledgeondemand\svc-vcf-ca may have Enroll.
# Any other principal (Authenticated Users, Domain Users, Domain Computers, a
# custom group, or an individual account) completely defeats the control.
# So we must enumerate who actually has Enroll and assert the set is exactly one.

# certutil -v -template output lists permissions in a section like:
#   Enroll
#     knowledgeondemand\svc-vcf-ca
# or in older formats might show it differently. We must validate we can
# positively identify this section; if not, we FAIL rather than guess.

# Validate we got template data (not an error or empty output)
if ([string]::IsNullOrWhiteSpace($t)) {
    Write-Fail "Cannot verify: certutil -v -template returned empty output"
    exit 1
}

if ($t -notmatch 'VMware') {
    Write-Fail "Cannot verify: template name not found in certutil output"
    Write-Info "Output does not appear to be template data"
    exit 1
}

# Attempt to find the Enroll section in the output.
# Look for "Enroll" as a label, optionally followed by structured info.
# Pattern handles possible formatting like:
#   "Enroll" followed by accounts on following indented lines, or
#   "Enroll:\n  account"
# If this pattern does NOT match, we cannot reliably identify the Enroll ACL,
# so we FAIL rather than making an unverified assertion.
if ($t -notmatch '(Enroll[^\n]*\n([\s\S]*?)(?=\n\S|\Z))') {
    Write-Fail "Cannot verify: Enroll section not found in certutil output"
    Write-Info "Output format may have changed or does not contain ACE information"
    exit 1
}

$enrollSection = $matches[1]

# Extract principals from the Enroll section.
# Split the section into lines and collect non-empty, indented account names.
# Accounts typically appear as "domain\account" or may be SIDs (S-1-...).
$enrolledPrincipals = @()
$lines = $enrollSection -split '\n'
foreach ($line in $lines) {
    # Skip the "Enroll" label line itself and empty lines
    if ($line -match '^\s+([\w.-]+\\[\w.-]+)' -or $line -match '^\s+(S-1-[\d-]+)') {
        $principal = $matches[1].Trim()
        if ($principal) {
            $enrolledPrincipals += $principal
        }
    }
}

# Validate we found at least one principal. If Enroll exists but lists no one,
# something is wrong with our parsing or the template is malformed.
if ($enrolledPrincipals.Count -eq 0) {
    Write-Fail "Cannot verify: no principals found in Enroll section"
    Write-Info "Enroll ACE exists but lists no accounts; output may be malformed"
    exit 1
}

# Check for the forbidden broad principals by SID and name
foreach ($principal in $enrolledPrincipals) {
    if ($principal -eq 'S-1-5-11' -or $principal -match 'Authenticated Users' -or `
        $principal -match 'Domain Users' -or $principal -match 'Domain Computers' -or `
        $principal -match 'Everyone') {
        Write-Fail "Broad principal has Enroll: $principal"
        exit 1
    }
}

# Validate the set is exactly {knowledgeondemand\svc-vcf-ca}
$expectedPrincipal = 'knowledgeondemand\svc-vcf-ca'
if ($enrolledPrincipals.Count -ne 1) {
    Write-Fail "Multiple principals have Enroll permission (expected exactly one)"
    foreach ($p in $enrolledPrincipals) {
        Write-Info "  Principal: $p"
    }
    exit 1
}

if ($enrolledPrincipals[0] -ne $expectedPrincipal) {
    Write-Fail "Enroll is granted to unexpected principal: $($enrolledPrincipals[0])"
    Write-Info "Expected: $expectedPrincipal"
    exit 1
}

Write-Ok "Enroll permission restricted to knowledgeondemand\svc-vcf-ca only"

# ------------------------------ verification: ESC6 flag is not set on CA --
Write-Step "Verification: ESC6 flag not set on CA (EDITF_ATTRIBUTESUBJECTALTNAME2)"
Write-Info "Running: certutil -getreg policy\EditFlags"

# Capture certutil output and exit code. The default $ErrorActionPreference is
# 'Stop', so we must handle potential errors from certutil explicitly.
$efOutput = @()
$efError = ''
try {
    $efOutput = @(certutil -getreg policy\EditFlags 2>&1)
} catch {
    $efError = $_.Exception.Message
}

# Validate we got output
if ($efOutput.Count -eq 0 -or [string]::IsNullOrWhiteSpace(($efOutput -join ''))) {
    Write-Fail "Cannot verify: certutil -getreg returned no output"
    if ($efError) { Write-Info "Error: $efError" }
    exit 1
}

$efText = $efOutput -join "`n"

# Look for the EditFlags value explicitly. The output should contain a line like:
#   "EditFlags REG_DWORD = 0x00000000"
# or show a value with flag names. If we cannot find a value, we cannot verify.
if ($efText -notmatch 'EditFlags|0x[0-9a-fA-F]+|\d+') {
    Write-Fail "Cannot verify: EditFlags value not found in certutil output"
    Write-Info "Output format may have changed or CA is not properly configured"
    exit 1
}

# Check if EDITF_ATTRIBUTESUBJECTALTNAME2 is explicitly mentioned as set.
# This flag allows subjects to supply a SAN in a request on any template.
# It should NOT be set.
if ($efText -match 'EDITF_ATTRIBUTESUBJECTALTNAME2|0x40000|262144') {
    Write-Fail "EDITF_ATTRIBUTESUBJECTALTNAME2 is set (ESC6)"
    Write-Info "This allows any template to accept an attacker-specified SAN."
    Write-Info "CA properties -> Policy Module -> Properties -> clear the flag"
    exit 1
}

# If the output looks normal but does not mention the flag being set, we pass.
Write-Ok "ESC6 flag not set"

# ------------------------------------------------------------------ summary --
Write-Step "All verification checks passed"
Write-Ok "Template is correctly configured for VCF"
Write-Host ""
exit 0
