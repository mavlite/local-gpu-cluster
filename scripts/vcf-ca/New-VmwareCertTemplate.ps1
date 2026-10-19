<#
.SYNOPSIS
    Creates and publishes the 'VMware' certificate template. [OPERATOR], on dns01.

.DESCRIPTION
    Builds the template VMware's documentation describes, without the GUI:

      Compatibility   CA = Windows Server 2008 R2, recipient = Windows 7 / 2008 R2
      Extensions      Application Policies: Server Authentication REMOVED
                      Basic Constraints:    enabled
                      Key Usage:            + Signature is proof of origin
                                              (nonrepudiation)
      Subject Name    Supply in the request
      Security        svc-vcf-ca: Read + Enroll, nothing else

    It works by exporting the stock 'Web Server' template, applying those
    edits, and importing the result -- which is what "duplicate Web Server and
    change these four things" means mechanically.

    WHY A VENDORED MODULE. Creating a schema-v2+ template requires a unique
    msPKI-Cert-Template-OID *and* a matching msPKI-Enterprise-Oid object under
    CN=OID,CN=Public Key Services, generated as
        [forest base OID].[random 8 digits].[random 8 digits]
    with the OID object's CN being [same 8 digits].[32 hex characters],
    uniqueness-checked. Hand-rolling that is where a plausible-looking
    implementation goes quietly wrong, so this drives Ashley McGlone's
    ADCSTemplate module (MIT), vendored at a pinned version under
    vendor/ADCSTemplate/1.0.1.1 so the domain controller never reaches
    PowerShell Gallery and the exact code is reviewable in git.

    WHAT IS STILL PROVEN BY MEASUREMENT, NOT BY THIS SCRIPT. No published
    source -- not the module, not the Ansible role that does the same job, not
    MS-ADA3 or MS-CRTD -- states which attribute encodes Basic Constraints
    "Enable this extension" as opposed to its criticality
    (pKICriticalExtensions). This script adds 2.5.29.19 to
    pKICriticalExtensions, which is the best-supported reading, and then tells
    you to settle it the only way that is not inference: issue one certificate
    and look for the Basic Constraints extension in it.

    Dry run by default; pass -Apply to create.

.PARAMETER SourceTemplate
    Display name of the template to duplicate. VMware says 'Web Server'.

.PARAMETER TemplateName
    Display name AND cn of the new template. The CA is given the cn, and a
    display-name mismatch fails at certificate REQUEST time rather than at
    configuration time, so keep it free of spaces.

.PARAMETER EnrollIdentity
    The one principal granted Enroll. With Server Authentication removed the
    issued certificate carries no EKU restriction, so this ACE is the only
    thing deciding who can obtain an unrestricted certificate -- it is the
    control, not hygiene.

.PARAMETER Apply
    Actually create, publish and permission the template.

.EXAMPLE
    .\New-VmwareCertTemplate.ps1
.EXAMPLE
    .\New-VmwareCertTemplate.ps1 -Apply
#>
[CmdletBinding()]
param(
    [string]$SourceTemplate = 'Web Server',
    [string]$TemplateName   = 'VMware',
    [string]$EnrollIdentity = 'knowledgeondemand\svc-vcf-ca',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [..] $m" -ForegroundColor Gray }
function Write-Warn { param([string]$m) Write-Host "  [!] $m"  -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "  [X] $m"  -ForegroundColor Red }

# X.509 key usage bits live in the first byte, most-significant first.
$KU_DIGITAL_SIGNATURE = 0x80
$KU_NON_REPUDIATION   = 0x40
$KU_KEY_ENCIPHERMENT  = 0x20
$OID_SERVER_AUTH      = '1.3.6.1.5.5.7.3.1'
$OID_BASIC_CONSTRAINTS = '2.5.29.19'
# SCHEMA VERSION: 2, and this DEVIATES from VMware's documentation.
#
# VMware specifies Compatibility = "Windows Server 2008 R2 / Windows 7", which
# certtmpl.msc renders as schema version 3. Built that way and published, VCF
# Operations rejected the registration with
#
#     "Certificate template VMware was not found on the Microsoft CA."
#
# even though the template was published, readable and enrollable by
# svc-vcf-ca. The cause, measured directly: the legacy web enrolment page does
# not offer v3 templates. Fetching
# https://<ca>/certsrv/certrqxt.asp as the service account returned a 10,981
# byte page in which the string "VMware" does not appear at all, while the
# same template showed in certutil -CATemplates and on the CA object's
# certificateTemplates attribute. VCF reads /certsrv, so what /certsrv offers
# is what counts.
#
# Schema 2 (Server 2003 / XP compatibility) is offered by the legacy pages and
# still gets a template OID, which schema 1 does not have and the import
# requires. The project's ORIGINAL instruction -- "leave Compatibility at the
# lowest offered version, a modern default produces a template that rejects
# web-enrolment CSR submission" -- was right about this, and replacing it with
# VMware's documented value was a regression. Everything else in VMware's
# procedure is followed exactly.
$SCHEMA_WEB_ENROLMENT_COMPATIBLE = 2

Write-Step "Loading the vendored ADCSTemplate module"
# Canonical location first, then a recursive search, then alongside this
# script. The fallbacks exist because this file gets copied to the CA host and
# a flattened copy is the normal result of that -- failing on the directory
# layout rather than on the module's absence would be an unhelpful error.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$candidates = @(
    (Join-Path $here 'vendor\ADCSTemplate\1.0.1.1\ADCSTemplate.psd1')
    (Join-Path $here 'ADCSTemplate.psd1')
)
$vendor = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $vendor) {
    $found = Get-ChildItem -LiteralPath $here -Recurse -Filter 'ADCSTemplate.psd1' -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($found) { $vendor = $found.FullName }
}
if (-not $vendor) {
    Write-Fail "Vendored ADCSTemplate module not found."
    Write-Info "Looked for ADCSTemplate.psd1 at, and recursively under, $here"
    Write-Info "It is committed at scripts/vcf-ca/vendor/ADCSTemplate/1.0.1.1/."
    exit 1
}
try {
    Import-Module ActiveDirectory -ErrorAction Stop
    Import-Module $vendor -Force -ErrorAction Stop
} catch {
    Write-Fail "Could not import: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    Write-Info "This script runs on dns01, where the ActiveDirectory module is present."
    exit 1
}
Write-Ok "ADCSTemplate $((Get-Module ADCSTemplate).Version) loaded from vendor/"

# --------------------------------------------------- resolve the identity FIRST --
# This runs before anything is created, because the module sets the ACL only
# AFTER creating the OID object and the template. An identity it cannot
# translate therefore leaves a half-made template behind -- which is exactly
# what happened on the first real run.
#
# The cause is worth stating: NetBIOS domain names are capped at 15
# characters, so the domain knowledgeondemand.net is KNOWLEDGEONDEMA, and
# 'knowledgeondemand\svc-vcf-ca' does not translate at all. Rather than
# hard-code either spelling, take the NetBIOS name from the directory.
Write-Step "Resolving the enrolment identity"
$sam = $EnrollIdentity
if ($sam -match '\\') { $sam = ($sam -split '\\')[-1] }
elseif ($sam -match '@') { $sam = ($sam -split '@')[0] }

$acct = Get-ADUser -Filter "SamAccountName -eq '$sam'" -ErrorAction SilentlyContinue
if (-not $acct) {
    $grp = Get-ADGroup -Filter "SamAccountName -eq '$sam'" -ErrorAction SilentlyContinue
    if ($grp) { $acct = $grp }
}
if (-not $acct) {
    Write-Fail "No user or group with sAMAccountName '$sam' in this domain."
    Write-Info "Create the enrolment account first -- see the Prerequisites section of"
    Write-Info "scripts/vcf-ca/README.md."
    exit 1
}
$netbios = (Get-ADDomain).NetBIOSName
$resolvedIdentity = "$netbios\$($acct.SamAccountName)"
try {
    $sid = (New-Object Security.Principal.NTAccount($resolvedIdentity)).Translate(
               [Security.Principal.SecurityIdentifier]).Value
} catch {
    Write-Fail "'$resolvedIdentity' does not translate to a SID."
    Write-Fail "Nothing has been created. Fix the identity before re-running."
    exit 1
}
if ($sid -ne $acct.SID.Value) {
    Write-Fail "'$resolvedIdentity' translates to $sid but the account's SID is $($acct.SID.Value)."
    Write-Fail "Refusing to grant Enroll to an ambiguous identity."
    exit 1
}
Write-Ok "$resolvedIdentity -> $sid"
if ($resolvedIdentity -ne $EnrollIdentity) {
    Write-Info "(given as '$EnrollIdentity'; the NetBIOS domain name is '$netbios')"
}
$EnrollIdentity = $resolvedIdentity

# ------------------------------------------------------------- already there? --
Write-Step "Checking whether '$TemplateName' already exists"
$existing = Get-ADCSTemplate -DisplayName $TemplateName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Warn "'$TemplateName' already exists (cn=$($existing.cn))."
    Write-Info "This script does not modify an existing template -- that would change"
    Write-Info "what already-issued certificates were validated against. Remove it"
    Write-Info "deliberately first if you intend to rebuild:"
    Write-Info "    Remove-ADCSTemplate -DisplayName '$TemplateName'"
    Write-Info "Then verify the current one with New-VcfCertificateTemplate.ps1."
    exit 1
}
Write-Ok "not present"

# ------------------------------------------------------------------- export --
Write-Step "Exporting the stock '$SourceTemplate' template"
$json = Export-ADCSTemplate -DisplayName $SourceTemplate
if (-not $json) { Write-Fail "Export of '$SourceTemplate' returned nothing."; exit 1 }
$t = $json | ConvertFrom-Json
Write-Ok "exported $(@($t | Get-Member -MemberType NoteProperty).Count) properties"

function Show-Prop {
    param([string]$Name)
    if ($t.PSObject.Properties.Name -notcontains $Name) { return '(absent)' }
    $v = $t.$Name
    if ($null -eq $v) { return '(null)' }
    if ($v -is [string]) { return $v }
    if ($v -is [System.Collections.IEnumerable]) { return (@($v) -join ', ') }
    return [string]$v
}

Write-Step "Applying VMware's documented edits"

# --- 1. Application Policies: remove Server Authentication -------------------
# The stock Web Server template's ONLY EKU is Server Authentication. Removing
# it leaves no EKU extension at all, which makes the issued certificate valid
# for every purpose -- which is what VCF needs. Leaving it in RESTRICTS the
# certificate to server authentication.
foreach ($attr in 'pKIExtendedKeyUsage', 'msPKI-Certificate-Application-Policy') {
    if ($t.PSObject.Properties.Name -notcontains $attr) { continue }
    $before = @($t.$attr)
    $after  = @($before | Where-Object { $_ -ne $OID_SERVER_AUTH })
    Write-Info "$attr : [$($before -join ', ')]"
    if ($after.Count -eq 0) {
        # An empty multi-valued attribute is not the same as an absent one, and
        # the import would try to set an empty collection. Drop the property.
        $t.PSObject.Properties.Remove($attr)
        Write-Host "      -> removed entirely (no EKU restriction)" -ForegroundColor White
    } else {
        $t.$attr = $after
        Write-Host "      -> [$($after -join ', ')]" -ForegroundColor White
    }
}

# --- 2. Key Usage: add nonRepudiation --------------------------------------
if ($t.PSObject.Properties.Name -contains 'pKIKeyUsage') {
    $ku = @([byte[]]$t.pKIKeyUsage)
    $wasHex = ($ku | ForEach-Object { '0x{0:x2}' -f $_ }) -join ' '
    $ku[0] = [byte]($ku[0] -bor $KU_NON_REPUDIATION)
    $t.pKIKeyUsage = $ku
    $nowHex = ($ku | ForEach-Object { '0x{0:x2}' -f $_ }) -join ' '
    Write-Info "pKIKeyUsage : $wasHex -> $nowHex"
    $bits = @()
    if ($ku[0] -band $KU_DIGITAL_SIGNATURE) { $bits += 'digitalSignature' }
    if ($ku[0] -band $KU_NON_REPUDIATION)   { $bits += 'nonRepudiation' }
    if ($ku[0] -band $KU_KEY_ENCIPHERMENT)  { $bits += 'keyEncipherment' }
    Write-Host "      -> $($bits -join ', ')" -ForegroundColor White
} else {
    Write-Fail "pKIKeyUsage absent from the export -- cannot add nonRepudiation"
    exit 1
}

# --- 3. Basic Constraints ---------------------------------------------------
$crit = @()
if ($t.PSObject.Properties.Name -contains 'pKICriticalExtensions') { $crit = @($t.pKICriticalExtensions) }
Write-Info "pKICriticalExtensions : [$($crit -join ', ')]"
if ($crit -notcontains $OID_BASIC_CONSTRAINTS) {
    $crit += $OID_BASIC_CONSTRAINTS
    if ($t.PSObject.Properties.Name -contains 'pKICriticalExtensions') { $t.pKICriticalExtensions = $crit }
    else { $t | Add-Member -NotePropertyName 'pKICriticalExtensions' -NotePropertyValue $crit }
    Write-Host "      -> [$($crit -join ', ')]  (added Basic Constraints)" -ForegroundColor White
    Write-Warn "This is the best-supported reading, NOT a documented mapping."
    Write-Warn "Confirm by issuing a certificate and looking for the extension."
} else {
    Write-Ok "Basic Constraints already listed"
}

# --- 4. Compatibility / schema version --------------------------------------
$oldSchema = Show-Prop 'msPKI-Template-Schema-Version'
if ($t.PSObject.Properties.Name -contains 'msPKI-Template-Schema-Version') {
    $t.'msPKI-Template-Schema-Version' = $SCHEMA_WEB_ENROLMENT_COMPATIBLE
} else {
    $t | Add-Member -NotePropertyName 'msPKI-Template-Schema-Version' -NotePropertyValue $SCHEMA_WEB_ENROLMENT_COMPATIBLE
}
Write-Info "msPKI-Template-Schema-Version : $oldSchema -> $SCHEMA_WEB_ENROLMENT_COMPATIBLE  (legacy /certsrv does not offer v3)"

# A fresh template starts its own revision history rather than inheriting the
# stock template's.
foreach ($pair in @(@{n='revision'; v=100}, @{n='msPKI-Template-Minor-Revision'; v=0})) {
    if ($t.PSObject.Properties.Name -contains $pair.n) { $t.($pair.n) = $pair.v }
    else { $t | Add-Member -NotePropertyName $pair.n -NotePropertyValue $pair.v }
}
Write-Info "revision : 100, minor 0"

# --- 5. Subject Name: confirm, do not change --------------------------------
# ENROLLEE_SUPPLIES_SUBJECT is 0x1. The stock Web Server template already has
# it, so this is a confirmation. Getting it wrong here, with no EKU
# restriction, is the ESC1 shape -- so it is asserted rather than assumed.
$nameFlag = 0
if ($t.PSObject.Properties.Name -contains 'msPKI-Certificate-Name-Flag') { $nameFlag = [int]$t.'msPKI-Certificate-Name-Flag' }
Write-Info "msPKI-Certificate-Name-Flag : $nameFlag"
if (($nameFlag -band 0x1) -ne 0x1) {
    Write-Fail "ENROLLEE_SUPPLIES_SUBJECT (0x1) is NOT set on '$SourceTemplate'."
    Write-Fail "VCF supplies the subject, so this is required. Refusing to continue."
    exit 1
}
Write-Ok "Supply in the request is set"

$modified = $t | ConvertTo-Json -Depth 10 -Compress

if (-not $Apply) {
    Write-Step "DRY RUN"
    Write-Info "WOULD create template '$TemplateName' (cn '$($TemplateName.Replace(' ',''))')"
    Write-Info "WOULD publish it to every CA in the forest"
    Write-Info "WOULD grant Read + Enroll to $EnrollIdentity, and nothing else"
    Write-Host ""
    Write-Warn "Pass -Apply to create it."
    exit 0
}

# ------------------------------------------------------------------- create --
Write-Step "Creating, publishing and permissioning"
try {
    New-ADCSTemplate -DisplayName $TemplateName -JSON $modified `
        -Identity $EnrollIdentity -Publish -ErrorAction Stop
    Write-Ok "created and published"
} catch {
    Write-Fail "Creation failed: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    Write-Info "Nothing is half-made that matters: if the template object exists but is"
    Write-Info "wrong, remove it with Remove-ADCSTemplate -DisplayName '$TemplateName'"
    Write-Info "and re-run, rather than editing it in place."
    exit 1
}

# ------------------------------------------------------------------- verify --
# Read the object back and check each documented requirement. "New-ADCSTemplate
# did not throw" is not evidence that the object matches the specification.
Write-Step "Verifying the created template against VMware's specification"
$new = Get-ADCSTemplate -DisplayName $TemplateName
if (-not $new) { Write-Fail "Template not found after creation."; exit 1 }

$fail = 0
function Check {
    param([string]$What, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { Write-Ok "$What$(if ($Detail) { " -- $Detail" })" }
    else { Write-Fail "$What$(if ($Detail) { " -- $Detail" })"; $script:fail++ }
}

Check "cn is '$($TemplateName.Replace(' ',''))'" ($new.cn -eq $TemplateName.Replace(' ','')) "cn=$($new.cn)"
Check "displayName is '$TemplateName'" ($new.displayName -eq $TemplateName) "displayName=$($new.displayName)"

$newEku = @()
if ($new.PSObject.Properties.Name -contains 'pKIExtendedKeyUsage' -and $new.pKIExtendedKeyUsage) { $newEku = @($new.pKIExtendedKeyUsage) }
Check "no Server Authentication EKU" ($newEku -notcontains $OID_SERVER_AUTH) "EKU=[$($newEku -join ', ')]"

$newKu = @([byte[]]$new.pKIKeyUsage)
Check "Key Usage includes nonRepudiation" (($newKu[0] -band $KU_NON_REPUDIATION) -eq $KU_NON_REPUDIATION) ("0x{0:x2}" -f $newKu[0])

$newCrit = @()
if ($new.PSObject.Properties.Name -contains 'pKICriticalExtensions' -and $new.pKICriticalExtensions) { $newCrit = @($new.pKICriticalExtensions) }
Check "Basic Constraints listed" ($newCrit -contains $OID_BASIC_CONSTRAINTS) "[$($newCrit -join ', ')]"

Check "schema version is $SCHEMA_WEB_ENROLMENT_COMPATIBLE" ([int]$new.'msPKI-Template-Schema-Version' -eq $SCHEMA_WEB_ENROLMENT_COMPATIBLE) "v$($new.'msPKI-Template-Schema-Version')"
Check "Supply in the request set" ((([int]$new.'msPKI-Certificate-Name-Flag') -band 0x1) -eq 0x1) "flag=$($new.'msPKI-Certificate-Name-Flag')"
Check "template OID assigned" ([bool]$new.'msPKI-Cert-Template-OID') "$($new.'msPKI-Cert-Template-OID')"

Write-Step "Result"
if ($fail -gt 0) {
    Write-Fail "$fail requirement(s) not met. Remove and rebuild rather than editing:"
    Write-Info "    Remove-ADCSTemplate -DisplayName '$TemplateName'"
    exit 1
}
Write-Ok "every checked requirement is met"
Write-Host ""
Write-Warn "NOT YET PROVEN. The ACL and Basic Constraints are the two things this"
Write-Warn "cannot settle by reading the object:"
Write-Info "1. Run New-VcfCertificateTemplate.ps1 -Apply to verify the Enroll ACE is"
Write-Info "   limited to $EnrollIdentity, and the CA-level rights alongside it."
Write-Info "2. Issue ONE certificate from this template and confirm the result carries"
Write-Info "   a Basic Constraints extension and no EKU restriction. That is the only"
Write-Info "   check that settles the Basic Constraints mapping."
Write-Host ""
exit 0
