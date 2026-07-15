Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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

    if ([string]::IsNullOrWhiteSpace($selected)) {
        $selected = $env:FLEXSHOW_CONFIG
    }
    if ([string]::IsNullOrWhiteSpace($selected)) {
        $selected = 'config/flexshow.json'
    }

    if (-not [System.IO.Path]::IsPathRooted($selected)) {
        $selected = Join-Path $root $selected
    }

    $fullPath = [System.IO.Path]::GetFullPath($selected)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "FlexShow config does not exist: $fullPath"
    }

    try {
        $null = Get-Content -LiteralPath $fullPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "FlexShow config is not valid JSON: $fullPath`n$($_.Exception.Message)"
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
        & $Executable @PrefixArgs -c 'import sys; raise SystemExit(0)' *> $null
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

    throw 'No usable Python 3 runtime was found. Set FLEXSHOW_PYTHON, create .venv, install Python, or install TouchDesigner.'
}

function Invoke-FlexShowCli {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('discover', 'validate', 'plan', 'diagnose', 'start', 'stop')]
        [string]$Command,

        [AllowEmptyString()]
        [string]$Config,

        [ValidateSet('', 'installation', 'vr', 'combined')]
        [string]$Experience = '',

        [ValidateSet('', 'fog', 'procedural', 'hybrid')]
        [string]$Completion = '',

        [ValidateSet('', 'auto', '3080ti_16gb', '4090', '5090')]
        [string]$Tier = '',

        [ValidateSet('None', 'DryRun', 'Execute')]
        [string]$ActionMode = 'None'
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

    switch ($ActionMode) {
        'DryRun' { $arguments.Add('--dry-run') }
        'Execute' { $arguments.Add('--execute') }
    }

    Write-Host "[FlexShow] command=$Command mode=$ActionMode config=$configPath python=$($python.Source)"
    & $python.Executable @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "FlexShow controller exited with code $exitCode."
    }
}
