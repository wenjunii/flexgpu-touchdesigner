[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('3080ti_16gb', '4090', '5090')]
    [string]$Profile,

    [ValidateSet('depth_anything', 'mock')]
    [string]$Backend = 'depth_anything',

    [ValidateRange(0, 31)]
    [int]$GpuIndex = 0,

    [string]$NvidiaSmi = '',

    [switch]$AllowProfileMismatch,

    [ValidateRange(1, 65535)]
    [int]$InputTcpPort = 9251,

    [ValidateRange(1, 65535)]
    [int]$InputUdpPort = 9250,

    [ValidateRange(1, 65535)]
    [int]$OutputTcpPort = 9261,

    [ValidateRange(0.0, 300.0)]
    [double]$ListenerWaitSeconds = 120.0,

    [ValidateRange(196, 1024)]
    [int]$InputSize = 384,

    [ValidateRange(64, 2048)]
    [int]$MaxEdge,

    [ValidateRange(4096, 4194304)]
    [int]$TargetPixels,

    [ValidateRange(1, 120)]
    [int]$CalibrationFrames = 12,

    [ValidateRange(0.0, 100.0)]
    [double]$PercentileLow = 2.0,

    [ValidateRange(0.0, 100.0)]
    [double]$PercentileHigh = 98.0,

    [ValidateSet('near_is_larger', 'near_is_smaller')]
    [string]$RawOrder = 'near_is_larger',

    [ValidateRange(0.01, 100.0)]
    [double]$PseudoNearM = 0.5,

    [ValidateRange(0.01, 100.0)]
    [double]$PseudoFarM = 4.0,

    [ValidateRange(0.01, 100.0)]
    [double]$ForegroundFarM = 4.0,

    [ValidateRange(1.0, 179.0)]
    [double]$HorizontalFovDeg = 60.0,

    [ValidateRange(1, 1000000000)]
    [int]$MaxFrames,

    [ValidateRange(0.01, 86400.0)]
    [double]$DurationSeconds,

    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot '_GeneratedGeometry.Common.ps1')
$isolatedPython = Join-Path $root '.venv\depth-anything\Scripts\python.exe'
$python = $isolatedPython
$worker = Join-Path $root 'tools\moge2_worker.py'
$model = Join-Path $root 'runtime\depth-anything-v2-small'
$cache = Join-Path $root 'runtime\depth-anything-cache'

if ($PercentileLow -ge $PercentileHigh) {
    throw 'PercentileLow must be lower than PercentileHigh.'
}
if ($PseudoNearM -ge $PseudoFarM) {
    throw 'PseudoNearM must be lower than PseudoFarM.'
}
if ($ForegroundFarM -lt $PseudoNearM -or $ForegroundFarM -gt $PseudoFarM) {
    throw 'ForegroundFarM must stay inside the pseudo-metre slab.'
}

$detectedGpu = $null
if ($Backend -eq 'depth_anything') {
    $detectedGpu = Assert-FlexGpuGeneratedGeometryProfile `
        -Profile $Profile `
        -GpuIndex $GpuIndex `
        -NvidiaSmi $NvidiaSmi `
        -AllowProfileMismatch:$AllowProfileMismatch
}

$profileMaxEdges = @{
    '3080ti_16gb' = 384
    '4090' = 512
    '5090' = 512
}
$effectiveMaxEdge = if ($PSBoundParameters.ContainsKey('MaxEdge')) {
    $MaxEdge
}
elseif ($Profile -eq '3080ti_16gb') {
    512
}
else {
    [int]$profileMaxEdges[$Profile]
}
$effectiveTargetPixels = if ($PSBoundParameters.ContainsKey('TargetPixels')) {
    $TargetPixels
}
elseif ($Profile -eq '3080ti_16gb') {
    147456
}
else {
    $null
}
$geometryBudgetSource = if ($PSBoundParameters.ContainsKey('TargetPixels') -or
        $PSBoundParameters.ContainsKey('MaxEdge')) {
    'operator override'
}
elseif ($Profile -eq '3080ti_16gb') {
    '3080 adaptive default'
}
else {
    'worker profile default'
}

$arguments = @(
    $worker, 'serve',
    '--profile', $Profile,
    '--backend', $Backend,
    '--provider', 'depth_anything',
    '--device', 'cuda:0',
    '--model-dir', $model,
    '--cache-dir', $cache,
    '--input-host', '127.0.0.1',
    '--input-tcp-port', [string]$InputTcpPort,
    '--input-udp-port', [string]$InputUdpPort,
    '--output-host', '127.0.0.1',
    '--output-tcp-port', [string]$OutputTcpPort,
    '--output-connect-timeout-s', [string]$ListenerWaitSeconds,
    '--input-size', [string]$InputSize,
    '--max-edge', [string]$effectiveMaxEdge,
    '--calibration-frames', [string]$CalibrationFrames,
    '--percentile-low', [string]$PercentileLow,
    '--percentile-high', [string]$PercentileHigh,
    '--raw-order', $RawOrder,
    '--pseudo-near-m', [string]$PseudoNearM,
    '--pseudo-far-m', [string]$PseudoFarM,
    '--foreground-far-m', [string]$ForegroundFarM,
    '--horizontal-fov-deg', [string]$HorizontalFovDeg
)
if ($null -ne $effectiveTargetPixels) {
    $arguments += @('--target-pixels', [string]$effectiveTargetPixels)
}
if ($PSBoundParameters.ContainsKey('MaxFrames')) {
    $arguments += @('--max-frames', [string]$MaxFrames)
}
if ($PSBoundParameters.ContainsKey('DurationSeconds')) {
    $arguments += @('--duration-s', [string]$DurationSeconds)
}

$plan = [ordered]@{
    status = if ($Start) { 'authorized' } else { 'preview' }
    role = 'generated_image_geometry'
    geometry_provider = 'depth_anything'
    profile = $Profile
    backend = $Backend
    physical_gpu_index = $GpuIndex
    worker_device = 'cuda:0 (relative to CUDA_VISIBLE_DEVICES)'
    detected_gpu = if ($null -eq $detectedGpu) { 'not required by mock backend' } else { $detectedGpu }
    profile_mismatch_override = [bool]$AllowProfileMismatch
    input_tcp = "127.0.0.1`:$InputTcpPort"
    input_udp = "127.0.0.1`:$InputUdpPort"
    output_tcp = "127.0.0.1`:$OutputTcpPort"
    listener_wait_seconds = $ListenerWaitSeconds
    input_size = $InputSize
    geometry_max_edge = $effectiveMaxEdge
    geometry_target_pixels = $effectiveTargetPixels
    geometry_budget_source = $geometryBudgetSource
    adaptive_geometry_examples = if ($Profile -eq '3080ti_16gb') {
        @(
            '512x512 -> 384x384',
            '1024x567 -> 512x284',
            '1024x576 -> 512x288'
        )
    }
    else {
        @('profile max-edge default; no 3080 target-pixel override')
    }
    calibration_frames = $CalibrationFrames
    pseudo_metre_slab = @($PseudoNearM, $PseudoFarM)
    python = $python
    worker = $worker
    model = if ($Backend -eq 'depth_anything') { $model } else { 'not used by mock backend' }
    contains_generated_rgb = $true
    opens_webcam = $false
    execution = 'foreground; press Ctrl+C to stop'
}
$plan | ConvertTo-Json -Depth 4

if (-not $Start) {
    Write-Host '[Depth Anything Geometry] Preview only. Select depth_anything in SHOW_CONTROL, then add -Start. The worker waits for the listener.'
    return
}
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) {
    throw 'tools\moge2_worker.py is missing.'
}
if (-not (Test-Path -LiteralPath $isolatedPython -PathType Leaf)) {
    if ($Backend -ne 'mock') {
        throw 'The isolated Depth Anything environment is missing. Run Initialize-DepthAnything.ps1 -Install first.'
    }
    $pathPython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $pathPython) {
        throw 'Mock mode needs Python with NumPy on PATH, or the isolated Depth Anything environment.'
    }
    $python = $pathPython.Source
}
if ($Backend -eq 'depth_anything' -and
        -not (Test-Path -LiteralPath (Join-Path $model 'model.safetensors') -PathType Leaf)) {
    throw 'The pinned V2 Small model is missing. Run Initialize-DepthAnything.ps1 -DownloadModel first.'
}

Assert-FlexGpuNoGeneratedGeometryWorker -RepositoryRoot $root

$previousCuda = [Environment]::GetEnvironmentVariable('CUDA_VISIBLE_DEVICES', 'Process')
$previousPythonUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
try {
    $env:CUDA_VISIBLE_DEVICES = [string]$GpuIndex
    $env:PYTHONUTF8 = '1'
    try {
        $Host.UI.RawUI.WindowTitle = "FlexGPU Depth Anything Geometry Worker [$Profile, GPU $GpuIndex]"
    }
    catch {
        # Window titles are best-effort for non-console PowerShell hosts.
    }
    Write-Host "[Depth Anything Geometry] Starting foreground worker on physical GPU $GpuIndex."
    & $python @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Depth Anything geometry worker exited with code $exitCode."
    }
}
finally {
    if ($null -eq $previousCuda) {
        Remove-Item Env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue
    }
    else {
        $env:CUDA_VISIBLE_DEVICES = $previousCuda
    }
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
}
