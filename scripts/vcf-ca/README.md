# Lab certificate authority (AD CS)

A single-tier Enterprise Root CA (`knowledgeondemand-LabRoot-CA`) on `dns01`
(172.16.10.150), stood up because the domain controller could not serve LDAPS
at all -- proven to block AD identity in VCF Operations -- and because VCF has
no ACME support, so an in-lab CA is the only way to autoenrol the DC's own
certificate and issue to the eight certificates SDDC Manager manages.

```powershell
.\Test-LabCAHealth.ps1                          # read-only, safe any time
.\Publish-LabRootTrust.ps1 -RootPem .\lab-root.pem          # dry run
.\Publish-LabRootTrust.ps1 -RootPem .\lab-root.pem -Apply
```

Every script here is dry-run by default; nothing writes until you pass
`-Apply`. See the design doc for the full analysis:
`docs/superpowers/specs/2026-09-29-lab-certificate-authority-design.md`.

## Prerequisites (do these before step 1)

**Create the enrolment service account.** Nothing in this directory creates it,
and `Register-VcfCA.ps1` refuses to run without it:

```powershell
# on dns01, as a Domain Admin
New-ADUser -Name 'svc-vcf-ca' -SamAccountName 'svc-vcf-ca' `
    -UserPrincipalName 'svc-vcf-ca@knowledgeondemand.net' `
    -AccountPassword (Read-Host -AsSecureString 'password for svc-vcf-ca') `
    -PasswordNeverExpires $true -Enabled $true
```

**Deny it interactive and remote-desktop logon.** It is an enrolment identity,
not a person; it never needs a session on any host. This is a spec requirement,
not a nicety -- it is the containment for a leaked enrolment password.

```powershell
# GPO linked to the Domain Controllers OU (and to any OU holding member servers):
#   Computer Configuration -> Policies -> Windows Settings -> Security Settings
#     -> Local Policies -> User Rights Assignment
#       "Deny log on locally"                 -> knowledgeondemand\svc-vcf-ca
#       "Deny log on through Remote Desktop Services" -> knowledgeondemand\svc-vcf-ca
gpupdate /force
# verify from the target host:
secedit /export /areas USER_RIGHTS /cfg C:\Windows\Temp\ur.inf
Select-String -Path C:\Windows\Temp\ur.inf -Pattern 'SeDenyInteractiveLogonRight|SeDenyRemoteInteractiveLogonRight'
```

**Add its credentials to `C:\Users\willi\.vcflab\credentials.env`.** Neither key
exists there today; `Register-VcfCA.ps1` names them explicitly when they are
missing rather than failing with a null reference:

```
AD_CA_ENROLL_USER=knowledgeondemand\svc-vcf-ca
AD_CA_ENROLL_PASS=<the password set above>
```

**Create the `pki` DNS record.** `Set-CaRevocationEndpoints.ps1` refuses to run
without it, by design:

```powershell
# from the workstation -- the record is already declared in the script's table
.\scripts\vcf-dns\Set-VCFLabDnsRecord.ps1
.\scripts\vcf-dns\Set-VCFLabDnsRecord.ps1 -Apply -Credential (Get-Credential KNOWLEDGEONDEMAND\Administrator)
```

`pki.knowledgeondemand.net` -> `172.16.10.150`, and only that address.

**This is a convention, not a fix.** An earlier revision of this README said
the CA could not use its own hostname because `dns01.knowledgeondemand.net`
resolved to two addresses. That was wrong — measured on the DC on 2026-10-01,
it resolves to exactly one (`172.16.10.150`), from a static record in a zone
with `dynamicUpdate=None`. The two-address answer came from asking the DC
about itself, where the local resolver reports the machine's own interfaces.

The reason to keep a dedicated name is narrower: a certificate carries its CDP
and AIA URLs for its whole life, so naming the *service* rather than the
*host* means the CA can move off this domain controller later without
reissuing everything. If you would rather not add the record, run
`Set-CaRevocationEndpoints.ps1 -CdpHost dns01.knowledgeondemand.net` and it
becomes a no-op that still asserts the host is single-homed.

The record carries no PTR — that address's PTR belongs to `dns01`, and two
PTRs on one address make reverse lookups return both.

## Ownership split

No credential in `credentials.env` carries Domain Admin, and adding one is a
decision the operator makes deliberately -- so this project is split down the
middle. **[OPERATOR]** scripts must be run by a human, interactively, on
`dns01` itself. **[SCRIPTED]** scripts run from a workstation against SDDC
Manager's or VCF Operations' REST API with credentials already held.

| Script | Ownership | Runs on |
|---|---|---|
| `Install-LabCA.ps1` | **[OPERATOR]** | `dns01` |
| `Set-CaRevocationEndpoints.ps1` | **[OPERATOR]** | `dns01` |
| `Set-CAWebEnrollmentHardening.ps1` | **[OPERATOR]** | `dns01` |
| `Test-CaRevocationEndpoints.ps1` | **[SCRIPTED]** | inside the lab (see below) |
| `Test-LdapsHandshake.ps1` | **[SCRIPTED]** | workstation |
| `New-VcfCertificateTemplate.ps1` | **[OPERATOR]** | `dns01` (both template builds are manual in `certtmpl.msc`; the script prints instructions, then verifies) |
| `Test-LabCAHealth.ps1` | **[SCRIPTED]** | workstation |
| `Publish-LabRootTrust.ps1` | **[SCRIPTED]** | workstation |
| `Register-VcfCA.ps1` | **[SCRIPTED]** | workstation |
| `Add-OpsIdentitySource.ps1` | **[SCRIPTED]** | workstation |
| Certificate rotation (Task 7, no script) | mixed -- see [Rotation runbook](#rotation-runbook-after-everything-above-is-green) | `dns01` + workstation |

## Order, and why it is the order

| # | Step | Script |
|---|---|---|
| 0 | **Clone `dns01`** -- the last rollback point in the whole project | `New-Dns01RollbackClone.ps1 -Apply` |
| 1 | Install the CA | `Install-LabCA.ps1 -Apply` |
| 1b | **Point CDP/AIA at a single-homed name** -- before ANY certificate is issued | `Set-CaRevocationEndpoints.ps1 -Apply` |
| 2 | Harden web enrolment | `Set-CAWebEnrollmentHardening.ps1 -Apply` |
| 3 | **Publish `Domain Controller Authentication`** -- without this nothing autoenrols | manual + `New-VcfCertificateTemplate.ps1` section A |
| 4 | Force + confirm DC autoenrolment | `certutil -pulse`, `certutil -dcinfo verify`, then `Test-LabCAHealth.ps1` |
| 5 | Bind the DC's certificate to IIS on 443 | manual, snippet below |
| 6 | Create + verify the `VMware` template, CA ACE and CA auditing | `New-VcfCertificateTemplate.ps1 -Apply` |
| 7 | Distribute the root to vCenter + SDDC Manager | `Publish-LabRootTrust.ps1 -Apply` |
| 8 | Register the CA with SDDC Manager | `Register-VcfCA.ps1 -Apply` |
| 9 | Rotate the eight SDDC-managed certificates | manual runbook, see below |
| 10 | Add AD as an identity source in VCF Operations | `Add-OpsIdentitySource.ps1 -Apply` |

### Step 0 -- clone `dns01`, and understand that it is the last one

Once the CA exists, `dns01` must never be rolled back (see the invariant
below), so the moment before `Install-LabCA.ps1 -Apply` is the final moment a
copy of this VM is usable. Take it now. The install script prompts for a typed
`CONFIRM` that you have, and refuses to install without it. Everything from
step 1 onwards is forward-only.

**A VM snapshot is not available for this VM.** `dns01` runs on the standalone
management host (192.168.6.167), which has Software Memory Tiering enabled, and
ESXi refuses snapshots on a tiered system:

> Snapshots are not yet supported on a system with Software Memory Tiering enabled.

That is no loss. Snapshot-reverting a domain controller risks USN rollback, so
a cold copy taken with the guest cleanly shut down is the better artifact
anyway. `New-Dns01RollbackClone.ps1` does exactly that: graceful guest
shutdown, a host-local copy through the host's own `VirtualDiskManager` (no
SSH opened, no network round-trip), power back on, then a verification that
DNS is serving again -- a real query, not a port check.

```powershell
.\New-Dns01RollbackClone.ps1              # dry run -- shows exactly what it would copy
.\New-Dns01RollbackClone.ps1 -Apply       # ~61 GB; lab DNS and AD are DOWN meanwhile
```

Every VCF component resolves its peers through this DC, so run it in a window
where the lab can lose name resolution. To roll back: power off `dns01`,
register the copy's `.vmx` in the host's inventory, and power that on.

### Step 1b -- CDP and AIA, before the CA signs anything

A certificate carries the CDP and AIA URLs that were configured **at the moment
it was signed**. Changing them later does not repair a certificate already
issued; the only fix is reissuing it. So this sits between install and first
issuance, and it is not optional.

```powershell
# on dns01
.\Set-CaRevocationEndpoints.ps1              # dry run -- prints current vs new
.\Set-CaRevocationEndpoints.ps1 -Apply
```

It refuses to run unless `pki.knowledgeondemand.net` resolves to exactly one
address, because a multi-homed CDP host is the defect it exists to remove.

**What AD CS actually writes at install, measured on this CA:**

```
CRLPublicationURLs      65:C:\...\CertEnroll\%3%8%9.crl
                        79:ldap:///CN=%7%8,...,CN=CDP,...
                         0:http://%1/CertEnroll/%3%8%9.crl
                         0:file://%1/CertEnroll/%3%8%9.crl
CACertPublicationURLs    1:C:\...\CertEnroll\%1_%3%4.crt
                         3:ldap:///CN=%7,CN=AIA,...
                         0:http://%1/CertEnroll/%1_%3%4.crt
                         0:file://%1/CertEnroll/%1_%3%4.crt
```

**The http entries carry flag `0`** — inert placeholders. By default the only
URL that reaches an issued certificate is the `ldap:///` one, which the lab's
Photon-based appliances cannot follow. So the script does two things: it
rewrites the host, **and** it promotes the flag.

Flag prefixes, paths and `%` tokens are otherwise preserved byte for byte, and
non-HTTP entries (local paths, `file://`, `ldap:///`) are left exactly as
found.

**Which flag, and why that one.** The `ldap` CDP entry that does reach issued
certificates carries `79 = 1+2+4+8+64`. The publish bits (1, 64) are
meaningless for http — a CA cannot publish over http — leaving `2+4+8`. Bit 4
concerns delta-CRL discovery and deltas are disabled here, so **10 = 2+8** for
CDP and **2** for AIA.

`10` was chosen deliberately because it contains *both* candidate bits:
Microsoft's primary reference does not enumerate them, and two independent
reviews of this project reached opposite conclusions about whether `2` or `8`
means "include in the CDP extension of issued certificates". `10` is correct
under either reading.

**Confirmed empirically on 2026-10-01.** The DC's autoenrolled certificate
carries
`URL=http://pki.knowledgeondemand.net/CertEnroll/knowledgeondemand-LabRoot-CA.crl`
in its CDP extension and the matching `.crt` in its AIA, and both fetch and
parse. So `10`/`2` work; the ambiguity never needed resolving.

The script then reads both values back and compares them element by element
before restarting `certsvc` and publishing a fresh CRL (with a retry, because
`certutil -CRL` answers *RPC server is unavailable* for a few seconds after the
service restarts).

**CDP and AIA are plain HTTP on purpose.** Serving them over HTTPS is circular:
validating the HTTPS certificate requires fetching a CRL, which would require
validating an HTTPS certificate. CRLs and CA certificates are signed objects,
so the transport does not need to supply integrity. Do not "upgrade" these URLs
to 443.

**Configuration verified is not revocation working, and this is a required
step, not a nicety.** The read-back above compares what was written against
what was intended — it cannot detect a wrong flag prefix, which is the failure
that matters. So before issuing anything real:

1. Issue **one throwaway certificate** from the CA, any template.
2. Run the end-to-end gate against it. It reads the URLs out of the
   certificate itself, so a flag that never reached the CDP extension shows up
   here and nowhere else:

```powershell
.\Test-CaRevocationEndpoints.ps1 -CertificateFile .\throwaway.cer
```

3. Only once that exits `0`, proceed to the template and rotation steps.

The same test also works against a live endpoint once LDAPS is up:

```powershell
.\Test-CaRevocationEndpoints.ps1 -FromTlsEndpoint dns01.knowledgeondemand.net:636
```

Run on `dns01` it proves the CA's output and the CRL's validity, but not that a
lab consumer can reach the endpoint -- `dns01` is reaching itself. For the claim
that actually matters, ask from inside the lab:

```bash
pct exec 159 -- curl -sS -o /dev/null -w '%{http_code}\n' \
    http://pki.knowledgeondemand.net/CertEnroll/
```

The test exits `2` for INCONCLUSIVE when it cannot resolve or reach a URL,
rather than reporting a failure it did not observe. A workstation on a VPN with
`block-outside-dns` always returns `2`, however healthy the lab is.

### Step 3 -- publish the DC template (nothing autoenrols without it)

`CAPolicy.inf` sets `LoadDefaultTemplates=0`, so the CA published **no**
templates at promotion -- not even a DC template. Until you do this by hand, the
domain controller never enrols, LDAPS never starts, and steps 4 onward cannot
work. `New-VcfCertificateTemplate.ps1` (run with no arguments) prints the full
procedure as section A; the short form is:

1. `certsrv.msc` -> Certificate Templates -> right-click -> New -> Certificate
   Template to Issue -> **Domain Controller Authentication** (the standard
   template; do not duplicate it, do not substitute the older
   `Domain Controller` template).
2. `certtmpl.msc` -> Domain Controller Authentication -> Security -> grant
   **`knowledgeondemand\Domain Controllers`** -> Enroll **and** Autoenroll.
   Not `Domain Computers`. Not `Authenticated Users`. A broader Autoenroll
   grant hands a Client Authentication certificate to every machine in the
   domain, which is the relay surface `LoadDefaultTemplates=0` exists to avoid.

`New-VcfCertificateTemplate.ps1 -Apply` (step 6) asserts both: that the template
is issued, and that Autoenroll went to the Domain Controllers group only.

### Step 4 -- force and confirm autoenrolment

Do not sit through the ~90-120 minute GPO cycle; trigger it:

```powershell
# on dns01
certutil -pulse                       # force the autoenrolment cycle now
certutil -dcinfo verify               # verifies the DC's own cert chain + LDAPS readiness

# confirm a Server Authentication cert actually landed in the machine store
Get-ChildItem Cert:\LocalMachine\My |
    Where-Object { $_.EnhancedKeyUsageList.FriendlyName -contains 'Server Authentication' } |
    Format-List Subject, Issuer, Thumbprint, NotAfter
```

Then `Test-LabCAHealth.ps1` from inside the lab. If `certutil -dcinfo verify`
reports no certificate, step 3 did not take -- re-check the Autoenroll ACE
before anything else.

### Step 5 -- bind the certificate to IIS on 443 (no script does this)

`Set-CAWebEnrollmentHardening.ps1` set Require-SSL on `/certsrv`, so it is
unreachable over HTTP on purpose (403 before any auth challenge) while port 80
stays bound for `/CertEnroll`. Binding 443 is manual:

```powershell
# on dns01, after step 4 confirms a Server Authentication certificate exists
$cert = Get-ChildItem Cert:\LocalMachine\My |
    Where-Object { $_.Subject -like "*dns01.knowledgeondemand.net*" -and
                   $_.EnhancedKeyUsageList.FriendlyName -contains 'Server Authentication' } |
    Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $cert) { throw 'no Server Authentication certificate in LocalMachine\My -- redo step 4' }

Import-Module WebAdministration
New-WebBinding -Name 'Default Web Site' -Protocol https -Port 443 -IPAddress '*'
$binding = Get-WebBinding -Name 'Default Web Site' -Protocol https
$binding.AddSslCertificate($cert.Thumbprint, 'My')

# verify from the admin workstation allowed by the scoped firewall rule
Invoke-WebRequest -Uri 'https://dns01.knowledgeondemand.net/certsrv' -UseBasicParsing
```

### Step 7 -- verify the chain after distributing the root

`Publish-LabRootTrust.ps1` verifies by thumbprint against each store's own API.
Verify the chain independently as well, from a host that is *not* the CA:

```bash
# validates that the endpoint's chain terminates in the lab root you published
openssl s_client -connect dns01.knowledgeondemand.net:636 \
    -servername dns01.knowledgeondemand.net \
    -CAfile ./lab-root.pem -showcerts </dev/null 2>/dev/null \
  | grep -E 'Verify return code|subject=|issuer='
# want: "Verify return code: 0 (ok)"  -- anything else means the root did not land

# same check against the web enrolment endpoint once step 5 is done
openssl s_client -connect dns01.knowledgeondemand.net:443 \
    -CAfile ./lab-root.pem </dev/null 2>/dev/null | grep 'Verify return code'
```

A `Verify return code: 19 (self signed certificate in certificate chain)` means
the `-CAfile` root is not the one that signed the leaf -- re-export it with
`certutil -ca.cert lab-root.cer` then `certutil -encode lab-root.cer lab-root.pem`.

Two ordering constraints in here are load-bearing, not just tidy:

- **Root trust must be distributed (step 7) before any certificate is
  rotated (step 9), or the management plane partitions.** A resource
  re-issued from the lab CA before vCenter and SDDC Manager trust that CA's
  root presents a chain nothing else validates -- the exact failure mode this
  project exists to avoid, just moved one certificate later.
- **SDDC Manager is rotated LAST in step 9, after vCenter.** SDDC Manager is
  the *instrument* that rotates vCenter's certificate. Rotating SDDC
  Manager's own certificate first restarts it mid-sequence and invalidates
  every in-flight task and token the rotation of the other seven resources
  depends on.

Step 10 is last for a narrower reason: it is the original goal (AD identity in
VCF Operations), and everything from step 1 onward exists only to make LDAPS
real enough for `Add-OpsIdentitySource.ps1`'s validation call to pass.

## Four settings CAPolicy.inf locks in at install time

`Install-LabCA.ps1` copies `CAPolicy.inf` to `C:\Windows` and installs from
it. These four values cannot be changed afterward without uninstalling the
CA, rebuilding the machine, and re-seeding every trust store in step 7 by
hand:

| Setting | Value | Why |
|---|---|---|
| Root validity | 10 years | The AD CS default is 5. AD CS silently *truncates* any certificate template whose validity outlives the CA's remaining life -- a 2-year template starts shortening from year 3, and the whole fabric would expire and cascade-renew at year 5. |
| CRL period | 52 weeks | The default is 1 week. A DC powered off for a planned maintenance window over 2 weeks would stop publishing CRLs, and strict validators (VCF LCM included) hard-fail on a CRL whose `nextUpdate` has passed. |
| Key length | RSA 2048 | Minimum acceptable, standard, widely supported. |
| Hash algorithm | SHA-256 | Standard; `AlternateSignatureAlgorithm` (CMS format) is explicitly left off. |

`LoadDefaultTemplates=0` is also set, and it is **not** in this "cannot change"
list -- templates can be issued or unissued by hand at any time after install.
It suppresses the dozen templates an Enterprise CA would otherwise publish at
promotion, and its consequence is blunt: **the CA publishes nothing at all**,
so the DC does not autoenrol and LDAPS does not start until step 3 publishes
`Domain Controller Authentication` by hand. That template is published
deliberately, with Autoenroll scoped to the `Domain Controllers` group; ESC8 is
closed at the web-enrolment endpoint (Negotiate disabled, HTTPS-only, firewall
scoped to SDDC Manager), not by withholding a template the lab cannot work
without.

## CA auditing, and the monthly review that makes it worth anything

`svc-vcf-ca` requests certificates with the subject **supplied in the request**,
and no name constraint is enforceable on that. The design accepts this *only*
because the mitigation is detective: issuance is audited and the audit is read.
An audit nobody reads is not a control. `New-VcfCertificateTemplate.ps1 -Apply`
asserts `CA\AuditFilter` is 127 and fails if it is not.

```powershell
# on dns01, once (step 6 verifies it)
certutil -setreg CA\AuditFilter 127            # all seven CA event categories
auditpol /set /subcategory:"Certification Services" /success:enable /failure:enable
net stop certsvc ; net start certsvc

# confirm both halves -- the CA filter AND the OS audit policy
certutil -getreg CA\AuditFilter
auditpol /get /subcategory:"Certification Services"
```

**Monthly, on the first of the month**, review every certificate issued since
the last review and confirm each CN is one you expect (the eight SDDC-managed
resources, the DC itself, nothing else):

```powershell
# on dns01 -- every request since the last review
certutil -view -restrict "NotBefore>=2026-09-01" `
    -out "RequestID,RequesterName,CommonName,NotBefore,CertificateTemplate"
```

Any CN outside the expected set means `svc-vcf-ca` issued for a host it has no
business issuing for -- treat it as a credential compromise, rotate
`AD_CA_ENROLL_PASS`, and revoke the certificate before anything else.

## Two invariants this project must not "fix"

**Port 389 must keep refusing simple binds.** That is Windows Server 2025's
LDAP signing enforcement working correctly, not a defect this CA introduces.
`Test-LabCAHealth.ps1` gate 3 asserts it **stays** true, cross-checked with a
Negotiate bind (unaffected by signing enforcement) so an unreachable host
cannot masquerade as a correct refusal. If gate 3 ever fails because 389
starts accepting simple binds, that is a regression in domain security
posture, not a bug in the gate -- do not relax signing enforcement to make it
pass.

**`dns01` must never be snapshot-reverted once the CA exists.** Reverting
rolls the CA database back and reuses serial numbers that have already been
issued and are already trusted elsewhere. The lab's usual rollback tool is
therefore forbidden for this one VM; use the CA-aware rollback in the
[rotation runbook](#rotation-runbook-after-everything-above-is-green)
instead.

## What is still blocked after all of this succeeds

Be precise about the boundary: the CA delivers working LDAPS, AD-backed
login to VCF Operations, and one certificate chain across the eight
SDDC-managed endpoints. It does **not** deliver the fleet.

| Still broken | Why |
|---|---|
| `fleet-lcm` stays 401 | It needs a VIDB token, and nothing in the deployment path (VIDB's own config schema, the VCF spec's `vidbSpec`, or VCF Operations' visibility into VIDB) configures VIDB's access policy. That is unrelated day-2 work. |
| VIDB login stays broken (`"Invalid access policy"`) | This is an authorisation fault inside VIDB, not a certificate fault -- proven by testing: `POST /suite-api/api/auth/sources/test` shows the AD Operations link now works, but nothing connects an Operations identity source to VIDB's own access policy. |
| VIDB and the fleet certificates stay VSP-issued (`OU=vcfms`) | They are absent from SDDC Manager's certificate inventory, so `Register-VcfCA.ps1`'s registration cannot reach them. Whether AD CS can re-issue them at all is unresolved. |
| VCF Operations, the Operations Collector, and the License Server are not in SDDC Manager's certificate inventory at all | Verified against the live API: the inventory is exactly `3x ESXI, 2x NSXT_MANAGER, SDDC_MANAGER, VCENTER, VSP`. These three appliances need their own certificate path, not this one. |

## Verification checks that must be validated on the first real run

These could not be exercised without a live CA and are written to fail
closed, but none of them has seen real `certutil` or API output yet. Recheck
each the first time the corresponding script runs against the real CA:

- **`New-VcfCertificateTemplate.ps1`'s ESC1 principal check** now collects
  `Allow <rights> <principal>` ACE lines out of `certutil -v -template VMware`
  directly (the earlier version anchored on the first literal `Enroll`, which
  in real output is inside `msPKI-Enrollment-Flag`, and extracted zero
  principals). It asserts the enrolment-capable set equals
  `knowledgeondemand\svc-vcf-ca` **plus** an explicit allow-list of the
  built-in admin principals (`Domain Admins`, `Enterprise Admins`,
  `BUILTIN\Administrators`, `NT AUTHORITY\SYSTEM`) that hold Full Control on
  every template by default and cannot sensibly be removed. Zero Allow lines
  found is a FAIL, not a pass. The regex has been exercised against
  representative output but not against this CA's real output.
- **The same script's ESC6 check** now reads `$LASTEXITCODE`, parses the
  `EditFlags REG_DWORD = <hex>` value and tests `-band 0x40000`. It no longer
  sniffs text: the previous guard's `\d+` alternative matched
  `CertUtil: -getreg command FAILED: 0x80070002`, so a failed command printed
  `[OK] ESC6 flag not set`. An absent or unparseable value is now "cannot
  verify", which is a FAIL.
- **The same script's DC-template, CA-ACE and AuditFilter checks** are new and
  have never run against a live CA. The CA-ACE check accepts either
  `Request Certificates` or `Enroll` on the `svc-vcf-ca` line of
  `certutil -getreg CA\Security`, because certutil's rendering of
  `CA_ACCESS_ENROLL` varies by version; if neither appears it fails closed.
- **`Publish-LabRootTrust.ps1`'s** `Confirm-ThumbprintPresent` and
  **`Register-VcfCA.ps1`'s** registration both assume a JSON response shape
  for the trust-store `GET`s (`/v1/sddc-manager/trusted-certificates` and
  `/api/vcenter/certificate-management/vcenter/trusted-root-chains`) that
  was never exercised against live data, because no root certificate existed
  yet to publish. The check is fail-closed by construction -- an
  unparseable, empty, or non-matching response is a FAIL, never a silent
  PASS -- but the actual schema is still unconfirmed.

## Why `Register-VcfCA.ps1` prints the proof procedure instead of running it

`GET /v1/certificate-authorities` echoes back whatever was just `PUT` to it
and passes with a wrong password and an unreachable URL -- it is
configuration-time acceptance, not a functional gate. The real gate is
issuing an actual certificate for one low-value resource (one NSX manager)
and confirming it comes back signed by `knowledgeondemand-LabRoot-CA`. That
requires a CSR request body against SDDC Manager's certificate API, and that
body was never established -- probing got as far as discovering a required
`fqdn` field was missing from an early attempt, not a working request.
Fabricating a plausible-looking body would read as authoritative and be
untested, which is worse than an honest gap. So the script prints the manual
procedure (generate the CSR, fulfil it, check
`GET /v1/domains/{id}/resource-certificates` for the one resource that now
shows the lab root as issuer) and lets the operator run it by hand.

## Rotation runbook (after everything above is green)

Task 7 of the plan produced no code -- it is deliberately operational. Do
not run it until steps 1-8 above are complete and `Test-LabCAHealth.ps1`
passes all four gates.

1. **Take backups first.** SDDC Manager backup, plus appliance snapshots of
   vCenter, NSX, SDDC Manager and VSP -- **excluding `dns01`** (see the
   snapshot invariant above). `GET /v1/domains/{id}/resource-certificates`
   is **not** a rollback artefact: it returns public certificates and
   metadata, never private keys, so nothing can be restored from it. The
   snapshots are the only real rollback.
2. **Confirm NTP on all three hosts before issuing anything.** A skewed
   clock produces certificates that fail validation immediately.
3. **Set `vpxd.certmgmt.mode` to `custom` before touching any ESXi host.**
   Left at the `vmca` default, vCenter regenerates VMCA-signed host
   certificates on the next renew or reconnect and silently undoes the
   rotation.
4. **Rotate one resource at a time, in this order:** NSX (both
   certificates) -> VSP -> ESXi (one host at a time, `EnsureAccessibility`
   evacuation mode -- N-1 headroom on a 3-host vSAN) -> vCenter -> **SDDC
   Manager last**, for the ordering reason above.
5. **After each resource, confirm three things, not one:**
   - `GET /v1/domains/{id}/resource-certificates` shows the new issuer.
   - The resource still answers.
   - Every peer that pins its **leaf thumbprint** has been re-registered --
     vCenter<->NSX compute-manager, SDDC Manager's stored vCenter
     thumbprint, vCenter<->ESXi host thumbprints, VCF Operations adapters.
     Root trust (step 7, above) does nothing for these; they pin the leaf,
     not the chain, so distributing the root does not carry them forward.
   - For ESXi specifically, use `Get-HostInventoryDrift` (not
     `Confirm-HostInventorySync`, which repairs by default) **with VMs
     running** -- it compares VM power state and passes vacuously on an
     evacuated host.

## Running `Test-LabCAHealth.ps1` from off-lab

This workstation cannot resolve `dns01.knowledgeondemand.net`. The script
resolves the CA host exactly once, up front, and if that fails it marks all
four gates FAIL rather than let a name-resolution failure be reinterpreted
by a later gate as "refused" or "closed" -- the same false-pass shape gates
3 and 4 guard against internally. Confirmed by a live run from here:

```
FATAL: dns01.knowledgeondemand.net does not resolve from this host
  4 gate(s) failed
```

Run it from inside the lab, or add a hosts entry for `dns01`, before
trusting a FAIL from this gate as evidence of a real problem.

## Files

| File | Purpose |
|---|---|
| `CAPolicy.inf` | install-time CA policy; copied to `C:\Windows` by `Install-LabCA.ps1` |
| `Install-LabCA.ps1` | [OPERATOR] installs ADCS + Web Enrolment, applies `CAPolicy.inf`; requires a typed `CONFIRM` that the `dns01` rollback clone exists, backs up any existing `CAPolicy.inf`, and short-circuits if ADCS is already installed |
| `Set-CAWebEnrollmentHardening.ps1` | [OPERATOR] disables Negotiate (the ESC8 control), enables Basic, EPA=Require on windowsAuth, Require-SSL on `/certsrv`, keeps `/CertEnroll` anonymous on port 80, scopes 443 to SDDC Manager and 80 to the lab subnet, reports broad 443 rules |
| `Set-CaRevocationEndpoints.ps1` | [OPERATOR] points CDP/AIA at the single-homed `pki` name over HTTP; refuses a multi-homed name; read-back verified; restarts certsvc and publishes a CRL |
| `Test-CaRevocationEndpoints.ps1` | [SCRIPTED] reads CDP/AIA out of a real issued certificate, resolves and fetches each, validates the CRL parses and has not expired; exits 2 for INCONCLUSIVE |
| `New-VcfCertificateTemplate.ps1` | [OPERATOR] prints build instructions for BOTH templates (DC + VMware), verifies issuance, cn, EKU, ESC1, DC Autoenroll scoping, ESC6, the CA-level Request Certificates ACE and `CA\AuditFilter` |
| `Test-LabCAHealth.ps1` | [SCRIPTED] read-only gates: LDAPS up, LDAPS bind works, 389 still refuses, and gate 4's three claims -- port 80 OPEN (CDP reachable), `/certsrv` refuses cleartext (not 401), `/CertEnroll` served anonymously |
| `Publish-LabRootTrust.ps1` | [SCRIPTED] pushes the root to vCenter's and SDDC Manager's trust stores, verifies by thumbprint |
| `Register-VcfCA.ps1` | [SCRIPTED] registers the Microsoft CA with SDDC Manager, prints the manual issuance proof |
| `Add-OpsIdentitySource.ps1` | [SCRIPTED] validates then creates the AD identity source in VCF Operations |

## Status

`Test-LabCAHealth.ps1` was run one final time from this workstation for this
task: 4/4 gates FAIL, all attributable to `dns01` not resolving from here
(see above), not to any gate defect. The CA install itself (Task 1) is
complete and committed; template, trust-distribution, registration and AD
identity source scripts (Tasks 2-6, 8) are written, reviewed, and
parse-checked, with the unvalidated checks listed above still pending their
first live run. Task 7 (rotation) has produced no code by design -- follow the
runbook above when the time comes.

The final review round added the step that made the whole plan reachable:
`LoadDefaultTemplates=0` meant the CA published **no** templates, so the DC
would never have autoenrolled and LDAPS would never have started. Publishing
`Domain Controller Authentication` (step 3) is now an explicit, verified step
rather than an assumption.
