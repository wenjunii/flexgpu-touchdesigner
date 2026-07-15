<#
.SYNOPSIS
Previews or stops processes owned by the selected FlexShow runtime manifest.

.DESCRIPTION
Without -Stop this script performs a dry run. Supplying -Stop explicitly
authorizes shutdown. It never kills processes by executable name and is safe to
repeat when nothing owned by the selected manifest is running.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Alias('ConfigPath')]
    [string]$Config = '',

    [switch]$Stop,

    [switch]$Json,

    [switch]$ExitWithCode
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

$mode = if ($Stop) { 'Execute' } else { 'DryRun' }
if (-not $Stop -and -not $Json) {
    Write-Host '[FlexShow] Preview only. Add -Stop to authorize shutdown.'
}
elseif ($Stop -and -not $Json) {
    Write-Warning 'Windows Stop force-terminates identity-verified show processes. Save TouchDesigner edits first.'
}

Invoke-FlexShowCli -Command stop -Config $Config -ActionMode $mode -Json:$Json -ExitWithCode:$ExitWithCode
