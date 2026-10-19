<#
.SYNOPSIS
    Publishes and verifies the two certificate templates this lab needs:
    'Domain Controller Authentication' (so the DC autoenrols and LDAPS starts)
    and 'VMware' (so SDDC Manager can issue the eight VCF certificates).

.DESCRIPTION
    CAPolicy.inf sets LoadDefaultTemplates=0, so the CA publishes NO templates
    at promotion -- not even a Domain Controller template. Until one is
    published by hand, dns01 never autoenrols, LDAPS never starts, and the
    entire purpose of this CA is unreachable. Publishing both templates is
    therefore an operator step, and this script is what proves it was done
    correctly.

    The certificate template used by VCF must carry both Server Authentication
    and Client Authentication in the Enhanced Key Usage (EKU), must require the
    subject to be supplied in the request, and must restrict enrolment to a
    single service account. These constraints prevent certificate issuance from
    being delegated to anyone who can authenticate to AD.

    Templates are created by hand in certtmpl.msc -- there is no supported ADCS
    cmdlet for template creation, and a scripted ADSI clone is fragile enough
    that a wrong template costs more than the five minutes of manual setup it
    saves.

    This script prints build instructions, then verifies that what was built
    is secure. Verification checks are the only controls standing between the
    enrolment account and arbitrary certificate issuance: all must pass. Every
    check fails closed -- an unreadable, empty or unparseable certutil response
    is "cannot verify", which is a FAILURE, never a pass.

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
    Verifies both templates, the CA-level enrolment right, and CA auditing.
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

# The expected enrolment identity for the VMware template.
# Resolved from the directory, not hard-coded. NetBIOS domain names are capped
# at 15 characters, so the domain knowledgeondemand.net is KNOWLEDGEONDEMA --
# and 'knowledgeondemand\svc-vcf-ca', which this line used to contain, does not
# translate to a SID at all and never matches a real ACE. The comparison below
# is also done on the sAMAccountName portion so it cannot be defeated by
# whichever spelling certutil happens to print.
$ExpectedEnrolleeSam = 'svc-vcf-ca'
$ExpectedEnrollee = $null
try {
    Import-Module ActiveDirectory -ErrorAction Stop
    $ExpectedEnrollee = "$((Get-ADDomain).NetBIOSName)\$ExpectedEnrolleeSam"
} catch { }
if (-not $ExpectedEnrollee) { $ExpectedEnrollee = $ExpectedEnrolleeSam }

<#
    Reads enrolment rights for a template from ACTIVE DIRECTORY, which is the
    authoritative source, instead of parsing certutil output.

    This exists because certutil lies by omission. Measured on this CA:

        certutil -v -template VMware
            Allow Full Control   KNOWLEDGEONDEMA\Domain Admins
            Allow Read           NT AUTHORITY\Authenticated Users
            Allow Full Control   NT AUTHORITY\SYSTEM
            ...

    while the directory holds

        KNOWLEDGEONDEMA\svc-vcf-ca   ENROLL

    certutil rendered no Enroll ACE at all. A verifier parsing that output
    reports "svc-vcf-ca does NOT have Enroll" against a correctly permissioned
    template -- a false negative on the ESC1 gate, which is the most
    security-critical check in this project. The same omission made the
    Domain Controllers Autoenroll check fail on a correct template.

    Returns objects with Principal, Enroll and AutoEnroll. Full Control
    (GenericAll) confers both, so it is reported as granting both.
#>
function Get-TemplateEnrolmentAcl {
    param([Parameter(Mandatory)][string]$TemplateCn)
    $ENROLL_GUID     = '0e10c968-78fb-11d2-90d4-00c04f79dc55'
    $AUTOENROLL_GUID = 'a05b8cc2-17bc-4802-a710-e7c15ab866a2'
    try { Import-Module ActiveDirectory -ErrorAction Stop } catch { return $null }
    try {
        $cfgNc = (Get-ADRootDSE).configurationNamingContext
        $dn = "CN=$TemplateCn,CN=Certificate Templates,CN=Public Key Services,CN=Services,$cfgNc"
        $aces = (Get-Acl "AD:$dn").Access
    } catch { return $null }

    $byPrincipal = @{}
    foreach ($a in $aces) {
        if ($a.AccessControlType -ne 'Allow') { continue }
        $who = $a.IdentityReference.Value
        if (-not $byPrincipal.ContainsKey($who)) {
            $byPrincipal[$who] = [pscustomobject]@{ Principal = $who; Enroll = $false; AutoEnroll = $false; ViaFullControl = $false }
        }
        $guid = $a.ObjectType.ToString()
        if ($a.ActiveDirectoryRights -match 'GenericAll') {
            $byPrincipal[$who].Enroll = $true
            $byPrincipal[$who].AutoEnroll = $true
            $byPrincipal[$who].ViaFullControl = $true
        }
        if ($guid -eq $ENROLL_GUID)     { $byPrincipal[$who].Enroll = $true }
        if ($guid -eq $AUTOENROLL_GUID) { $byPrincipal[$who].AutoEnroll = $true }
    }
    return @($byPrincipal.Values | Where-Object { $_.Enroll -or $_.AutoEnroll })
}

# Returns the bare account name from DOMAIN\user, user@domain or user, so two
# spellings of the same principal compare equal.
function Get-AccountNamePart {
    param([string]$Principal)
    if (-not $Principal) { return '' }
    $p = $Principal.Trim()
    # .Contains/.Split with a char, not -match/-split with a pattern: a
    # backslash in a regex needs escaping, and an under-escaped one becomes the
    # pattern '\' which throws "Illegal \ at end of pattern" at runtime.
    $bs = [char]92
    if ($p.Contains($bs)) { $p = $p.Split($bs)[-1] }
    elseif ($p.Contains('@')) { $p = $p.Split('@')[0] }
    return $p.ToLower()
}

# Built-in administrative principals that hold Full Control on EVERY certificate
# template by default and cannot sensibly be removed (removing Enterprise Admins
# from a template's ACL breaks template management itself). "Exactly one
# principal" is therefore unsatisfiable; the assertion is that the set of
# enrolment-capable principals equals {$ExpectedEnrollee} PLUS this explicit,
# documented allow-list, and NOTHING else.
# Listed by ACCOUNT NAME only, deliberately. These used to carry a
# 'knowledgeondemand\' prefix, which is not this domain's NetBIOS name --
# NetBIOS caps at 15 characters, so it is KNOWLEDGEONDEMA. The prefixed
# entries therefore never matched, and the verification reported
# KNOWLEDGEONDEMA\Domain Admins as an unexpected enrolment principal on a
# correctly built template. Comparing on the account name removes the
# dependency on how certutil renders the domain portion.
$BuiltinAdminPrincipals = @(
    'domain admins'
    'enterprise admins'
    'administrators'
    'system'
)

# Principals that must NEVER hold enrolment on either template. Listed by name
# and by well-known SID because certutil renders unresolvable principals as SIDs.
$ForbiddenPrincipals = @(
    'authenticated users'      # S-1-5-11 -- this is ESC1
    'domain users'
    'domain computers'
    'everyone'                 # S-1-1-0
    's-1-5-11'
    's-1-1-0'
)

# Runs certutil, returning the combined output as one string plus the exit code.
# certutil writes diagnostics to stderr; 2>&1 folds them in so the caller sees
# everything, but the ONLY trustworthy success signal is the exit code, which is
# why it is returned separately and checked by every caller.
function Invoke-Certutil {
    param([string[]]$CertutilArgs)
    $out = ''
    $code = -1
    try {
        $raw = & certutil @CertutilArgs 2>&1
        $code = $LASTEXITCODE
        $out = (@($raw) | ForEach-Object { [string]$_ }) -join "`n"
    } catch {
        $out = $_.Exception.Message
        $code = -1
    }
    [pscustomobject]@{ Output = $out; ExitCode = $code }
}

# Collects "Allow <rights> <principal>" ACE lines out of a certutil ACL dump.
#
# The previous implementation anchored a section on the first literal "Enroll"
# in the output -- which in real certutil output is inside the
# msPKI-Enrollment-Flag line, not the ACE block -- and then required the
# principal to be the FIRST token on the line. Real ACE lines read
#     Allow Enroll    KNOWLEDGEONDEMAND\svc-vcf-ca
# so zero principals were ever extracted and the check could not pass. Match the
# ACE lines directly instead.
function Get-AllowAce {
    param([string]$Text)
    $aces = @()
    $pattern = '(?m)^\s*Allow\s+(Full Control|Enroll,\s*AutoEnroll|AutoEnroll,\s*Enroll|AutoEnroll|Enroll)\s+(\S.*?)\s*$'
    foreach ($m in [regex]::Matches($Text, $pattern)) {
        $aces += [pscustomobject]@{
            Rights    = ($m.Groups[1].Value -replace '\s+', ' ')
            Principal = $m.Groups[2].Value.Trim()
        }
    }
    ,$aces
}

# Full Control confers Enroll, so both count as enrolment-capable.
function Test-GrantsEnroll {
    param([string]$Rights)
    if ($Rights -eq 'Full Control') { return $true }
    return ($Rights -match 'Enroll')
}

function Test-GrantsAutoEnroll {
    param([string]$Rights)
    if ($Rights -eq 'Full Control') { return $true }
    return ($Rights -match 'AutoEnroll')
}

# Parses `certutil -CATemplates` into cn/displayName pairs. Output lines read:
#     VMware: VMware -- Auto-Enroll
#     DomainControllerAuthentication: Domain Controller Authentication
# A substring test against the whole blob cannot tell the cn from the display
# name, and would happily match "VMwareOld" -- which is exactly the mismatch
# this script warns about at build time.
function Get-IssuedTemplate {
    param([string]$Text)
    $rows = @()
    foreach ($line in ($Text -split "`n")) {
        $m = [regex]::Match($line, '^\s*(\S+):\s*(\S.*?)\s*$')
        if (-not $m.Success) { continue }
        $display = $m.Groups[2].Value
        # Strip the trailing " -- Auto-Enroll" / " -- Access is denied." status.
        $display = ($display -replace '\s+--\s.*$', '').Trim()
        $rows += [pscustomobject]@{
            Cn          = $m.Groups[1].Value.Trim()
            DisplayName = $display
        }
    }
    ,$rows
}

# ------------------------------------------------- print build instructions --
Write-Step "Certificate template build instructions"
Write-Host @"
THE 'VMware' TEMPLATE IS NOW AUTOMATED. Use New-VmwareCertTemplate.ps1,
which exports the stock Web Server template, applies the edits in section B
below, creates and publishes the result, grants Enroll to svc-vcf-ca only,
and verifies each documented property. The GUI steps in section B are kept
as the reference for what it does, and as the fallback.

    .\New-VmwareCertTemplate.ps1          # dry run, shows every edit
    .\New-VmwareCertTemplate.ps1 -Apply

An earlier revision of this header claimed template creation could not be
scripted. That was wrong: it can, and the only genuinely fiddly part --
generating a unique msPKI-Cert-Template-OID together with its
msPKI-Enterprise-Oid object -- is solved by the vendored ADCSTemplate module
rather than reimplemented.

Section A ('Domain Controller Authentication') remains a one-click publish of
a STANDARD template, so it stays manual: there is nothing to build.

CAPolicy.inf set LoadDefaultTemplates=0, so the CA has published NOTHING.
Both templates below must be issued by hand or the lab does not work.

-- A. 'Domain Controller Authentication' (REQUIRED -- this is what makes LDAPS
      exist; without it dns01 never autoenrols and the whole project stalls)

  A1. certsrv.msc -> Certificate Templates -> right-click -> New ->
      Certificate Template to Issue -> 'Domain Controller Authentication'
      This is the STANDARD template. Do not duplicate it, do not invent a
      replacement, do not use the older 'Domain Controller' template.

  A2. certtmpl.msc -> 'Domain Controller Authentication' -> Security
      - Grant: knowledgeondemand\Domain Controllers -> Enroll + Autoenroll
      - Do NOT grant Autoenroll to Domain Computers.
      - Do NOT grant Autoenroll to Authenticated Users.
      Autoenroll on a broader group hands a Client Authentication certificate
      to every machine in the domain, which is the PetitPotam relay surface
      LoadDefaultTemplates=0 was set to avoid.

  A3. Force the DC to pick it up now rather than waiting for the GPO cycle:
        certutil -pulse
        certutil -dcinfo verify

-- B. 'VMware' (the SDDC Manager template)

  B1. Duplicate 'Web Server'

      THESE STEPS ARE VMWARE'S, VERBATIM FROM THE 9.1 DOCUMENTATION:
      techdocs.broadcom.com > VCF 9.0 and later > 9.1 > Fleet Management >
      Certificate Management > Configure a Certificate Authority >
      Prepare Your Microsoft Certificate Authority ...

      An earlier revision of this script told you to KEEP Server
      Authentication and said nothing about Basic Constraints or Key Usage.
      That contradicted the documentation, and this script's own verification
      would then have REJECTED a correctly built template. Both are corrected.

  B2. Compatibility tab
      - Certification Authority : Windows Server 2008 R2
      - Certificate recipient   : Windows 7 / Server 2008 R2

  B3. General tab
      - Template display name: 'VMware'
      - Confirm the TEMPLATE NAME (cn) is also 'VMware' in the Name field.
        The CA is given the cn, and a display-name mismatch fails at
        certificate REQUEST time, not at configuration time. This script
        verifies the cn specifically, not just that 'VMware' appears somewhere.

  B4. Extensions tab -- three changes, all required
      - Application Policies : select 'Server Authentication', click Remove.
        Counter-intuitive but documented and correct: the stock Web Server
        template's only EKU is Server Authentication, so removing it leaves
        the issued certificate with NO EKU restriction, which makes it valid
        for every purpose VCF needs. Leaving it in RESTRICTS the certificate
        to server authentication only.
        ** Because the issued certificate is then unrestricted, the Enroll ACE
           in B6 is the only thing limiting who can obtain one. Treat it as
           load-bearing, not hygiene. **
      - Basic Constraints   : tick 'Enable this extension'
      - Key Usage           : tick 'Signature is proof of origin
                              (nonrepudiation)'; leave every other option at
                              its default

  B5. Subject Name tab
      - 'Supply in the request' must be selected.
        Note the stock Web Server template already has this
        (msPKI-Certificate-Name-Flag = 1), so confirm rather than change it.
      - Do not set Subject Name alternatives; VCF provides the SAN.

  B6. Security tab -- least privilege, per VMware's service-account topic
      - knowledgeondemand\svc-vcf-ca :  Read = ALLOW, Enroll = ALLOW
                                        Full Control / Write / Autoenroll = not set
      - Remove any Authenticated Users / Domain Users ENROLL ACE. With no EKU
        restriction and subject-in-request, a broad Enroll grant here is ESC1.
      - Domain Admins / Enterprise Admins / BUILTIN\Administrators keep Full
        Control; that is a default that cannot sensibly be removed and is
        explicitly allow-listed by this script's verification.

  B7. Issue it: certsrv.msc -> Certificate Templates -> right-click New ->
      Certificate Template to Issue -> VMware

-- C. CA-level settings (separate from anything on a template)

  C1. CA properties -> Security (certsrv.msc), per VMware's documented
      least-privilege table for the service account:
      - knowledgeondemand\svc-vcf-ca :
            Read                        = NOT selected
            Issue and Manage Certificates = ALLOW
            Manage CA                   = NOT selected
            Request Certificates        = ALLOW
      - NOTE: 'Issue and Manage Certificates' was MISSING from this script's
        earlier instructions, which listed only Request Certificates. VMware
        requires both.
      - This is SEPARATE from the template Enroll ACE in B6. Both are
        required; neither implies the other.

  C2. Enable CA auditing (the detective control the design relies on, because
      subject-in-request issuance cannot be constrained by name):
        certutil -setreg CA\AuditFilter 127
        auditpol /set /subcategory:"Certification Services" /success:enable /failure:enable
        net stop certsvc && net start certsvc

"@ -ForegroundColor Cyan

# ---------------------------------------------------- dry-run guard and exit --
if (-not $Apply) {
    Write-Warn "DRY RUN -- pass -Apply to perform verification"
    Write-Host ""
    exit 0
}

$failures = 0

# ---------------------------------------- verification: templates are issued --
Write-Step "Verification: both templates are issued on the CA"
Write-Info "Running: certutil -CATemplates"
$catRes = Invoke-Certutil @('-CATemplates')
if ($catRes.ExitCode -ne 0) {
    Write-Fail "Cannot verify: certutil -CATemplates exited $($catRes.ExitCode)"
    Write-Info $catRes.Output
    exit 1
}

$issuedTemplates = Get-IssuedTemplate $catRes.Output
if ($issuedTemplates.Count -eq 0) {
    Write-Fail "Cannot verify: no templates parsed from certutil -CATemplates output"
    Write-Info "The CA publishes nothing at all (LoadDefaultTemplates=0), or the"
    Write-Info "output format changed. Either way this is a FAILURE, not a pass."
    exit 1
}

# M7: assert the cn, not a substring of the blob. 'VMwareOld' must not pass.
$vmwareRow = @($issuedTemplates | Where-Object { $_.Cn -eq 'VMware' })
if ($vmwareRow.Count -eq 0) {
    Write-Fail "No issued template has cn 'VMware'"
    Write-Info "Issued template cn values seen: $(($issuedTemplates | ForEach-Object { $_.Cn }) -join ', ')"
    Write-Info "SDDC Manager is given the cn. A template whose DISPLAY NAME is"
    Write-Info "'VMware' but whose cn is something else fails at request time."
    Write-Info "Fix in certtmpl.msc -> VMware -> General -> Template name."
    $failures++
} else {
    Write-Ok "Template cn 'VMware' is issued (display name: '$($vmwareRow[0].DisplayName)')"
}

# C2: the DC template. Standard cn is DomainControllerAuthentication.
$dcRow = @($issuedTemplates | Where-Object {
    $_.Cn -eq 'DomainControllerAuthentication' -or $_.DisplayName -eq 'Domain Controller Authentication'
})
if ($dcRow.Count -eq 0) {
    Write-Fail "'Domain Controller Authentication' is NOT issued on the CA"
    Write-Info "LoadDefaultTemplates=0 means it was never auto-published. Without it"
    Write-Info "dns01 never autoenrols, LDAPS never starts, and nothing downstream"
    Write-Info "in this project can work. See section A of the instructions above."
    $failures++
} else {
    Write-Ok "Template 'Domain Controller Authentication' is issued (cn: $($dcRow[0].Cn))"
}

# ---------------------------------------- verification: EKU is correct --
Write-Step "Verification: VMware EKU carries Server + Client Authentication"
Write-Info "Running: certutil -v -template VMware"
$tplRes = Invoke-Certutil @('-v', '-template', 'VMware')
if ($tplRes.ExitCode -ne 0) {
    Write-Fail "Cannot verify: certutil -v -template VMware exited $($tplRes.ExitCode)"
    Write-Info $tplRes.Output
    exit 1
}
$t = $tplRes.Output
if ([string]::IsNullOrWhiteSpace($t)) {
    Write-Fail "Cannot verify: certutil -v -template returned empty output"
    exit 1
}

# VMware's documented template REMOVES Server Authentication from Application
# Policies, which leaves the stock Web Server template with no EKU at all. An
# earlier version of this check asserted that Server AND Client Authentication
# must both be PRESENT -- the opposite of the documentation -- so it would have
# rejected a correctly built template and passed a restricted one.
#
# Why removing it is right: the issued certificate then carries no EKU
# restriction, so it is valid for every purpose VCF needs. Leaving Server
# Authentication in RESTRICTS the certificate to server auth only.
#
# The security consequence is real and is checked separately below: an
# unrestricted certificate means the Enroll ACE is the ONLY thing deciding who
# can obtain one. That is why the ESC1 check that follows is not optional.
$ekuLines = @($t -split "`r?`n" | Where-Object { $_ -match 'Server Authentication|Client Authentication|Smart Card Logon|Code Signing' })
if ($ekuLines.Count -eq 0) {
    Write-Ok "No EKU restriction on the template -- as VMware's procedure requires"
    Write-Info "Issued certificates are unrestricted, so the Enroll ACE below is the control"
} else {
    # Not a hard failure: a restricted template still issues, it just may not
    # serve every VCF purpose. Report it as a deviation and let the operator
    # decide, rather than blocking on a judgement VMware already made.
    Write-Warn "The template carries an EKU restriction, which VMware's procedure removes:"
    foreach ($l in $ekuLines) { Write-Info "    $($l.Trim())" }
    Write-Info "VMware 9.1: Extensions -> Application Policies -> select Server Authentication -> Remove."
    Write-Info "A restricted certificate may be rejected for some VCF component roles."
}

# ----------------------------------- verification: no broad enrolment ACE --
Write-Step "Verification: VMware enrolment is restricted (ESC1 control)"
Write-Info "This is the ONLY control preventing arbitrary certificate issuance"

# AD is the authoritative source; the certutil parse is kept only as a
# fallback for a host without the ActiveDirectory module, and it announces
# itself because it is known to omit Enroll ACEs.
$vmwareAdAcl = Get-TemplateEnrolmentAcl -TemplateCn 'VMware'
if ($null -ne $vmwareAdAcl) {
    Write-Info "ACL source: Active Directory (authoritative)"
    $vmwareAces = @($vmwareAdAcl | ForEach-Object {
        [pscustomobject]@{
            Principal = $_.Principal
            Rights    = $(if ($_.ViaFullControl) { 'Full Control' } elseif ($_.Enroll -and $_.AutoEnroll) { 'Enroll, AutoEnroll' } elseif ($_.AutoEnroll) { 'AutoEnroll' } else { 'Enroll' })
        } })
} else {
    Write-Warn "Falling back to parsing certutil output, which OMITS Enroll ACEs."
    Write-Warn "A pass from this source is not trustworthy; install RSAT/ActiveDirectory."
    $vmwareAces = Get-AllowAce $t
}
$vmwareEnroll = @($vmwareAces | Where-Object { Test-GrantsEnroll $_.Rights })

if ($vmwareEnroll.Count -eq 0) {
    # Fail closed: finding no Allow ACEs means the parse failed or the output
    # is not what we think it is. It does NOT mean nobody can enrol.
    Write-Fail "Cannot verify: no 'Allow <rights> <principal>' ACE lines found for VMware"
    Write-Info "certutil output format may have changed. This is a FAILURE."
    $failures++
} else {
    # The allow-list and the ACE are both reduced to the account name before
    # comparison, so the domain portion's spelling cannot cause a false
    # finding. $ForbiddenPrincipals still matches against the FULL principal,
    # because some of its entries are well-known SIDs rather than names.
    $allowed = @($BuiltinAdminPrincipals) + @($ExpectedEnrolleeSam.ToLower())
    $unexpected = @()
    $forbidden  = @()
    foreach ($ace in $vmwareEnroll) {
        $full = $ace.Principal.ToLower()
        $name = Get-AccountNamePart $ace.Principal
        foreach ($f in $ForbiddenPrincipals) {
            if ($full -like "*$f*") { $forbidden += $ace.Principal }
        }
        if ($allowed -notcontains $name) { $unexpected += "$($ace.Principal) ($($ace.Rights))" }
    }

    foreach ($f in ($forbidden | Select-Object -Unique)) {
        Write-Fail "Broad principal can enrol on VMware: $f -- this is ESC1"
    }
    foreach ($u in ($unexpected | Select-Object -Unique)) {
        Write-Fail "Unexpected principal can enrol on VMware: $u"
    }

    # Compare on the account name, not the full DOMAIN\user string: certutil's
    # rendering of the domain portion is not something to depend on.
    $sawExpected = @($vmwareEnroll | Where-Object {
        (Get-AccountNamePart $_.Principal) -eq $ExpectedEnrolleeSam.ToLower()
    }).Count -gt 0
    if (-not $sawExpected) {
        Write-Fail "$ExpectedEnrollee does NOT have Enroll on VMware -- SDDC Manager cannot issue"
    }

    if ($forbidden.Count -gt 0 -or $unexpected.Count -gt 0 -or -not $sawExpected) {
        Write-Info "Permitted set is exactly: $ExpectedEnrollee plus the documented"
        Write-Info "built-in admin principals ($($BuiltinAdminPrincipals -join '; '))."
        $failures++
    } else {
        Write-Ok "VMware enrolment limited to $ExpectedEnrollee plus built-in admins"
    }
}

# ------------------------- verification: DC template autoenrol scoping (C2) --
Write-Step "Verification: Domain Controller Authentication autoenrols the right group"
if ($dcRow.Count -eq 0) {
    Write-Fail "Skipped: the template is not issued (see above)"
} else {
    $dcCn = $dcRow[0].Cn
    Write-Info "Running: certutil -v -template $dcCn"
    $dcRes = Invoke-Certutil @('-v', '-template', $dcCn)
    if ($dcRes.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($dcRes.Output)) {
        Write-Fail "Cannot verify: certutil -v -template $dcCn exited $($dcRes.ExitCode)"
        $failures++
    } else {
        $dcAdAcl = Get-TemplateEnrolmentAcl -TemplateCn 'DomainControllerAuthentication'
        if ($null -ne $dcAdAcl) {
            Write-Info "ACL source: Active Directory (authoritative)"
            $dcAces = @($dcAdAcl | ForEach-Object {
                [pscustomobject]@{
                    Principal = $_.Principal
                    Rights    = $(if ($_.ViaFullControl) { 'Full Control' } elseif ($_.Enroll -and $_.AutoEnroll) { 'Enroll, AutoEnroll' } elseif ($_.AutoEnroll) { 'AutoEnroll' } else { 'Enroll' })
                } })
        } else {
            Write-Warn "Falling back to parsing certutil output, which OMITS Enroll ACEs."
            $dcAces = Get-AllowAce $dcRes.Output
        }
        $dcAuto = @($dcAces | Where-Object { Test-GrantsAutoEnroll $_.Rights })
        if ($dcAuto.Count -eq 0) {
            Write-Fail "Cannot verify: no Allow ACE granting AutoEnroll found on $dcCn"
            Write-Info "Without Autoenroll the DC never enrols and LDAPS never starts."
            $failures++
        } else {
            # Compared on the ACCOUNT NAME, not DOMAIN\name: the hard-coded
            # 'knowledgeondemand\...' prefix is not this domain's NetBIOS name
            # (NetBIOS caps at 15 characters, so it is KNOWLEDGEONDEMA), so
            # these comparisons never matched and the check failed on a
            # correctly permissioned template.
            #
            # 'Enterprise Read-only Domain Controllers' is accepted alongside
            # the two DC groups: it is DC-scoped by definition and the stock
            # template grants it, so flagging it would be a false finding.
            $dcAllowedAuto = @(
                'domain controllers'
                'enterprise domain controllers'
                'enterprise read-only domain controllers'
            )
            $dcOk = $false
            $dcBad = @()
            foreach ($ace in $dcAuto) {
                $name = Get-AccountNamePart $ace.Principal
                if ($dcAllowedAuto -contains $name) { $dcOk = $true; continue }
                if ($BuiltinAdminPrincipals -contains $name) { continue }
                $dcBad += "$($ace.Principal) ($($ace.Rights))"
            }
            foreach ($b in ($dcBad | Select-Object -Unique)) {
                Write-Fail "Autoenroll granted beyond Domain Controllers: $b"
            }
            if (-not $dcOk) {
                Write-Fail "Autoenroll is NOT granted to knowledgeondemand\Domain Controllers"
                Write-Info "Grant it in certtmpl.msc -> $dcCn -> Security."
            }
            if ($dcBad.Count -gt 0 -or -not $dcOk) {
                Write-Info "Autoenroll must go to Domain Controllers -- NOT Domain Computers,"
                Write-Info "NOT Authenticated Users. A broader grant issues Client"
                Write-Info "Authentication certificates to every machine in the domain."
                $failures++
            } else {
                Write-Ok "Autoenroll restricted to the Domain Controllers group"
            }
        }
    }
}

# ------------------------------ verification: ESC6 flag is not set on CA --
Write-Step "Verification: ESC6 flag not set on CA (EDITF_ATTRIBUTESUBJECTALTNAME2)"
Write-Info "Running: certutil -getreg policy\EditFlags"

# The previous guard sniffed the output text for 'EditFlags|0x[0-9a-fA-F]+|\d+'.
# That \d+ alternative matches almost anything, including
#   "CertUtil: -getreg command FAILED: 0x80070002"
# so an outright certutil failure sailed past the guard, found no flag name, and
# printed "[OK] ESC6 flag not set". Check the exit code, then parse the VALUE and
# test the bit. An absent value is "cannot verify", which is a FAILURE.
$efRes = Invoke-Certutil @('-getreg', 'policy\EditFlags')
if ($efRes.ExitCode -ne 0) {
    Write-Fail "Cannot verify: certutil -getreg policy\EditFlags exited $($efRes.ExitCode)"
    Write-Info $efRes.Output
    $failures++
} else {
    $efMatch = [regex]::Match($efRes.Output, 'EditFlags\s+REG_DWORD\s*=\s*(?:0x)?([0-9a-fA-F]+)')
    if (-not $efMatch.Success) {
        Write-Fail "Cannot verify: no 'EditFlags REG_DWORD = <value>' in certutil output"
        Write-Info "An absent or unparseable value is NOT the same as a cleared flag."
        Write-Info $efRes.Output
        $failures++
    } else {
        $efHex = $efMatch.Groups[1].Value
        $efVal = 0
        $parsed = $false
        try {
            $efVal = [Convert]::ToUInt32($efHex, 16)
            $parsed = $true
        } catch {
            Write-Fail "Cannot verify: EditFlags value '$efHex' is not a hex DWORD"
            $failures++
        }
        if ($parsed) {
            if (($efVal -band 0x40000) -ne 0) {
                Write-Fail ("EDITF_ATTRIBUTESUBJECTALTNAME2 is SET (ESC6): EditFlags = 0x{0:x}" -f $efVal)
                Write-Info "This allows any template to accept an attacker-specified SAN."
                Write-Info "Clear it:  certutil -setreg policy\EditFlags -EDITF_ATTRIBUTESUBJECTALTNAME2"
                Write-Info "then restart certsvc."
                $failures++
            } else {
                Write-Ok ("ESC6 flag not set (EditFlags = 0x{0:x})" -f $efVal)
            }
        }
    }
}

# ------------------ verification: CA-level rights per VMware table (H6c) --
Write-Step "Verification: svc-vcf-ca CA-level rights match VMware's documented table"
Write-Info "Running: certutil -getreg CA\Security"
Write-Info "This is SEPARATE from the template Enroll ACE; neither implies the other."

$secRes = Invoke-Certutil @('-getreg', 'CA\Security')
if ($secRes.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($secRes.Output)) {
    Write-Fail "Cannot verify: certutil -getreg CA\Security exited $($secRes.ExitCode)"
    $failures++
} else {
    $allowLines = @([regex]::Matches($secRes.Output, '(?m)^\s*Allow\s+\S.*$') | ForEach-Object { $_.Value })
    if ($allowLines.Count -eq 0) {
        Write-Fail "Cannot verify: no Allow ACE lines in CA\Security output"
        $failures++
    } else {
        # VMware's documented table for this account is BOTH rights:
        #   Issue and Manage Certificates = ALLOW   (CA_ACCESS_OFFICER, 0x002)
        #   Request Certificates          = ALLOW   (CA_ACCESS_ENROLL,  0x200)
        #   Read / Manage CA              = not selected
        # An earlier version checked only Request Certificates, so a CA missing
        # the Officer right passed and then failed at issuance time.
        #
        # certutil renders these with version-dependent wording, so each right
        # is matched on either of its spellings.
        $svcLines = @($allowLines | Where-Object { $_ -match 'svc-vcf-ca' })
        if ($svcLines.Count -eq 0) {
            Write-Fail "svc-vcf-ca holds NO Allow ACE on the CA at all"
            Write-Info "CA properties -> Security -> add knowledgeondemand\svc-vcf-ca with"
            Write-Info "  Issue and Manage Certificates = allow, Request Certificates = allow."
            Write-Info "See step C1 above."
            $failures++
        } else {
            $hasEnroll  = @($svcLines | Where-Object { $_ -match 'Request Certificates' -or $_ -match 'Enroll' }).Count -gt 0
            # certutil renders CA_ACCESS_OFFICER as "Certificate Manager", not
            # "Issue and Manage Certificates" (the GUI's wording) and not
            # "Officer". Measured: "Allow Certificate Manager Enroll
            # KNOWLEDGEONDEMA\svc-vcf-ca". Matching only the GUI wording
            # produced a false negative against a correctly permissioned CA.
            $hasOfficer = @($svcLines | Where-Object {
                $_ -match 'Issue and Manage Certificates' -or $_ -match 'Certificate Manager' -or $_ -match 'Officer'
            }).Count -gt 0
            if ($hasEnroll)  { Write-Ok "Request Certificates (CA_ACCESS_ENROLL) granted" }
            else { Write-Fail "Request Certificates NOT granted on the CA"; $failures++ }
            if ($hasOfficer) { Write-Ok "Issue and Manage Certificates (CA_ACCESS_OFFICER) granted" }
            else {
                Write-Fail "Issue and Manage Certificates NOT granted on the CA"
                Write-Info "VMware requires it alongside Request Certificates; neither implies the other."
                $failures++
            }
            # Over-grant is a finding too: the documented table withholds these.
            $overGrant = @($svcLines | Where-Object { $_ -match 'Manage CA' -or $_ -match 'CA Administrator' })
            if ($overGrant.Count -gt 0) {
                Write-Warn "svc-vcf-ca appears to hold Manage CA, which VMware's table withholds:"
                foreach ($l in $overGrant) { Write-Info "    $($l.Trim())" }
            }
        }
    }
}

# --------------------------------------- verification: CA auditing (H6a) --
Write-Step "Verification: CA auditing is enabled (the detective control)"
Write-Info "Running: certutil -getreg CA\AuditFilter"
Write-Info "Subject-in-request issuance cannot be name-constrained, so the design"
Write-Info "accepts that risk ONLY because issuance is audited and reviewed."

$afRes = Invoke-Certutil @('-getreg', 'CA\AuditFilter')
if ($afRes.ExitCode -ne 0) {
    Write-Fail "Cannot verify: certutil -getreg CA\AuditFilter exited $($afRes.ExitCode)"
    Write-Info "An unset AuditFilter also returns non-zero. Set it: certutil -setreg CA\AuditFilter 127"
    $failures++
} else {
    $afMatch = [regex]::Match($afRes.Output, 'AuditFilter\s+REG_DWORD\s*=\s*(?:0x)?([0-9a-fA-F]+)')
    if (-not $afMatch.Success) {
        Write-Fail "Cannot verify: no 'AuditFilter REG_DWORD = <value>' in certutil output"
        Write-Info $afRes.Output
        $failures++
    } else {
        $afVal = 0
        try { $afVal = [Convert]::ToUInt32($afMatch.Groups[1].Value, 16) } catch { $afVal = -1 }
        if ($afVal -ne 127) {
            Write-Fail ("CA\AuditFilter is {0}, expected 127 (all seven event categories)" -f $afVal)
            Write-Info "  certutil -setreg CA\AuditFilter 127"
            Write-Info "  auditpol /set /subcategory:`"Certification Services`" /success:enable /failure:enable"
            Write-Info "  net stop certsvc && net start certsvc"
            $failures++
        } else {
            Write-Ok "CA\AuditFilter = 127"
            Write-Info "Auditing the CA is only half of it -- the Object Access audit policy"
            Write-Info "must also be on, and issuance must actually be REVIEWED monthly:"
            Write-Info "  auditpol /get /subcategory:`"Certification Services`""
            Write-Info "  certutil -view -restrict `"NotBefore>=<last review date>`" -out `"RequestID,CommonName,RequesterName`""
        }
    }
}

# ------------------------------------------------------------------ summary --
Write-Host ""
if ($failures -gt 0) {
    Write-Step "VERIFICATION FAILED"
    Write-Fail "$failures check(s) failed -- do not proceed to Publish-LabRootTrust.ps1"
    Write-Host ""
    exit 1
}

Write-Step "All verification checks passed"
Write-Ok "Both templates are published and correctly scoped; CA auditing is on"
Write-Host ""
exit 0
