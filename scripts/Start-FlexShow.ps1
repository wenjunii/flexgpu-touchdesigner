<#
.SYNOPSIS
Previews or starts the configured FlexShow processes.

.DESCRIPTION
Without -Start this script only prints the resolved execution plan. Supplying
-Start explicitly authorizes process launch. Repeating an authorized start is
safe because flexgpu.py reuses its runtime manifest and skips owned processes
that are already running.
#>
[CmdletBinding()]
param(
    [Alias('ConfigPath')]
    [string]$Config = '',

    [ValidateSet('', 'installation', 'vr', 'combined')]
    [string]$Experience = '',

    [ValidateSet('', 'fog', 'procedural', 'hybrid')]
    [string]$Completion = '',

    [ValidateSet('', 'auto', '3080ti_16gb', '4090', '5090')]
    [string]$Tier = '',

    [switch]$Start
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

if ($Start) {
    Invoke-FlexShowCli -Command start -Config $Config -Experience $Experience -Completion $Completion -Tier $Tier -ActionMode Execute
}
else {
    Write-Host '[FlexShow] Preview only. Add -Start to authorize process launch.'
    Invoke-FlexShowCli -Command plan -Config $Config -Experience $Experience -Completion $Completion -Tier $Tier
}
