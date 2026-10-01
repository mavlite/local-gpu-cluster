<#
.SYNOPSIS
    Asserts Get-DnsAnswer tells "the server said no" apart from "I could not ask".

.DESCRIPTION
    This project has repeatedly shipped verification that cannot distinguish the
    desired state from an inability to observe it. Get-DnsAnswer exists because
    New-Dns01RollbackClone.ps1 did it again: it reported "Lab DNS is not
    serving" after a completely successful clone, on a healthy DC, because the
    workstation running the check was on a VPN using OpenVPN's
    block-outside-dns -- WFP filters that permit port 53 only to the VPN's own
    resolvers. Every other DNS server timed out identically to a dead service,
    and WFP is invisible to Get-NetFirewallRule, so both ends looked clean.

    Three states, and the middle one is the whole point:
      ok       -- answered with an address
      noanswer -- answered authoritatively that there is no address (REAL fault)
      error    -- could not be asked at all (says NOTHING about the server)

    NXDOMAIN throws in PowerShell exactly as a timeout does, so a naive
    try/catch files a working-server answer with the unreachable cases.

    The function is extracted from the script by AST so this tests the shipped
    code, not a copy that can drift from it.
#>
[CmdletBinding()]
param(
    [string]$ScriptPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# $PSScriptRoot is not populated in param defaults under PowerShell 5.1, so
# resolve the default here instead of in the parameter block.
if (-not $ScriptPath) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ScriptPath = Join-Path $here '..\New-Dns01RollbackClone.ps1'
}

$ast = [System.Management.Automation.Language.Parser]::ParseFile(
           (Resolve-Path $ScriptPath), [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $n.Name -eq 'Get-DnsAnswer' }, $true)
if (-not $fn) { Write-Host "  [FAIL] Get-DnsAnswer not found in $ScriptPath" -ForegroundColor Red; exit 1 }
. ([scriptblock]::Create($fn.Extent.Text))

# Live cases cover only what is deterministic here. "A name that does not
# exist" is NOT deterministic: the resolver in use answered NXDOMAIN on one
# run and returned an answer on the next, so asserting on it produces a test
# that fails for reasons unrelated to the code. The classification logic is
# what is actually at risk, so it is tested below with stubs instead.
$cases = @()
$own = (Get-DnsClientServerAddress -AddressFamily IPv4 |
        Where-Object { $_.ServerAddresses } | Select-Object -First 1).ServerAddresses
if ($own) {
    $cases += @{ label = 'live: resolver + real name -> ok'; server = $own[0]; name = 'example.com'; expect = 'ok' }
} else {
    Write-Host "  [SKIP] no IPv4 resolver configured; skipping the live 'ok' case" -ForegroundColor Yellow
}
# 192.0.2.0/24 is TEST-NET-1 (RFC 5737): guaranteed never routable, so this is
# a reliable stand-in for "cannot be asked".
$cases += @{ label = 'live: black-hole address   -> error'; server = '192.0.2.1'; name = 'example.com'; expect = 'error' }

$fail = 0
foreach ($c in $cases) {
    $r  = Get-DnsAnswer -Server $c.server -Name $c.name
    $ok = $r.State -eq $c.expect
    if (-not $ok) { $fail++ }
    Write-Host ("  [{0}] {1}  (got '{2}')" -f $(if ($ok) { ' OK ' } else { 'FAIL' }), $c.label, $r.State) `
        -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}

# ---------------------------------------------------------------------------
# Classification, tested without the network. A function named Resolve-DnsName
# in the calling scope shadows the real cmdlet, and PowerShell resolves the
# name dynamically at call time, so Get-DnsAnswer picks up the stub even though
# it was defined elsewhere. These three are the distinctions that the original
# bug collapsed.
# ---------------------------------------------------------------------------
$stubCases = @(
    @{ label = 'stub: NXDOMAIN throw        -> noanswer'
       expect = 'noanswer'
       body = { throw [Exception]::new('no-such.example.com : DNS name does not exist') } }
    @{ label = 'stub: answer, but no A rec  -> noanswer'
       expect = 'noanswer'
       body = { [pscustomobject]@{ Name = 'x'; Type = 'SOA' } } }
    @{ label = 'stub: timeout throw         -> error'
       expect = 'error'
       body = { throw [Exception]::new('x : This operation returned because the timeout period expired') } }
    @{ label = 'stub: host unreachable      -> error'
       expect = 'error'
       body = { throw [Exception]::new('x : A socket operation was attempted to an unreachable network') } }
)
foreach ($c in $stubCases) {
    $r = & {
        param($stubBody)
        function Resolve-DnsName { & $stubBody }
        Get-DnsAnswer -Server '203.0.113.1' -Name 'stub.example.com'
    } $c.body
    $ok = $r.State -eq $c.expect
    if (-not $ok) { $fail++ }
    Write-Host ("  [{0}] {1}  (got '{2}')" -f $(if ($ok) { ' OK ' } else { 'FAIL' }), $c.label, $r.State) `
        -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}

$total = $cases.Count + $stubCases.Count
Write-Host ""
if ($fail) { Write-Host "  $fail of $total cases FAILED" -ForegroundColor Red; exit 1 }
Write-Host "  all $total cases passed" -ForegroundColor Green
exit 0
