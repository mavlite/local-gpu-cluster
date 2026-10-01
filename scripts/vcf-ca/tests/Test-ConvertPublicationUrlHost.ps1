<#
.SYNOPSIS
    Asserts the CDP/AIA host rewrite changes the host and nothing else.

.DESCRIPTION
    Set-CaRevocationEndpoints.ps1 deliberately does not construct AD CS
    publication entries from scratch. The flag prefix decides whether a url
    reaches the CDP extension of issued certificates, a wrong one cannot be
    fixed after issuance, and the documentation for those bits is thin enough
    that two careful reviews of this project disagreed about them. So the
    script preserves whatever AD CS configured and substitutes only the host.

    That makes the substitution itself load-bearing: if it mangles a flag
    prefix, a % token or a path, the result is the same unfixable outcome by a
    different route. These cases cover the real default shapes AD CS writes,
    including the %1 host token and the %3%8%9 filename tokens, which a [Uri]
    round-trip would re-encode.

    The function is extracted from the shipped script by AST, so this cannot
    drift from the code it tests.
#>
[CmdletBinding()]
param([string]$ScriptPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ScriptPath) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ScriptPath = Join-Path $here '..\Set-CaRevocationEndpoints.ps1'
}
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
           (Resolve-Path -LiteralPath $ScriptPath).ProviderPath, [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $n.Name -eq 'Convert-PublicationUrlHost' }, $true)
if (-not $fn) {
    Write-Host "  [FAIL] Convert-PublicationUrlHost not found in $ScriptPath" -ForegroundColor Red
    exit 1
}
. ([scriptblock]::Create($fn.Extent.Text))

$NEW = 'pki.knowledgeondemand.net'

$cases = @(
    # --- entries that must be left completely alone -----------------------
    @{ label = 'local file path untouched'
       entry = '1:C:\Windows\system32\CertSrv\CertEnroll\%3%8%9.crl'
       http  = $false
       want  = '1:C:\Windows\system32\CertSrv\CertEnroll\%3%8%9.crl' }
    @{ label = 'ldap:/// untouched'
       entry = '2:ldap:///CN=%7%8,CN=%2,CN=CDP,CN=Public Key Services,CN=Services,%6%10'
       http  = $false
       want  = '2:ldap:///CN=%7%8,CN=%2,CN=CDP,CN=Public Key Services,CN=Services,%6%10' }
    @{ label = 'file:// untouched (not http)'
       entry = '0:file://%1/CertEnroll/%3%8%9.crl'
       http  = $false
       want  = '0:file://%1/CertEnroll/%3%8%9.crl' }
    @{ label = 'UNC path untouched'
       entry = '1:\\dns01\CertEnroll\%3%8%9.crl'
       http  = $false
       want  = '1:\\dns01\CertEnroll\%3%8%9.crl' }

    # --- http entries: host replaced, EVERYTHING else preserved ----------
    @{ label = 'AD CS default %1 host token replaced, flags+tokens kept'
       entry = '6:http://%1/CertEnroll/%3%8%9.crl'
       http  = $true
       want  = "6:http://$NEW/CertEnroll/%3%8%9.crl" }
    @{ label = 'flag 10 preserved exactly'
       entry = '10:http://dns01.knowledgeondemand.net/CertEnroll/%3%8%9.crl'
       http  = $true
       want  = "10:http://$NEW/CertEnroll/%3%8%9.crl" }
    @{ label = 'flag 2 preserved exactly'
       entry = '2:http://dns01.knowledgeondemand.net/CertEnroll/%1_%3%4.crt'
       http  = $true
       want  = "2:http://$NEW/CertEnroll/%1_%3%4.crt" }
    @{ label = 'large flag (79) preserved'
       entry = '79:http://old.example.net/CertEnroll/%3%8%9.crl'
       http  = $true
       want  = "79:http://$NEW/CertEnroll/%3%8%9.crl" }
    @{ label = 'host:port replaced wholesale'
       entry = '2:http://dns01.knowledgeondemand.net:8080/CertEnroll/%3%8%9.crl'
       http  = $true
       want  = "2:http://$NEW/CertEnroll/%3%8%9.crl" }
    @{ label = 'https preserved as https'
       entry = '2:https://dns01.knowledgeondemand.net/CertEnroll/%3%8%9.crl'
       http  = $true
       want  = "2:https://$NEW/CertEnroll/%3%8%9.crl" }
    @{ label = 'no path at all is still recognised as http'
       entry = '2:http://dns01.knowledgeondemand.net'
       http  = $true
       want  = "2:http://$NEW" }
    @{ label = 'deep path preserved'
       entry = '2:http://old/a/b/c/%3%8%9.crl'
       http  = $true
       want  = "2:http://$NEW/a/b/c/%3%8%9.crl" }
    @{ label = 'already correct host reports no change'
       entry = "2:http://$NEW/CertEnroll/%3%8%9.crl"
       http  = $true
       want  = "2:http://$NEW/CertEnroll/%3%8%9.crl"
       changed = $false }
)

$fail = 0
foreach ($c in $cases) {
    $r = Convert-PublicationUrlHost -Entry $c.entry -NewHost $NEW

    $errs = @()
    if ($r.Value -cne $c.want)   { $errs += "value '$($r.Value)' != '$($c.want)'" }
    if ($r.IsHttp -ne $c.http)   { $errs += "IsHttp $($r.IsHttp) != $($c.http)" }
    # Changed defaults to "true for http cases whose host actually differs".
    $wantChanged = if ($c.ContainsKey('changed')) { $c.changed } else { $c.http }
    if ($r.Changed -ne $wantChanged) { $errs += "Changed $($r.Changed) != $wantChanged" }
    # A non-http entry must never be reported as rewritten.
    if (-not $c.http -and $r.Changed) { $errs += 'non-http entry reported as changed' }

    if ($errs.Count) {
        $fail++
        Write-Host ("  [FAIL] {0}" -f $c.label) -ForegroundColor Red
        foreach ($e in $errs) { Write-Host "         $e" -ForegroundColor Red }
    } else {
        Write-Host ("  [ OK ] {0}" -f $c.label) -ForegroundColor Green
    }
}

Write-Host ""
if ($fail) { Write-Host "  $fail of $($cases.Count) cases FAILED" -ForegroundColor Red; exit 1 }
Write-Host "  all $($cases.Count) cases passed" -ForegroundColor Green
exit 0
