<#
.SYNOPSIS
    Reads the CA's own request table. Run this ON the CA host (dns01).

.DESCRIPTION
    Answers the one question VCF cannot: did a certificate request ever REACH
    the CA, and what did the CA do with it?

    This matters because an accepted replacement request is not an issued
    certificate. A replacement against the VCF Operations Cloud Proxy was
    accepted by the API and never produced a CSR at all -- its certificate
    belongs to its own self-signed tunnel PKI, not the fleet PKI. From the VCF
    side that is indistinguishable from a slow task. From here it is obvious:
    no new row in the request table means nobody asked.

    The CA request table is authoritative in a way the VCF inventory is not:
    it records failures and denials, which the fleet view never surfaces.

    Read-only. Runs nothing that changes CA state.

.PARAMETER Last
    How many of the most recent requests to show.

.PARAMETER SinceMinutes
    Only show requests submitted within this many minutes. 0 means no limit.

.PARAMETER FailuresOnly
    Show only failed, denied and pending requests.

.NOTES
    Dispositions: 9 pending, 20 issued, 21 revoked, 30 failed, 31 denied.

    This script must run on the CA host. It cannot run from a workstation:
    reading the request table needs local access to the CA service, and the
    lab's workstation cannot even resolve lab names (OpenVPN's
    block-outside-dns hijacks port 53).

.EXAMPLE
    .\Get-CaIssuanceLog.ps1
    The 20 most recent requests, with disposition.

.EXAMPLE
    .\Get-CaIssuanceLog.ps1 -SinceMinutes 30
    Did anything arrive in the last half hour? Use this right after a VCF
    certificate replacement to tell "still running" from "never asked".

.EXAMPLE
    .\Get-CaIssuanceLog.ps1 -FailuresOnly
#>
[CmdletBinding()]
param(
    [int]$Last = 20,
    [int]$SinceMinutes = 0,
    [switch]$FailuresOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$DispositionName = @{
    9  = 'pending'
    15 = 'CA cert'
    16 = 'CA cert chain'
    20 = 'ISSUED'
    21 = 'revoked'
    30 = 'FAILED'
    31 = 'DENIED'
}

function Write-Head { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Line { param([string]$m) Write-Host "  $m" }

# certutil writes to stderr on non-fatal conditions and sets a non-zero exit
# code for "no rows", which is not an error here. Never let that abort the run.
function Invoke-CertUtil {
    param([string[]]$Arguments)
    $prev = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $out = & certutil @Arguments 2>&1
        return @{ Lines = @($out | ForEach-Object { [string]$_ }); Code = $LASTEXITCODE }
    } finally {
        $ErrorActionPreference = $prev
    }
}

Write-Head "CA service"
$svc = Get-Service certsvc -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "  certsvc not present -- this is not the CA host. Run this on dns01." -ForegroundColor Red
    exit 1
}
Write-Line "certsvc: $($svc.Status)"
$ca = Invoke-CertUtil @('-cainfo', 'name')
foreach ($l in $ca.Lines) { if ($l -match '\S') { Write-Line $l.Trim() } }

# --------------------------------------------------------------- restrict ---
$restrict = @()
if ($SinceMinutes -gt 0) {
    # certutil wants a locale-formatted datetime; let .NET produce it.
    $cut = (Get-Date).AddMinutes(-$SinceMinutes)
    $restrict += "Request.SubmittedWhen>=$($cut.ToString('g'))"
}
$columns = 'Request.RequestID,Request.SubmittedWhen,Request.Disposition,Request.DispositionMessage,Request.RequesterName,Request.CommonName,Request.CertificateTemplate'

function Get-Rows {
    param([string[]]$Restrict)
    # Not $args: that is an automatic variable inside a function, and writing
    # to it is asking for trouble.
    $cuArgs = @('-view')
    if ($Restrict.Count -gt 0) { $cuArgs += @('-restrict', ($Restrict -join ',')) }
    $cuArgs += @('-out', $columns)
    $r = Invoke-CertUtil $cuArgs

    # certutil emits one "Row N:" block per request, with "Field: value" lines.
    $rows = @()
    $cur = $null
    foreach ($line in $r.Lines) {
        if ($line -match '^\s*Row\s+(\d+):') {
            if ($cur) { $rows += $cur }
            $cur = @{}
            continue
        }
        if ($null -eq $cur) { continue }
        if ($line -match '^\s*([^:]+):\s*(.*)$') {
            $name = $Matches[1].Trim()
            $val  = $Matches[2].Trim()
            # Disposition prints as "20 -- Issued"; keep the numeric part.
            if ($name -like '*Request Disposition*' -or $name -eq 'Request Disposition') {
                if ($val -match '^(\d+)') { $cur['Disposition'] = [int]$Matches[1] }
            }
            elseif ($name -like '*Request ID*')          { $cur['Id']        = $val }
            elseif ($name -like '*Submission Date*' -or $name -like '*SubmittedWhen*') { $cur['When'] = $val }
            elseif ($name -like '*Requester Name*')      { $cur['Requester'] = $val }
            elseif ($name -like '*Common Name*')         { $cur['CN']        = $val }
            elseif ($name -like '*Certificate Template*'){ $cur['Template']  = $val }
            elseif ($name -like '*Disposition Message*') { $cur['Message']   = $val }
        }
    }
    if ($cur) { $rows += $cur }
    ,$rows
}

function Field { param($Row, [string]$Name, $Default = '') if ($Row.ContainsKey($Name) -and $Row[$Name]) { $Row[$Name] } else { $Default } }

$label = if ($SinceMinutes -gt 0) { "requests in the last $SinceMinutes minute(s)" } else { "the $Last most recent requests" }
Write-Head $label
$rows = Get-Rows -Restrict $restrict

if ($FailuresOnly) {
    $rows = @($rows | Where-Object { @(9, 30, 31) -contains (Field $_ 'Disposition' -1) })
}

if (@($rows).Count -eq 0) {
    if ($SinceMinutes -gt 0) {
        Write-Host "  NO REQUESTS in that window." -ForegroundColor Yellow
        Write-Line "If a VCF certificate replacement was accepted but nothing appears here,"
        Write-Line "VCF never submitted a CSR. That is a VCF-side problem, not a CA problem,"
        Write-Line "and waiting longer will not change it."
    } else {
        Write-Line "(no rows)"
    }
    exit 0
}

# Newest last in certutil output; show the tail.
$show = @($rows)
if (-not $FailuresOnly -and $SinceMinutes -eq 0 -and $show.Count -gt $Last) {
    $show = @($show[($show.Count - $Last)..($show.Count - 1)])
}

foreach ($r in $show) {
    $d    = Field $r 'Disposition' -1
    $name = if ($DispositionName.ContainsKey([int]$d)) { $DispositionName[[int]$d] } else { "disposition $d" }
    $colour = switch ([int]$d) { 20 { 'Green' } 30 { 'Red' } 31 { 'Red' } 9 { 'Yellow' } default { 'Gray' } }
    Write-Host ("  #{0,-5} {1,-8} {2,-20} {3}" -f (Field $r 'Id' '?'), $name, (Field $r 'Template' '-'), (Field $r 'CN' '-')) -ForegroundColor $colour
    Write-Line ("        submitted {0}   requester {1}" -f (Field $r 'When' '?'), (Field $r 'Requester' '?'))
    $msg = Field $r 'Message'
    if ($msg -and [int]$d -ne 20) { Write-Line ("        {0}" -f $msg) }
}

$issued = @($rows | Where-Object { (Field $_ 'Disposition' -1) -eq 20 }).Count
$failed = @($rows | Where-Object { @(30, 31) -contains (Field $_ 'Disposition' -1) }).Count
$pend   = @($rows | Where-Object { (Field $_ 'Disposition' -1) -eq 9 }).Count
Write-Head "summary"
Write-Line "issued $issued   failed/denied $failed   pending $pend"
if ($pend -gt 0) {
    Write-Host "  Pending requests mean the template requires manual approval." -ForegroundColor Yellow
    Write-Line "The VMware template should issue automatically; check its approval setting."
}
exit 0
