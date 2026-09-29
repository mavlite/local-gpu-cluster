
# ============================================================================
# TEST OVERRIDES -- appended to a COPY of VCFLab.Common.ps1 by the stub harness.
#
# These must live at the end of Common.ps1, not in the harness: each script
# dot-sources Common into its own script scope, and those definitions shadow
# anything the harness defined in a parent scope. Appending here is the only
# way an override actually wins.
#
# Everything that would touch the network or sleep is replaced. Nothing in a
# stub run may reach the live lab.
# ============================================================================
function Test-TcpPort { param([Parameter(ValueFromRemainingArguments)]$a) $true }
function Start-Sleep  { param([Parameter(ValueFromRemainingArguments)]$a) }
function Invoke-LabRest { param([Parameter(ValueFromRemainingArguments)]$a)
    throw 'stub run attempted real REST I/O' }
function Test-VCenterApi     { param([Parameter(ValueFromRemainingArguments)]$a) $true }
function Test-SddcManagerApi { param([Parameter(ValueFromRemainingArguments)]$a) $true }
function Test-NsxCluster     { param([Parameter(ValueFromRemainingArguments)]$a) $true }
function Test-OperationsApi  { param([Parameter(ValueFromRemainingArguments)]$a) $true }
function Send-WakeOnLan      { param([Parameter(ValueFromRemainingArguments)]$a) $true }
function Disable-CertificateValidation { }
function Disconnect-AllViServers { }
