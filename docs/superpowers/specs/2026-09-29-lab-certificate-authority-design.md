# Lab certificate authority — design

**Status:** revision 2, after architectural, security and operational review
**Scope:** an internal CA for the VCF lab, and the certificates issued from it
**Immediate driver:** the AD domain controller cannot serve LDAPS, which is
**proven** to block identity in VCF Operations, and assumed to block VIDB

## Purpose

Give the lab one certificate authority that issues to every management endpoint
and distributes its root to domain members automatically.

Revision 2 corrects an overclaim. The first draft promised renewal "without
anybody remembering to". That is true only for `dns01` itself, which AD CS
autoenrols. **SDDC Manager's Microsoft CA integration automates issuance and
installation on demand; it does not schedule renewal.** At roughly month 24 the
whole rotation must be repeated by hand, including the vCenter step this design
calls its riskiest action.

Today there are eight VCF certificates from vCenter's VMCA, a VIDB certificate
from a third source, and a domain controller with no certificate at all.

## Why not Let's Encrypt or Cloudflare

`knowledgeondemand.net` is on Cloudflare (`ace.ns`/`braelyn.ns.cloudflare.com`),
there is no CAA record, and no lab name resolves publicly. DNS-01 would work:
Let's Encrypt never contacts the host it certifies. Feasibility is not the
blocker.

The blocker is that **VCF has no ACME support**. SDDC Manager speaks exactly
two dialects, `microsoftCertificateAuthoritySpec` and
`openSslCertificateAuthoritySpec` — verified by probing the live API.

The honest comparison, corrected: **one manual rotation every 2 years with
in-product CSR orchestration, versus one every 90 days across eleven endpoints
with orchestration we build ourselves.** Not automatic versus manual. The
decision still holds on that ratio, and on the fact that AD CS is the only
option that can autoenrol the DC certificate this project exists to obtain.

Cloudflare Origin CA was considered and rejected: it cannot autoenrol a DC
certificate, VCF cannot consume it natively, and revocation is weak. The first
draft also argued "the root still needs distributing by hand" — that argument is
withdrawn, because it is equally true of AD CS for every appliance here.

## Architecture

A **single-tier Enterprise Root CA** on `dns01` (`172.16.10.150`,
`DC=knowledgeondemand,DC=net`).

**The domain functional level is 10, which is Windows Server 2025** — not 2016,
as revision 1 stated. This is load-bearing. Server 2025 enforces LDAP signing
and channel binding by default, which is the *expected* explanation for the
"Strong authentication is required" rejection on port 389. That behaviour is
correct, will persist after this work, and must not be relaxed.

Two-tier is rejected for a lab, with one correction: this binds for the life of
the trust fabric. A single-tier root cannot later be promoted; you build a new
root and re-seed every trust store populated by hand in step 5.

**Placement on the DC is an accepted compromise**, with costs beyond lifecycle
coupling:

- The root key is online on the forest's highest-value asset, and an Enterprise
  CA is placed in `NTAuthCertificates` — it can mint credentials AD accepts for
  authentication. "A lab CA is a real CA" understates this.
- It requires IIS and Web Enrolment **on a domain controller**, which puts an
  HTTP application stack inside the LSASS/NTDS trust boundary.
- **`dns01` cannot be snapshot-reverted once the CA exists.** Reverting rolls
  the CA database back, reusing serial numbers already issued and trusted. The
  lab's most natural rollback tool becomes forbidden for this one VM.
- Single fault domain: DNS, AD, LDAPS, CA, CRL and AIA all fail together.

### Consumers

| Consumer | Certified how | Ongoing effort |
|---|---|---|
| `dns01` LDAPS | AD CS autoenrolment, but **only after an operator publishes a DC template** (see below) | automatic once published |
| `dns01` IIS `/certsrv` | issued from this CA, bound to 443 manually | one-time |
| 8 VCF resources | SDDC Manager, `PUT /v1/certificate-authorities` | one manual rotation per 2 years |
| VCF Operations, Collector, License Server | **not** in SDDC Manager's inventory | unresolved |
| VIDB / fleet | VSP-issued (`OU=vcfms`) | unresolved |
| Domain members | automatic via Public Key Services — **no GPO to author** | none |
| Appliances | manual root import, one-time | one-time |
| ESXi | via vCenter's `TRUSTED_ROOTS`, **not** per-host | one-time |

#### The DC template must be published by hand — decision and rationale

`CAPolicy.inf` sets `LoadDefaultTemplates=0`, so the CA publishes **no**
templates on promotion. Autoenrolment for the DC is therefore not automatic in
the way this table originally claimed: with nothing published, `dns01` never
enrols, LDAPS never starts, and the project's whole objective is unreachable.

**Decision:** publish the standard **`Domain Controller Authentication`**
template after install, and grant **Autoenroll to the `Domain Controllers`
group only** — never `Domain Computers`, never `Authenticated Users`.
`LoadDefaultTemplates=0` stays as it is; its job is suppressing the dozen other
defaults, not suppressing this one.

**Why that is sound, given the PetitPotam/ESC8 reasoning that motivated
`LoadDefaultTemplates=0` in the first place:** ESC8 is a *relay* attack against
the web-enrolment endpoint, and it is closed on the endpoint, not on the
template. `Set-CAWebEnrollmentHardening.ps1` disables Negotiate on `/CertSrv`
(with no NTLM accepted there is nothing to relay), requires SSL on `/CertSrv`
so no credential crosses cleartext, and scopes inbound 443 to SDDC Manager plus
one optional admin address. Separately, enrolment on the DC template is restricted
to the `Domain Controllers` group, so the coercion target set is the three DCs
that already hold DC certificates by definition — coercing one buys an attacker
nothing it did not already have. Suppressing the template would not have closed
ESC8 anyway; it would only have broken LDAPS.

The 8 SDDC-managed resources, verified by querying the live API, are:
**3× ESXI, 2× NSXT_MANAGER, SDDC_MANAGER, VCENTER, VSP.** VCF Operations, the
Operations Collector and the License Server are **absent** — so the endpoint
this design exists to unblock is not certificate-managed by SDDC Manager, and
needs its own path.

**Correction, revision 3 — `/CertEnroll` must stay anonymous on port 80.**
Revision 2 had the hardening script remove the port 80 binding outright and
enable Basic authentication on `/CertEnroll` as well as `/CertSrv`. Both were
wrong, and together they would have broken revocation checking for the entire
fabric.

`/CertEnroll` is not an ESC8 surface. ESC8 relays NTLM to an endpoint that
*authenticates*; `/CertEnroll` is a static directory holding the CRL and the CA
certificate — signed objects — and accepts no credentials, so there is nothing
there to relay. Putting Basic auth on it does not harden it; it makes every CRL
fetch answer `401`, and no consumer sends credentials when fetching a CRL.
Removing the port 80 binding has the same effect by a different route.

The cleartext protection belongs on `/CertSrv` alone, as a Require-SSL flag.
IIS evaluates the SSL requirement *before* issuing an authentication challenge,
so an HTTP request to `/CertSrv` answers `403.4` and is never offered Basic —
the same protection the port-80 removal gave, without taking `/CertEnroll` with
it.

**CDP and AIA are plain HTTP by design, not by omission.** Serving them over
HTTPS is circular: validating the HTTPS certificate requires fetching a CRL,
which would require validating an HTTPS certificate. This is why port 80 is
allowed from the lab subnet (`Set-CAWebEnrollmentHardening.ps1
-CrlConsumerSubnet`) while 443 stays scoped to SDDC Manager.

**Correction, revision 3 — the CA may not use its own hostname for CDP/AIA.**
AD CS builds its HTTP CDP and AIA from the CA server's DNS name, which here is
`dns01.knowledgeondemand.net` — and that resolves to **two** addresses,
`172.16.10.150` (lab) and `192.168.6.197` (management). An appliance that
round-robins onto the management address cannot fetch the CRL, and strict
validators hard-fail on a CRL they cannot retrieve. `Set-CaRevocationEndpoints.ps1`
points both at `pki.knowledgeondemand.net`, an A record bound to the lab
address only, and refuses to run if that name resolves to more than one
address. A CNAME to `dns01` would inherit the defect it exists to remove.

This must happen between install and first issuance: a certificate carries the
URLs configured at the moment it was signed, and no later change repairs an
already-issued certificate.

### The VCF certificate template

Contract established by probing the live API:

```
PUT /v1/certificate-authorities
{"microsoftCertificateAuthoritySpec": {
    "serverUrl":    "https://dns01.knowledgeondemand.net/certsrv",
    "username":     "knowledgeondemand\\svc-vcf-ca",
    "secret":       "read from AD_CA_ENROLL_PASS at run time, never literal here",
    "templateName": "VMware"}}
```

All four fields are mandatory. **`templateName` must be the template's `cn`,
not its display name** — a mismatch fails at certificate request time, not at
configuration time.

CA name `knowledgeondemand-LabRoot-CA`, template `VMware`, enrolment account
`knowledgeondemand\svc-vcf-ca`, distinct from the read-only `svc-vcf-ldap`.
The CA name is baked into every certificate and renaming afterwards is fragile,
so it is as much a decision as the template name.

**Template EKU — settled by measurement, not by argument.** Security review
argued for Server Authentication only, to avoid the ESC1 pattern that
"subject supplied in the request" creates. Architectural and operational review
argued VCF requires Client Authentication. The certificates VCF is *actually
using today* decide it:

| Endpoint | EKU in use |
|---|---|
| SDDC Manager | Server Auth **+ Client Auth** |
| NSX vip and node | Server Auth **+ Client Auth** |
| VSP platform | Server Auth only |
| vCenter | **no EKU extension** — any purpose |

Client Authentication is therefore required in practice. Revision 1's
Server-Auth-only claim is withdrawn. Note that vCenter's key usage is Digital
Signature, Non Repudiation and Key Encipherment — **not** Data Encipherment, so
the review claim that Data Encipherment is required is unsupported by the
evidence and is treated as unverified.

**Consequence, stated plainly:** with Client Auth plus subject-in-request, the
only control left against escalation is enrolment rights. `svc-vcf-ca` becomes
a credential that can mint certificates AD will accept for authentication, and
a copy of it lives inside SDDC Manager. **Compromise of SDDC Manager must be
treated as domain escalation.** Compensating controls are mandatory, not
optional — see Security requirements.

Template settings: cloned from Web Server, subject supplied in request, Server
and Client Authentication, 2-year validity, enrol permission for `svc-vcf-ca`
only, manager approval off (the automation requires it), and a compatibility
level low enough to avoid CNG/KSP constraints that reject web-enrolment CSRs.

### Credentials

`AD_CA_ENROLL_USER` (`knowledgeondemand\svc-vcf-ca`) and `AD_CA_ENROLL_PASS` in
`C:\Users\willi\.vcflab\credentials.env`, matching the lab's existing
convention: outside git, owner-only, read per invocation, never on a command
line, never in a transcript. The account must be password-never-expires —
otherwise every future rotation fails with an authentication error long after
anyone remembers the account exists — with a recorded manual rotation cadence.

## Install-time decisions that cannot be changed later

Write `CAPolicy.inf` **before** installing. Each of these is one-way without
re-rooting and re-seeding every trust store:

- **Root validity: 10–20 years.** The default is 5. AD CS silently truncates any
  certificate whose validity exceeds the CA's remaining life, so a 2-year
  template starts quietly shortening from year 3 and the entire fabric expires
  at once at year 5.
- **CRL period: 52 weeks with generous overlap.** The default week means a DC
  powered off for a fortnight stops publishing, and strict validators hard-fail.
- **CDP/AIA published over HTTP** at a name every appliance resolves. Defaults
  include `ldap:///` URLs that Photon appliances and ESXi cannot use. Note the
  lab spans two zones — `dns01.knowledgeondemand.net` and
  `idb.lab.knowledgeondemand.net` — so confirm appliances resolve the parent.
- **RSA 2048 minimum, SHA-256.** ESXi 9 and VCF 9 reject SHA-1 and weaker keys.

## Security requirements

These are requirements, not recommendations. Each closes a live attack path.

- **Extended Protection for Authentication set to Require** on the CertSrv and
  CertEnroll virtual directories, and **port 80 unbound**. Web enrolment with
  Basic or Integrated auth is the ESC8 NTLM-relay target. Without EPA this is
  exploitable the moment AD CS is installed. Basic auth over plain HTTP would
  also put `svc-vcf-ca`'s password on the wire in base64 on every request.
- **Firewall the CertSrv virtual directories** to SDDC Manager's address and the
  admin workstation, not the whole /24. `dns01` has **no web stack today** —
  ports 80 and 443 are both closed — so this genuinely opens new surface rather
  than hardening existing surface.
- **Audit what is published — there is nothing to prune.** `LoadDefaultTemplates=0`
  means no template is auto-published at Enterprise CA promotion, so the earlier
  instruction to "prune the default templates auto-published at promotion" is
  wrong: they were never published. The requirement inverts. Exactly two
  templates get published, both by hand, and each must be audited *as published*:
  `Domain Controller Authentication` (**Autoenroll to `Domain Controllers` only**)
  and `VMware` (**Enroll to `knowledgeondemand\svc-vcf-ca` only**). Enumerate with
  `certutil -CATemplates` and confirm nothing else appears. The PetitPotam →
  relay → PKINIT concern is addressed at the web-enrolment endpoint (Negotiate
  disabled, HTTPS-only, scoped firewall), not by withholding the DC template —
  see "The DC template must be published by hand" above.
- **Verify, post-build, on the live CA** — not in this document: template EKU
  list, template enrolment ACL contains no `Authenticated Users` or
  `Domain Users`, and `certutil -getreg policy\EditFlags` does **not** have
  `EDITF_ATTRIBUTESUBJECTALTNAME2` set (ESC6, which would widen every template).
- **No name constraint is enforceable** on subject-in-request issuance, so
  `svc-vcf-ca` can request a certificate for any hostname. Mitigation is
  detective, not preventive: enable CA auditing and review `certutil -view`
  for CNs outside the expected set.
- `svc-vcf-ca` needs **Request Certificates** on the CA itself (CA properties →
  Security), separate from template Enroll, and should be denied interactive
  and remote-desktop logon by GPO.

## Sequence

1. **Write `CAPolicy.inf`, then install AD CS** as an Enterprise Root CA with
   the **Certification Authority Web Enrolment** role service.
   *Check:* `pKIEnrollmentService` exists under
   `CN=Enrollment Services,CN=Public Key Services,CN=Services,CN=Configuration,DC=knowledgeondemand,DC=net`
   and the root appears under `CN=Certification Authorities` and
   `CN=NTAuthCertificates` in the same subtree. Confirm the root's validity and
   CRL period match `CAPolicy.inf` — this is the only moment they are cheap to
   fix.

2. **Configure `/certsrv`:** enable IIS **Basic Authentication** on the CertSrv
   application (the stock site is Windows/Negotiate, which SDDC Manager cannot
   speak), apply EPA, unbind port 80.

3. **Confirm the DC autoenrolled.** Autoenrolment fires on a Group Policy cycle
   (90–120 minutes), not at install. Force it with `certutil -pulse`; Schannel
   may need a beat or an NTDS restart before presenting the certificate.
   *Check:* `openssl s_client -connect dns01.knowledgeondemand.net:636 -CAfile <root>`
   — **by FQDN, with explicit validation.** Testing by IP passes against a
   certificate whose SAN carries only the FQDN, then fails for any
   name-validating client. `certutil -dcinfo verify` as a second opinion.

4. **Bind a certificate from this CA to IIS on 443**, so
   `https://dns01.../certsrv` presents a valid chain. Without this, step 6 fails
   with a TLS error that reads like a credential problem.

5. **Distribute the root.** Domain members are automatic via Public Key
   Services — **no GPO to author**. Verify the root's SHA-256 thumbprint
   out-of-band against `certutil -dump` rather than trusting whatever an import
   fetches.

   Two writable trust paths were confirmed against the live lab, and **neither
   needs a VIDB token**:

   - **vCenter:** `POST /api/vcenter/certificate-management/vcenter/trusted-root-chains`
     with `{"spec": {...}}`, authenticated by vCenter SSO. Currently holds one
     entry, the VMCA root `BA7EEC554D199A3E29FA5107D17E16A7A9D521D6`. ESXi
     inherits from here, not per host.
   - **SDDC Manager:** `POST /v1/sddc-manager/trusted-certificates` with
     `{"certificate": "<PEM>"}`. Verified routed — an invalid PEM reaches
     certificate validation rather than a 404. Its 171 entries carry only
     `alias` and `certificate`, **no distribution scope**, so this store is
     SDDC Manager's own trust and nothing else's.

   NSX takes its own import. VSP is the unresolved one — see Open questions.

   *Check:* a per-appliance trust assertion, one command each, not a checkbox.

6. **Register the CA with SDDC Manager.**
   *Check:* **not** `GET /v1/certificate-authorities` — that echoes what was
   just stored and passes with a wrong password and an unreachable URL. The
   meaningful gate is to **request one certificate for one low-value resource
   and see it come back signed.**

7. **Set `vpxd.certmgmt.mode` to `custom`** before touching any ESXi host.
   Left at the `vmca` default, vCenter regenerates VMCA-signed host
   certificates on the next renew or reconnect and silently undoes the work.

8. **Take backups before rotating anything:** SDDC Manager backup plus
   appliance snapshots. This is the actual rollback, and it must exist before
   step 9 begins. `dns01` itself is excluded — see Architecture.
   Confirm NTP is healthy on all three hosts first; a skewed clock produces
   certificates that fail validation immediately.

9. **Rotate, one resource at a time.** Order corrected from revision 1:
   **NSX (both certificates), VSP, ESXi (one host at a time,
   `EnsureAccessibility`, N-1 headroom on a 3-host vSAN), then vCenter, then
   SDDC Manager last.** Revision 1 put SDDC Manager before vCenter, which is
   wrong: SDDC Manager is the *instrument* that rotates vCenter, and rotating
   its own certificate restarts it and invalidates in-flight tasks and tokens.
   *Check after each:* `GET /v1/domains/{id}/resource-certificates` shows the
   new issuer, **and** the resource still answers, **and** peers that pin its
   thumbprint have been re-registered.

10. **Return to the original goal:** add AD as an identity source in VCF
    Operations, configured **by FQDN over 636**, then retest the VIDB login.

    This step is fully specified programmatically. The contract comes from the
    appliance's own REST reference at `/suite-api/docs/rest/index.html`, not
    from guesswork:

    ```
    POST /suite-api/api/auth/sources
    {"name": "knowledgeondemand-AD",
     "sourceType": {"id":"ACTIVE_DIRECTORY","name":"ACTIVE_DIRECTORY",
                    "others":[],"otherAttributes":{}},
     "others": [], "otherAttributes": {}, "certificates": [],
     "property": [{"name":"host","value":"dns01.knowledgeondemand.net"},
                  {"name":"domain","value":"knowledgeondemand.net"},
                  {"name":"use-ssl","value":"true"},
                  {"name":"port","value":"636"},
                  {"name":"base-domain","value":"dc=knowledgeondemand,dc=net"},
                  {"name":"common-name","value":"sAMAccountName"},
                  {"name":"user-name","value":"knowledgeondemand\\svc-vcf-ldap"},
                  {"name":"password","value":"from AD_BIND_PASS at run time"}]}
    ```

    The `others: []` / `otherAttributes: {}` members are load-bearing — a
    `sourceType` without them is rejected as null. Properties are name/value
    pairs under `property`, not top-level fields.

    **Validate before creating.** `POST /suite-api/api/auth/sources/test` takes
    the identical body and creates nothing. Use it as the gate.

    With SSL enabled the create call returns the certificates it found, and the
    source is not usable until a follow-up `PATCH /suite-api/api/auth/sources`
    supplies the certificate details. Budget for two calls, not one.

**Root distribution before rotation is necessary but not sufficient.** It
addresses chain trust only. vCenter↔NSX compute-manager registration, SDDC
Manager's stored vCenter thumbprint, vCenter↔ESXi host thumbprints and VCF
Operations adapters all pin **leaf thumbprints**, which a trusted root does
nothing for. Each needs its own re-acceptance step after rotation.

## Risks

**Rotating vCenter is the riskiest action here**, and its blast radius is wider
than revision 1 stated: every peer pinning its thumbprint must be re-registered,
not just SDDC Manager.

**ESXi rotation interacts with a known fragility.** On 2026-09-29 `vpxa` on
hyp03 wedged and vCenter's inventory went stale. Certificate replacement
restarts hostd and vpxa and disconnects the host. Two cautions on the check:
`Confirm-HostInventorySync` **repairs by default** — it calls `Repair-HostVpxa`
— so use `-ReportOnly` for a gate, and it returns a boolean, not a drift count;
`Get-HostInventoryDrift` returns counts. More importantly it compares **VM
power state**, so on an evacuated host it sees nothing to disagree about and
passes vacuously — exactly the condition ESXi rotation creates. It is not a
valid gate here.

`2026-09-21-esx-provisioning-design.md` and commit `2e4f24c` record that hosts
served a stale certificate until rebooted. "The resource still answers" would
have passed in that exact failure.

**Every rotation drops sessions** — UI, API tokens, PowerCLI connections — and
fails in-flight SDDC Manager tasks. Re-authenticate between checks.

**If SDDC Manager is compromised, treat `svc-vcf-ca` and the CA's issuance log
as compromised**: rotate the account, audit `certutil -view` for unexpected CNs.

**A single-tier root cannot be revoked.** Key compromise means replacing every
leaf and re-seeding the root on every appliance and host by hand. Accepted.

## If the CA is lost

Revision 1 said a DC rebuild "invalidates every certificate issued here". That
is wrong, and the correct shape matters because the real symptom arrives late:

1. **Immediately:** nothing breaks. Existing leaves keep validating.
2. **Within the CRL period:** publication has stopped, so strict validators
   begin hard-failing — the first visible symptom, disconnected from the cause.
3. **Staggered over 2 years:** each leaf expires with no way to renew,
   including `dns01`'s own LDAPS certificate, re-blocking this project.
4. **A rebuilt CA with the same name but a new key is a different root.** Old
   leaves do not chain to it.

Back up the CA private key, the database, **and
`HKLM\SYSTEM\CurrentControlSet\Services\CertSvc\Configuration`** — without the
registry hive a restore does not reconstitute a working CA. Do this on a
schedule, not only during planned maintenance.

## Rollback

- Steps 1–4 are **not** the no-op revision 1 claimed. Installing an Enterprise
  Root CA writes CA, enrolment, AIA and NTAuth objects forest-wide and pushes
  the root to domain members. Uninstalling does not retract cached trust or
  clean NTAuth; that is manual.
- Step 5 adds trust; removing the root from a store undoes it.
- Step 6 is `DELETE /v1/certificate-authorities/Microsoft` — **the type segment
  is required**; the bare path returns 400, verified.
- **Step 9 is not reversible from API data.**
  `GET /v1/domains/{id}/resource-certificates` returns the public certificate
  and metadata, **not the private key**. Revision 1 presented it as a restore
  artefact; it is not. The real reversal is restoring the step 8 snapshot, or
  re-issuing a fresh VMCA certificate — a *different* certificate requiring the
  same peer re-registration as the forward direction. Rollback is a second
  rotation with the same blast radius, not an undo.

## Verification

- `openssl s_client -connect dns01.knowledgeondemand.net:636 -CAfile <root>`
  validates, **by FQDN**, from a non-domain-joined client
- `svc-vcf-ldap` completes a simple bind over 636 and reads the directory;
  port 389 still refuses simple binds, which is correct and expected
- `GET /v1/domains/{id}/resource-certificates` shows all 8 issued by the lab
  root, none by `CN=CA`
- Every rotated resource answers, and each thumbprint-pinning peer is
  re-registered
- `Get-HostInventoryDrift` reports zero drift with VMs running, not on a
  quiesced cluster
- VCF Operations lists an ACTIVE_DIRECTORY source, by FQDN, that resolves a user
- The VIDB UI renders a login instead of *"Invalid access policy"*

## Out of scope

Public certificates (Cloudflare and Let's Encrypt keep the tunnel and public
edge), Logs and Networks certificates until those are deployed, two-tier PKI,
HSM-backed keys, and OCSP.

## Open questions

**Can the VSP trust store be seeded at all?** Investigated 2026-09-29. The risk
is **materially reduced but not eliminated**, and the residue is worth stating
precisely.

Established:

- SDDC Manager reaches VSP without a VIDB token — `GET /v1/vsp-clusters`
  returns the cluster, `platformFqdn platform.lab.knowledgeondemand.net`.
- VSP is one of the 8 resources in SDDC Manager's certificate inventory
  (`resourceType: VSP`), so SDDC Manager owns replacement of VSP's *own*
  certificate.
- vCenter's trusted-root-chains store is readable and **writable with vCenter
  SSO credentials alone**.
- SDDC Manager's trust store is writable, but carries no distribution scope, so
  it trusts on SDDC Manager's behalf only.

Not established: **that adding the root to vCenter propagates to the Supervisor
control-plane nodes.** It is the plausible mechanism — the Supervisor is
deployed and managed by vCenter, as ESXi trust already is — but it was not
proven, and no VSP-side API is reachable to confirm it directly.

Consequence for sequencing: the plan no longer looks likely to strand, because
every write needed is reachable with credentials we hold. The check at step 5
must still *assert* VSP trust rather than assume it, and if VSP turns out not to
inherit, the fallback is to rotate everything except VSP and leave it on its
VSP-issued certificate — the same fallback already accepted for VIDB and the
fleet.

**How are VCF Operations, the Collector and the License Server certified?** They
are absent from SDDC Manager's inventory. VCF Operations is the endpoint this
design exists to unblock.

**Can VIDB and the fleet be re-issued from AD CS?** Unknown, same token problem.
If the answer is no, they keep VSP-issued certificates and the design still
meets its purpose.

**Is the causal chain in the header actually established?** Partly — tested
2026-09-29 against the live appliance, and the answer changed.

**First link: PROVEN.** VCF Operations cannot consume this AD until LDAPS
works. Using `POST /suite-api/api/auth/sources/test`, which validates without
creating, an ACTIVE_DIRECTORY source failed the bind on **both** transports and
with every account-name form tried — `DOMAIN\sam`, bare `sAMAccountName` and
the full DN, over 389 and 636. That the failure is real and not an artefact was
established by control: the same token returns 200 on `GET auth/sources`, and a
deliberately bogus `sourceType` returns a clean `400 Invalid source type`, so
the AD body reaches the bind and the bind is what fails. This matches the two
independently observed causes — 389 refuses simple binds under Server 2025
signing enforcement, and 636 presents no certificate.

So the CA is genuinely required for AD identity. That is no longer an
assumption.

**Second link: researched 2026-09-29, and the evidence points AGAINST it.**

Nothing in the deployment path configures VIDB's access policy:

- **VIDB's own configuration schema** (`configuration-schema-vidb-9.1.1.0.25679886.yaml`,
  read from the depot) contains only `ingress`, `size` and `storage`. No
  identity source, no directory, no policy.
- **The VCF deployment spec** carries `vidbSpec` as `{"hostname": ...}` and
  nothing else — see `vcf-spec-tools/vcfspec/render.py`. Bring-up places VIDB;
  it does not configure its identity.
- **VCF Operations**, which manages VIDB as `VMWARE_INFRA_MANAGEMENT`
  (`VMSP_VIDB_INSTANCE`), collects exactly three properties about it, all
  certificate-expiry. It has no visibility of VIDB's policy.
- The error string appears in **no client bundle** — it is generated
  server-side — and every VIDB policy API (`/acs/rulesets`, `/acs/rules`,
  `/acs/associations`) returns 401.

So VIDB's access policy is day-2 configuration with no automated path found,
and **no established mechanism connects an Operations identity source to it**.
The one thing that would connect them — Operations' `VIDB` source type — needs
`client-id`, `client-secret`, `issuer-url` and `tenant`, and those can only come
from a VIDB that already works. The circularity is structural, not a missing
credential.

**Consequence for this design:** the CA is still required and still correct —
the first link is proven. But it should **not** be expected to unblock VIDB or
fleet-lcm. Step 10's second half may well fail with every other step green, and
if it does, the cause is not certificates. Deploying Logs and Networks probably
needs a route other than fleet-lcm; VCF Operations, which already manages both
VIDB and the fleet, is the place to look once AD identity works there.

A cheaper test was attempted and is a dead end: the `VC` source type, which
would use vCenter SSO and need no LDAPS at all, returns **HTTP 500** from
`POST auth/sources` even with the vendor's exact documented shape. It appears
not to be creatable through this API.

**Note for the bind account:** `svc-vcf-ldap` has **no userPrincipalName set**,
so UPN-form authentication cannot work against it. Either set one or use
`DOMAIN\sAMAccountName`, and pair it with `common-name: sAMAccountName` rather
than `userPrincipalName`.
