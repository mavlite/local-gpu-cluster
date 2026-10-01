<#
.SYNOPSIS
    Replaces one VCF certificate with one signed by the registered lab CA, and
    verifies the replacement actually happened.

.DESCRIPTION
    Registering a CA only proves VCF Operations could reach and read it.
    Nothing proves it can obtain a certificate until one is issued, so this
    script exists to make that claim checkable:

        PUT /suite-api/api/fleet-management/certificate-management/certificates/{key}
        body { "caType": "MSCA" }

    Note the enum differs from the CA-registration endpoint, which wants
    certificateAuthorityType "MICROSOFT". They are not the same vocabulary, so
    this tries the documented value and reports what the server said rather
    than assuming.

    After the request it polls the inventory until issuedBy names the lab root,
    because a 200 here means "accepted", not "replaced" -- an earlier attempt
    against the Cloud Proxy was accepted and never submitted a CSR at all (its
    certificate belongs to its own self-signed tunnel PKI, not to the fleet
    PKI). A silent non-replacement is the failure mode to guard against.

    Targets are ranked by blast radius, lowest first, and -ListTargets prints
    that ranking. The ordering is deliberate: standalone services, then the
    platform runtime, then identity, then Operations itself, then the
    management plane (NSX, vCenter, SDDC Manager) and ESX last. SDDC Manager
    should be the final rotation in any sequence.

    Only certificates the fleet reports as CUSTOMER_MANAGED_* with chain role
    LEAF are offered; monitor-only and non-leaf entries are not replaceable.

    Dry run by default. -Apply performs the replacement.

.PARAMETER CommonName
    DNS name of the certificate to replace, as -ListTargets prints it. Resolved
    to a resource key at run time, so no key is hard-coded.

.PARAMETER ResourceKey
    The certificate resource key, if you would rather name it directly.

.PARAMETER ListTargets
    Print the ranked replaceable certificates and exit. Changes nothing.

.PARAMETER TimeoutMinutes
    How long to wait for the replacement to show up in the inventory.

.PARAMETER Apply
    Perform the replacement.

.REQUIRES
    OPERATIONS_ADMIN_USER / OPERATIONS_ADMIN_PASS in the credential file, and a
    CA already registered (see Register-VcfCA.ps1).

.EXAMPLE
    .\Update-VcfCertificate.ps1 -ListTargets

.EXAMPLE
    .\Update-VcfCertificate.ps1 -CommonName licsrv.lab.knowledgeondemand.net
    Dry run against the lowest-blast-radius target.

.EXAMPLE
    .\Update-VcfCertificate.ps1 -CommonName licsrv.lab.knowledgeondemand.net -Apply
#>
[CmdletBinding()]
param(
    [string]$CommonName,
    [string]$ResourceKey,
    [switch]$ListTargets,
    [int]$TimeoutMinutes = 15,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

# Blast-radius ranking. Lower runs first. SDDC Manager and ESX last by design.
$script:Rank = @{
    'LICENSE_SERVER'       = 1
    'CLOUD_PROXY'          = 2
    'VCF_SERVICES_RUNTIME' = 3
    'IDENTITY_BROKER'      = 4
    'VCF_OPERATIONS'       = 5
    'NSXT_MANAGER'         = 6
    'VCENTER'              = 7
    'SDDC_MANAGER'         = 8
    'ESX'                  = 9
}

$script:SecretValues = @()
function Protect-Secret {
    param([string]$Text)
    if (-not $Text) { return $Text }
    foreach ($s in $script:SecretValues) { if ($s) { $Text = $Text.Replace($s, '<redacted>') } }
    $Text
}

# The bulk query omits fields the single-certificate GET includes, and dot
# access on a missing property throws under StrictMode -- including inside
# catch blocks, where the throw replaces the error being reported.
function Get-Field {
    param($Object, [string]$Name, $Default = '')
    if ($null -eq $Object) { return $Default }
    if (@($Object.PSObject.Properties.Name) -notcontains $Name) { return $Default }
    if ($null -eq $Object.$Name) { return $Default }
    $Object.$Name
}

function Get-RestErrorDetail {
    param($ErrRecord)
    $msg = $ErrRecord.Exception.Message.Split([Environment]::NewLine)[0]
    $resp = Get-Field $ErrRecord.Exception 'Response' $null
    if ($resp) {
        try {
            $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd()
            if ($body) { return (Protect-Secret ((("$msg $body") -replace '\s+', ' ').Trim())) }
        } catch { }
    }
    Protect-Secret $msg
}

$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation
$ops = $cfg.Appliances.Operations.Ip

foreach ($k in @('OPERATIONS_ADMIN_PASS')) {
    if ($cred.ContainsKey($k) -and $cred[$k]) { $script:SecretValues += $cred[$k] }
}
foreach ($k in @('OPERATIONS_ADMIN_USER', 'OPERATIONS_ADMIN_PASS')) {
    if (-not $cred.ContainsKey($k) -or -not $cred[$k]) {
        Write-Fail "Missing credential key $k in $($cfg.CredentialFile)"
        exit 1
    }
}

# ------------------------------------------------------------------ token ---
Write-Step "Acquiring VCF Operations token"
try {
    $tokResp = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/token/acquire" -Method POST `
        -Headers @{ Accept = 'application/json' } `
        -Body @{ username = $cred['OPERATIONS_ADMIN_USER']; password = $cred['OPERATIONS_ADMIN_PASS'] }
} catch {
    Write-Fail "Could not acquire a token: $(Get-RestErrorDetail $_)"
    exit 1
}
$tok = Get-Field $tokResp 'token' ''
if (-not $tok) { Write-Fail "VCF Operations returned no token"; exit 1 }
$hdr  = @{ Authorization = "vRealizeOpsToken $tok"; Accept = 'application/json' }
$base = "https://$ops/suite-api/api/fleet-management/certificate-management"
Write-Ok "token acquired"

# ------------------------------------------------------------- inventory ---
function Get-Inventory {
    $q = Invoke-LabRest -Uri "$base/certificates/query" -Method POST -Headers $hdr -Body @{} -TimeoutSec 120
    @(Get-Field $q 'vcfCertificateModels' @())
}
function Get-Replaceable {
    param($All)
    @($All | Where-Object {
        (Get-Field $_ 'category') -eq 'TLS_CERT' -and
        (Get-Field (Get-Field $_ 'certificateMetadata' $null) 'managementLevel') -like 'CUSTOMER_MANAGED*' -and
        (Get-Field (Get-Field $_ 'certificateMetadata' $null) 'certificateChainRole') -eq 'LEAF'
    })
}
function Get-PrimaryName {
    param($Cert)
    $dns = @(Get-Field (Get-Field $Cert 'subjectAlternativeNames' $null) 'dns' @())
    if ($dns.Count -gt 0) { return [string]$dns[0] }
    [string](Get-Field $Cert 'issuedToCommonName' (Get-Field $Cert 'issuedTo'))
}
function Get-RankOf {
    param($Cert)
    $a = [string](Get-Field $Cert 'appliance')
    if ($script:Rank.ContainsKey($a)) { return $script:Rank[$a] }
    99
}

Write-Step "Reading the fleet certificate inventory"
try { $all = Get-Inventory } catch { Write-Fail "query failed: $(Get-RestErrorDetail $_)"; exit 1 }
Write-Info "$($all.Count) certificates in the fleet inventory"
$signedByUs = @($all | Where-Object { (Get-Field $_ 'issuedBy') -match 'LabRoot-CA' })
Write-Info "already signed by the lab CA: $($signedByUs.Count)"

$replaceable = Get-Replaceable -All $all | Sort-Object @{ e = { Get-RankOf $_ } }, @{ e = { Get-PrimaryName $_ } }

if ($ListTargets) {
    Write-Step "Replaceable certificates, lowest blast radius first"
    foreach ($c in $replaceable) {
        $ip = @(Get-Field (Get-Field $c 'subjectAlternativeNames' $null) 'ip' @())
        $ours = if ((Get-Field $c 'issuedBy') -match 'LabRoot-CA') { ' [lab CA]' } else { '' }
        Write-Info ("{0,-22} {1,-42} days={2,-5} key={3}{4}" -f `
            (Get-Field $c 'appliance'), (Get-PrimaryName $c), (Get-Field $c 'daysToExpire'), (Get-Field $c 'certificateResourceKey'), $ours)
        if ($ip.Count -gt 0) { Write-Info ("{0,-22} IP SAN {1}" -f '', ($ip -join ',')) }
    }
    Write-Warn2 "Rotate SDDC Manager LAST."
    exit 0
}

# ---------------------------------------------------------- pick a target ---
if (-not $CommonName -and -not $ResourceKey) {
    Write-Fail "Name a target with -CommonName or -ResourceKey (see -ListTargets)."
    exit 1
}
$target = $null
if ($ResourceKey) {
    $target = @($replaceable | Where-Object { (Get-Field $_ 'certificateResourceKey') -eq $ResourceKey })[0]
    if (-not $target) {
        Write-Fail "No replaceable certificate with key '$ResourceKey'."
        Write-Info  "It may exist but be monitor-only or non-leaf. Check -ListTargets."
        exit 1
    }
} else {
    $hits = @($replaceable | Where-Object { (Get-PrimaryName $_) -eq $CommonName })
    if ($hits.Count -eq 0) {
        Write-Fail "No replaceable certificate named '$CommonName'. See -ListTargets."
        exit 1
    }
    if ($hits.Count -gt 1) {
        Write-Fail "'$CommonName' matches $($hits.Count) certificates; use -ResourceKey."
        exit 1
    }
    $target = $hits[0]
}
$key = [string](Get-Field $target 'certificateResourceKey')

Write-Step "Target"
Write-Info "appliance    $(Get-Field $target 'displayApplianceType') ($(Get-Field $target 'appliance'))"
Write-Info "name         $(Get-PrimaryName $target)"
Write-Info "key          $key"
Write-Info "issuedTo     $(Get-Field $target 'issuedTo')"
Write-Info "issuedBy     $(Get-Field $target 'issuedBy')"
Write-Info "status       $(Get-Field $target 'displayStatus')  daysToExpire=$(Get-Field $target 'daysToExpire')"
$tip = @(Get-Field (Get-Field $target 'subjectAlternativeNames' $null) 'ip' @())
if ($tip.Count -gt 0) {
    Write-Info "SAN ip       $($tip -join ',')"
    Write-Warn2 "This certificate carries an IP SAN. The AD CS template must copy it"
    Write-Warn2 "into the issued certificate; if it does not, expect a validation failure."
}
if ((Get-Field $target 'issuedBy') -match 'LabRoot-CA') {
    Write-Ok "already signed by the lab CA -- nothing to do"
    exit 0
}
if ((Get-Field $target 'appliance') -eq 'SDDC_MANAGER') {
    Write-Warn2 "SDDC Manager should be the LAST certificate rotated, not an early one."
}

if (-not $Apply) {
    Write-Info "DRY RUN -- pass -Apply to request replacement. Nothing was changed."
    exit 0
}

# --------------------------------------------------------------- replace ----
Write-Step "Requesting replacement from the registered CA"
$accepted = ''
foreach ($shape in @(
    @{ Name = "caType=MSCA";      Body = @{ caType = 'MSCA' } },
    @{ Name = "caType=MICROSOFT"; Body = @{ caType = 'MICROSOFT' } }
)) {
    try {
        $r = Invoke-LabRest -Uri "$base/certificates/$key" -Method PUT -Headers $hdr -Body $shape.Body -TimeoutSec 180
        Write-Ok "accepted with $($shape.Name)"
        $accepted = $shape.Name
        if ($r) {
            $j = ($r | ConvertTo-Json -Depth 5 -Compress)
            if ($j.Length -gt 400) { $j = $j.Substring(0, 400) + '...' }
            Write-Info (Protect-Secret $j)
        }
        break
    } catch {
        Write-Warn2 "rejected ($($shape.Name)): $(Get-RestErrorDetail $_)"
    }
}
if (-not $accepted) {
    Write-Fail "No request shape was accepted -- nothing was replaced."
    exit 1
}

# ---------------------------------------------------------------- verify ----
# "Accepted" is not "replaced". Poll until issuedBy names the lab root.
Write-Step "Waiting for the new certificate to appear (up to $TimeoutMinutes min)"
$started  = Get-Date
$deadline = $started.AddMinutes($TimeoutMinutes)
$replaced = $false
$lastSeen = ''
$i = 0
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 20
    $i++
    try { $now = @(Get-Inventory | Where-Object { (Get-Field $_ 'certificateResourceKey') -eq $key })[0] }
    catch { Write-Warn2 "inventory read failed: $(Get-RestErrorDetail $_)"; continue }
    if (-not $now) {
        Write-Warn2 "key $key is no longer listed -- the certificate may have been re-keyed."
        break
    }
    $issuer = [string](Get-Field $now 'issuedBy')
    $elapsed = [int]((Get-Date) - $started).TotalSeconds
    if ($issuer -match 'LabRoot-CA') {
        Write-Ok "replaced after ${elapsed}s -- issued by the lab CA"
        $replaced = $true
        break
    }
    if ($issuer -ne $lastSeen -or $i % 3 -eq 0) {
        Write-Info ("...{0,4}s status={1} issuedBy={2}" -f $elapsed, (Get-Field $now 'displayStatus'), (($issuer -split ',')[0]))
        $lastSeen = $issuer
    }
}

Write-Step "After"
try { $after = @(Get-Inventory | Where-Object { (Get-Field $_ 'certificateResourceKey') -eq $key })[0] } catch { $after = $null }
if ($after) {
    Write-Info "issuedTo     $(Get-Field $after 'issuedTo')"
    Write-Info "issuedBy     $(Get-Field $after 'issuedBy')"
    Write-Info "status       $(Get-Field $after 'displayStatus')  daysToExpire=$(Get-Field $after 'daysToExpire')"
}
if (-not $replaced) {
    Write-Fail ("NOT replaced within {0} minutes (waited {1}s)." -f $TimeoutMinutes, [int]((Get-Date) - $started).TotalSeconds)
    Write-Info  "The request was accepted, so this is either a still-running task or a"
    Write-Info  "silent non-submission. Check the CA's own request table on the CA host --"
    Write-Info  "if no new row appeared there, Operations never asked for a certificate:"
    Write-Info  "  certutil -view -restrict ""RequestID>0"" -out ""RequestID,RequesterName,CommonName,Disposition"""
    exit 1
}
Write-Ok "certificate for $(Get-PrimaryName $target) is now signed by the lab CA"
exit 0
