<#
.SYNOPSIS
    Adds Active Directory as an identity source in VCF Operations.

.DESCRIPTION
    Validate-then-create. POST /suite-api/api/auth/sources/test creates
    nothing and actually exercises the LDAPS bind server-side -- unlike
    SDDC Manager's certificate-authorities GET (see Register-VcfCA.ps1),
    this test endpoint cannot echo back a stored value, because nothing is
    ever stored by it. A failure here is a real bind failure, always
    reported with the server's own error text (never swallowed into a bare
    "failed").

    others: @() and otherAttributes: @{} on sourceType are load-bearing --
    per the appliance's own REST reference (/suite-api/docs/rest/index.html)
    a sourceType without them is rejected as null.

    svc-vcf-ldap has no userPrincipalName, so UPN-form authentication cannot
    work against it; this uses knowledgeondemand\svc-vcf-ldap with
    common-name sAMAccountName instead.

    The suite-api token is acquired fresh on every run (POST
    /suite-api/api/auth/token/acquire) -- it expires quickly and is never
    cached to disk or reused across invocations.

    Dry run (validate only) by default; pass -Apply to create after
    validation passes. Validation always runs first, and creation is
    refused outright if validation did not pass -- there is no path from a
    failed validate straight into a create.

.PARAMETER Apply
    Create the identity source after a passing validation. Without -Apply,
    only validates (creates nothing).

.REQUIRES
    OPERATIONS_ADMIN_USER, OPERATIONS_ADMIN_PASS, AD_BIND_PASS in the
    credential file.

.EXAMPLE
    .\Add-OpsIdentitySource.ps1
    Validate only. Before LDAPS works, this is expected to fail with a bind
    error -- a pass at this stage would mean the validation is not actually
    reaching the bind, which is itself worth investigating.

.EXAMPLE
    .\Add-OpsIdentitySource.ps1 -Apply
    Validates, and if that passes, creates the identity source. A follow-up
    PATCH /suite-api/api/auth/sources is still required before use once SSL
    discovers certificates.
#>
[CmdletBinding()]
param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

function Get-RestErrorDetail {
    param($ErrRecord)
    $msg  = $ErrRecord.Exception.Message
    $resp = $ErrRecord.Exception.Response
    if ($resp) {
        try {
            $stream = $resp.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $body = $reader.ReadToEnd()
            if ($body) { return "$msg -- $body" }
        } catch { }
    }
    $msg
}

$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation
$ops = $cfg.Appliances.Operations.Ip

# ---------------------------------------------------- required credentials --
Write-Step "Checking credentials"
$missing = @()
foreach ($k in @('OPERATIONS_ADMIN_USER', 'OPERATIONS_ADMIN_PASS', 'AD_BIND_PASS')) {
    if (-not $cred.ContainsKey($k) -or -not $cred[$k]) { $missing += $k }
}
if ($missing.Count -gt 0) {
    Write-Fail ("Missing credential key(s) in {0}: {1}" -f $cfg.CredentialFile, ($missing -join ', '))
    exit 1
}
Write-Ok "required credential keys present"

# ------------------------------------------------------------------ token ---
Write-Step "Acquiring VCF Operations token"
$tok = $null
try {
    $tokResp = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/token/acquire" -Method POST `
        -Body @{ username = $cred['OPERATIONS_ADMIN_USER']; password = $cred['OPERATIONS_ADMIN_PASS'] } `
        -Headers @{ Accept = 'application/json' }
    $tok = $tokResp.token
} catch {
    Write-Fail "Could not reach VCF Operations to acquire a token: $(Get-RestErrorDetail $_)"
    exit 1
}
if (-not $tok) {
    Write-Fail "VCF Operations responded but returned no token -- cannot authenticate"
    exit 1
}
$hdr = @{ Authorization = "vRealizeOpsToken $tok" }
Write-Ok "token acquired (acquired fresh this run; not reused across runs)"

# ---------------------------------------------------------------- body -----
# svc-vcf-ldap has NO userPrincipalName, so UPN-form authentication cannot
# work against it. Use DOMAIN\sAMAccountName, paired with common-name
# sAMAccountName.
$body = @{
    name       = 'knowledgeondemand-AD'
    sourceType = @{ id = 'ACTIVE_DIRECTORY'; name = 'ACTIVE_DIRECTORY'; others = @(); otherAttributes = @{} }
    others          = @()
    otherAttributes = @{}
    certificates    = @()
    property = @(
        @{ name = 'display-name';     value = 'knowledgeondemand' }
        @{ name = 'host';             value = 'dns01.knowledgeondemand.net' }
        @{ name = 'domain';           value = 'knowledgeondemand.net' }
        @{ name = 'host-auto-select'; value = 'false' }
        @{ name = 'use-ssl';          value = 'true' }
        @{ name = 'port';             value = '636' }
        @{ name = 'base-domain';      value = 'dc=knowledgeondemand,dc=net' }
        @{ name = 'common-name';      value = 'sAMAccountName' }
        @{ name = 'user-name';        value = 'knowledgeondemand\svc-vcf-ldap' }
        @{ name = 'password';         value = $cred['AD_BIND_PASS'] }
    )
}

# ------------------------------------------------------------ validation ---
Write-Step "Validating the identity source (creates nothing)"
$validated = $false
$validateDetail = ''
try {
    Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources/test" -Method POST -Headers $hdr -Body $body | Out-Null
    $validated = $true
} catch {
    $validateDetail = Get-RestErrorDetail $_
}

if ($validated) {
    Write-Ok "validation passed"
} else {
    Write-Fail "validation failed: $validateDetail"
}

# --------------------------------------------------------------- dry run ---
if (-not $Apply) {
    Write-Info "DRY RUN -- pass -Apply to create (only reachable after validation passes)"
    if (-not $validated) { exit 1 }
    exit 0
}

# ---------------------------------------------------- refuse on bad gate ---
if (-not $validated) {
    Write-Fail "Refusing to create: validation did not pass. Fix LDAPS / credentials first."
    exit 1
}

# ------------------------------------------------------------------ create -
Write-Step "Creating the identity source"
try {
    Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources" -Method POST -Headers $hdr -Body $body | Out-Null
} catch {
    Write-Fail "POST /suite-api/api/auth/sources failed: $(Get-RestErrorDetail $_)"
    exit 1
}
Write-Ok "identity source created"
Write-Warn2 "With SSL enabled the response carries discovered certificates; a follow-up"
Write-Warn2 "PATCH /suite-api/api/auth/sources is required before this source can be used."
exit 0
