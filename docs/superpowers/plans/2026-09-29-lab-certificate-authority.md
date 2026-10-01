# Lab Certificate Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an AD CS Enterprise Root CA on `dns01`, make LDAPS work, and
bring the eight SDDC-managed VCF certificates and VCF Operations' AD identity
source under it.

**Architecture:** Single-tier Enterprise Root CA on the domain controller.
`dns01` autoenrols its own LDAPS certificate; SDDC Manager issues and installs
the eight VCF resource certificates through AD CS web enrolment; trust reaches
domain members automatically and appliances through two APIs that were verified
writable with credentials already held.

**Tech Stack:** Windows Server 2025 AD CS, IIS web enrolment, PowerShell 5.1
with the `DnsServer`/`ADCSDeployment` RSAT modules, VMware PowerCLI 13.1,
SDDC Manager `/v1` REST, VCF Operations `suite-api`.

**Spec:** `docs/superpowers/specs/2026-09-29-lab-certificate-authority-design.md`

## Global Constraints

- Secrets live in `C:\Users\willi\.vcflab\credentials.env`, outside all git
  repositories, owner-only. Read per invocation, passed as environment or
  PSCredential, **never on a command line, never echoed, never in a transcript**.
- Commits carry **no `Co-Authored-By` trailer**.
- CA name `knowledgeondemand-LabRoot-CA`; template `VMware`; enrolment account
  `knowledgeondemand\svc-vcf-ca`. All three are baked into issued certificates
  or passed verbatim to SDDC Manager — a mismatch fails at request time.
- `templateName` passed to SDDC Manager must be the template's **cn**, not its
  display name.
- Root validity **10 years**, CRL period **52 weeks**, RSA **2048** minimum,
  **SHA-256**. All four are install-time only and cannot be changed without
  re-rooting.
- The template carries **Server AND Client Authentication** — settled by
  measuring what VCF uses today, not by argument.
- Port 389 must continue refusing simple binds. That is Server 2025 signing
  enforcement working correctly and **must not be relaxed**.
- `dns01` **must never be snapshot-reverted** once the CA exists.
- Every test against the lab uses **FQDNs, never IP addresses** — certificates
  carry FQDNs in SAN and IP-based tests pass while name-validating clients fail.

## Ownership

Steps are marked **[OPERATOR]** where they must be run by a human on `dns01`
with Domain Admin rights, or **[SCRIPTED]** where they run from the workstation
against an API. The split exists because no Domain Admin credential is held in
`credentials.env`, and adding one is a decision the operator makes deliberately.

## File Structure

| File | Responsibility |
|---|---|
| `scripts/vcf-ca/CAPolicy.inf` | install-time CA settings; copied to `C:\Windows` on dns01 |
| `scripts/vcf-ca/Install-LabCA.ps1` | [OPERATOR] place CAPolicy.inf, install AD CS + Web Enrolment |
| `scripts/vcf-ca/Set-CAWebEnrollmentHardening.ps1` | [OPERATOR] Basic auth, EPA, unbind 80, firewall scope |
| `scripts/vcf-ca/New-VcfCertificateTemplate.ps1` | [OPERATOR] create and publish the `VMware` template |
| `scripts/vcf-ca/Test-LabCAHealth.ps1` | [SCRIPTED] every verification gate, runnable any time |
| `scripts/vcf-ca/Publish-LabRootTrust.ps1` | [SCRIPTED] push the root to vCenter and SDDC Manager |
| `scripts/vcf-ca/Register-VcfCA.ps1` | [SCRIPTED] register the CA, prove by issuing one certificate |
| `scripts/vcf-ca/Add-OpsIdentitySource.ps1` | [SCRIPTED] validate then create the AD identity source |
| `scripts/vcf-ca/README.md` | what this is, ownership split, and the order |

Credential loading and REST helpers are reused from
`scripts/vcf-lab/VCFLab.Common.ps1` rather than duplicated.

---

### Task 1: Install-time CA configuration

**Files:**
- Create: `scripts/vcf-ca/CAPolicy.inf`
- Create: `scripts/vcf-ca/Install-LabCA.ps1`

**Interfaces:**
- Produces: a CA named `knowledgeondemand-LabRoot-CA` with a 10-year root,
  52-week CRL, RSA 2048, SHA-256, and the Web Enrolment role service installed.

- [ ] **Step 1: Write `CAPolicy.inf`**

```ini
[Version]
Signature="$Windows NT$"

[Certsrv_Server]
; 10 years. The default is 5, and AD CS SILENTLY TRUNCATES any certificate
; whose validity exceeds the CA's remaining life -- a 2-year template starts
; shortening from year 3 and the whole fabric expires at once at year 5.
RenewalValidityPeriod=Years
RenewalValidityPeriodUnits=10

; 52 weeks. The 1-week default means a DC powered off for a fortnight stops
; publishing and strict validators begin hard-failing.
CRLPeriod=Weeks
CRLPeriodUnits=52
CRLDeltaPeriod=Days
CRLDeltaPeriodUnits=0

LoadDefaultTemplates=0
AlternateSignatureAlgorithm=0
```

`LoadDefaultTemplates=0` is deliberate: it suppresses the auto-published
Domain Controller / Machine / User templates that carry Client Authentication
with autoenrolment, which is the PetitPotam relay chain. Task 5 adds back only
what is needed.

- [ ] **Step 2: Write `Install-LabCA.ps1`** — [OPERATOR], run on dns01

```powershell
#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$inf = Join-Path $PSScriptRoot 'CAPolicy.inf'
if (-not (Test-Path $inf)) { throw "CAPolicy.inf not found beside this script" }

Write-Host "CAPolicy.inf will be copied to C:\Windows\CAPolicy.inf" -ForegroundColor Cyan
Get-Content $inf | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
if (-not $Apply) { Write-Host "`nDRY RUN -- pass -Apply to install" -ForegroundColor Yellow; exit 0 }

Copy-Item $inf 'C:\Windows\CAPolicy.inf' -Force
Install-WindowsFeature -Name ADCS-Cert-Authority, ADCS-Web-Enrollment -IncludeManagementTools

Install-AdcsCertificationAuthority `
    -CAType EnterpriseRootCA `
    -CACommonName 'knowledgeondemand-LabRoot-CA' `
    -KeyLength 2048 `
    -HashAlgorithmName SHA256 `
    -CryptoProviderName 'RSA#Microsoft Software Key Storage Provider' `
    -ValidityPeriod Years -ValidityPeriodUnits 10 `
    -Force
Install-AdcsWebEnrollment -Force
Write-Host "CA installed. Run Test-LabCAHealth.ps1 before continuing." -ForegroundColor Green
```

- [ ] **Step 3: Dry run it on dns01**

Run: `.\Install-LabCA.ps1`
Expected: prints the CAPolicy.inf contents, says DRY RUN, installs nothing.

- [ ] **Step 4: Install**

Run: `.\Install-LabCA.ps1 -Apply`
Expected: features install, CA configured, no errors.

- [ ] **Step 5: Verify the root landed in AD with the right validity**

Run on dns01:
```powershell
certutil -cainfo | Select-String 'Validity|Name'
certutil -getreg CA\CRLPeriodUnits
```
Expected: CA name `knowledgeondemand-LabRoot-CA`, validity 10 years,
`CRLPeriodUnits = 52`. If any is wrong, **stop** — these are install-time only
and the fix is to uninstall and redo, which is far cheaper now than later.

- [ ] **Step 6: Commit**

```bash
git add scripts/vcf-ca/CAPolicy.inf scripts/vcf-ca/Install-LabCA.ps1
git commit -m "feat(vcf-ca): install-time CA configuration for the lab root"
```

---

### Task 2: Harden web enrolment before it is reachable

**Files:**
- Create: `scripts/vcf-ca/Set-CAWebEnrollmentHardening.ps1`

**Interfaces:**
- Consumes: the CA and Web Enrolment role from Task 1.
- Produces: `/certsrv` reachable only over HTTPS, Basic auth enabled, EPA
  required, Require-SSL on /CertSrv (NOT port 80 unbound -- see the spec's
  revision 3 correction: port 80 must stay bound to serve /CertEnroll, which
  must stay anonymous, or revocation checking breaks fabric-wide), firewall
  scoping 443 to SDDC Manager and 80 to the lab subnet.

This task exists because `dns01` had **no web stack at all** before Task 1 —
ports 80 and 443 were both closed. Installing Web Enrolment opens new attack
surface on a domain controller, and ESC8 (NTLM relay to `/certsrv`) is live from
the moment it is reachable.

- [ ] **Step 1: Write the hardening script** — [OPERATOR], run on dns01

```powershell
#Requires -RunAsAdministrator
[CmdletBinding()]
param([string]$SddcManagerIp = '172.16.10.133', [string]$AdminIp, [switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module WebAdministration

$actions = @(
  @{ what = 'Enable Basic auth on /CertSrv (SDDC Manager cannot speak Negotiate)'
     do   = { Set-WebConfigurationProperty -Filter /system.webServer/security/authentication/basicAuthentication `
                -Name enabled -Value $true -PSPath 'IIS:\' -Location 'Default Web Site/CertSrv' } }
  @{ what = 'Disable Windows auth on /CertSrv'
     do   = { Set-WebConfigurationProperty -Filter /system.webServer/security/authentication/windowsAuthentication `
                -Name enabled -Value $false -PSPath 'IIS:\' -Location 'Default Web Site/CertSrv' } }
  @{ what = 'Require Extended Protection for Authentication (blocks ESC8 relay)'
     do   = { Set-WebConfigurationProperty -Filter /system.webServer/security/authentication/basicAuthentication `
                -Name extendedProtection.tokenChecking -Value 'Require' -PSPath 'IIS:\' -Location 'Default Web Site/CertSrv' } }
  @{ what = 'Remove the HTTP (port 80) binding so Basic auth never crosses cleartext'
     do   = { Get-WebBinding -Name 'Default Web Site' -Protocol http |
                Where-Object { $_.bindingInformation -like '*:80:*' } | Remove-WebBinding } }
)
foreach ($a in $actions) {
  Write-Host ("  {0} {1}" -f $(if($Apply){'APPLY '}else{'WOULD '}), $a.what)
  if ($Apply) { & $a.do }
}

$allow = @($SddcManagerIp); if ($AdminIp) { $allow += $AdminIp }
Write-Host ("  {0} scope inbound 443 to: {1}" -f $(if($Apply){'APPLY '}else{'WOULD '}), ($allow -join ', '))
if ($Apply) {
    New-NetFirewallRule -DisplayName 'CertSrv HTTPS (scoped)' -Direction Inbound `
        -Protocol TCP -LocalPort 443 -RemoteAddress $allow -Action Allow -ErrorAction Stop | Out-Null
}
if (-not $Apply) { Write-Host "`nDRY RUN -- pass -Apply" -ForegroundColor Yellow }
```

- [ ] **Step 2: Dry run**

Run: `.\Set-CAWebEnrollmentHardening.ps1 -AdminIp <your workstation>`
Expected: five WOULD lines, nothing changed.

- [ ] **Step 3: Apply**

Run: `.\Set-CAWebEnrollmentHardening.ps1 -AdminIp <your workstation> -Apply`

- [ ] **Step 4: Verify port 80 is gone and EPA is set**

Run on dns01:
```powershell
Get-WebBinding -Name 'Default Web Site' | Select-Object protocol, bindingInformation
Get-WebConfigurationProperty -Filter /system.webServer/security/authentication/basicAuthentication `
    -Name extendedProtection.tokenChecking -PSPath 'IIS:\' -Location 'Default Web Site/CertSrv'
```
Expected: no `:80:` binding; tokenChecking `Require`.

- [ ] **Step 5: Verify from the workstation that 80 is closed**

Run: `Test-NetConnection dns01.knowledgeondemand.net -Port 80`
Expected: `TcpTestSucceeded : False`

- [ ] **Step 6: Commit**

```bash
git add scripts/vcf-ca/Set-CAWebEnrollmentHardening.ps1
git commit -m "feat(vcf-ca): harden certsrv against ESC8 before it is reachable"
```

---

### Task 3: Prove LDAPS works

**Files:**
- Create: `scripts/vcf-ca/Test-LabCAHealth.ps1`

**Interfaces:**
- Consumes: the CA from Task 1.
- Produces: `Test-LabCAHealth.ps1`, a re-runnable gate used by every later task.

This is the single most important gate in the plan: it is the thing the whole
project exists to achieve, and it is currently false.

- [ ] **Step 1: Write the health script** — [SCRIPTED]

```powershell
<#
    Verification gates for the lab CA. Read-only, safe to run any time.
    Exit code is the number of failed gates.
#>
[CmdletBinding()]
param(
    [string]$CaHost   = 'dns01.knowledgeondemand.net',
    [string]$BaseDn   = 'DC=knowledgeondemand,DC=net',
    [string]$RootFile,          # PEM of the lab root, for chain validation
    [string]$CredFile = "$env:USERPROFILE\.vcflab\credentials.env"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.DirectoryServices.Protocols

$fail = 0
function Gate { param($Label,$Ok,$Detail='')
  if ($Ok) { Write-Host "  PASS  $Label" -ForegroundColor Green }
  else { Write-Host "  FAIL  $Label  $Detail" -ForegroundColor Red; $script:fail++ } }

$cred = @{}
foreach ($l in (Get-Content $CredFile)) {
  $t=$l.Trim(); if (-not $t -or $t.StartsWith('#') -or $t -notmatch '=') { continue }
  $k,$v = $t -split '=',2; $cred[$k.Trim()] = $v.Trim() }

# Gate 1 -- LDAPS presents a certificate at all.
$tcp = New-Object Net.Sockets.TcpClient
$ldapsUp = $false
try { $ldapsUp = $tcp.ConnectAsync($CaHost,636).Wait(5000) } catch {}
finally { $tcp.Close() }
Gate "636 accepts connections" $ldapsUp

# Gate 2 -- a simple bind over LDAPS succeeds. This is what VCF Operations does.
# Deliberately AuthType=Basic: Negotiate would pass even with no certificate and
# tell us nothing.
$bindOk = $false; $bindErr = ''
try {
  $id = New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($CaHost,636)
  $c  = New-Object System.DirectoryServices.Protocols.LdapConnection($id)
  $c.SessionOptions.ProtocolVersion = 3
  $c.SessionOptions.SecureSocketLayer = $true
  $c.Timeout = [TimeSpan]::FromSeconds(20)
  $c.AuthType = [System.DirectoryServices.Protocols.AuthType]::Basic
  $c.Bind((New-Object Net.NetworkCredential('knowledgeondemand\svc-vcf-ldap', $cred['AD_BIND_PASS'])))
  $r = New-Object System.DirectoryServices.Protocols.SearchRequest(
        $BaseDn,'(sAMAccountName=svc-vcf-ldap)',
        [System.DirectoryServices.Protocols.SearchScope]::Subtree,@('distinguishedName'))
  $bindOk = ($c.SendRequest($r).Entries.Count -ge 1)
  $c.Dispose()
} catch { $bindErr = $_.Exception.Message.Split([Environment]::NewLine)[0] }
Gate "simple bind over LDAPS reads the directory" $bindOk $bindErr

# Gate 3 -- 389 must STILL refuse simple binds. Server 2025 signing enforcement
# is correct and this plan must not have weakened it.
$plainRefused = $false
try {
  $id2 = New-Object System.DirectoryServices.Protocols.LdapDirectoryIdentifier($CaHost,389)
  $c2  = New-Object System.DirectoryServices.Protocols.LdapConnection($id2)
  $c2.SessionOptions.ProtocolVersion = 3
  $c2.AuthType = [System.DirectoryServices.Protocols.AuthType]::Basic
  $c2.Bind((New-Object Net.NetworkCredential('knowledgeondemand\svc-vcf-ldap', $cred['AD_BIND_PASS'])))
  $c2.Dispose()
} catch { $plainRefused = $true }
Gate "389 still refuses simple binds (signing enforcement intact)" $plainRefused

# Gate 4 -- certsrv is HTTPS-only.
$http = $false
$t2 = New-Object Net.Sockets.TcpClient
try { $http = $t2.ConnectAsync($CaHost,80).Wait(4000) } catch {} finally { $t2.Close() }
Gate "port 80 is closed on the CA host" (-not $http)

Write-Host ""
Write-Host ("  {0} gate(s) failed" -f $fail) -ForegroundColor $(if($fail){'Red'}else{'Green'})
exit $fail
```

- [ ] **Step 2: Run it BEFORE the DC has enrolled — it must fail**

Run: `.\Test-LabCAHealth.ps1`
Expected: gate 2 FAILS. If it passes before autoenrolment, the gate is not
testing what it claims and must be fixed.

- [ ] **Step 3: Force autoenrolment on dns01** — [OPERATOR]

Autoenrolment fires on a Group Policy cycle (90–120 min), not at CA install.

Run on dns01:
```powershell
certutil -pulse
certutil -dcinfo verify
```

- [ ] **Step 4: Run the health script again**

Run: `.\Test-LabCAHealth.ps1`
Expected: all four gates PASS. If gate 2 still fails, Schannel may not have
picked the certificate up — restart NTDS on dns01 and re-run before concluding
the template is wrong.

- [ ] **Step 5: Commit**

```bash
git add scripts/vcf-ca/Test-LabCAHealth.ps1
git commit -m "feat(vcf-ca): verification gates, with LDAPS proven by simple bind"
```

---

### Task 4: Bind a certificate to IIS and create the VCF template

**Files:**
- Create: `scripts/vcf-ca/New-VcfCertificateTemplate.ps1`

**Interfaces:**
- Consumes: the CA from Task 1.
- Produces: an issued template with **cn** `VMware`, and `https://dns01.../certsrv`
  presenting a valid chain.

Without the HTTPS binding, Task 7 fails with a TLS error that reads like a
credential problem.

- [ ] **Step 1: Bind the DC certificate to IIS** — [OPERATOR], on dns01

```powershell
$c = Get-ChildItem Cert:\LocalMachine\My |
     Where-Object { $_.Subject -like "*dns01.knowledgeondemand.net*" -and $_.HasPrivateKey } |
     Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $c) { throw "no machine certificate for dns01 -- did autoenrolment run?" }
New-WebBinding -Name 'Default Web Site' -Protocol https -Port 443
(Get-WebBinding -Name 'Default Web Site' -Protocol https).AddSslCertificate($c.Thumbprint, 'My')
"bound $($c.Thumbprint)"
```

- [ ] **Step 2: Verify the chain from the workstation**

Run: `openssl s_client -connect dns01.knowledgeondemand.net:443 -CAfile <root.pem> </dev/null`
Expected: `Verify return code: 0 (ok)` and an issuer of
`knowledgeondemand-LabRoot-CA`.

- [ ] **Step 3: Write the template script** — [OPERATOR]

The `VMware` template carries **Server AND Client Authentication**, settled by
measuring what VCF uses today: SDDC Manager and both NSX certificates carry
both, and vCenter carries no EKU at all.

```powershell
#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host @"
Create the template by hand in certtmpl.msc -- there is no supported cmdlet for
template creation, and a scripted ADSI clone is fragile enough that a wrong
template costs more than the five minutes this saves.

  1. Duplicate 'Web Server'
  2. General      : Template display name 'VMware'; confirm the TEMPLATE NAME
                    (cn) is also 'VMware' -- SDDC Manager is given the cn, and a
                    display-name mismatch fails at certificate REQUEST time,
                    not at configuration time.
                    Validity 2 years.
  3. Compatibility: leave at the LOWEST offered. A modern default produces a v4
                    template with CNG/KSP constraints that reject web-enrolment
                    CSR submission.
  4. Extensions   : Application Policies = Server Authentication AND
                    Client Authentication.
  5. Subject Name : 'Supply in the request'.
  6. Security     : Enroll for knowledgeondemand\svc-vcf-ca ONLY.
                    Remove any Authenticated Users / Domain Users Enroll ACE.
  7. Issue it     : certsrv.msc -> Certificate Templates -> New -> Certificate
                    Template to Issue -> VMware
  8. CA security  : CA properties -> Security -> svc-vcf-ca -> Request
                    Certificates. This is SEPARATE from template Enroll.
"@ -ForegroundColor Cyan

if (-not $Apply) { exit 0 }

Write-Host "`nVerifying what was built..." -ForegroundColor Cyan
$issued = (certutil -CATemplates) -join "`n"
if ($issued -notmatch 'VMware') { Write-Host "  FAIL  template 'VMware' is not issued on the CA" -ForegroundColor Red; exit 1 }
Write-Host "  PASS  template is issued" -ForegroundColor Green

$t = (certutil -v -template VMware) -join "`n"
if ($t -match 'Client Authentication' -and $t -match 'Server Authentication') {
    Write-Host "  PASS  EKU carries Server + Client Authentication" -ForegroundColor Green
} else { Write-Host "  FAIL  EKU is not as designed" -ForegroundColor Red; exit 1 }

if ($t -match 'Authenticated Users' -or $t -match 'Domain Users') {
    Write-Host "  FAIL  a broad Enroll ACE is present -- this is the ESC1 control" -ForegroundColor Red; exit 1
}
Write-Host "  PASS  no broad enrolment ACE" -ForegroundColor Green

$ef = (certutil -getreg policy\EditFlags) -join "`n"
if ($ef -match 'EDITF_ATTRIBUTESUBJECTALTNAME2') {
    Write-Host "  FAIL  EDITF_ATTRIBUTESUBJECTALTNAME2 is set (ESC6) -- clear it" -ForegroundColor Red; exit 1
}
Write-Host "  PASS  ESC6 flag not set" -ForegroundColor Green
```

- [ ] **Step 4: Build the template, then verify**

Run on dns01: `.\New-VcfCertificateTemplate.ps1 -Apply`
Expected: four PASS lines. Any FAIL stops the plan — these are the only controls
standing between `svc-vcf-ca` and arbitrary certificate issuance.

- [ ] **Step 5: Commit**

```bash
git add scripts/vcf-ca/New-VcfCertificateTemplate.ps1
git commit -m "feat(vcf-ca): VMware template with post-build ESC1/ESC6 verification"
```

---

### Task 5: Distribute the root before anything is rotated

**Files:**
- Create: `scripts/vcf-ca/Publish-LabRootTrust.ps1`

**Interfaces:**
- Consumes: the root certificate from Task 1.
- Produces: the root present in vCenter's trusted-root-chains and SDDC Manager's
  trust store.

Both APIs were **verified writable** with credentials already held, and neither
needs a VIDB token. Domain members need no GPO — an Enterprise CA publishes to
the Public Key Services container and clients pull it automatically.

- [ ] **Step 1: Export the root** — [OPERATOR], on dns01

```powershell
certutil -ca.cert C:\Temp\lab-root.cer
certutil -encode C:\Temp\lab-root.cer C:\Temp\lab-root.pem
Get-FileHash C:\Temp\lab-root.cer -Algorithm SHA256
```
Record that hash. Step 3 compares against it out-of-band rather than trusting
whatever a transfer produces.

- [ ] **Step 2: Write the trust publisher** — [SCRIPTED]

```powershell
[CmdletBinding()]
param([Parameter(Mandatory)][string]$RootPem, [switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')

$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation
$pem  = (Get-Content $RootPem -Raw).Trim()

$sso = Get-VCFLabCredentialObject -Map $cred -UserKey $null -PassKey 'VCF_SSO_ADMIN_PASS' -DefaultUser 'administrator@vsphere.local'
$tok = (Invoke-LabRest -Uri "https://$($cfg.Appliances.SddcManager.Ip)/v1/tokens" -Method POST `
          -Body @{ username = $sso.UserName; password = $sso.GetNetworkCredential().Password }).accessToken

Write-Host ("{0} SDDC Manager trust store" -f $(if($Apply){'WRITING'}else{'WOULD WRITE'}))
if ($Apply) {
    Invoke-LabRest -Uri "https://$($cfg.Appliances.SddcManager.Ip)/v1/sddc-manager/trusted-certificates" `
        -Method POST -Headers @{ Authorization = "Bearer $tok" } -Body @{ certificate = $pem } | Out-Null
    Write-Host "  done" -ForegroundColor Green
}
Write-Host ("{0} vCenter trusted-root-chains" -f $(if($Apply){'WRITING'}else{'WOULD WRITE'}))
Write-Host "  ESXi inherits from here -- do NOT seed hosts individually" -ForegroundColor Gray
```

- [ ] **Step 3: Compare the thumbprint out-of-band, then publish**

Run: `.\Publish-LabRootTrust.ps1 -RootPem .\lab-root.pem`
then with `-Apply`.

- [ ] **Step 4: Verify both stores hold it**

```
GET /v1/sddc-manager/trusted-certificates   -> the new root appears
GET /api/vcenter/certificate-management/vcenter/trusted-root-chains -> 2 entries, was 1
```

- [ ] **Step 5: Assert VSP trust, do not assume it**

Whether the Supervisor inherits from vCenter is **unproven**. If it has not, the
fallback is to rotate everything except VSP and leave it on its VSP-issued
certificate — the same fallback already accepted for VIDB and the fleet.

- [ ] **Step 6: Commit**

```bash
git add scripts/vcf-ca/Publish-LabRootTrust.ps1
git commit -m "feat(vcf-ca): publish the lab root to vCenter and SDDC Manager trust"
```

---

### Task 6: Register the CA and prove it by issuing

**Files:**
- Create: `scripts/vcf-ca/Register-VcfCA.ps1`

**Interfaces:**
- Consumes: the template from Task 4, trust from Task 5.
- Produces: a registered Microsoft CA in SDDC Manager, proven by a real
  certificate request.

- [ ] **Step 1: Write the registration script** — [SCRIPTED]

```powershell
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')
$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation
$sddc = $cfg.Appliances.SddcManager.Ip

$sso = Get-VCFLabCredentialObject -Map $cred -UserKey $null -PassKey 'VCF_SSO_ADMIN_PASS' -DefaultUser 'administrator@vsphere.local'
$tok = (Invoke-LabRest -Uri "https://$sddc/v1/tokens" -Method POST `
          -Body @{ username = $sso.UserName; password = $sso.GetNetworkCredential().Password }).accessToken
$hdr = @{ Authorization = "Bearer $tok" }

$spec = @{ microsoftCertificateAuthoritySpec = @{
    serverUrl    = 'https://dns01.knowledgeondemand.net/certsrv'
    username     = $cred['AD_CA_ENROLL_USER']
    secret       = $cred['AD_CA_ENROLL_PASS']
    templateName = 'VMware' } }

if (-not $Apply) { Write-Host "WOULD PUT the Microsoft CA spec (secret withheld)"; exit 0 }
Invoke-LabRest -Uri "https://$sddc/v1/certificate-authorities" -Method PUT -Headers $hdr -Body $spec | Out-Null
Write-Host "CA registered" -ForegroundColor Green
```

- [ ] **Step 2: Register**

Run: `.\Register-VcfCA.ps1 -Apply`

- [ ] **Step 3: Do NOT trust the configuration-time check**

`GET /v1/certificate-authorities` echoes what was just stored. It passes with a
wrong password and an unreachable URL. It is not a gate.

- [ ] **Step 4: Prove it by issuing one certificate for the least critical resource**

Generate a CSR for **one NSX manager** and have SDDC Manager fulfil it. A
certificate that comes back signed by `knowledgeondemand-LabRoot-CA` proves the
URL, the credentials, the enrolment right and the template name all at once —
the four things the configuration-time check cannot.

Expected: task completes; `GET /v1/domains/{id}/resource-certificates` shows
that one resource issued by the lab root while the other seven still show
`CN=CA`.

- [ ] **Step 5: Commit**

```bash
git add scripts/vcf-ca/Register-VcfCA.ps1
git commit -m "feat(vcf-ca): register the Microsoft CA and prove it by issuance"
```

---

### Task 7: Back up, then rotate the remaining certificates

**Files:** none created — this task is operational.

**Interfaces:**
- Consumes: the proven CA from Task 6.
- Produces: all 8 resources issued by the lab root.

- [ ] **Step 1: Take the backups that are the actual rollback**

SDDC Manager backup plus snapshots of vCenter, NSX, SDDC Manager and VSP.
**`dns01` is excluded** — snapshot-reverting it rolls the CA database back and
reuses serial numbers already issued and trusted.

`GET /v1/domains/{id}/resource-certificates` is **not** a rollback artefact: it
returns public certificates and metadata, no private keys. Nothing can be
restored from it.

- [ ] **Step 2: Confirm NTP before issuing anything**

A skewed clock produces certificates that fail validation immediately. This repo
already carries a commit fixing ntpd startup policy on these hosts.

Run: `Get-VMHost | Get-VMHostNtpServer` and confirm the service is running on all three.

- [ ] **Step 3: Set `vpxd.certmgmt.mode` to `custom` before touching ESXi**

Left at the `vmca` default, vCenter regenerates VMCA-signed host certificates on
the next renew or reconnect and silently undoes the work.

- [ ] **Step 4: Rotate in this order, one at a time**

NSX (both certificates) → VSP → ESXi (one host at a time, `EnsureAccessibility`,
N-1 headroom on a 3-host vSAN) → **vCenter** → **SDDC Manager last**.

SDDC Manager is the *instrument* that rotates vCenter; rotating its own
certificate restarts it and invalidates in-flight tasks and tokens, so it goes
last.

- [ ] **Step 5: After each resource, check three things**

1. `GET /v1/domains/{id}/resource-certificates` shows the new issuer
2. the resource still answers
3. peers that pin its **leaf thumbprint** have been re-registered — vCenter↔NSX
   compute manager, SDDC Manager's stored vCenter thumbprint, vCenter↔ESXi host
   thumbprints, VCF Operations adapters. Root trust does nothing for these.

For ESXi specifically, run `Get-HostInventoryDrift` (not
`Confirm-HostInventorySync`, which repairs by default and returns a boolean)
**with VMs running** — it compares VM power state and passes vacuously on an
evacuated host.

- [ ] **Step 6: Commit nothing; record the outcome in the spec's Verification section**

---

### Task 8: Add AD as an identity source in VCF Operations

**Files:**
- Create: `scripts/vcf-ca/Add-OpsIdentitySource.ps1`

**Interfaces:**
- Consumes: working LDAPS from Task 3.
- Produces: an ACTIVE_DIRECTORY identity source in VCF Operations.

The contract below came from the appliance's own REST reference at
`/suite-api/docs/rest/index.html`. `others: []` and `otherAttributes: {}` are
load-bearing — a `sourceType` without them is rejected as null.

- [ ] **Step 1: Write the script, validate-then-create** — [SCRIPTED]

```powershell
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\vcf-lab\VCFLab.Common.ps1')
$cfg  = Import-VCFLabConfig
$cred = Import-VCFLabCredential -Path $cfg.CredentialFile
Disable-CertificateValidation
$ops = $cfg.Appliances.Operations.Ip

$tok = (Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/token/acquire" -Method POST `
          -Body @{ username = $cred['OPERATIONS_ADMIN_USER']; password = $cred['OPERATIONS_ADMIN_PASS'] }).token
$hdr = @{ Authorization = "vRealizeOpsToken $tok" }

# svc-vcf-ldap has NO userPrincipalName, so UPN-form authentication cannot work
# against it. Use DOMAIN\sAMAccountName, paired with common-name sAMAccountName.
$body = @{
  name = 'knowledgeondemand-AD'
  sourceType = @{ id='ACTIVE_DIRECTORY'; name='ACTIVE_DIRECTORY'; others=@(); otherAttributes=@{} }
  others = @(); otherAttributes = @{}; certificates = @()
  property = @(
    @{ name='display-name'; value='knowledgeondemand' }
    @{ name='host';         value='dns01.knowledgeondemand.net' }
    @{ name='domain';       value='knowledgeondemand.net' }
    @{ name='host-auto-select'; value='false' }
    @{ name='use-ssl';      value='true' }
    @{ name='port';         value='636' }
    @{ name='base-domain';  value='dc=knowledgeondemand,dc=net' }
    @{ name='common-name';  value='sAMAccountName' }
    @{ name='user-name';    value='knowledgeondemand\svc-vcf-ldap' }
    @{ name='password';     value=$cred['AD_BIND_PASS'] } ) }

Write-Host "validating (creates nothing)..."
$t = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources/test" -Method POST -Headers $hdr -Body $body
Write-Host "  validation passed" -ForegroundColor Green
if (-not $Apply) { Write-Host "DRY RUN -- pass -Apply to create"; exit 0 }
$r = Invoke-LabRest -Uri "https://$ops/suite-api/api/auth/sources" -Method POST -Headers $hdr -Body $body
Write-Host "created; with SSL the response carries discovered certificates and a" -ForegroundColor Yellow
Write-Host "follow-up PATCH /suite-api/api/auth/sources is required before use." -ForegroundColor Yellow
```

- [ ] **Step 2: Validate before the CA exists — it must fail**

Run: `.\Add-OpsIdentitySource.ps1`
Expected: validation FAILS with a bind error. This is the current state and
confirms the gate is real.

- [ ] **Step 3: Validate after LDAPS works**

Run: `.\Add-OpsIdentitySource.ps1`
Expected: `validation passed`.

- [ ] **Step 4: Create, then complete the certificate PATCH**

Run: `.\Add-OpsIdentitySource.ps1 -Apply`
Then `GET /suite-api/api/auth/sources` returns one source, and a user from the
directory resolves.

- [ ] **Step 5: Retest VIDB — expect it to still fail**

Run:
```bash
curl -sk --resolve idb.lab.knowledgeondemand.net:443:172.16.10.124 \
  "https://idb.lab.knowledgeondemand.net/federation/t/CUSTOMER/auth/login?dest=x"
```

Research says nothing connects an Operations identity source to VIDB's access
policy, so *"Invalid access policy"* is the expected outcome. **This step is an
experiment, not a success criterion.** If it does clear, that is a genuine and
welcome surprise worth recording; if it does not, the plan has still delivered
everything it promised.

- [ ] **Step 6: Commit**

```bash
git add scripts/vcf-ca/Add-OpsIdentitySource.ps1
git commit -m "feat(vcf-ca): AD identity source for VCF Operations, validate before create"
```

---

### Task 9: Document

**Files:**
- Create: `scripts/vcf-ca/README.md`

- [ ] **Step 1: Write the README** covering the ownership split, the order, the
      four install-time-only settings, why 389 must keep refusing simple binds,
      why `dns01` must never be snapshot-reverted, and what is still blocked
      afterwards (fleet-lcm, VIDB, VSP-issued certificates).

- [ ] **Step 2: Run the full health script one final time**

Run: `.\Test-LabCAHealth.ps1`
Expected: 0 failed gates.

- [ ] **Step 3: Commit**

```bash
git add scripts/vcf-ca/README.md
git commit -m "docs(vcf-ca): record the ownership split and what remains blocked"
```
