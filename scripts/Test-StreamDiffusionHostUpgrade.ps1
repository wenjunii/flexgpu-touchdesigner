<#
.SYNOPSIS
Read-only machine-local file/config preflight before a targeted RGB-host upgrade.
.DESCRIPTION
Does not launch TouchDesigner, change a config, copy a TOE, or test hardware.
Requires distinct, saved, ignored current/rollback TOEs with matching machine
tags. Live inactive-generator and public-settings checks run separately in TD.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Backup,
    [Parameter(Mandatory = $true)]
    [ValidateSet('3080ti_16gb', '4090', '5090')][string]$ExpectedTier
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
function Resolve-LocalPath([string]$Path) {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { $Path = Join-Path $root $Path }
    return [System.IO.Path]::GetFullPath($Path)
}
$projectPath = Resolve-LocalPath $Project
$backupPath = Resolve-LocalPath $Backup
if ($projectPath.Equals($backupPath, [System.StringComparison]::OrdinalIgnoreCase) -or
    -not ([System.IO.Path]::GetDirectoryName($projectPath)).Equals(
        [System.IO.Path]::GetDirectoryName($backupPath), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Current and rollback TOEs must be distinct files in the same project folder.'
}
# Reuse the existing reviewed repository/ignore/tier/name guards. WhatIf is
# unconditional: this preflight has no write mode.
$selector = Join-Path $PSScriptRoot 'Set-FlexShowLocalProject.ps1'
foreach ($path in @($projectPath, $backupPath)) {
    & $selector -Config $Config -Project $path -ExpectedTier $ExpectedTier -WhatIf | Out-Null
    if ((Get-Item -LiteralPath $path).Length -lt 1024) {
        throw 'Current and rollback TOEs must be nonempty saved projects (at least 1024 bytes).'
    }
}
$configPath = Resolve-LocalPath $Config
$document = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$configuredProject = Resolve-LocalPath ([string]$document.processes.world.project)
if (-not $configuredProject.Equals($projectPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Config must already point to the destination current TOE; review local retargeting separately.'
}
$sha = [System.Security.Cryptography.SHA256]::Create()
$stream = [System.IO.File]::OpenRead($backupPath)
try { $backupHash = [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
finally { $stream.Dispose(); $sha.Dispose() }
[ordered]@{
    status = 'preflight_pass'
    expected_tier = $ExpectedTier
    config_changed = $false
    project_changed = $false
    backup_sha256 = $backupHash
    live_checks = 'required separately in TouchDesigner'
    hardware_acceptance = $false
} | ConvertTo-Json
