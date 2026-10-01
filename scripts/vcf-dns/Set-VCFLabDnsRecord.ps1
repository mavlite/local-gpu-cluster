<#
.SYNOPSIS
    Creates and verifies the forward (A) and reverse (PTR) records the VCF lab
    components need, on the Windows DNS server dns01.

.DESCRIPTION
    Declarative and idempotent: the $Records table below states what the zone
    should contain, and the script makes it so. A record that is already
    correct is reported and left alone. A record whose address disagrees with
    the table is reported as a CONFLICT and NOT changed unless -Force is given,
    because silently repointing a name that something is already using is how
    you lose an afternoon.

    Dry run unless -Apply is passed.

    This uses the DnsServer module (RSAT), not PowerCLI -- PowerCLI talks to
    vSphere and has no DNS cmdlets. dns01 is a Windows DNS server (RPC 135,
    LDAP 389, WinRM 5985 all open), so it is managed over WinRM:

        Install:  Add-WindowsCapability -Online -Name Rsat.Dns.Tools~~~~0.0.1.0
        Check:    Get-Command -Module DnsServer

    Both halves matter. VCF checks reverse resolution during bring-up, and a
    missing PTR has already cost this lab a bring-up failure that 300+ unit
    tests did not catch.

.PARAMETER Apply
    Actually create records. Without it the script only reports.

.PARAMETER Force
    Permit repointing a name whose existing address disagrees with the table.

.EXAMPLE
    .\Set-VCFLabDnsRecord.ps1
.EXAMPLE
    .\Set-VCFLabDnsRecord.ps1 -Apply -Credential (Get-Credential KNOWLEDGEONDEMAND\Administrator)
#>
[CmdletBinding()]
param(
    [string]$DnsServer = '172.16.10.150',
    [pscredential]$Credential,
    [switch]$Apply,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------- the record set
# Everything the VCF lab components resolve to. Existing entries are listed
# deliberately, so running this is also an audit of the whole set rather than
# just a creator of new ones.
#
# Logs 9.1.1 and Networks 9.1.1 differ in shape, and it changes what DNS needs:
#
#   VRLI  deploys as a supervisor workload behind an ingress. Its schema
#         (configuration-schema-operations-logs-9.1.1.0.25679624.yaml) asks for
#         ingress.component.fqdns[] and ingress.component.vips.ipv4[] -- one
#         name, one VIP, exactly like fleet -> 172.16.10.116.
#
#   VRNI  deploys as TWO appliances, platform and collector, each its own VM
#         with its own address. Only the platform name existed.
$Records = @(
    @{ Name = 'log-insight';    Zone = 'knowledgeondemand.net'; IPv4 = '172.16.10.137'; Note = 'Logs ingress VIP' }
    @{ Name = 'vrni';           Zone = 'knowledgeondemand.net'; IPv4 = '172.16.10.136'; Note = 'Networks platform' }
    @{ Name = 'vrni-collector'; Zone = 'knowledgeondemand.net'; IPv4 = '172.16.10.138'; Note = 'Networks collector (NEW)' }
    # The CA's CDP/AIA endpoint. It exists because dns01.knowledgeondemand.net
    # resolves to TWO addresses -- 172.16.10.150 and 192.168.6.197 -- and a
    # certificate's CRL URL must name an address every lab consumer can reach.
    # A VCF appliance that round-robins onto 192.168.6.197 cannot fetch the
    # CRL, and strict validators hard-fail on a CRL they cannot retrieve.
    # A CNAME to dns01 would inherit exactly the problem it is meant to avoid.
    # NoReverse: this is a second name for an address whose PTR belongs to
    # dns01. Two PTRs on one address make reverse lookups return both.
    @{ Name = 'pki'; Zone = 'knowledgeondemand.net'; IPv4 = '172.16.10.150'; NoReverse = $true
       Note = 'CA CDP/AIA endpoint -- single-homed name for the DC lab address' }
)

# ------------------------------------------------------------------- helpers --
function Write-Step { param([string]$m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Write-Info { param([string]$m) Write-Host "  [info] $m" -ForegroundColor Gray }
function Write-Warn2{ param([string]$m) Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "  [FAIL] $m" -ForegroundColor Red }

function Get-ReverseZoneName {
    <#
        172.16.10.138 -> 10.16.172.in-addr.arpa, and "138" as the record name.
        Assumes /24 reverse zones, which is what this lab uses.
    #>
    param([Parameter(Mandatory)][string]$IPv4)
    $o = $IPv4.Split('.')
    if ($o.Count -ne 4) { throw "not an IPv4 address: $IPv4" }
    [pscustomobject]@{
        ZoneName   = "{0}.{1}.{2}.in-addr.arpa" -f $o[2], $o[1], $o[0]
        RecordName = $o[3]
    }
}

# ------------------------------------------------------------------ connect --
Write-Step "Connecting to $DnsServer"
if (-not (Get-Module -ListAvailable -Name DnsServer)) {
    Write-Fail "The DnsServer module is not installed."
    Write-Info "Install it with:  Add-WindowsCapability -Online -Name Rsat.Dns.Tools~~~~0.0.1.0"
    exit 1
}
Import-Module DnsServer -ErrorAction Stop

$cimArgs = @{ ComputerName = $DnsServer }
if ($Credential) { $cimArgs.Credential = $Credential }
try {
    $cim = New-CimSession @cimArgs -ErrorAction Stop
} catch {
    Write-Fail "Could not open a CIM session to ${DnsServer}: $($_.Exception.Message)"
    Write-Info "dns01 is a domain controller; you will need domain credentials."
    Write-Info "Pass them with -Credential (Get-Credential KNOWLEDGEONDEMAND\Administrator)."
    exit 1
}
Write-Ok "CIM session established"

if (-not $Apply) { Write-Warn2 "DRY RUN -- pass -Apply to create records" }

# ------------------------------------------------------------------- zones ---
Write-Step "Zones"
$zones = Get-DnsServerZone -CimSession $cim | Where-Object { -not $_.IsAutoCreated }
foreach ($z in ($zones | Sort-Object ZoneName)) {
    Write-Info ("{0,-34} {1}" -f $z.ZoneName, $z.ZoneType)
}

function Test-ZonePresent {
    param([string]$Name)
    [bool]($zones | Where-Object { $_.ZoneName -eq $Name })
}

# ----------------------------------------------------------------- records ---
$created = 0; $already = 0; $conflict = 0; $failed = 0

foreach ($r in $Records) {
    $fqdn = "$($r.Name).$($r.Zone)"
    Write-Step "$fqdn -> $($r.IPv4)   [$($r.Note)]"

    if (-not (Test-ZonePresent $r.Zone)) {
        Write-Fail "forward zone $($r.Zone) does not exist on this server"
        $failed++; continue
    }

    # --- forward (A) ---
    $existing = Get-DnsServerResourceRecord -CimSession $cim -ZoneName $r.Zone `
                    -Name $r.Name -RRType A -ErrorAction SilentlyContinue
    # A name can hold several A records, so flatten rather than assume one.
    $haveIp = @()
    if ($existing) {
        $haveIp = @($existing | ForEach-Object { $_.RecordData.IPv4Address.IPAddressToString })
    }

    if ($haveIp -contains $r.IPv4) {
        Write-Ok "A record already correct"
        $already++
    } elseif ($existing) {
        Write-Warn2 "CONFLICT: A record exists as $($haveIp -join ', ')"
        if (-not $Force) {
            Write-Info "not changing it; re-run with -Force if the table is right"
            $conflict++; continue
        }
        if ($Apply) {
            Remove-DnsServerResourceRecord -CimSession $cim -ZoneName $r.Zone `
                -Name $r.Name -RRType A -Force -ErrorAction Stop
            Add-DnsServerResourceRecordA -CimSession $cim -ZoneName $r.Zone `
                -Name $r.Name -IPv4Address $r.IPv4 -ErrorAction Stop
            Write-Ok "A record repointed to $($r.IPv4)"
            $created++
        } else { Write-Info "WOULD repoint A record to $($r.IPv4)" }
    } else {
        if ($Apply) {
            try {
                Add-DnsServerResourceRecordA -CimSession $cim -ZoneName $r.Zone `
                    -Name $r.Name -IPv4Address $r.IPv4 -ErrorAction Stop
                Write-Ok "A record created"
                $created++
            } catch {
                Write-Fail "could not create A record: $($_.Exception.Message)"
                $failed++; continue
            }
        } else { Write-Info "WOULD create A record" }
    }

    # --- reverse (PTR) ---
    # A record that deliberately shares an address with another host must not
    # claim the PTR. One address has one canonical name; a second PTR makes
    # reverse lookups return both, and this lab has already been bitten by a
    # reverse-DNS defect that 300+ unit tests missed.
    if ($r.ContainsKey('NoReverse') -and $r.NoReverse) {
        Write-Info "PTR deliberately skipped (NoReverse) -- this address's PTR belongs to another name"
        continue
    }

    $rev = Get-ReverseZoneName -IPv4 $r.IPv4
    if (-not (Test-ZonePresent $rev.ZoneName)) {
        Write-Warn2 "reverse zone $($rev.ZoneName) does not exist -- PTR skipped"
        Write-Info "VCF bring-up checks reverse resolution; create the zone before deploying"
        continue
    }

    $wantPtr = "$fqdn."
    $exPtr = Get-DnsServerResourceRecord -CimSession $cim -ZoneName $rev.ZoneName `
                 -Name $rev.RecordName -RRType Ptr -ErrorAction SilentlyContinue
    $havePtr = @()
    if ($exPtr) { $havePtr = @($exPtr | ForEach-Object { $_.RecordData.PtrDomainName }) }

    if ($havePtr -contains $wantPtr) {
        Write-Ok "PTR already correct"
    } elseif ($exPtr -and -not $Force) {
        Write-Warn2 "CONFLICT: PTR for $($r.IPv4) is $($havePtr -join ', ')"
        $conflict++
    } elseif ($Apply) {
        try {
            if ($exPtr) {
                Remove-DnsServerResourceRecord -CimSession $cim -ZoneName $rev.ZoneName `
                    -Name $rev.RecordName -RRType Ptr -Force -ErrorAction Stop
            }
            Add-DnsServerResourceRecordPtr -CimSession $cim -ZoneName $rev.ZoneName `
                -Name $rev.RecordName -PtrDomainName $wantPtr -ErrorAction Stop
            Write-Ok "PTR created in $($rev.ZoneName)"
        } catch {
            Write-Fail "could not create PTR: $($_.Exception.Message)"
            $failed++
        }
    } else {
        Write-Info "WOULD create PTR $($rev.RecordName).$($rev.ZoneName) -> $wantPtr"
    }
}

# ------------------------------------------------------------------ verify ---
if ($Apply) {
    Write-Step "Verifying against $DnsServer (both directions)"
    foreach ($r in $Records) {
        $fqdn = "$($r.Name).$($r.Zone)"
        $fwd = $null; $rev = $null
        try {
            $fwd = (Resolve-DnsName -Name $fqdn -Server $DnsServer -Type A -ErrorAction Stop |
                    Where-Object { $_.Type -eq 'A' } | Select-Object -First 1).IPAddress
        } catch { }
        try {
            $rev = (Resolve-DnsName -Name $r.IPv4 -Server $DnsServer -Type PTR -ErrorAction Stop |
                    Select-Object -First 1).NameHost
        } catch { }

        # A NoReverse record deliberately has no PTR of its own -- the address's
        # PTR belongs to another name -- so demanding one here would report a
        # failure, and exit non-zero, on a completely correct zone.
        $noReverse = ($r.ContainsKey('NoReverse') -and $r.NoReverse)
        $reverseOk = $noReverse -or ($rev -eq $fqdn)

        if ($fwd -eq $r.IPv4 -and $reverseOk) {
            if ($noReverse) {
                $v = if ($rev) { $rev } else { '<none>' }
                Write-Ok "$fqdn -> $($r.IPv4)   (no PTR by design; $($r.IPv4) reverses to $v)"
            } else {
                Write-Ok "$fqdn <-> $($r.IPv4)"
            }
        } else {
            # No ?? here: this has to run on Windows PowerShell 5.1, where the
            # null-coalescing operator is a parse error, not a nicety.
            $f = if ($fwd) { $fwd } else { '<none>' }
            $v = if ($rev) { $rev } else { '<none>' }
            if ($noReverse) { Write-Fail ("$fqdn : forward=$f (reverse not required)") }
            else            { Write-Fail ("$fqdn : forward=$f reverse=$v") }
            $failed++
        }
    }
}

Remove-CimSession $cim -ErrorAction SilentlyContinue

Write-Step "Result"
Write-Info "already correct: $already   created: $created   conflicts: $conflict   failures: $failed"
if ($conflict) { Write-Warn2 "conflicts were left untouched; review them before using -Force" }
if (-not $Apply) { Write-Info "this was a dry run; nothing was changed" }
exit ([int]($failed -gt 0))
