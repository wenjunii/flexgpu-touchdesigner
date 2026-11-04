<#
.SYNOPSIS
Read-only inventory of explicitly named test environments in a Git worktree.
.DESCRIPTION
Reports sizes, venv identity, active-process/config references, and keep flags.
Never removes files, selects obsolete environments, or schedules cleanup.
Absence of a reference is not proof of non-use: dependency review is required.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Worktree,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$')]
    [string[]]$EnvironmentName,
    [string[]]$KeepEnvironmentName = @()
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$worktreeRoot = [IO.Path]::GetFullPath($Worktree).TrimEnd('\', '/')

function Get-GitPath([string]$Directory, [string]$Argument) {
    $value = @(& git -C $Directory rev-parse $Argument 2>$null)
    if ($LASTEXITCODE -ne 0 -or $value.Count -ne 1) { throw 'Unable to verify Git worktree identity.' }
    $path = [string]$value[0]
    if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $Directory $path }
    return [IO.Path]::GetFullPath($path).TrimEnd('\', '/')
}
if ((Get-GitPath $worktreeRoot '--show-toplevel') -ne $worktreeRoot -or
        (Get-GitPath $worktreeRoot '--git-common-dir') -ne (Get-GitPath $root '--git-common-dir')) {
    throw 'Target must be a worktree of this repository, not another project or a subdirectory.'
}
$names = @($EnvironmentName | Select-Object -Unique)
if ($names.Count -ne $EnvironmentName.Count) { throw 'Duplicate environment names are not accepted.' }
$venvRoot = Join-Path $worktreeRoot '.venv'

# Inspect every existing ancestor before enumeration. Do not follow junctions.
function Assert-NoReparseAncestor([string]$Path) {
    $cursor = $Path
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Inventory paths must not traverse a symlink or junction.'
            }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}
Assert-NoReparseAncestor $venvRoot
$checkoutRoots = @(& git -c core.quotepath=false -C $root worktree list --porcelain |
    Where-Object { $_.StartsWith('worktree ') } | ForEach-Object { $_.Substring(9) })
if ($LASTEXITCODE -ne 0) { throw 'Unable to inventory repository worktrees.' }
$configFiles = @()
foreach ($checkout in $checkoutRoots) {
    $config = Join-Path $checkout 'config'
    if (Test-Path -LiteralPath $config) {
        $configFiles += @(Get-ChildItem -LiteralPath $config -File | Where-Object {
            $_.Name -like 'local-*.json' -or $_.Name -like '*-local.json'
        })
    }
    $mcp = Join-Path $checkout '.codex/config.toml'
    if (Test-Path -LiteralPath $mcp) { $configFiles += Get-Item -LiteralPath $mcp }
}
$processCheck = 'available'
try { $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop) }
catch { $processes = @(); $processCheck = 'unavailable' }
$items = @()
foreach ($name in $names) {
    $path = [IO.Path]::GetFullPath((Join-Path $venvRoot $name))
    if ([IO.Path]::GetDirectoryName($path) -ne $venvRoot) { throw 'Environment must be directly inside .venv.' }
    Assert-NoReparseAncestor $path
    $node = Get-Item -LiteralPath $path -Force
    if (-not $node.PSIsContainer) { throw 'Environment target must be a directory.' }
    $cfg = Join-Path $path 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) { throw 'Target has no pyvenv.cfg identity.' }
    Assert-NoReparseAncestor $cfg
    $bytes = [long]0; $files = [long]0; $reparse = [long]0
    $pending = New-Object 'System.Collections.Generic.Stack[System.IO.DirectoryInfo]'
    $pending.Push((New-Object IO.DirectoryInfo($path)))
    while ($pending.Count) {
        foreach ($entry in $pending.Pop().EnumerateFileSystemInfos()) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { $reparse++; continue }
            if ($entry -is [IO.DirectoryInfo]) { $pending.Push($entry) }
            else { $files++; $bytes += $entry.Length }
        }
    }
    $normalized = $path.Replace('\', '/').ToLowerInvariant()
    $pattern = [regex]::Escape($normalized) + '(?=/|["''\s]|$)'
    $references = @()
    foreach ($configFile in $configFiles) {
        $text = [IO.File]::ReadAllText($configFile.FullName).Replace('\\', '\').Replace('\', '/').ToLowerInvariant()
        if ($text -match $pattern) { $references += $configFile.FullName }
    }
    $pids = @($processes | Where-Object {
        ($_.CommandLine -and $_.CommandLine.Replace('\', '/').ToLowerInvariant() -match $pattern) -or
        ($_.ExecutablePath -and $_.ExecutablePath.Replace('\', '/').ToLowerInvariant() -match $pattern)
    } | ForEach-Object { $_.ProcessId })
    $keep = ($worktreeRoot -eq $root) -or ($name -in $KeepEnvironmentName)
    $classification = if ($keep) { 'keep' } elseif ($reparse -or $pids.Count -or $references.Count -or
        $processCheck -ne 'available') { 'blocked' } else { 'requires_dependency_review' }
    $items += [ordered]@{ name=$name; path=$path; bytes=$bytes; files=$files;
        pyvenv_cfg_sha256=(Get-FileHash -LiteralPath $cfg -Algorithm SHA256).Hash;
        reparse_points=$reparse; process_ids=$pids; config_references=$references;
        classification=$classification }
}
[ordered]@{ version='flexgpu-test-environment-inventory/v1'; mode='read_only';
    deletes_files=$false; worktree=$worktreeRoot; process_check=$processCheck;
    dependency_review_required=$true; environments=$items } | ConvertTo-Json -Depth 7
