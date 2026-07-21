[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Install,
    [switch]$DownloadModel
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$venv = Join-Path $root '.venv\depth-anything'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $root 'integrations\depth_anything\requirements-runtime.txt'
$worker = Join-Path $root 'tools\depth_anything_worker.py'
$cache = Join-Path $root 'runtime\depth-anything-cache'
$model = Join-Path $root 'runtime\depth-anything-v2-small'
$modelId = 'depth-anything/Depth-Anything-V2-Small-hf'
$modelRevision = '870a35c76c2bc1d82fbde922d95015496cb7dd6c'
$modelSha256 = '3152477ce0d8d6978d76b995120de97cb5b928701fd0f817769f59e249a16b70'
$torchIndex = 'https://download.pytorch.org/whl/cu128'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Write-Host "[Depth Anything] $Label"
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Resolve-BasePython {
    if ($Python) {
        $candidate = [System.IO.Path]::GetFullPath($Python)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw 'Python was not found at the supplied path.'
        }
        return $candidate
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $launcher) {
        throw 'Python launcher py.exe was not found; pass -Python C:\path\python.exe.'
    }
    $resolved = & $launcher.Source -3.11 -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or -not $resolved) {
        throw 'Python 3.11 is required for the isolated Depth Anything environment.'
    }
    return [System.IO.Path]::GetFullPath(@($resolved)[-1].Trim())
}

$plan = [ordered]@{
    status = 'preview'
    optional_sensor_emulator = $true
    repository = $root
    environment = $venv
    python = if ($Python) { $Python } else { 'py.exe -3.11' }
    pytorch = 'torch 2.11.0 + torchvision 0.26.0 (CUDA 12.8 official wheels)'
    model_id = $modelId
    model_revision = $modelRevision
    model_sha256 = $modelSha256
    model_license = 'Apache-2.0 (Small only; review upstream terms before deployment)'
    model_path = $model
    cache_path = $cache
    install_authorized = [bool]$Install
    download_authorized = [bool]$DownloadModel
}

if (-not $Install -and -not $DownloadModel) {
    $plan | ConvertTo-Json -Depth 4
    Write-Host '[Depth Anything] Preview only. Add -Install, then use -DownloadModel as a separate explicit step.'
    return
}

$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:HF_HOME = $cache
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'

if ($Install) {
    $basePython = Resolve-BasePython
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-Checked -Executable $basePython -Arguments @('-m', 'venv', $venv) -Label 'Create isolated Python environment'
    }
    Invoke-Checked -Executable $venvPython -Arguments @('-m', 'pip', 'install', 'pip==25.1.1') -Label 'Pin pip'
    Invoke-Checked -Executable $venvPython -Arguments @(
        '-m', 'pip', 'install',
        'torch==2.11.0', 'torchvision==0.26.0',
        '--index-url', $torchIndex
    ) -Label 'Install pinned PyTorch CUDA runtime'
    Invoke-Checked -Executable $venvPython -Arguments @(
        '-m', 'pip', 'install', '-r', $requirements
    ) -Label 'Install pinned Depth Anything runtime dependencies'
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw 'The Depth Anything environment is missing. Run this script with -Install first.'
}

if ($DownloadModel) {
    Invoke-Checked -Executable $venvPython -Arguments @(
        $worker, 'model-install', '--model-dir', $model, '--cache-dir', $cache
    ) -Label 'Download and SHA-256 verify the pinned official V2 Small model'
}

& $venvPython $worker doctor --model-dir $model
$doctorCode = $LASTEXITCODE
if ($DownloadModel -and $doctorCode -ne 0) {
    throw "Depth Anything doctor failed with exit code $doctorCode."
}
if (-not $DownloadModel -and $doctorCode -ne 0) {
    Write-Warning 'Runtime installation completed, but the model is intentionally absent. Run -DownloadModel separately if you want the optional real backend.'
}
