[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('3080ti_16gb', '4090', '5090')]
    [string]$Profile,

    [ValidateSet('moge2', 'mock')]
    [string]$Backend = 'moge2',

    [ValidateRange(0, 31)]
    [int]$GpuIndex = 0,

    [string]$NvidiaSmi = '',

    [switch]$AllowProfileMismatch,

    [string]$InputHost = '127.0.0.1',

    [ValidateRange(1, 65535)]
    [int]$InputTcpPort = 9211,

    [ValidateRange(1, 65535)]
    [int]$InputUdpPort = 9210,

    [string]$OutputHost = '127.0.0.1',

    [ValidateRange(1, 65535)]
    [int]$OutputTcpPort = 9221,

    [ValidateRange(0.0, 300.0)]
    [double]$ListenerWaitSeconds = 120.0,

    [ValidateRange(1, 1000000000)]
    [int]$MaxFrames,

    [ValidateRange(0.01, 86400.0)]
    [double]$DurationSeconds,

    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot '_GeneratedGeometry.Common.ps1')
$python = Join-Path $root '.venv\moge2\Scripts\python.exe'
$worker = Join-Path $root 'tools\moge2_worker.py'
$model = Join-Path $root 'runtime\moge2-model\model.pt'
$cache = Join-Path $root 'runtime\moge2-cache'

function Assert-SafeHost {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 255 -or $Value -match '\s') {
        throw "$Label must be a non-empty host or IP address without whitespace."
    }
}

Assert-SafeHost -Value $InputHost -Label 'InputHost'
Assert-SafeHost -Value $OutputHost -Label 'OutputHost'

$detectedGpu = $null
if ($Backend -eq 'moge2') {
    $detectedGpu = Assert-FlexGpuGeneratedGeometryProfile `
        -Profile $Profile `
        -GpuIndex $GpuIndex `
        -NvidiaSmi $NvidiaSmi `
        -AllowProfileMismatch:$AllowProfileMismatch
}

$arguments = @(
    $worker,
    'serve',
    '--profile', $Profile,
    '--backend', $Backend,
    '--device', 'cuda:0',
    '--model-path', $model,
    '--cache-dir', $cache,
    '--input-host', $InputHost,
    '--input-tcp-port', [string]$InputTcpPort,
    '--input-udp-port', [string]$InputUdpPort,
    '--output-host', $OutputHost,
    '--output-tcp-port', [string]$OutputTcpPort,
    '--output-connect-timeout-s', [string]$ListenerWaitSeconds
)
if ($PSBoundParameters.ContainsKey('MaxFrames')) {
    $arguments += @('--max-frames', [string]$MaxFrames)
}
if ($PSBoundParameters.ContainsKey('DurationSeconds')) {
    $arguments += @('--duration-s', [string]$DurationSeconds)
}

$plan = [ordered]@{
    status = if ($Start) { 'authorized' } else { 'preview' }
    profile = $Profile
    backend = $Backend
    physical_gpu_index = $GpuIndex
    worker_device = 'cuda:0 (relative to CUDA_VISIBLE_DEVICES)'
    detected_gpu = if ($null -eq $detectedGpu) { 'not required by mock backend' } else { $detectedGpu }
    profile_mismatch_override = [bool]$AllowProfileMismatch
    input_tcp = "$InputHost`:$InputTcpPort"
    input_udp = "$InputHost`:$InputUdpPort"
    output_tcp = "$OutputHost`:$OutputTcpPort"
    listener_wait_seconds = $ListenerWaitSeconds
    python = $python
    worker = $worker
    model = if ($Backend -eq 'moge2') { $model } else { 'not used by mock backend' }
    execution = 'foreground; press Ctrl+C to stop'
}
$plan | ConvertTo-Json -Depth 4

if (-not $Start) {
    Write-Host '[MoGe-2] Preview only. Select moge2 in SHOW_CONTROL, then add -Start in this separate PowerShell. The worker waits for the listener.'
    return
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The isolated MoGe-2 Python environment is missing. Run Initialize-MoGe2.ps1 -Install first.'
}
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) {
    throw 'tools\moge2_worker.py is missing.'
}
if ($Backend -eq 'moge2' -and -not (Test-Path -LiteralPath $model -PathType Leaf)) {
    throw 'The pinned MoGe-2 model is missing. Run Initialize-MoGe2.ps1 -DownloadModel first.'
}
if ($InputHost -notin @('127.0.0.1', 'localhost', '::1') -or
        $OutputHost -notin @('127.0.0.1', 'localhost', '::1')) {
    Write-Warning 'WorldBus v1 is not authenticated or encrypted. Use only a trusted private show network and firewall these ports.'
}

Assert-FlexGpuNoGeneratedGeometryWorker -RepositoryRoot $root

$previousCuda = [Environment]::GetEnvironmentVariable('CUDA_VISIBLE_DEVICES', 'Process')
$previousPythonUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
try {
    $env:CUDA_VISIBLE_DEVICES = [string]$GpuIndex
    $env:PYTHONUTF8 = '1'
    Write-Host "[MoGe-2] Starting foreground worker on physical GPU $GpuIndex."
    & $python @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "MoGe-2 worker exited with code $exitCode."
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
