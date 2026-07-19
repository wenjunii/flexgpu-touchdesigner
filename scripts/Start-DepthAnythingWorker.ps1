[CmdletBinding()]
param(
    [ValidateSet('3080ti_16gb', '4090', '5090')]
    [string]$Profile = '3080ti_16gb',

    [ValidateSet('depth_anything', 'mock')]
    [string]$Backend = 'depth_anything',

    [ValidateSet('auto', 'webcam', 'mock')]
    [string]$Capture = 'auto',

    [ValidateRange(0, 31)]
    [int]$GpuIndex = 0,

    [ValidateRange(0, 31)]
    [int]$CameraIndex = 0,

    [ValidateSet('auto', 'msmf', 'dshow', 'any')]
    [string]$CameraBackend = 'auto',

    [ValidateRange(64, 7680)]
    [int]$CameraWidth = 640,

    [ValidateRange(64, 4320)]
    [int]$CameraHeight = 480,

    [ValidateRange(196, 1024)]
    [int]$InputSize = 384,

    [ValidateRange(64, 640)]
    [int]$OutputWidth = 256,

    [ValidateRange(64, 480)]
    [int]$OutputHeight = 144,

    [ValidateRange(0.1, 60.0)]
    [double]$InferenceHz = 5.0,

    [ValidateSet('session_frozen', 'fixed')]
    [string]$CalibrationMode = 'session_frozen',

    [ValidateRange(0.0, 100.0)]
    [double]$PercentileLow = 2.0,

    [ValidateRange(0.0, 100.0)]
    [double]$PercentileHigh = 98.0,

    [ValidateRange(1, 120)]
    [int]$CalibrationFrames = 12,

    [double]$RawLow,

    [double]$RawHigh,

    [ValidateSet('near_is_larger', 'near_is_smaller')]
    [string]$RawOrder = 'near_is_larger',

    [ValidateRange(0.01, 100.0)]
    [double]$PseudoNearM = 0.5,

    [ValidateRange(0.01, 100.0)]
    [double]$PseudoFarM = 4.0,

    [ValidateRange(0.01, 100.0)]
    [double]$ForegroundFarM = 3.0,

    [ValidateRange(1.0, 179.0)]
    [double]$HorizontalFovDeg = 70.0,

    [string]$OutputHost = '127.0.0.1',

    [ValidateRange(1, 65535)]
    [int]$OutputTcpPort = 9241,

    [ValidateRange(50, 60000)]
    [int]$StaleAfterMs = 800,

    [ValidateRange(1, 1000000000)]
    [int]$MaxFrames,

    [ValidateRange(0.01, 86400.0)]
    [double]$DurationSeconds,

    [switch]$AllowTrustedNetwork,

    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$isolatedPython = Join-Path $root '.venv\depth-anything\Scripts\python.exe'
$python = $isolatedPython
$worker = Join-Path $root 'tools\depth_anything_worker.py'
$model = Join-Path $root 'runtime\depth-anything-v2-small'
$cache = Join-Path $root 'runtime\depth-anything-cache'

function Assert-SafeHost {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 255 -or $Value -match '\s') {
        throw 'OutputHost must be a non-empty host or IP address without whitespace.'
    }
}

Assert-SafeHost -Value $OutputHost
if ($PercentileLow -ge $PercentileHigh) {
    throw 'PercentileLow must be lower than PercentileHigh.'
}
if ($PseudoNearM -ge $PseudoFarM) {
    throw 'PseudoNearM must be lower than PseudoFarM.'
}
if ($ForegroundFarM -lt $PseudoNearM -or $ForegroundFarM -gt $PseudoFarM) {
    throw 'ForegroundFarM must stay inside the pseudo-metre slab.'
}
if ([long]$OutputWidth * [long]$OutputHeight -gt 307200) {
    throw 'OutputWidth multiplied by OutputHeight must not exceed 307200 pixels.'
}
$hasRawLow = $PSBoundParameters.ContainsKey('RawLow')
$hasRawHigh = $PSBoundParameters.ContainsKey('RawHigh')
if ($CalibrationMode -eq 'fixed' -and (-not $hasRawLow -or -not $hasRawHigh)) {
    throw 'Fixed calibration requires both -RawLow and -RawHigh.'
}
if ($CalibrationMode -eq 'session_frozen' -and ($hasRawLow -or $hasRawHigh)) {
    throw 'RawLow/RawHigh are only valid with -CalibrationMode fixed.'
}
if ($CalibrationMode -eq 'fixed' -and $RawLow -ge $RawHigh) {
    throw 'RawLow must be lower than RawHigh.'
}

$resolvedCapture = if ($Capture -eq 'auto') {
    if ($Backend -eq 'mock') { 'mock' } else { 'webcam' }
}
else {
    $Capture
}
$isLoopback = $OutputHost -in @('127.0.0.1', 'localhost', '::1')
if (-not $isLoopback -and -not $AllowTrustedNetwork) {
    throw 'Non-loopback output requires -AllowTrustedNetwork. WorldBus is not authenticated or encrypted.'
}

$arguments = @(
    $worker, 'serve',
    '--profile', $Profile,
    '--backend', $Backend,
    '--capture', $Capture,
    '--device', 'cuda:0',
    '--model-dir', $model,
    '--cache-dir', $cache,
    '--camera-index', [string]$CameraIndex,
    '--camera-backend', $CameraBackend,
    '--camera-width', [string]$CameraWidth,
    '--camera-height', [string]$CameraHeight,
    '--input-size', [string]$InputSize,
    '--output-width', [string]$OutputWidth,
    '--output-height', [string]$OutputHeight,
    '--inference-hz', [string]$InferenceHz,
    '--horizontal-fov-deg', [string]$HorizontalFovDeg,
    '--calibration-mode', $CalibrationMode,
    '--percentile-low', [string]$PercentileLow,
    '--percentile-high', [string]$PercentileHigh,
    '--calibration-frames', [string]$CalibrationFrames,
    '--raw-order', $RawOrder,
    '--pseudo-near-m', [string]$PseudoNearM,
    '--pseudo-far-m', [string]$PseudoFarM,
    '--foreground-far-m', [string]$ForegroundFarM,
    '--output-host', $OutputHost,
    '--output-tcp-port', [string]$OutputTcpPort,
    '--stale-after-ms', [string]$StaleAfterMs
)
if ($hasRawLow) {
    $arguments += @('--raw-low', [string]$RawLow)
}
if ($hasRawHigh) {
    $arguments += @('--raw-high', [string]$RawHigh)
}
if ($AllowTrustedNetwork) {
    $arguments += '--allow-trusted-network'
}
if ($PSBoundParameters.ContainsKey('MaxFrames')) {
    $arguments += @('--max-frames', [string]$MaxFrames)
}
if ($PSBoundParameters.ContainsKey('DurationSeconds')) {
    $arguments += @('--duration-s', [string]$DurationSeconds)
}

$plan = [ordered]@{
    status = if ($Start) { 'authorized' } else { 'preview' }
    optional_sensor_emulator = $true
    profile = $Profile
    backend = $Backend
    capture = $resolvedCapture
    webcam_will_open = [bool]($Start -and $resolvedCapture -eq 'webcam')
    physical_gpu_index = $GpuIndex
    worker_device = 'cuda:0 (relative to CUDA_VISIBLE_DEVICES)'
    camera_index = $CameraIndex
    camera_backend = $CameraBackend
    camera_resolution = @($CameraWidth, $CameraHeight)
    input_size = $InputSize
    output_size = @($OutputWidth, $OutputHeight)
    output_limits = [ordered]@{
        max_width = 640
        max_height = 480
        max_pixels = 307200
    }
    inference_hz = $InferenceHz
    calibration = $CalibrationMode
    output_tcp = "$OutputHost`:$OutputTcpPort"
    reserved_udp_metadata = "$OutputHost`:9240 (not opened)"
    contains_rgb = $false
    execution = 'foreground; press Ctrl+C to stop'
}
$plan | ConvertTo-Json -Depth 4

if (-not $Start) {
    Write-Host '[Depth Anything] Preview only. Start the TouchDesigner sensor receiver first, then add -Start.'
    return
}
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) {
    throw 'tools\depth_anything_worker.py is missing.'
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
    Write-Host '[Depth Anything] Mock mode is using PATH Python; no model or webcam runtime is required.'
}
if ($Backend -eq 'depth_anything' -and -not (Test-Path -LiteralPath (Join-Path $model 'model.safetensors') -PathType Leaf)) {
    throw 'The pinned V2 Small model is missing. Run Initialize-DepthAnything.ps1 -DownloadModel first.'
}

$previousCuda = [Environment]::GetEnvironmentVariable('CUDA_VISIBLE_DEVICES', 'Process')
$previousPythonUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
try {
    $env:CUDA_VISIBLE_DEVICES = [string]$GpuIndex
    $env:PYTHONUTF8 = '1'
    Write-Host "[Depth Anything] Starting optional foreground worker; camera RGB stays process-local."
    & $python @arguments --start
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Depth Anything worker exited with code $exitCode."
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
