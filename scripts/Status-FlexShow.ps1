<#
.SYNOPSIS
Reports read-only status for processes owned by the selected FlexShow manifest.

.DESCRIPTION
Status never creates a runtime directory, acquires a mutation lock, starts a
process, or sends a shutdown signal. It reports running, dead, identity-refused,
and in-progress session states from the atomically replaced runtime manifest.
Identity-matched apps are distinguished as alive (no ready heartbeat yet),
ready, or stale/frozen without changing any process or heartbeat file.
Windows status uses native handle-bound command-line identity when available
and a bounded WMI compatibility fallback otherwise.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Alias('ConfigPath')]
    [string]$Config = '',

    [switch]$Json,

    [switch]$ExitWithCode
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

if (-not $Json) {
    Write-Host '[FlexShow] Read-only runtime ownership and session status.'
}
Invoke-FlexShowCli -Command status -Config $Config -ActionMode None -Json:$Json -ExitWithCode:$ExitWithCode
