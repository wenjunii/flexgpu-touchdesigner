<#
.SYNOPSIS
Checks the FlexShow configuration, GPUs, commands, and runtime ownership.

.DESCRIPTION
Diagnostics are always read-only and never launch configured show processes.
The legacy -Start/-Run switch is accepted for compatibility but does not enable
additional probes or mutations.
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

    [Alias('Run')]
    [switch]$Start,

    [switch]$Json,

    [switch]$ExitWithCode
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

if ($Start -and -not $Json) {
    Write-Warning '-Start/-Run is retained only for compatibility; diagnostics are always read-only.'
}
elseif (-not $Json) {
    Write-Host '[FlexShow] Read-only diagnostic; no show process will be launched.'
}

Invoke-FlexShowCli -Command diagnose -Config $Config -Experience $Experience -Completion $Completion -Tier $Tier -NvidiaSmi $NvidiaSmi -ActionMode DryRun -Json:$Json -ExitWithCode:$ExitWithCode
