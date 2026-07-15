<#
.SYNOPSIS
Previews or starts the configured FlexShow processes.

.DESCRIPTION
Without -Start this script runs the full preflight and previews the resolved
runtime plan. Supplying -Start explicitly authorizes process launch. Repeating an authorized start is
safe because flexgpu.py reuses its runtime manifest and skips owned processes
that are already running. If launch settings changed, it refuses reuse and asks
you to stop the old process first.
#>
#Requires -Version 5.1
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

    [string]$NvidiaSmi = '',

    [switch]$Start,

    [switch]$Json,

    [switch]$ExitWithCode
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

if ($Start) {
    Invoke-FlexShowCli -Command start -Config $Config -Experience $Experience -Completion $Completion -Tier $Tier -NvidiaSmi $NvidiaSmi -ActionMode Execute -Json:$Json -ExitWithCode:$ExitWithCode
}
else {
    if (-not $Json) {
        Write-Host '[FlexShow] Full preflight preview only. Add -Start to authorize process launch.'
    }
    Invoke-FlexShowCli -Command start -Config $Config -Experience $Experience -Completion $Completion -Tier $Tier -NvidiaSmi $NvidiaSmi -ActionMode DryRun -Json:$Json -ExitWithCode:$ExitWithCode
}
