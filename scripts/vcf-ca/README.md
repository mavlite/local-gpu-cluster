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

## Ownership split

No credential in `credentials.env` carries Domain Admin, and adding one is a
decision the operator makes deliberately -- so this project is split down the
middle. **[OPERATOR]** scripts must be run by a human, interactively, on
`dns01` itself. **[SCRIPTED]** scripts run from a workstation against SDDC
Manager's or VCF Operations' REST API with credentials already held.

| Script | Ownership | Runs on |
|---|---|---|
| `Install-LabCA.ps1` | **[OPERATOR]** | `dns01` |
| `Set-CAWebEnrollmentHardening.ps1` | **[OPERATOR]** | `dns01` |
| `New-VcfCertificateTemplate.ps1` | **[OPERATOR]** | `dns01` (template build is manual in `certtmpl.msc`; the script prints instructions, then verifies) |
| `Test-LabCAHealth.ps1` | **[SCRIPTED]** | workstation |
| `Publish-LabRootTrust.ps1` | **[SCRIPTED]** | workstation |
| `Register-VcfCA.ps1` | **[SCRIPTED]** | workstation |
| `Add-OpsIdentitySource.ps1` | **[SCRIPTED]** | workstation |
| Certificate rotation (Task 7, no script) | mixed -- see [Rotation runbook](#rotation-runbook-after-everything-above-is-green) | `dns01` + workstation |

## Order, and why it is the order

| # | Step | Script |
|---|---|---|
| 1 | Install the CA | `Install-LabCA.ps1 -Apply` |
| 2 | Harden web enrolment | `Set-CAWebEnrollmentHardening.ps1 -Apply` |
| 3 | Wait for DC autoenrolment (~90-120 min GPO cycle), then confirm | `Test-LabCAHealth.ps1` |
| 4 | Bind the DC's certificate to IIS on 443 | manual, then `New-VcfCertificateTemplate.ps1` builds the template |
| 5 | Create + verify the `VMware` template | `New-VcfCertificateTemplate.ps1 -Apply` |
| 6 | Distribute the root to vCenter + SDDC Manager | `Publish-LabRootTrust.ps1 -Apply` |
| 7 | Register the CA with SDDC Manager | `Register-VcfCA.ps1 -Apply` |
| 8 | Rotate the eight SDDC-managed certificates | manual runbook, see below |
| 9 | Add AD as an identity source in VCF Operations | `Add-OpsIdentitySource.ps1 -Apply` |

Two ordering constraints in here are load-bearing, not just tidy:

- **Root trust must be distributed (step 6) before any certificate is
  rotated (step 8), or the management plane partitions.** A resource
  re-issued from the lab CA before vCenter and SDDC Manager trust that CA's
  root presents a chain nothing else validates -- the exact failure mode this
  project exists to avoid, just moved one certificate later.
- **SDDC Manager is rotated LAST in step 8, after vCenter.** SDDC Manager is
  the *instrument* that rotates vCenter's certificate. Rotating SDDC
  Manager's own certificate first restarts it mid-sequence and invalidates
  every in-flight task and token the rotation of the other seven resources
  depends on.

Step 9 is last for a narrower reason: it is the original goal (AD identity in
VCF Operations), and everything from step 1 onward exists only to make LDAPS
real enough for `Add-OpsIdentitySource.ps1`'s validation call to pass.

## Four settings CAPolicy.inf locks in at install time

`Install-LabCA.ps1` copies `CAPolicy.inf` to `C:\Windows` and installs from
it. These four values cannot be changed afterward without uninstalling the
CA, rebuilding the machine, and re-seeding every trust store in step 6 by
hand:

| Setting | Value | Why |
|---|---|---|
| Root validity | 10 years | The AD CS default is 5. AD CS silently *truncates* any certificate template whose validity outlives the CA's remaining life -- a 2-year template starts shortening from year 3, and the whole fabric would expire and cascade-renew at year 5. |
| CRL period | 52 weeks | The default is 1 week. A DC powered off for a planned maintenance window over 2 weeks would stop publishing CRLs, and strict validators (VCF LCM included) hard-fail on a CRL whose `nextUpdate` has passed. |
| Key length | RSA 2048 | Minimum acceptable, standard, widely supported. |
| Hash algorithm | SHA-256 | Standard; `AlternateSignatureAlgorithm` (CMS format) is explicitly left off. |

`LoadDefaultTemplates=0` is also set (suppresses the Domain Controller /
Machine / User default templates and the PetitPotam relay surface they carry
via autoenrolled Client Authentication), but it is not in this "cannot
change" list the same way -- templates can be issued or unissued by hand at
any time after install.

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

## Four verification checks that must be validated on the first real run

These could not be exercised without a live CA and are written to fail
closed, but none of them has seen real `certutil` or API output yet. Recheck
each the first time the corresponding script runs against the real CA:

- **`New-VcfCertificateTemplate.ps1`'s ESC1 principal check** assumes
  indented lines under `Enroll` in `certutil -v -template VMware` output
  carry principal names in `domain\account` or SID form. If the real output
  is formatted differently, the check reports "cannot verify" and fails
  closed -- but that has not been proven against a real template.
- **The same script's ESC6 check** looks for `EDITF_ATTRIBUTESUBJECTALTNAME2`
  in `certutil -getreg policy\EditFlags` output. An EditFlags value that is
  **absent entirely** is not the same thing as a value that explicitly
  clears the flag, and the check has not seen either case for real.
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
not run it until steps 1-7 above are complete and `Test-LabCAHealth.ps1`
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
     Root trust (step 6, above) does nothing for these; they pin the leaf,
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
| `Install-LabCA.ps1` | [OPERATOR] installs ADCS + Web Enrolment, applies `CAPolicy.inf` |
| `Set-CAWebEnrollmentHardening.ps1` | [OPERATOR] Basic auth + EPA on `/CertSrv`, unbinds port 80, scopes firewall to SDDC Manager |
| `New-VcfCertificateTemplate.ps1` | [OPERATOR] prints template build instructions, verifies EKU / ESC1 / ESC6 post-build |
| `Test-LabCAHealth.ps1` | [SCRIPTED] four read-only gates: LDAPS up, LDAPS bind works, 389 still refuses, port 80 closed |
| `Publish-LabRootTrust.ps1` | [SCRIPTED] pushes the root to vCenter's and SDDC Manager's trust stores, verifies by thumbprint |
| `Register-VcfCA.ps1` | [SCRIPTED] registers the Microsoft CA with SDDC Manager, prints the manual issuance proof |
| `Add-OpsIdentitySource.ps1` | [SCRIPTED] validates then creates the AD identity source in VCF Operations |

## Status

`Test-LabCAHealth.ps1` was run one final time from this workstation for this
task: 4/4 gates FAIL, all attributable to `dns01` not resolving from here
(see above), not to any gate defect. The CA install itself (Task 1) is
complete and committed; template, trust-distribution, registration and AD
identity source scripts (Tasks 2-6, 8) are written, reviewed, and
parse-checked, with the four unvalidated checks listed above still pending
their first live run. Task 7 (rotation) has produced no code by design --
follow the runbook above when the time comes.
