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

    The bind login name is the UPN form (svc-vcf-ldap@knowledgeondemand.net),
    derived from AD_BIND_USER, which holds a distinguished name. The NetBIOS
    form is deliberately rejected: NetBIOS names cap at 15 characters, so this
    domain is KNOWLEDGEONDEMA, and the knowledgeondemand\user form resolves to
    nothing. An earlier comment here claimed the account has no
    userPrincipalName; it has one, and the UPN form sidesteps truncation.

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
    Validates, and if that passes, creates the identity source: POST, then the
    PATCH that accepts the discovered LDAPS certificates, then a read-back to
    confirm the source is actually listed. Importing a principal and granting it
    a role is a separate step -- see Import-OpsAdPrincipal.ps1.
#>
[CmdletBinding()]
param(
    # Login name for the LDAP bind. NOT derived from AD at run time because
    # this script runs from a workstation without the ActiveDirectory module.
    #
    # The previous hard-coded value was 'knowledgeondemand\svc-vcf-ldap',
    # which does not resolve: NetBIOS domain names cap at 15 characters, so
    # this domain is KNOWLEDGEONDEMA. Measured -- 'knowledgeondemand\...'
    # fails to translate to a SID, 'KNOWLEDGEONDEMA\...' succeeds. The same
    # mistake had already broken template creation and two verification gates.
    #
    # The UPN form is preferred because it carries no NetBIOS truncation to get
    # wrong. An old comment here claimed svc-vcf-ldap has no
    # userPrincipalName; it does -- svc-vcf-ldap@knowledgeondemand.net --
    # so that justification for DOMAIN\user no longer applies.
    [string]$BindUserName,
    [string]$Domain = 'knowledgeondemand.net',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

# Secret values that must never reach the console. The request body carries
# AD_BIND_PASS in a `property` entry, and the suite-api validation endpoint
# echoes request arguments back in some failure responses -- so an error body
# can contain the very password that was just sent. Populated after the
# credential file loads.
$script:SecretValues = @()

# Redacts every known secret from arbitrary text before it is printed. Applied
# to every error body, not only the ones expected to carry a secret.
function Protect-Secret {
    param([string]$Text)
    if (-not $Text) { return $Text }
    foreach ($s in $script:SecretValues) {
        if ($s) { $Text = $Text.Replace($s, '<redacted>') }
    }
    $Text
}

function Test-HasProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $false }
    (@($Object.PSObject.Properties.Name) -contains $Name)
}

function Get-RestErrorDetail {
    param($ErrRecord)
    $msg   = $ErrRecord.Exception.Message
    $inner = $ErrRecord.Exception.InnerException
    while ($inner) {
        $msg  += " -- $($inner.Message)"
        $inner = $inner.InnerException
    }
    $resp = $ErrRecord.Exception.Response
    if ($resp) {
        try {
            $stream = $resp.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $body = $reader.ReadToEnd()
            if ($body) { return (Protect-Secret "$msg -- $body") }
        } catch { }
    }
    Protect-Secret $msg
}

$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation
$ops = $cfg.Appliances.Operations.Ip

# Register every secret this script handles before the first REST call.
foreach ($k in @('AD_BIND_PASS', 'OPERATIONS_ADMIN_PASS')) {
    if ($cred.ContainsKey($k) -and $cred[$k]) { $script:SecretValues += $cred[$k] }
}

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
# Resolve the bind login name. AD_BIND_USER in the credential file holds a
# DISTINGUISHED NAME (CN=svc-vcf-ldap,CN=Users,DC=...), which is not a login
# name, so it is converted rather than used as-is.
if (-not $BindUserName) {
    $raw = if ($cred.ContainsKey('AD_BIND_USER')) { $cred['AD_BIND_USER'] } else { '' }
    if ($raw -match '^\s*CN=([^,]+),') {
        # A DN: take the CN and build a UPN for the configured domain.
        $BindUserName = "$($Matches[1])@$Domain"
    } elseif ($raw -and ($raw -match '@' -or $raw.Contains([char]92))) {
        # Already a usable login form (UPN or DOMAIN\user).
        $BindUserName = $raw
    } else {
        Write-Fail "Cannot determine a bind login name from AD_BIND_USER ('$raw')."
        Write-Info "Pass -BindUserName explicitly, e.g. svc-vcf-ldap@$Domain"
        exit 1
    }
}
Write-Info "bind login name: $BindUserName"
# StartsWith with an explicit char, not a regex: a trailing backslash in a
# pattern throws "Illegal \ at end of pattern", and this very guard did.
if ($BindUserName.StartsWith('knowledgeondemand' + [char]92)) {
    Write-Fail "'$BindUserName' uses a NetBIOS domain that does not exist."
    Write-Fail "NetBIOS caps at 15 characters; this domain is KNOWLEDGEONDEMA."
    Write-Info "Use the UPN form instead: svc-vcf-ldap@$Domain"
    exit 1
}
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
        @{ name = 'user-name';        value = $BindUserName }
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
# Creating an SSL identity source is TWO calls, not one. The POST returns 200
# and a fully populated source object, but nothing is persisted: with use-ssl
# true the response carries the certificates Operations discovered at the LDAPS
# endpoint, and the source only exists once those are accepted back via PATCH.
#
# Measured: after a "successful" POST alone, GET /auth/sources returned
# {"sources": []} and AD authentication 401'd. An earlier version of this script
# named the required PATCH in a warning and then discarded the response it
# needed (| Out-Null), so the create could never complete.
Write-Step "Creating the identity source (POST)"
$posted = $null
try {
    $posted = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources" -Method POST `
        -Headers $hdr -Body $body -TimeoutSec 120
} catch {
    Write-Fail "POST /suite-api/api/auth/sources failed: $(Get-RestErrorDetail $_)"
    exit 1
}
if (-not $posted) {
    Write-Fail "POST returned no body, so the discovered certificates cannot be accepted."
    exit 1
}
Write-Ok "POST accepted"

$discovered = @()
if (Test-HasProperty $posted 'certificates') { $discovered = @($posted.certificates) }
Write-Info "certificates discovered at the LDAPS endpoint: $($discovered.Count)"
foreach ($c in $discovered) {
    if (Test-HasProperty $c 'thumbprint') { Write-Info "  thumbprint $($c.thumbprint)" }
}
if ($discovered.Count -eq 0 -and $body.property | Where-Object { $_.name -eq 'use-ssl' -and $_.value -eq 'true' }) {
    Write-Warn2 "use-ssl is true but no certificate was discovered -- the PATCH below"
    Write-Warn2 "will likely leave the source unusable. Check LDAPS on the directory host."
}

Write-Step "Accepting the discovered certificates (PATCH)"
$patchBody = $posted
if ($discovered.Count -gt 0) { $patchBody.certificates = $discovered }
$created = $null
try {
    $created = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources" -Method PATCH `
        -Headers $hdr -Body $patchBody -TimeoutSec 120
} catch {
    Write-Fail "PATCH /suite-api/api/auth/sources failed: $(Get-RestErrorDetail $_)"
    Write-Fail "The source is NOT usable -- the POST alone persists nothing."
    exit 1
}
if (Test-HasProperty $created 'id') { Write-Ok "source persisted with id $($created.id)" }
else { Write-Ok "PATCH accepted" }

# ------------------------------------------------------------------ verify ---
# "Created" is not "present". Read the list back rather than trusting the PATCH.
Write-Step "Verifying the source is listed"
$listed = $null
try {
    $listed = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources" -Method GET `
        -Headers $hdr -TimeoutSec 90
} catch {
    Write-Fail "GET /suite-api/api/auth/sources failed: $(Get-RestErrorDetail $_)"
    exit 1
}
$sources = @()
if (Test-HasProperty $listed 'sources') { $sources = @($listed.sources) }
if ($sources.Count -eq 0) {
    Write-Fail "Operations lists no identity sources -- the create did not take effect."
    exit 1
}
foreach ($s in $sources) { Write-Info "listed: name='$($s.name)' type='$($s.sourceType.id)'" }
Write-Ok "identity source present in VCF Operations"

Write-Warn2 "The source alone does not grant access: a directory principal must be"
Write-Warn2 "imported and given a role before it can log in. Use Import-OpsAdPrincipal.ps1."
Write-Info  "At login, authSource is the source's DISPLAY NAME ('knowledgeondemand'),"
Write-Info  "not its name ('knowledgeondemand-AD') -- measured; the latter returns 401."
exit 0
