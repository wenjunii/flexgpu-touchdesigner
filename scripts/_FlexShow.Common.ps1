#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'FlexShow operator scripts currently support Windows only.'
}

function Get-FlexShowRepositoryRoot {
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
}

function Resolve-FlexShowConfig {
    param(
        [AllowEmptyString()]
        [string]$Config
    )

    $root = Get-FlexShowRepositoryRoot
    $selected = $Config
    $useRepositoryRoot = $false

    if ([string]::IsNullOrWhiteSpace($selected)) {
        $selected = $env:FLEXSHOW_CONFIG
    }
    if ([string]::IsNullOrWhiteSpace($selected)) {
        $selected = $env:FLEXGPU_CONFIG
    }
    if ([string]::IsNullOrWhiteSpace($selected)) {
        $selected = 'config/flexshow.json'
        $useRepositoryRoot = $true
    }

    if (-not [System.IO.Path]::IsPathRooted($selected)) {
        $basePath = if ($useRepositoryRoot) { $root } else { (Get-Location).Path }
        $selected = Join-Path $basePath $selected
    }

    $fullPath = [System.IO.Path]::GetFullPath($selected)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "FlexShow config does not exist: $fullPath"
    }

    return $fullPath
}

function Test-FlexShowPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [string[]]$PrefixArgs = @()
    )

    try {
        & $Executable @PrefixArgs -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Get-FlexShowPython {
    $root = Get-FlexShowRepositoryRoot
    $candidates = [System.Collections.Generic.List[object]]::new()

    if (-not [string]::IsNullOrWhiteSpace($env:FLEXSHOW_PYTHON)) {
        $candidates.Add([pscustomobject]@{
            Executable = $env:FLEXSHOW_PYTHON
            PrefixArgs = @()
            Source = 'FLEXSHOW_PYTHON'
        })
    }

    foreach ($relativePath in @('.venv/Scripts/python.exe', 'venv/Scripts/python.exe')) {
        $candidatePath = Join-Path $root $relativePath
        if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
            $candidates.Add([pscustomobject]@{
                Executable = $candidatePath
                PrefixArgs = @()
                Source = $relativePath
            })
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        $candidates.Add([pscustomobject]@{
            Executable = $pythonCommand.Source
            PrefixArgs = @()
            Source = 'PATH:python'
        })
    }

    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand) {
        $candidates.Add([pscustomobject]@{
            Executable = $pyCommand.Source
            PrefixArgs = @('-3')
            Source = 'PATH:py -3'
        })
    }

    $derivativeRoot = Join-Path $env:ProgramFiles 'Derivative'
    if (Test-Path -LiteralPath $derivativeRoot -PathType Container) {
        $touchDesignerFolders = Get-ChildItem -LiteralPath $derivativeRoot -Directory -Filter 'TouchDesigner*' -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTime -Descending

        foreach ($folder in $touchDesignerFolders) {
            $embeddedPython = Join-Path $folder.FullName 'bin/python.exe'
            if (Test-Path -LiteralPath $embeddedPython -PathType Leaf) {
                $candidates.Add([pscustomobject]@{
                    Executable = $embeddedPython
                    PrefixArgs = @()
                    Source = "TouchDesigner:$($folder.Name)"
                })
            }
        }
    }

    foreach ($candidate in $candidates) {
        if (Test-FlexShowPython -Executable $candidate.Executable -PrefixArgs $candidate.PrefixArgs) {
            return $candidate
        }
    }

    throw 'No usable Python 3.10+ runtime was found. Set FLEXSHOW_PYTHON, create .venv, install Python, or install TouchDesigner.'
}

function Invoke-FlexShowCli {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('discover', 'validate', 'plan', 'diagnose', 'start', 'stop', 'status', 'recover')]
        [string]$Command,

        [AllowEmptyString()]
        [string]$Config,

        [ValidateSet('', 'installation', 'vr', 'combined')]
        [string]$Experience = '',

        [ValidateSet('', 'fog', 'procedural', 'hybrid')]
        [string]$Completion = '',

        [ValidateSet('', 'auto', '3080ti_16gb', '4090', '5090', 'custom')]
        [string]$Tier = '',

        [AllowEmptyString()]
        [string]$NvidiaSmi = '',

        [ValidateRange(1, 3)]
        [int]$RecoveryAttempts = 1,

        [ValidateRange(0, 600000)]
        [Nullable[int]]$WaitReadyMs = $null,

        [switch]$RestartRunning,

        [ValidateSet('None', 'DryRun', 'Execute')]
        [string]$ActionMode = 'None',

        [switch]$Json,

        [switch]$ExitWithCode
    )

    $root = Get-FlexShowRepositoryRoot
    $toolPath = Join-Path $root 'tools/flexgpu.py'
    if (-not (Test-Path -LiteralPath $toolPath -PathType Leaf)) {
        throw "FlexShow controller does not exist: $toolPath"
    }

    $configPath = Resolve-FlexShowConfig -Config $Config
    $python = Get-FlexShowPython

    $arguments = [System.Collections.Generic.List[string]]::new()
    foreach ($prefixArg in $python.PrefixArgs) {
        $arguments.Add($prefixArg)
    }
    $arguments.Add($toolPath)
    $arguments.Add($Command)
    $arguments.Add('--config')
    $arguments.Add($configPath)

    if (-not [string]::IsNullOrWhiteSpace($Experience)) {
        $arguments.Add('--experience')
        $arguments.Add($Experience)
    }
    if (-not [string]::IsNullOrWhiteSpace($Completion)) {
        $arguments.Add('--completion')
        $arguments.Add($Completion)
    }
    if (-not [string]::IsNullOrWhiteSpace($Tier)) {
        $arguments.Add('--tier')
        $arguments.Add($Tier)
    }
    if (-not [string]::IsNullOrWhiteSpace($NvidiaSmi)) {
        $arguments.Add('--nvidia-smi')
        $arguments.Add($NvidiaSmi)
    }
    if ($Command -eq 'recover') {
        $arguments.Add('--attempts')
        $arguments.Add([string]$RecoveryAttempts)
        if ($RestartRunning) {
            $arguments.Add('--restart-running')
        }
    }
    if ($null -ne $WaitReadyMs -and $Command -in @('start', 'recover')) {
        $arguments.Add('--wait-ready-ms')
        # PowerShell unwraps Nullable[int] values during parameter binding, so
        # an explicitly supplied value is normally an Int32 rather than an
        # object with a .Value property.
        $arguments.Add([string]([int]$WaitReadyMs))
    }

    switch ($ActionMode) {
        'DryRun' { $arguments.Add('--dry-run') }
        'Execute' { $arguments.Add('--execute') }
    }
    if ($Json) {
        $arguments.Add('--json')
    }

    if (-not $Json) {
        Write-Host "[FlexShow] command=$Command mode=$ActionMode config=$configPath python=$($python.Source)"
    }
    # Native stderr is useful structured CLI output, not a PowerShell parsing
    # failure. Keep it visible while preventing ErrorActionPreference=Stop from
    # interrupting us before the controller exit code is captured.
    $previousErrorActionPreference = $ErrorActionPreference
    $hadNativePreference = Test-Path Variable:PSNativeCommandUseErrorActionPreference
    $previousNativePreference = $null
    if ($hadNativePreference) {
        $previousNativePreference = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = 'Continue'
        & $python.Executable @arguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hadNativePreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }
    if ($exitCode -ne 0) {
        if ($ExitWithCode) {
            exit $exitCode
        }
        throw "FlexShow controller exited with code $exitCode."
    }
}
