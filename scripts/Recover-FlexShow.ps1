<#
.SYNOPSIS
Previews or performs bounded recovery of a separate FlexShow AI process.

.DESCRIPTION
Without -Recover this script is read-only. An authorized recovery can start a
missing/dead AI role, while -RestartRunning also gracefully replaces a healthy
AI role. The controller never restarts the world/render role implicitly and
refuses recovery when that dependency is unhealthy. -WaitReadyMs makes each
attempt succeed only after a valid application-ready heartbeat.
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

    [ValidateRange(1, 3)]
    [int]$Attempts = 1,

    [switch]$RestartRunning,

    [switch]$Recover,

    [ValidateRange(0, 600000)]
    [Nullable[int]]$WaitReadyMs = $null,

    [switch]$Json,

    [switch]$ExitWithCode
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

$mode = if ($Recover) { 'Execute' } else { 'DryRun' }
if (-not $Recover -and -not $Json) {
    Write-Host '[FlexShow] AI recovery preview only. Add -Recover to authorize mutation.'
}
elseif ($Recover -and -not $Json) {
    Write-Warning 'Only the separate AI role may be restarted; world/render is never restarted automatically.'
}

Invoke-FlexShowCli -Command recover -Config $Config -Experience $Experience -Completion $Completion -Tier $Tier -NvidiaSmi $NvidiaSmi -RecoveryAttempts $Attempts -RestartRunning:$RestartRunning -WaitReadyMs $WaitReadyMs -ActionMode $mode -Json:$Json -ExitWithCode:$ExitWithCode
