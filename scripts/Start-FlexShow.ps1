<#
.SYNOPSIS
Previews or starts the configured FlexShow processes.

.DESCRIPTION
Without -Start this script runs the full preflight and previews the resolved
runtime plan. Supplying -Start explicitly authorizes process launch. Repeating an authorized start is
safe because flexgpu.py reuses its runtime manifest and skips owned processes
that are already running. If launch settings changed, it refuses reuse and asks
you to stop the old process first. -WaitReadyMs optionally requires each app to
publish the atomic heartbeat/readiness contract before start succeeds.
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

    [ValidateSet('', 'auto', '3080ti_16gb', '4090', '5090', 'custom')]
    [string]$Tier = '',

    [string]$NvidiaSmi = '',

    [switch]$Start,

    [ValidateRange(0, 600000)]
    [Nullable[int]]$WaitReadyMs = $null,

    [switch]$Json,

    [switch]$ExitWithCode
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

$mode = if ($Start) { 'Execute' } else { 'DryRun' }
if (-not $Start -and -not $Json) {
    Write-Host '[FlexShow] Full preflight preview only. Add -Start to authorize process launch.'
}

$invokeArguments = @{
    Command = 'start'
    Config = $Config
    Experience = $Experience
    Completion = $Completion
    Tier = $Tier
    NvidiaSmi = $NvidiaSmi
    ActionMode = $mode
    Json = $Json
    ExitWithCode = $ExitWithCode
}
# Passing an omitted Nullable[int] as an explicit $null makes Windows
# PowerShell 5.1 run ValidateRange against null. Preserve omission so the
# configuration's readiness default remains authoritative.
if ($PSBoundParameters.ContainsKey('WaitReadyMs')) {
    $invokeArguments['WaitReadyMs'] = [int]$WaitReadyMs
}

Invoke-FlexShowCli @invokeArguments
