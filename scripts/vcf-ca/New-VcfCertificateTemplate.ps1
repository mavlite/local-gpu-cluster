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
$ExpectedEnrollee = 'knowledgeondemand\svc-vcf-ca'

# Built-in administrative principals that hold Full Control on EVERY certificate
# template by default and cannot sensibly be removed (removing Enterprise Admins
# from a template's ACL breaks template management itself). "Exactly one
# principal" is therefore unsatisfiable; the assertion is that the set of
# enrolment-capable principals equals {$ExpectedEnrollee} PLUS this explicit,
# documented allow-list, and NOTHING else.
$BuiltinAdminPrincipals = @(
    'knowledgeondemand\domain admins'
    'knowledgeondemand\enterprise admins'
    'builtin\administrators'
    'nt authority\system'
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
Create the templates by hand in certtmpl.msc -- there is no supported cmdlet
for template creation, and a scripted ADSI clone is fragile enough that a
wrong template costs more than the five minutes this saves.

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

  B2. General: Set Template display name to 'VMware'
      - Confirm the TEMPLATE NAME (cn) is also 'VMware' in the Name field.
        SDDC Manager is given the cn, and a display-name mismatch fails at
        certificate REQUEST time, not at configuration time. This script
        verifies the cn specifically, not just that 'VMware' appears somewhere.
      - Validity period: 2 years

  B3. Compatibility: Leave at the LOWEST offered version
      - A modern default produces a v4 template with CNG/KSP constraints that
        reject web-enrolment CSR submission.

  B4. Extensions: Application Policies
      - Server Authentication AND Client Authentication (both required)

  B5. Subject Name: 'Supply in the request'
      - Do not set any Subject Name alternatives; SDDC Manager provides the SAN.

  B6. Security: Enroll ACE
      - Add: knowledgeondemand\svc-vcf-ca -> Enroll (allow)
      - Remove: Any Authenticated Users / Domain Users Enroll ACE (this is ESC1)
      - Domain Admins / Enterprise Admins / BUILTIN\Administrators keep Full
        Control; that is a default that cannot sensibly be removed and is
        explicitly allow-listed by this script's verification.

  B7. Issue it: certsrv.msc -> Certificate Templates -> right-click New ->
      Certificate Template to Issue -> VMware

-- C. CA-level settings (separate from anything on a template)

  C1. CA properties -> Security
      - Add: knowledgeondemand\svc-vcf-ca -> Request Certificates (allow)
      - NOTE: This is SEPARATE from the template Enroll ACE in B6. Both are
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

$hasServerAuth = $t -match 'Server Authentication'
$hasClientAuth = $t -match 'Client Authentication'

if (-not ($hasServerAuth -and $hasClientAuth)) {
    Write-Fail "EKU is not as designed"
    if (-not $hasServerAuth) { Write-Info "  Missing: Server Authentication" }
    if (-not $hasClientAuth) { Write-Info "  Missing: Client Authentication" }
    Write-Info "VCF certificates must carry both; see General -> Extensions tab."
    $failures++
} else {
    Write-Ok "EKU carries Server + Client Authentication"
}

# ----------------------------------- verification: no broad enrolment ACE --
Write-Step "Verification: VMware enrolment is restricted (ESC1 control)"
Write-Info "This is the ONLY control preventing arbitrary certificate issuance"

$vmwareAces = Get-AllowAce $t
$vmwareEnroll = @($vmwareAces | Where-Object { Test-GrantsEnroll $_.Rights })

if ($vmwareEnroll.Count -eq 0) {
    # Fail closed: finding no Allow ACEs means the parse failed or the output
    # is not what we think it is. It does NOT mean nobody can enrol.
    Write-Fail "Cannot verify: no 'Allow <rights> <principal>' ACE lines found for VMware"
    Write-Info "certutil output format may have changed. This is a FAILURE."
    $failures++
} else {
    $allowed = @($BuiltinAdminPrincipals) + @($ExpectedEnrollee.ToLower())
    $unexpected = @()
    $forbidden  = @()
    foreach ($ace in $vmwareEnroll) {
        $p = $ace.Principal.ToLower()
        foreach ($f in $ForbiddenPrincipals) {
            if ($p -like "*$f*") { $forbidden += $ace.Principal }
        }
        if ($allowed -notcontains $p) { $unexpected += "$($ace.Principal) ($($ace.Rights))" }
    }

    foreach ($f in ($forbidden | Select-Object -Unique)) {
        Write-Fail "Broad principal can enrol on VMware: $f -- this is ESC1"
    }
    foreach ($u in ($unexpected | Select-Object -Unique)) {
        Write-Fail "Unexpected principal can enrol on VMware: $u"
    }

    $sawExpected = @($vmwareEnroll | Where-Object { $_.Principal.ToLower() -eq $ExpectedEnrollee.ToLower() }).Count -gt 0
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
        $dcAces = Get-AllowAce $dcRes.Output
        $dcAuto = @($dcAces | Where-Object { Test-GrantsAutoEnroll $_.Rights })
        if ($dcAuto.Count -eq 0) {
            Write-Fail "Cannot verify: no Allow ACE granting AutoEnroll found on $dcCn"
            Write-Info "Without Autoenroll the DC never enrols and LDAPS never starts."
            $failures++
        } else {
            $dcOk = $false
            $dcBad = @()
            foreach ($ace in $dcAuto) {
                $p = $ace.Principal.ToLower()
                if ($p -eq 'knowledgeondemand\domain controllers' -or
                    $p -eq 'knowledgeondemand\enterprise domain controllers') {
                    $dcOk = $true
                    continue
                }
                if ($BuiltinAdminPrincipals -contains $p) { continue }
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

# ------------------- verification: CA-level Request Certificates ACE (H6c) --
Write-Step "Verification: svc-vcf-ca holds Request Certificates on the CA itself"
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
        # certutil renders CA_ACCESS_ENROLL as either "Request Certificates" or
        # "Enroll" depending on version; accept either on the svc account's line.
        $hit = @($allowLines | Where-Object {
            $_ -match 'svc-vcf-ca' -and ($_ -match 'Request Certificates' -or $_ -match 'Enroll')
        })
        if ($hit.Count -eq 0) {
            Write-Fail "svc-vcf-ca does NOT hold Request Certificates on the CA"
            Write-Info "Add it: CA properties -> Security -> knowledgeondemand\svc-vcf-ca"
            Write-Info "        -> Request Certificates (allow). See step C1 above."
            $failures++
        } else {
            Write-Ok "svc-vcf-ca holds Request Certificates on the CA"
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
