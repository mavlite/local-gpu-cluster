<#
.SYNOPSIS
    Registers the Microsoft (lab) CA with SDDC Manager as a certificate
    authority.

.DESCRIPTION
    PUTs a microsoftCertificateAuthoritySpec to SDDC Manager. This alone does
    NOT prove the CA works: GET /v1/certificate-authorities echoes back
    whatever was just stored, and passes with a wrong password and an
    unreachable URL. It is configuration-time acceptance, not a functional
    gate, and this script does not pretend otherwise -- it prints the actual
    proof procedure (issuing a real certificate for one NSX manager) rather
    than declaring success from the PUT response or a follow-up GET.

    Dry run by default; pass -Apply to register.

.PARAMETER Apply
    Actually PUT the CA spec to SDDC Manager. Without -Apply, only reports
    what would be sent (the secret is withheld from all output).

.REQUIRES
    AD_CA_ENROLL_USER and AD_CA_ENROLL_PASS in the credential file. As of
    this writing these do not exist yet -- the script fails with a clear,
    named message rather than a null-reference if they are missing.

.EXAMPLE
    .\Register-VcfCA.ps1
    Dry run: shows what would be sent.

.EXAMPLE
    .\Register-VcfCA.ps1 -Apply
    Registers the CA, then prints the issuance procedure that actually
    proves it.
#>
[CmdletBinding()]
param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

# Secret values that must never reach the console. SDDC Manager echoes request
# arguments back in some validation failures, so an error body can contain the
# very password that was just sent. Populated after the credential file loads.
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
$sddc = $cfg.Appliances.SddcManager.Ip

# Register every secret this script handles before the first REST call.
foreach ($k in @('AD_CA_ENROLL_PASS', 'VCF_SSO_ADMIN_PASS', 'AD_BIND_PASS')) {
    if ($cred.ContainsKey($k) -and $cred[$k]) { $script:SecretValues += $cred[$k] }
}

# ---------------------------------------------------- required credentials --
# A missing key here must fail with its own name, not a null-reference three
# lines later when it is interpolated into the request body.
Write-Step "Checking enrolment credentials"
$missing = @()
foreach ($k in @('AD_CA_ENROLL_USER', 'AD_CA_ENROLL_PASS')) {
    if (-not $cred.ContainsKey($k) -or -not $cred[$k]) { $missing += $k }
}
if ($missing.Count -gt 0) {
    Write-Fail ("Missing credential key(s) in {0}: {1}" -f $cfg.CredentialFile, ($missing -join ', '))
    Write-Info "Add them as NAME=value lines before registering the CA."
    exit 1
}
Write-Ok "AD_CA_ENROLL_USER / AD_CA_ENROLL_PASS present"

# ------------------------------------------------------------------ token ---
Write-Step "Acquiring SDDC Manager token"
$sso = Get-VCFLabCredentialObject -Map $cred -UserKey $null -PassKey 'VCF_SSO_ADMIN_PASS' -DefaultUser 'administrator@vsphere.local'
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
$hdr = @{ Authorization = "Bearer $tok" }
Write-Ok "token acquired"

$spec = @{
    microsoftCertificateAuthoritySpec = @{
        serverUrl    = 'https://dns01.knowledgeondemand.net/certsrv'
        username     = $cred['AD_CA_ENROLL_USER']
        secret       = $cred['AD_CA_ENROLL_PASS']
        templateName = 'VMware'
    }
}

# ------------------------------------------------------------- dry-run -----
Write-Step "Registering the Microsoft CA"
if (-not $Apply) {
    Write-Info "WOULD PUT https://$sddc/v1/certificate-authorities (secret withheld)"
    Write-Info "  serverUrl    = $($spec.microsoftCertificateAuthoritySpec.serverUrl)"
    Write-Info "  username     = $($spec.microsoftCertificateAuthoritySpec.username)"
    Write-Info "  templateName = $($spec.microsoftCertificateAuthoritySpec.templateName)"
    Write-Info "Pass -Apply to register."
    exit 0
}

try {
    Invoke-LabRest -Uri "https://$sddc/v1/certificate-authorities" -Method PUT -Headers $hdr -Body $spec | Out-Null
} catch {
    Write-Fail "PUT /v1/certificate-authorities failed: $(Get-RestErrorDetail $_)"
    exit 1
}
Write-Ok "SDDC Manager accepted the CA registration request"

# -------------------------------------------------- this is not the gate ---
Write-Step "This is NOT proof the CA works"
Write-Warn2 "GET /v1/certificate-authorities echoes back whatever was just stored."
Write-Warn2 "It passes with a wrong password and an unreachable URL -- it is not a gate."
Write-Host ""
Write-Info "Proof requires issuing a real certificate against the least critical resource:"
Write-Info "  1. Generate a CSR for ONE NSX manager (not every resource)."
Write-Info "  2. Have SDDC Manager fulfil it from this CA registration."
Write-Info "  3. GET /v1/domains/{id}/resource-certificates and confirm that ONE"
Write-Info "     resource now shows an issuer of 'knowledgeondemand-LabRoot-CA'"
Write-Info "     while the other resources still show 'CN=CA'."
Write-Info "A successful issuance proves the URL, the credentials, the enrolment"
Write-Info "right, and the template name all at once -- the four things the"
Write-Info "configuration-time check above cannot prove."
exit 0
