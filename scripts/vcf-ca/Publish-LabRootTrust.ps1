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

# Secret values that must never reach the console. VCF_SSO_ADMIN_PASS is sent
# as a request credential to SDDC Manager (token acquisition) and to vCenter
# (session acquisition), and SDDC Manager echoes request arguments back in
# some validation failures -- so an error body can contain the very password
# that was just sent. Populated after the credential file loads.
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

# ------------------------------------------------------------------ helpers --

function Get-RestErrorDetail {
    param($ErrRecord)
    $msg   = $ErrRecord.Exception.Message
    $inner = $ErrRecord.Exception.InnerException
    while ($inner) {
        $msg  += " -- $($inner.Message)"
        $inner = $inner.InnerException
    }
    # Under Set-StrictMode -Version Latest, reading a property an object does
    # not carry THROWS. Not every exception reaching here is a WebException --
    # a DNS failure, a TLS failure or a PowerShell-level error has no Response
    # at all -- and this function runs inside the caller's catch block, so the
    # throw replaces the real error with "The property 'Response' cannot be
    # found on this object". That is exactly what happened on the first real
    # vCenter run: the actual cause was never printed.
    $resp = $null
    if ($ErrRecord.Exception.PSObject.Properties.Name -contains 'Response') {
        $resp = $ErrRecord.Exception.Response
    }
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
    # @() is load-bearing: Get-PemBlocksFromText returns $null when it matches
    # nothing, and under Set-StrictMode -Version Latest $null.Count THROWS --
    # from inside this verification helper, so a store that simply held no
    # certificates reported "The property 'Count' cannot be found on this
    # object" instead of the real finding. Observed on the first real vCenter
    # verification.
    $blocks = @(Get-PemBlocksFromText $json)
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

<#
    vCenter's trusted-root-chains LIST endpoint does NOT return certificates.
    It returns chain identifiers only:

        [ { "chain": "BA7EEC55..." }, { "chain": "4FF484BF..." } ]

    Each chain must then be fetched individually to get its PEM. Scanning the
    LIST response for certificate blocks therefore finds none and reports
    "cannot verify" against a store that actually holds the root -- which is
    what happened on the first real run, after the write had already succeeded.

    Fail-closed throughout: an empty list, a chain whose GET fails, or a chain
    with no parseable certificate all count as "could not verify", never as a
    pass. Verified against the live API 2026-10-01.
#>
function Confirm-VcenterTrustedRoot {
    param([string]$VcHost, [hashtable]$Headers, [string]$Thumbprint)
    $base = "https://$VcHost/api/vcenter/certificate-management/vcenter/trusted-root-chains"

    # The store is eventually consistent after a write: immediately following
    # the POST the LIST returned $null on the first real run, and @($null) has
    # a Count of 1 -- so the loop saw one null entry, read no certificate, and
    # reported "1 chain(s) listed but no certificate could be read" against a
    # store that in fact already held the root. Filter nulls so a null response
    # is never counted as a chain, and retry a few times before concluding.
    $list = @()
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $raw = Invoke-LabRest -Uri $base -Method GET -Headers $Headers
            $list = @($raw | Where-Object { $null -ne $_ })
        } catch {
            Write-Fail "vCenter trusted-root-chains : LIST failed: $(Get-RestErrorDetail $_) -- cannot verify"
            return $false
        }
        if ($list.Count -gt 0) { break }
        if ($attempt -lt 5) {
            Write-Info "LIST returned nothing yet (attempt $attempt/5); the store settles after a write"
            Start-Sleep -Seconds 4
        }
    }
    if ($list.Count -eq 0) {
        Write-Fail "vCenter trusted-root-chains : LIST returned no chains after 5 attempts -- cannot verify"
        return $false
    }

    $seen = @()
    foreach ($item in $list) {
        $id = $null
        if ($null -ne $item -and $item.PSObject.Properties.Name -contains 'chain') { $id = [string]$item.chain }
        elseif ($item) { $id = [string]$item }
        if (-not $id) { continue }
        try {
            $one = Invoke-LabRest -Uri "$base/$id" -Method GET -Headers $Headers
            $json = $one | ConvertTo-Json -Depth 12 -Compress
            foreach ($b in @(Get-PemBlocksFromText $json)) {
                $t = Get-PemThumbprint $b
                if ($t) { $seen += $t }
            }
        } catch {
            Write-Warn2 "chain $id could not be read: $(Get-RestErrorDetail $_)"
        }
    }

    if ($seen.Count -eq 0) {
        Write-Fail "vCenter trusted-root-chains : $($list.Count) chain(s) listed but no certificate could be read -- cannot verify"
        return $false
    }
    if ($seen -contains $Thumbprint) {
        Write-Ok "vCenter trusted-root-chains : root thumbprint $Thumbprint confirmed present (of $($seen.Count) certificate(s) across $($list.Count) chain(s))"
        return $true
    }
    Write-Fail "vCenter trusted-root-chains : root thumbprint $Thumbprint NOT among the $($seen.Count) certificate(s) found"
    return $false
}

# --------------------------------------------------------------- load input --

if (-not (Test-Path $RootPem)) {
    Write-Fail "Root certificate not found: $RootPem"
    exit 1
}
$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation

# Register every secret this script handles before the first REST call.
foreach ($k in @('VCF_SSO_ADMIN_PASS')) {
    if ($cred.ContainsKey($k) -and $cred[$k]) { $script:SecretValues += $cred[$k] }
}

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
    Write-Ok "POST accepted"
} catch {
    $sddcErr = Get-RestErrorDetail $_
    # A certificate already in the trust store is the DESIRED state, not a
    # failure. Treating the 409 as fatal made this script impossible to re-run
    # after ANY partial success: the SDDC Manager half would conflict and exit
    # before the vCenter half was ever attempted, so a run that failed at
    # vCenter could never be completed. Observed on the first real run:
    #   409 {"errorCode":"CERTIFICATE_CHAIN_EXISTS_IN_TRUST_STORE", ...}
    # The verification step below is what confirms the root is actually there,
    # and it does not care how it arrived.
    if ($sddcErr -match 'CERTIFICATE_CHAIN_EXISTS_IN_TRUST_STORE' -or $sddcErr -match '\(409\)') {
        Write-Ok "already present in the SDDC Manager trust store -- nothing to write"
    } else {
        Write-Fail "POST /v1/sddc-manager/trusted-certificates failed: $sddcErr"
        exit 1
    }
}

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
# Body shape: SETTLED BY EXECUTION 2026-10-01 against vCenter 9.x.
#
# The design doc recorded this endpoint taking {"spec": {...}}. That is WRONG
# for this build -- it answers
#   400 UNEXPECTED_INPUT "Found unexpected fields [spec] in structure
#   com.vmware.vcenter.certificate_management.vcenter.trusted_root_chains.create_spec"
# The unwrapped CreateSpec shape {"cert_chain": {"cert_chain": [...]}} is
# accepted. So the unwrapped shape is attempted FIRST and the spec-wrapped
# shape is kept only as a fallback, for an older build that might want it.
#
# Keeping the fallback rather than deleting it: this cost nothing and it is
# what turned an outright failure into a successful write on the first real
# run, when the documented shape turned out to be wrong.
$vcFlatBody = @{ cert_chain = @{ cert_chain = @($pem) } }
$vcSpecBody = @{ spec = @{ cert_chain = @{ cert_chain = @($pem) } } }
$vcUri = "https://$vc/api/vcenter/certificate-management/vcenter/trusted-root-chains"
$vcPosted = $false
try {
    # Unwrapped CreateSpec first: proven against this build on 2026-10-01. The
    # spec-wrapped shape the design doc recorded answers 400 UNEXPECTED_INPUT
    # here, so trying it first guaranteed a failed request on every run.
    Invoke-LabRest -Uri $vcUri -Method POST -Headers $vcHdr -Body $vcFlatBody | Out-Null
    $vcPosted = $true
    Write-Ok "POST accepted (unwrapped CreateSpec body)"
} catch {
    $firstErr = Get-RestErrorDetail $_
    # Already present is the desired state, as it is for SDDC Manager.
    if ($firstErr -match 'ALREADY_EXISTS|already exists|\(409\)') {
        $vcPosted = $true
        Write-Ok "already present in vCenter trusted-root-chains -- nothing to write"
    } else {
        Write-Warn2 "unwrapped body rejected: $firstErr"
        Write-Warn2 "retrying with the spec-wrapped shape {spec:{cert_chain:{...}}}"
        try {
            Invoke-LabRest -Uri $vcUri -Method POST -Headers $vcHdr -Body $vcSpecBody | Out-Null
            $vcPosted = $true
            Write-Ok "POST accepted (spec-wrapped body)"
            Write-Warn2 'This build wants the spec-wrapped shape -- the opposite of what was'
            Write-Warn2 'observed on 2026-10-01. Record which build does which.'
        } catch {
            Write-Fail "POST trusted-root-chains failed with BOTH body shapes"
            Write-Fail "  unwrapped    : $firstErr"
            Write-Fail "  spec-wrapped : $(Get-RestErrorDetail $_)"
            exit 1
        }
    }
}
if (-not $vcPosted) {
    Write-Fail "vCenter trust write did not complete -- cannot verify"
    exit 1
}

Write-Step "Verifying vCenter trusted-root-chains holds the root"
$vcOk = $false
try {
    $vcOk = Confirm-VcenterTrustedRoot -VcHost $vc -Headers $vcHdr -Thumbprint $rootThumbprint
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
