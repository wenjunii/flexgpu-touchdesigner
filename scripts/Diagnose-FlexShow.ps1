<#
.SYNOPSIS
Checks the FlexShow configuration, GPUs, commands, and runtime ownership.

.DESCRIPTION
The default diagnostic is a dry run. Add -Start to authorize active probes
implemented by flexgpu.py. Diagnostics never launch configured show processes.
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

    [Alias('Run')]
    [switch]$Start
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

$mode = if ($Start) { 'Execute' } else { 'DryRun' }
if (-not $Start) {
    Write-Host '[FlexShow] Passive diagnostic. Add -Start (or -Run) to authorize active probes.'
}

Invoke-FlexShowCli -Command diagnose -Config $Config -Experience $Experience -Completion $Completion -Tier $Tier -ActionMode $mode
