<#
.SYNOPSIS
Previews or stops processes owned by the selected FlexShow runtime manifest.

.DESCRIPTION
Without -Stop this script performs a dry run. Supplying -Stop explicitly
authorizes shutdown. It never kills processes by executable name and is safe to
repeat when nothing owned by the selected manifest is running.
#>
[CmdletBinding()]
param(
    [Alias('ConfigPath')]
    [string]$Config = '',

    [switch]$Stop
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

$mode = if ($Stop) { 'Execute' } else { 'DryRun' }
if (-not $Stop) {
    Write-Host '[FlexShow] Preview only. Add -Stop to authorize shutdown.'
}

Invoke-FlexShowCli -Command stop -Config $Config -ActionMode $mode
