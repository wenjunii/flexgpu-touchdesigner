<#
.SYNOPSIS
Safely previews or removes stale entries from the project-scoped Envoy registry.

.DESCRIPTION
The default invocation is read-only. An entry is retained only when its
TouchDesigner process is alive and the declared loopback port is listening
under the same process ID. Use -Repair to back up the registry and atomically
remove only stale or malformed entries.

This script never starts, stops, or kills TouchDesigner. It is scoped to the
FlexGPU runtime directory and does not inspect a TOE or private component.
#>
[CmdletBinding()]
param(
    [string]$Registry,
    [switch]$Repair
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $root 'runtime\embody-ai\.embody')
)
if ([string]::IsNullOrWhiteSpace($Registry)) {
    $Registry = Join-Path $runtimeRoot 'envoy.json'
}
$registryPath = [System.IO.Path]::GetFullPath($Registry)
$runtimePrefix = $runtimeRoot.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $registryPath.StartsWith(
        $runtimePrefix,
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The Envoy registry must remain inside this checkout runtime directory.'
}
if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
    throw 'The project-scoped Envoy registry does not exist.'
}

$registryData = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
if ($null -eq $registryData.instances) {
    throw 'The Envoy registry has no instances object.'
}

$kept = [ordered]@{}
$stale = [System.Collections.Generic.List[object]]::new()
foreach ($property in @($registryData.instances.PSObject.Properties)) {
    $name = [string]$property.Name
    $entry = $property.Value
    $reason = $null
    try {
        $processId = [int]$entry.td_pid
        $port = [int]$entry.port
        if ($processId -lt 1 -or $port -lt 1 -or $port -gt 65535) {
            throw 'invalid pid or port'
        }
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $process -or $process.ProcessName -ne 'TouchDesigner') {
            $reason = 'TouchDesigner process is not alive'
        }
        else {
            $listener = @(
                Get-NetTCPConnection `
                    -State Listen `
                    -LocalPort $port `
                    -ErrorAction SilentlyContinue |
                    Where-Object {
                        $_.OwningProcess -eq $processId -and
                        $_.LocalAddress -in @('127.0.0.1', '::1')
                    }
            )
            if ($listener.Count -eq 0) {
                $reason = 'declared loopback listener is not owned by the process'
            }
        }
    }
    catch {
        $reason = 'registry entry is malformed'
    }

    if ($null -eq $reason) {
        $kept[$name] = $entry
    }
    else {
        $stale.Add([ordered]@{
            instance = $name
            reason = $reason
        })
    }
}

$active = [string]$registryData.active
$activeRetained = $kept.Contains($active)
$newActive = if ($activeRetained) {
    $active
}
elseif ($kept.Count -gt 0) {
    [string]@($kept.Keys)[0]
}
else {
    ''
}

$backupPath = $null
$status = if ($stale.Count -eq 0) { 'clean' } else { 'preview' }
if ($Repair -and $stale.Count -gt 0) {
    $directory = [System.IO.Path]::GetDirectoryName($registryPath)
    $leaf = [System.IO.Path]::GetFileName($registryPath)
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupPath = Join-Path $directory "$leaf.stale-$stamp.bak"
    Copy-Item -LiteralPath $registryPath -Destination $backupPath -ErrorAction Stop

    $registryData.instances = [pscustomobject]$kept
    $registryData.active = $newActive
    $temporaryPath = Join-Path $directory "$leaf.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $registryData | ConvertTo-Json -Depth 8
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            $json + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item `
            -LiteralPath $temporaryPath `
            -Destination $registryPath `
            -Force `
            -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
    $status = 'repaired'
}

[ordered]@{
    status = $status
    repair_requested = [bool]$Repair
    kept_instances = @($kept.Keys)
    stale_instances = @($stale)
    active_before = $active
    active_after = $newActive
    backup_created = $null -ne $backupPath
} | ConvertTo-Json -Depth 6
