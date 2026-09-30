<#
.SYNOPSIS
    Publishes the lab root CA certificate to vCenter's and SDDC Manager's
    trust stores.

.DESCRIPTION
    Both APIs are writable with credentials already held in the standard
    credential file, and neither needs a VIDB token. Domain members need no
    GPO for this -- an Enterprise CA publishes to the Public Key Services
    container and clients pull it automatically. ESXi hosts inherit trust
    from vCenter; they are never seeded individually.

    Dry run by default; pass -Apply to actually write.

    Every write here is followed by a read-back that looks for the root
    certificate's own SHA-1 thumbprint inside the store's response, computed
    locally from -RootPem rather than trusted from whatever the API echoes
    back. A GET that succeeds but does not contain the certificate is a
    FAILURE, not a pass, and a GET that cannot be parsed at all is also a
    FAILURE with its own distinct message -- this script never reports
    success from an inability to look.

.PARAMETER RootPem
    Path to the lab root CA certificate, PEM-encoded (see Task 1 / the CA
    export step: certutil -ca.cert, then certutil -encode).

.PARAMETER Apply
    Actually write to the trust stores. Without -Apply, only reports what
    would be written.

.REQUIRES nothing beyond network access to vCenter and SDDC Manager.

.EXAMPLE
    .\Publish-LabRootTrust.ps1 -RootPem .\lab-root.pem
    Dry run: shows what would be written.

.EXAMPLE
    .\Publish-LabRootTrust.ps1 -RootPem .\lab-root.pem -Apply
    Publishes the root to both trust stores and verifies it landed in both.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RootPem,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

# ------------------------------------------------------------------ helpers --

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

# Parses a single PEM block into an X509Certificate2 so we have a locally
# computed thumbprint to look for, independent of anything the API says.
function Get-PemThumbprint {
    param([string]$PemText)
    try {
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($PemText)
        $cert  = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(,$bytes)
        return $cert.Thumbprint
    } catch {
        return $null
    }
}

# Pulls every "-----BEGIN CERTIFICATE-----...-----END CERTIFICATE-----" block
# out of arbitrary text. Used against a flattened JSON dump of a store's GET
# response so this script does not have to guess field names for a schema
# that was never confirmed against a live API.
function Get-PemBlocksFromText {
    param([string]$Text)
    if (-not $Text) { return @() }
    $normalized = $Text -replace '\\r\\n', "`r`n" -replace '\\n', "`n"
    $ms = [regex]::Matches($normalized, '-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----')
    @($ms | ForEach-Object { $_.Value })
}

# Fail-closed presence check: given a GET response object and the thumbprint
# we are looking for, returns $true only if that thumbprint is found among
# the certificates embedded in the response. A response that can't be turned
# into JSON, or that contains no certificate blocks at all, is reported as
# "could not verify" -- never silently treated as a pass.
function Confirm-ThumbprintPresent {
    param($ResponseObject, [string]$Thumbprint, [string]$StoreLabel)
    $json = $null
    try { $json = $ResponseObject | ConvertTo-Json -Depth 12 -Compress } catch { }
    if (-not $json) {
        Write-Fail "$StoreLabel : GET response could not be serialized for inspection -- cannot verify"
        return $false
    }
    $blocks = Get-PemBlocksFromText $json
    if ($blocks.Count -eq 0) {
        Write-Fail "$StoreLabel : GET response contained no certificate blocks -- cannot verify"
        return $false
    }
    $found = @($blocks | ForEach-Object { Get-PemThumbprint $_ }) -contains $Thumbprint
    if (-not $found) {
        Write-Fail "$StoreLabel : root thumbprint $Thumbprint not found among $($blocks.Count) certificate(s) returned"
        return $false
    }
    Write-Ok "$StoreLabel : root thumbprint $Thumbprint confirmed present"
    $true
}

# --------------------------------------------------------------- load input --

if (-not (Test-Path $RootPem)) {
    Write-Fail "Root certificate not found: $RootPem"
    exit 1
}
$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation

$pem = (Get-Content $RootPem -Raw).Trim()
$rootThumbprint = Get-PemThumbprint $pem
if (-not $rootThumbprint) {
    Write-Fail "Could not parse $RootPem as an X.509 certificate -- refusing to publish an unverifiable file"
    exit 1
}
Write-Info "root certificate thumbprint (local): $rootThumbprint"

$sso = Get-VCFLabCredentialObject -Map $cred -UserKey $null -PassKey 'VCF_SSO_ADMIN_PASS' -DefaultUser 'administrator@vsphere.local'
$sddc = $cfg.Appliances.SddcManager.Ip
$vc   = $cfg.VCenter.Ip

# ------------------------------------------------------------ dry-run guard --

if (-not $Apply) {
    Write-Step "DRY RUN"
    Write-Info "WOULD WRITE SDDC Manager trust store  (POST https://$sddc/v1/sddc-manager/trusted-certificates)"
    Write-Info "WOULD WRITE vCenter trusted-root-chains (POST https://$vc/api/vcenter/certificate-management/vcenter/trusted-root-chains)"
    Write-Info "ESXi inherits from vCenter -- do NOT seed hosts individually"
    Write-Info "Pass -Apply to publish and verify."
    exit 0
}

# --------------------------------------------------- SDDC Manager: token ----

Write-Step "Acquiring SDDC Manager token"
$tok = $null
try {
    $tokResp = Invoke-LabRest -Uri "https://$sddc/v1/tokens" -Method POST `
        -Body @{ username = $sso.UserName; password = $sso.GetNetworkCredential().Password }
    $tok = $tokResp.accessToken
} catch {
    Write-Fail "Could not reach SDDC Manager to acquire a token: $(Get-RestErrorDetail $_)"
    exit 1
}
if (-not $tok) {
    Write-Fail "SDDC Manager responded but returned no accessToken -- cannot authenticate"
    exit 1
}
$sddcHdr = @{ Authorization = "Bearer $tok" }
Write-Ok "token acquired"

# --------------------------------------------------- SDDC Manager: write ----

Write-Step "Writing SDDC Manager trust store"
try {
    Invoke-LabRest -Uri "https://$sddc/v1/sddc-manager/trusted-certificates" -Method POST `
        -Headers $sddcHdr -Body @{ certificate = $pem } | Out-Null
} catch {
    Write-Fail "POST /v1/sddc-manager/trusted-certificates failed: $(Get-RestErrorDetail $_)"
    exit 1
}
Write-Ok "POST accepted"

Write-Step "Verifying SDDC Manager trust store holds the root"
$sddcOk = $false
try {
    $sddcList = Invoke-LabRest -Uri "https://$sddc/v1/sddc-manager/trusted-certificates" -Method GET -Headers $sddcHdr
    $sddcOk = Confirm-ThumbprintPresent -ResponseObject $sddcList -Thumbprint $rootThumbprint -StoreLabel 'SDDC Manager'
} catch {
    Write-Fail "GET /v1/sddc-manager/trusted-certificates failed: $(Get-RestErrorDetail $_) -- cannot verify"
}

# -------------------------------------------------------- vCenter: session --

Write-Step "Acquiring vCenter API session"
$vcSessionId = $null
try {
    $vcSessionId = Invoke-LabRest -Uri "https://$vc/api/session" -Method POST -BasicCredential $sso
} catch {
    Write-Fail "Could not establish a vCenter API session: $(Get-RestErrorDetail $_)"
    Write-Fail "SDDC Manager write $(if ($sddcOk) { 'and verification succeeded' } else { 'or verification did not' }); vCenter write was NOT attempted"
    exit 1
}
if (-not $vcSessionId) {
    Write-Fail "vCenter responded but returned no session id -- cannot authenticate"
    exit 1
}
$vcHdr = @{ 'vmware-api-session-id' = $vcSessionId }
Write-Ok "session acquired"

# -------------------------------------------------------- vCenter: write ----

Write-Step "Writing vCenter trusted-root-chains"
# Body shape: the design doc records this endpoint taking {"spec": {...}}
# (probed against the live lab), while the vSphere Automation API's
# CertificateManagement.Vcenter.TrustedRootChains.CreateSpec model is normally
# posted unwrapped as {"cert_chain": {"cert_chain": [...]}} on the /api/ prefix.
# The two disagree and neither was ever exercised with a real root certificate
# to publish, because none existed until now. Rather than pick one and guess,
# send the SPEC'S recorded shape first -- it is the only one backed by a live
# probe -- and fall back to the unwrapped model shape if the server rejects it.
# Both attempts are reported; nothing is retried silently.
$vcSpecBody = @{ spec = @{ cert_chain = @{ cert_chain = @($pem) } } }
$vcFlatBody = @{ cert_chain = @{ cert_chain = @($pem) } }
$vcUri = "https://$vc/api/vcenter/certificate-management/vcenter/trusted-root-chains"
$vcPosted = $false
try {
    Invoke-LabRest -Uri $vcUri -Method POST -Headers $vcHdr -Body $vcSpecBody | Out-Null
    $vcPosted = $true
    Write-Ok "POST accepted (spec-wrapped body, as recorded in the design doc)"
} catch {
    $firstErr = Get-RestErrorDetail $_
    Write-Warn2 "spec-wrapped body rejected: $firstErr"
    Write-Warn2 "retrying with the unwrapped CreateSpec shape {cert_chain:{cert_chain:[...]}}"
    try {
        Invoke-LabRest -Uri $vcUri -Method POST -Headers $vcHdr -Body $vcFlatBody | Out-Null
        $vcPosted = $true
        Write-Ok "POST accepted (unwrapped body)"
        Write-Warn2 'The design doc''s recorded {"spec": {...}} shape is WRONG for this'
        Write-Warn2 'vCenter build -- correct the doc once this run is confirmed good.'
    } catch {
        Write-Fail "POST trusted-root-chains failed with BOTH body shapes"
        Write-Fail "  spec-wrapped : $firstErr"
        Write-Fail "  unwrapped    : $(Get-RestErrorDetail $_)"
        exit 1
    }
}
if (-not $vcPosted) {
    Write-Fail "vCenter trust write did not complete -- cannot verify"
    exit 1
}

Write-Step "Verifying vCenter trusted-root-chains holds the root"
$vcOk = $false
try {
    $vcList = Invoke-LabRest -Uri "https://$vc/api/vcenter/certificate-management/vcenter/trusted-root-chains" -Method GET -Headers $vcHdr
    $vcOk = Confirm-ThumbprintPresent -ResponseObject $vcList -Thumbprint $rootThumbprint -StoreLabel 'vCenter trusted-root-chains'
} catch {
    Write-Fail "GET /api/vcenter/certificate-management/vcenter/trusted-root-chains failed: $(Get-RestErrorDetail $_) -- cannot verify"
}

# ------------------------------------------------------------------ summary --

Write-Step "VSP trust is unproven"
Write-Warn2 "Whether the Supervisor inherits trust from vCenter is NOT established by this"
Write-Warn2 "script. If it does not, the accepted fallback is to leave VSP on its own"
Write-Warn2 "VSP-issued certificate -- the same fallback already accepted for VIDB and the"
Write-Warn2 "fleet -- rather than assume inheritance and rotate it anyway."

Write-Host ""
if ($sddcOk -and $vcOk) {
    Write-Ok "root certificate confirmed present in both trust stores"
    exit 0
} else {
    Write-Fail "root certificate could not be confirmed in one or both trust stores -- see FAIL lines above"
    exit 1
}
