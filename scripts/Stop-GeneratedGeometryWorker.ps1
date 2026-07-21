<#
.SYNOPSIS
Previews or stops generated-image geometry workers from this checkout only.

.DESCRIPTION
Without -Stop this command is read-only. With -Stop it terminates only MoGe-2
or Depth Anything generated-geometry processes whose command line contains this
repository's exact tools\moge2_worker.py path. It cannot match the separate
audience-camera Depth Anything worker or a worker from another checkout.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('all', 'moge2', 'depth_anything')]
    [string]$Provider = 'all',

    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot '_GeneratedGeometry.Common.ps1')

$workers = @(Get-FlexGpuGeneratedGeometryWorkers -RepositoryRoot $root |
    Where-Object { $Provider -eq 'all' -or $_.Provider -eq $Provider })
$plan = [ordered]@{
    status = if ($Stop) { 'authorized' } else { 'preview' }
    provider = $Provider
    repository_root = $root
    matching_processes = @($workers | ForEach-Object {
        [ordered]@{
            provider = $_.Provider
            process_id = $_.ProcessId
            parent_process_id = $_.ParentProcessId
            name = $_.Name
        }
    })
}
$plan | ConvertTo-Json -Depth 5

if (-not $Stop) {
    Write-Host '[Generated Geometry] Preview only. Add -Stop to terminate the exact matching worker processes.'
    return
}
if ($workers.Count -eq 0) {
    Write-Host '[Generated Geometry] No matching worker is running.'
    return
}

$processIds = @($workers | ForEach-Object { $_.ProcessId })
$ordered = @($workers | Sort-Object @{
    Expression = {
        if ($processIds -contains $_.ParentProcessId) { 0 } else { 1 }
    }
})
foreach ($worker in $ordered) {
    Stop-Process -Id $worker.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500

$remaining = @(Get-FlexGpuGeneratedGeometryWorkers -RepositoryRoot $root |
    Where-Object { $Provider -eq 'all' -or $_.Provider -eq $Provider })
if ($remaining.Count -gt 0) {
    $remainingIds = ($remaining | ForEach-Object { $_.ProcessId }) -join ', '
    throw "Generated-geometry worker processes remain after stop: $remainingIds"
}
Write-Host '[Generated Geometry] Matching worker processes stopped.'
