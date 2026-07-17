[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Install,
    [switch]$DownloadModel
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$venv = Join-Path $root '.venv\moge2'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $root 'integrations\moge2\requirements-runtime.txt'
$probe = Join-Path $root 'tools\moge2_probe.py'
$cache = Join-Path $root 'runtime\moge2-cache'
$model = Join-Path $root 'runtime\moge2-model\model.pt'
$mogeRevision = '07444410f1e33f402353b99d6ccd26bd31e469e8'
$mogeRequirement = "moge @ git+https://github.com/microsoft/MoGe.git@$mogeRevision"
$torchIndex = 'https://download.pytorch.org/whl/cu128'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Write-Host "[MoGe-2] $Label"
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Resolve-BasePython {
    if ($Python) {
        $candidate = [System.IO.Path]::GetFullPath($Python)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "Python was not found at the supplied path."
        }
        return $candidate
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $launcher) {
        throw 'Python launcher py.exe was not found; pass -Python C:\path\python.exe.'
    }
    $resolved = & $launcher.Source -3.11 -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or -not $resolved) {
        throw 'Python 3.11 is required for the pinned MoGe-2 environment.'
    }
    return [System.IO.Path]::GetFullPath(@($resolved)[-1].Trim())
}

$plan = [ordered]@{
    status = 'preview'
    repository = $root
    environment = $venv
    python = if ($Python) { $Python } else { 'py.exe -3.11' }
    pytorch = 'torch 2.11.0 + torchvision 0.26.0 (CUDA 12.8 official wheels)'
    moge_revision = $mogeRevision
    model_path = $model
    cache_path = $cache
    install_authorized = [bool]$Install
    download_authorized = [bool]$DownloadModel
}

if (-not $Install -and -not $DownloadModel) {
    $plan | ConvertTo-Json -Depth 4
    Write-Host '[MoGe-2] Preview only. Add -Install, then run -DownloadModel as a separate explicit step.'
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
    ) -Label 'Install pinned MoGe runtime dependencies'
    Invoke-Checked -Executable $venvPython -Arguments @(
        '-m', 'pip', 'install', '--no-deps', $mogeRequirement
    ) -Label 'Install pinned Microsoft MoGe source without optional app dependencies'
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw 'The MoGe-2 environment is missing. Run this script with -Install first.'
}

if ($DownloadModel) {
    Invoke-Checked -Executable $venvPython -Arguments @(
        $probe, 'model-install', '--model-path', $model, '--cache-dir', $cache
    ) -Label 'Download and SHA-256 verify the pinned official ViT-S model'
}

& $venvPython $probe doctor --profile 3080ti_16gb --model-path $model
$doctorCode = $LASTEXITCODE
if ($DownloadModel -and $doctorCode -ne 0) {
    throw "MoGe-2 doctor failed with exit code $doctorCode."
}
if (-not $DownloadModel -and $doctorCode -ne 0) {
    Write-Warning 'Runtime installation completed, but the model is intentionally absent. Run -DownloadModel next.'
}
