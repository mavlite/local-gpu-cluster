# Vendored third-party code

Committed here rather than installed at run time, because the only machine that
needs it is a domain controller. Pinning the exact bytes in git means the DC
never reaches PowerShell Gallery, the version cannot drift under us, and the
code we actually run is reviewable in a diff. This repo already has a scar from
an unpinned dependency — `mcp>=1.2` was unbounded and `mcp 2.0.0` took a service
out — so the default here is: pin it, or don't use it.

## ADCSTemplate 1.0.1.1

| | |
|---|---|
| Source | <https://github.com/GoateePFE/ADCSTemplate> |
| PowerShell Gallery | `ADCSTemplate` 1.0.1.1, published 2024-06-02 |
| Author | Ashley McGlone |
| Licence | MIT (`ADCSTemplate/1.0.1.1/LICENSE`) |
| Obtained | `Save-Module -Name ADCSTemplate` on 2026-10-01 |
| Dependencies | `ActiveDirectory` only (present on a DC) |

SHA-256, first 32 characters:

```
ADCSTemplate.psm1   8A04EF092E3A8C7145C67C67FD3B6FB3   22048 bytes
ADCSTemplate.psd1   C5C200C3476F983A2923FB9A6B5CADC7    9442 bytes
LICENSE             3195308D1E66E3A208CCA8443AED4F2D    1071 bytes
README.md           E5FA98675AE17B8E45A9A1C17988D066   17893 bytes
```

The upstream package also ships `Build-ADCS.ps1`, `Demo.ps1`, two sample JSON
files and a DSC resource. None of those are used, so none are vendored.

### Why this module rather than our own code

`New-VmwareCertTemplate.ps1` needs to create a schema-v2+ certificate template,
and that requires a unique `msPKI-Cert-Template-OID` *plus* a matching
`msPKI-Enterprise-Oid` object under `CN=OID,CN=Public Key Services`, formed as

```
msPKI-Cert-Template-OID : [forest base OID].[random 8 digits].[random 8 digits]
OID object CN           : [same 8 digits].[32 hex characters]
```

uniqueness-checked against the existing OID objects. That is the part a
hand-rolled implementation gets plausibly but silently wrong, and it is already
solved here. Everything specific to this lab — which attributes to change, the
verification, and the issuance proof — lives in our own script, not in the
module.

### Functions used

- `Export-ADCSTemplate` — read the stock Web Server template as JSON
- `New-ADCSTemplate` — create, `-Publish`, and permission via `-Identity`
  (grants **Read and Enroll**, which is exactly VMware's documented table)
- `Get-ADCSTemplate` — read back for verification
- `Remove-ADCSTemplate` — used when a build has to be redone; it removes the
  enterprise-OID object too, which was confirmed by execution

### Updating it

Don't, without a reason. If you do: `Save-Module` the new version into a
scratch directory, diff it against what is here, replace the files, update the
version directory name, the table above and the hashes, and re-run
`New-VmwareCertTemplate.ps1` end to end against a template you then delete.
