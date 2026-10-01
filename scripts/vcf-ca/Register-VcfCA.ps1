<#
.SYNOPSIS
    Registers the lab CA with VCF Operations Fleet Management. [SCRIPTED].

.DESCRIPTION
    In VCF 9.x the certificate-authority control point is VCF OPERATIONS, not
    SDDC Manager. VMware's documented path is

        Manage > Fleet Management > Certificates > VCF Management
                                                 > Configure CA for Fleet

    and the equivalent API, confirmed against this lab on 2026-10-01, is

        GET/PUT https://<ops>/suite-api/api/fleet-management/
                              certificate-management/certificate-authorities

    An earlier version of this script PUT to SDDC Manager's
    /v1/certificate-authorities. That endpoint exists, but it is the 4.x/5.x
    path, and Broadcom KB 432263 warns that out-of-band certificate work on
    SDDC Manager using legacy procedures produces "PKIX path building failed"
    and a trust mismatch that then needs adapter restarts to repair. Operations
    also reports 39 managed certificates against SDDC Manager's 8, so it is the
    broader and correct control point.

    TWO GOTCHAS FROM THE KBs, both enforced here as pre-flight checks rather
    than discovered as a failed API call:

      * The username must be in UPN form. KB 416470: DOMAIN\username produces
        "Certificate authorities update failed." because the backslash breaks
        JSON escaping.
      * The password must not contain { or }. KB 432263: the API framework
        reads curly brackets as a variable placeholder and fails to expand it.

    AUTH SCHEME: vRealizeOpsToken, not Bearer. Published write-ups say Bearer;
    on this build Bearer returns 401. The token comes from
    POST /suite-api/api/auth/token/acquire with authSource 'local'.

    This script registers; it does not prove issuance. A GET echoes back
    whatever was stored, so the real proof is replacing one certificate and
    watching it come back signed by this CA.

    Dry run by default; pass -Apply to register.

.PARAMETER CaServerUrl
    Must begin with https:// and end with /certsrv -- VMware states this
    explicitly, and /certsrv is only reachable over TLS here because the
    hardening step sets Require-SSL on that virtual directory.

.PARAMETER TemplateName
    The template's cn, not its display name. A display-name mismatch fails at
    certificate REQUEST time rather than at configuration time.

.PARAMETER Apply
    Actually PUT the specification.

.EXAMPLE
    .\Register-VcfCA.ps1
.EXAMPLE
    .\Register-VcfCA.ps1 -Apply
#>
[CmdletBinding()]
param(
    [string]$CaServerUrl  = 'https://dns01.knowledgeondemand.net/certsrv',
    [string]$TemplateName = 'VMware',
    [string]$OperationsIp,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

function Write-Step  { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok    { param([string]$m) Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Write-Info  { param([string]$m) Write-Host "  [info] $m" -ForegroundColor Gray }
function Write-Warn2 { param([string]$m) Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Write-Fail  { param([string]$m) Write-Host "  [FAIL] $m" -ForegroundColor Red }

# Secrets that must never reach the console. Both the enrolment password and
# the Operations admin password are sent as request content, and APIs echo
# request arguments back in some validation failures.
$script:SecretValues = @()
function Protect-Secret {
    param([string]$Text)
    if (-not $Text) { return $Text }
    foreach ($s in $script:SecretValues) {
        if ($s) { $Text = $Text.Replace($s, '<redacted>') }
    }
    return $Text
}
function Get-RestErrorDetail {
    param($ErrRecord)
    $msg = $ErrRecord.Exception.Message
    $inner = $ErrRecord.Exception.InnerException
    while ($inner) { $msg += " -- $($inner.Message)"; $inner = $inner.InnerException }
    # Under StrictMode, reading a property an exception does not carry throws,
    # and this runs inside a catch -- so the throw would replace the real error.
    $resp = $null
    if ($ErrRecord.Exception.PSObject.Properties.Name -contains 'Response') {
        $resp = $ErrRecord.Exception.Response
    }
    if ($resp) {
        try {
            $reader = New-Object IO.StreamReader($resp.GetResponseStream())
            $body = $reader.ReadToEnd()
            if ($body) { return (Protect-Secret "$msg -- $body") }
        } catch { }
    }
    return (Protect-Secret $msg)
}

# ------------------------------------------------------------- load inputs --
$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation

foreach ($k in 'AD_CA_ENROLL_USER', 'AD_CA_ENROLL_PASS', 'OPERATIONS_ADMIN_PASS') {
    if (-not $cred.ContainsKey($k) -or -not $cred[$k]) {
        Write-Fail "$k is missing from the credential file."
        Write-Info "See the Prerequisites section of scripts/vcf-ca/README.md."
        exit 1
    }
}
foreach ($k in 'AD_CA_ENROLL_PASS', 'OPERATIONS_ADMIN_PASS') { $script:SecretValues += $cred[$k] }

$ops = if ($OperationsIp) { $OperationsIp } else { $cfg.Appliances.Operations.Ip }
$opsUser = if ($cred.ContainsKey('OPERATIONS_ADMIN_USER') -and $cred['OPERATIONS_ADMIN_USER']) {
    $cred['OPERATIONS_ADMIN_USER'] } else { 'admin' }
$enrollUser = $cred['AD_CA_ENROLL_USER']
$enrollPass = $cred['AD_CA_ENROLL_PASS']

Write-Step "Target"
Write-Info "VCF Operations : $ops  (admin user: $opsUser)"
Write-Info "CA server URL  : $CaServerUrl"
Write-Info "Template (cn)  : $TemplateName"
Write-Info "Enrolment user : $enrollUser"

# ------------------------------------------------- pre-flight, from the KBs --
Write-Step "Pre-flight checks taken from the Broadcom KBs"
$blockers = 0

# KB 416470
if ($enrollUser -match '\\') {
    Write-Fail "Enrolment username is in DOMAIN\user form: '$enrollUser'"
    Write-Fail "KB 416470: this produces 'Certificate authorities update failed.' because"
    Write-Fail "the backslash breaks JSON escaping. Use UPN form, e.g. svc-vcf-ca@domain.tld"
    $blockers++
} elseif ($enrollUser -notmatch '^[^@\s]+@[^@\s]+$') {
    Write-Fail "Enrolment username '$enrollUser' is not a UPN. KB 416470 requires UPN form."
    $blockers++
} else {
    Write-Ok "username is UPN form (KB 416470)"
}

# KB 432263
if ($enrollPass -match '[{}]') {
    Write-Fail "The enrolment password contains a curly bracket."
    Write-Fail "KB 432263: the API framework reads { } as a variable placeholder and the"
    Write-Fail "request fails with 'Not enough variable values available to expand'."
    Write-Fail "Rotate the password to one without { or }."
    $blockers++
} else {
    Write-Ok "password contains no curly brackets (KB 432263)"
}

# VMware states the URL format explicitly.
if ($CaServerUrl -notmatch '^https://') {
    Write-Fail "CA server URL must begin with https:// -- VMware states this explicitly."
    $blockers++
} elseif ($CaServerUrl -notmatch '/certsrv/?$') {
    Write-Fail "CA server URL must end with /certsrv."
    $blockers++
} else {
    Write-Ok "CA server URL is https and ends with /certsrv"
}

# The endpoint has to be reachable and actually accept these credentials,
# because Operations will make exactly this request. Checking it here turns a
# confusing "Certificate authorities update failed" into a clear local finding.
Write-Step "Proving /certsrv accepts the enrolment credentials over TLS"
try {
    $req = [Net.HttpWebRequest]::Create($CaServerUrl.TrimEnd('/') + '/')
    $req.Method = 'GET'
    $req.Timeout = 20000
    $req.AllowAutoRedirect = $false
    $pair = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${enrollUser}:${enrollPass}"))
    $req.Headers.Add('Authorization', "Basic $pair")
    $resp = $req.GetResponse()
    Write-Ok "GET $CaServerUrl returned $([int]$resp.StatusCode) as $enrollUser"
    $resp.Close()
} catch [Net.WebException] {
    $code = $null
    if ($_.Exception.PSObject.Properties.Name -contains 'Response' -and $_.Exception.Response) {
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
    }
    $msg = $_.Exception.Message
    if ($code -eq 401) {
        # A 401 IS a real finding: the credentials or the Basic auth setup are
        # wrong, and Operations will fail the same way.
        Write-Fail "401 from /certsrv with these credentials."
        Write-Fail "If IIS logs '401 2 5', the Web-Basic-Auth FEATURE is missing -- enabling"
        Write-Fail "basicAuthentication without it is accepted and does nothing (KB 432263)."
        Write-Info "Check:  Get-WindowsFeature Web-Basic-Auth   on the CA host."
        $blockers++
    } elseif ($msg -match 'could not be resolved|No such host|name or service not known') {
        # This machine not resolving the CA's name says nothing about whether
        # VCF Operations can reach it -- and Operations is the client that
        # matters. A workstation on a VPN using block-outside-dns cannot
        # resolve any lab name at all. Inconclusive, not a blocker.
        Write-Warn2 "Cannot resolve $CaServerUrl from THIS host -- check not performed."
        Write-Info "That is a statement about this workstation, not about the CA. Operations"
        Write-Info "is the client that matters. Verify from inside the lab if in doubt:"
        Write-Info "  curl -ksS -o /dev/null -w '%{http_code}\n' -u '<upn>' $CaServerUrl/"
    } else {
        # Reachability failures from here are likewise about this host's
        # position, not about the endpoint. Reported, not fatal.
        Write-Warn2 "GET $CaServerUrl did not complete from this host$(if ($code) { " ($code)" }): $(Protect-Secret $msg)"
        Write-Info "Not treated as a blocker: this host may simply have no route to the CA."
    }
} catch {
    Write-Warn2 "GET $CaServerUrl could not be attempted: $(Protect-Secret $_.Exception.Message)"
    Write-Info "Not treated as a blocker -- see above."
}

if ($blockers -gt 0) {
    Write-Step "Stopping"
    Write-Fail "$blockers pre-flight blocker(s). Nothing was sent to VCF Operations."
    exit 1
}

# --------------------------------------------------------- Operations token --
Write-Step "Acquiring a VCF Operations token"
$token = $null
try {
    $t = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/token/acquire" -Method POST `
            -Headers @{ Accept = 'application/json' } `
            -Body @{ username = $opsUser; password = $cred['OPERATIONS_ADMIN_PASS']; authSource = 'local' }
    foreach ($f in 'token','accessToken','access_token') {
        if ($t.PSObject.Properties.Name -contains $f -and $t.$f) { $token = $t.$f; break }
    }
} catch {
    Write-Fail "Token acquisition failed: $(Get-RestErrorDetail $_)"
    exit 1
}
if (-not $token) { Write-Fail "Operations returned no token field."; exit 1 }
$script:SecretValues += $token
Write-Ok "token acquired"

# vRealizeOpsToken, not Bearer -- Bearer returns 401 on this build.
$hdr = @{ Authorization = "vRealizeOpsToken $token"; Accept = 'application/json' }
$caUri = "https://$ops/suite-api/api/fleet-management/certificate-management/certificate-authorities"

# ------------------------------------------------------------ current state --
Write-Step "Current CA configuration in VCF Operations"
try {
    $before = Invoke-LabRest -Uri $caUri -Method GET -Headers $hdr
    $j = $before | ConvertTo-Json -Depth 8
    foreach ($line in ($j -split "`r?`n")) { Write-Info $line }
} catch {
    Write-Warn2 "Could not read the current configuration: $(Get-RestErrorDetail $_)"
}

$spec = @{
    microsoftCertificateAuthoritySpec = @{
        serverUrl    = $CaServerUrl
        username     = $enrollUser
        secret       = $enrollPass
        templateName = $TemplateName
    }
}

if (-not $Apply) {
    Write-Step "DRY RUN"
    Write-Info "WOULD PUT $caUri"
    Write-Info "  serverUrl    = $CaServerUrl"
    Write-Info "  username     = $enrollUser"
    Write-Info "  templateName = $TemplateName"
    Write-Info "  secret       = <withheld>"
    Write-Host ""
    Write-Warn2 "Pass -Apply to register."
    exit 0
}

# ------------------------------------------------------------------- register --
Write-Step "Registering"
try {
    Invoke-LabRest -Uri $caUri -Method PUT -Headers $hdr -Body $spec | Out-Null
    Write-Ok "PUT accepted"
} catch {
    Write-Fail "PUT failed: $(Get-RestErrorDetail $_)"
    Write-Info "If the body shape was rejected, the field names are the thing to check:"
    Write-Info "  serverUrl / username / secret / templateName inside"
    Write-Info "  microsoftCertificateAuthoritySpec."
    exit 1
}

Write-Step "Reading the stored configuration back"
$storedOk = $false
try {
    $after = Invoke-LabRest -Uri $caUri -Method GET -Headers $hdr
    $j = ($after | ConvertTo-Json -Depth 8)
    foreach ($line in ($j -split "`r?`n")) { Write-Info $line }
    # Match on the values we sent; a GET that echoes something else means the
    # server stored something other than what was asked for.
    if ($j -match [regex]::Escape($CaServerUrl) -and $j -match [regex]::Escape($TemplateName)) {
        $storedOk = $true
        Write-Ok "serverUrl and templateName are stored as sent"
    } else {
        Write-Fail "The stored configuration does not contain the values just sent."
    }
} catch {
    Write-Fail "Read-back failed: $(Get-RestErrorDetail $_)"
}

Write-Step "Registered, NOT yet proven"
Write-Warn2 "A GET echoes back whatever was stored. It does not prove the CA can issue."
Write-Info "Prove it by replacing ONE certificate -- pick the least critical resource --"
Write-Info "and confirming the replacement is signed by this CA:"
Write-Info "  Manage > Fleet Management > Certificates > select a component >"
Write-Info "  Replace with configured CA certificate"
Write-Info "or via the API:"
Write-Info "  PUT /suite-api/api/fleet-management/certificate-management/certificates/{key}"
Write-Info "      {\"caType\": \"MSCA\"}"
Write-Host ""
if (-not $storedOk) { exit 1 }
exit 0
