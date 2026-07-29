[CmdletBinding()]
param(
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$workerPath = [System.IO.Path]::GetFullPath(
    (Join-Path $root 'tools\depth_anything_worker.py'))
$launcherPath = [System.IO.Path]::GetFullPath(
    (Join-Path $root 'scripts\Start-DepthAnythingWorker.ps1'))

function Get-CheckoutSensorWorkers {
    $workerNeedle = $workerPath.ToLowerInvariant()
    $launcherNeedle = $launcherPath.ToLowerInvariant()
    @(Get-CimInstance Win32_Process | Where-Object {
        $commandLine = [string]$_.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) {
            return $false
        }
        $normalized = $commandLine.ToLowerInvariant()
        ($normalized.Contains($workerNeedle) -and
            $normalized.Contains(' serve ') -and
            ($normalized.Contains('--capture webcam') -or
                $normalized.Contains('--capture auto'))) -or
        ($normalized.Contains($launcherNeedle) -and
            $normalized.Contains('-start'))
    })
}

$matches = Get-CheckoutSensorWorkers
$plan = [ordered]@{
    status = if ($Stop) { 'authorized' } else { 'preview' }
    repository = $root
    role = 'audience_camera_depth_anything'
    matching_process_ids = @($matches | ForEach-Object { [int]$_.ProcessId })
    matching_process_count = @($matches).Count
}
$plan | ConvertTo-Json -Depth 3

if (-not $Stop) {
    Write-Host '[Depth Anything Sensor] Preview only. Add -Stop to terminate this checkout''s audience-camera worker.'
    return
}

if (@($matches).Count -eq 0) {
    Write-Host '[Depth Anything Sensor] No matching audience-camera worker is running.'
    return
}

# Stop Python children before their PowerShell launcher so no orphaned camera
# process keeps the DirectShow device open.
$ordered = @($matches | Sort-Object @{
    Expression = {
        if ([string]$_.Name -match '^python') { 0 } else { 1 }
    }
})
foreach ($process in $ordered) {
    Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 250

$remaining = Get-CheckoutSensorWorkers
if (@($remaining).Count -ne 0) {
    $remainingIds = @($remaining | ForEach-Object { [int]$_.ProcessId })
    throw "Audience-camera worker stop was incomplete; remaining PIDs: $($remainingIds -join ', ')"
}

Write-Host '[Depth Anything Sensor] Checkout-scoped audience-camera worker stopped.'
