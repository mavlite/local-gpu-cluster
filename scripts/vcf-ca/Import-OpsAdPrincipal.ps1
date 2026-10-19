<#
.SYNOPSIS
    Imports an Active Directory principal into VCF Operations and assigns it a
    role, so it can actually log in.

.DESCRIPTION
    Adding an identity source does NOT grant anyone access. The source only
    makes directory accounts visible to Operations; a principal still has to be
    imported and given a role before it can authenticate. Measured: with a
    healthy, persisted AD source and the local 'admin' as the only known
    principal, AD logins returned

        401 "The provided username/password or token is not valid."

    which reads like a credential problem and is in fact an authorization gap.
    Add-OpsIdentitySource.ps1 creates the source; this script completes the job.

    Two calls, both shapes taken from the appliance's own REST reference at
    https://<operations>/suite-api/docs/rest/index.html -- operations
    searchUsersForAuthSource and importUsers -- rather than guessed:

        POST /suite-api/api/auth/sources/{id}/users/search   { name }
        POST /suite-api/api/auth/sources/{id}/users          { users: [ ... ] }

    The search is useful on its own: it proves the source's bind account can
    SEARCH the directory, which validation alone does not establish. Note that
    GET on .../users returns 405 -- both are POST.

    At login, authSource is the source's DISPLAY NAME, not its name. For this
    lab that is 'knowledgeondemand'; passing the source name
    'knowledgeondemand-AD' returns 401. Measured both ways.

    Dry run by default: without -Apply the script searches and reports, and
    imports nothing.

.PARAMETER Account
    sAMAccountName of the directory principal to import.

.PARAMETER Role
    Operations role to grant. Run -ListRoles to see what the appliance offers.
    Granting 'Administrator' is a privilege escalation -- do it deliberately.

.PARAMETER SourceName
    Display name of the identity source to import from, as it appears in
    GET /suite-api/api/auth/sources. Resolved to an id at run time; the id is
    never hard-coded.

.PARAMETER ListRoles
    Print the roles this appliance offers and exit. Changes nothing.

.PARAMETER Apply
    Perform the import. Without it, only the search runs.

.REQUIRES
    OPERATIONS_ADMIN_USER, OPERATIONS_ADMIN_PASS in the credential file.
    AD_BIND_PASS is used only for the optional login proof (see -Account below).

.EXAMPLE
    .\Import-OpsAdPrincipal.ps1 -ListRoles

.EXAMPLE
    .\Import-OpsAdPrincipal.ps1 -Account svc-vcf-ldap -Role ReadOnly -Apply
    Imports the bind account read-only and then PROVES AD login by acquiring a
    token with it -- the only account whose password this repo holds, so the
    only one whose login can be verified from here.

.EXAMPLE
    .\Import-OpsAdPrincipal.ps1 -Account Mavlite -Role Administrator -Apply
    Imports a human administrator. The login cannot be proven from here because
    no password is held for it; the script says so rather than implying success.
#>
[CmdletBinding()]
param(
    [string]$Account,
    [string]$Role       = 'ReadOnly',
    [string]$SourceName = 'knowledgeondemand',
    [switch]$ListRoles,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

# Secrets that must never reach the console. Operations echoes request
# arguments back in some failure bodies, so an error can carry a password that
# was just sent.
$script:SecretValues = @()

function Protect-Secret {
    param([string]$Text)
    if (-not $Text) { return $Text }
    foreach ($s in $script:SecretValues) {
        if ($s) { $Text = $Text.Replace($s, '<redacted>') }
    }
    $Text
}

# Dot-access on a missing property throws under Set-StrictMode -Version Latest,
# including inside catch blocks, where the throw replaces the real error.
function Test-HasProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $false }
    (@($Object.PSObject.Properties.Name) -contains $Name)
}

function Get-RestErrorDetail {
    param($ErrRecord)
    $msg = $ErrRecord.Exception.Message.Split([Environment]::NewLine)[0]
    if (Test-HasProperty $ErrRecord.Exception 'Response') {
        $resp = $ErrRecord.Exception.Response
        if ($resp) {
            try {
                $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
                $body = $reader.ReadToEnd()
                if ($body) { return (Protect-Secret ((("$msg $body") -replace '\s+', ' ').Trim())) }
            } catch { }
        }
    }
    Protect-Secret $msg
}

$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation
$ops = $cfg.Appliances.Operations.Ip

foreach ($k in @('AD_BIND_PASS', 'OPERATIONS_ADMIN_PASS')) {
    if ($cred.ContainsKey($k) -and $cred[$k]) { $script:SecretValues += $cred[$k] }
}

# ---------------------------------------------------- required credentials --
$missing = @()
foreach ($k in @('OPERATIONS_ADMIN_USER', 'OPERATIONS_ADMIN_PASS')) {
    if (-not $cred.ContainsKey($k) -or -not $cred[$k]) { $missing += $k }
}
if ($missing.Count -gt 0) {
    Write-Fail ("Missing credential key(s) in {0}: {1}" -f $cfg.CredentialFile, ($missing -join ', '))
    exit 1
}

# ------------------------------------------------------------------ token ---
# Acquired fresh every run; expires quickly and is never cached to disk.
Write-Step "Acquiring VCF Operations token"
$tok = $null
try {
    $tokResp = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/token/acquire" -Method POST `
        -Body @{ username = $cred['OPERATIONS_ADMIN_USER']; password = $cred['OPERATIONS_ADMIN_PASS'] } `
        -Headers @{ Accept = 'application/json' }
    if (Test-HasProperty $tokResp 'token') { $tok = $tokResp.token }
} catch {
    Write-Fail "Could not acquire a token: $(Get-RestErrorDetail $_)"
    exit 1
}
if (-not $tok) {
    Write-Fail "VCF Operations responded but returned no token"
    exit 1
}
$hdr = @{ Authorization = "vRealizeOpsToken $tok"; Accept = 'application/json' }
Write-Ok "token acquired"

# ------------------------------------------------------------------ roles ---
if ($ListRoles) {
    Write-Step "Roles this appliance offers"
    try {
        $roles = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/roles" -Method GET -Headers $hdr -TimeoutSec 90
    } catch {
        Write-Fail "GET /suite-api/api/auth/roles failed: $(Get-RestErrorDetail $_)"
        exit 1
    }
    if (Test-HasProperty $roles 'userRoles') {
        foreach ($r in @($roles.userRoles)) { Write-Info $r.name }
    }
    exit 0
}

if (-not $Account) {
    Write-Fail "-Account is required (or pass -ListRoles)."
    exit 1
}

# --------------------------------------------------------- resolve source ---
# By display name, so the source id is never hard-coded into the repo.
Write-Step "Resolving identity source '$SourceName'"
try {
    $listed = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources" -Method GET -Headers $hdr -TimeoutSec 90
} catch {
    Write-Fail "GET /suite-api/api/auth/sources failed: $(Get-RestErrorDetail $_)"
    exit 1
}
$sources = @()
if (Test-HasProperty $listed 'sources') { $sources = @($listed.sources) }
if ($sources.Count -eq 0) {
    Write-Fail "Operations lists no identity sources. Run Add-OpsIdentitySource.ps1 -Apply first."
    Write-Info  "A POST that returns 200 without the follow-up PATCH persists nothing."
    exit 1
}
$source = $sources | Where-Object { $_.name -eq $SourceName } | Select-Object -First 1
if (-not $source) {
    Write-Fail "No identity source named '$SourceName'. Found: $(($sources.name) -join ', ')"
    exit 1
}
$sid = $source.id
Write-Ok "source '$SourceName' has id $sid"

# ----------------------------------------------------------------- search ---
# GET on this path returns 405: searching is a POST.
Write-Step "Searching the directory through the source for '$Account'"
$found = $null
try {
    $res = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources/$sid/users/search" -Method POST `
        -Headers $hdr -Body @{ name = $Account } -TimeoutSec 120
} catch {
    Write-Fail "search failed: $(Get-RestErrorDetail $_)"
    Write-Info  "This is a real bind-and-search failure: it exercises the source's"
    Write-Info  "bind account against the directory, which validation alone does not."
    exit 1
}
$matches = @()
if     (Test-HasProperty $res 'user-search-response') { $matches = @($res.'user-search-response') }
elseif (Test-HasProperty $res 'users')                { $matches = @($res.users) }
Write-Info "$($matches.Count) match(es)"
foreach ($m in $matches) {
    $dn = if (Test-HasProperty $m 'distinguishedName') { $m.distinguishedName } else { '' }
    Write-Info "  name='$($m.name)' dn='$dn'"
    if ($m.name -eq $Account) { $found = $m }
}
if (-not $found) {
    Write-Fail "'$Account' was not returned by the source; nothing to import."
    exit 1
}
Write-Ok "the source can search the directory, and '$Account' is visible through it"

# ---------------------------------------------------------------- dry run ---
if (-not $Apply) {
    Write-Info "DRY RUN -- would import '$Account' with role '$Role'. Pass -Apply."
    exit 0
}

# ----------------------------------------------------------------- import ---
if ($Role -eq 'Administrator') {
    Write-Warn2 "'$Account' will receive the Administrator role -- full control of"
    Write-Warn2 "VCF Operations. Reversible with DELETE /suite-api/api/auth/users/{id}."
}
Write-Step "Importing '$Account' with role '$Role'"
$dn = if (Test-HasProperty $found 'distinguishedName') { $found.distinguishedName } else { '' }
$principal = @{
    username           = $found.name
    firstName          = $(if (Test-HasProperty $found 'firstName')    { $found.firstName }    else { $null })
    lastName           = $(if (Test-HasProperty $found 'lastName')     { $found.lastName }     else { $null })
    password           = $null          # a directory principal has no local password
    emailAddress       = $(if (Test-HasProperty $found 'emailAddress') { $found.emailAddress } else { $null })
    distinguishedName  = $dn
    enabled            = $true          # the API sample ships false, which imports a dead account
    groupIds           = $null
    roleNames          = @($Role)
    'role-permissions' = @(
        @{
            roleName        = $Role
            allowAllObjects = $true
            others          = @()
            otherAttributes = @{}
        }
    )
    lastLoginTime      = 0
    others             = @()
    otherAttributes    = @{}
}
try {
    $imported = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources/$sid/users" -Method POST `
        -Headers $hdr -Body @{ users = @($principal) } -TimeoutSec 180
} catch {
    Write-Fail "import failed: $(Get-RestErrorDetail $_)"
    exit 1
}
Write-Ok "import accepted"
if (Test-HasProperty $imported 'users') {
    foreach ($x in @($imported.users)) {
        $rn = if (Test-HasProperty $x 'roleNames') { (@($x.roleNames) -join ',') } else { '' }
        Write-Info "  id=$($x.id) username=$($x.username) roles=[$rn]"
    }
}

# ----------------------------------------------------------------- verify ---
# "Accepted" is not "present" -- the identity source taught that lesson already.
Write-Step "Verifying the principal is listed with its role"
try {
    $all = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/users" -Method GET -Headers $hdr -TimeoutSec 90
} catch {
    Write-Fail "GET /suite-api/api/auth/users failed: $(Get-RestErrorDetail $_)"
    exit 1
}
$users = @()
if (Test-HasProperty $all 'users') { $users = @($all.users) }
$me = $users | Where-Object { $_.username -eq $Account } | Select-Object -First 1
if (-not $me) {
    Write-Fail "'$Account' is not listed after import -- the import did not take effect."
    exit 1
}
$myRoles = if (Test-HasProperty $me 'roleNames') { @($me.roleNames) } else { @() }
Write-Info "listed: $($me.username) roles=[$($myRoles -join ',')]"
if ($myRoles -notcontains $Role) {
    Write-Fail "'$Account' is listed but does NOT carry role '$Role' -- it cannot log in."
    exit 1
}
Write-Ok "'$Account' is imported and carries role '$Role'"

# ------------------------------------------------------------ login proof ---
# Only provable for the account whose password this repo holds. Testing any
# other account with AD_BIND_PASS would 401 for the wrong reason and look like
# a failed import -- precisely the false negative to avoid.
if ($Account -ne 'svc-vcf-ldap') {
    Write-Warn2 "No password is held for '$Account', so its login is NOT proven here."
    Write-Info  "Sign in to the Operations UI as '$Account' against the"
    Write-Info  "'$SourceName' source to confirm, or run this script with"
    Write-Info  "-Account svc-vcf-ldap -Role ReadOnly to prove the path end to end."
    exit 0
}

Write-Step "Proving AD authentication (acquiring a token as '$Account')"
$proved = $false
try {
    $r = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/token/acquire" -Method POST `
        -Headers @{ Accept = 'application/json' } `
        -Body @{ username = $Account; password = $cred['AD_BIND_PASS']; authSource = $SourceName }
    if (Test-HasProperty $r 'token') { $proved = $true }
} catch {
    Write-Fail "login failed: $(Get-RestErrorDetail $_)"
}
if ($proved) {
    Write-Ok "token acquired as '$Account' via authSource '$SourceName' -- AD login works"
    exit 0
}
Write-Fail "'$Account' is imported with a role but still cannot obtain a token."
exit 1
