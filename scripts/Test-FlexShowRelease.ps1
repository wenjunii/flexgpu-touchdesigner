<#
.SYNOPSIS
Runs the complete non-publishing FlexShow source release verification.

.DESCRIPTION
Compiles Python sources, validates shipped profiles, runs the unit suite and
synthetic benchmark, parses every PowerShell script, smoke-tests the machine
initializer with synthetic hardware, and checks the exact public surface.

The script never launches TouchDesigner or show processes. Live TouchDesigner,
private adapter, physical sensor, dual-GPU, headset, and venue acceptance tests
remain separate machine-local gates. It uses temporary and ignored outputs but
never changes Git state or tracked project files.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipPublicSync,

    [switch]$AllRefs
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

if ($SkipPublicSync -and $AllRefs) {
    throw '-AllRefs cannot be combined with -SkipPublicSync.'
}

$root = Get-FlexShowRepositoryRoot
$python = Get-FlexShowPython
$publicChecker = Join-Path $PSScriptRoot 'Test-PublicSync.ps1'
$initializer = Join-Path $PSScriptRoot 'Initialize-FlexShow.ps1'
$startWrapper = Join-Path $PSScriptRoot 'Start-FlexShow.ps1'
$recoverWrapper = Join-Path $PSScriptRoot 'Recover-FlexShow.ps1'

function Invoke-CheckedPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host "[FlexShow release] $Name"
    $commandArguments = [System.Collections.Generic.List[string]]::new()
    foreach ($prefixArgument in $python.PrefixArgs) {
        $commandArguments.Add($prefixArgument)
    }
    foreach ($argument in $Arguments) {
        $commandArguments.Add($argument)
    }

    $previousErrorActionPreference = $ErrorActionPreference
    $hadNativePreference = Test-Path Variable:PSNativeCommandUseErrorActionPreference
    $previousNativePreference = $null
    if ($hadNativePreference) {
        $previousNativePreference = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        # unittest writes normal progress to stderr. Do not promote it to a
        # terminating PowerShell error; the captured native exit code remains
        # authoritative for every Python step.
        $ErrorActionPreference = 'Continue'
        & $python.Executable @commandArguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hadNativePreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }
    if ($exitCode -ne 0) {
        throw "$Name failed with code $exitCode."
    }
}

function Test-PowerShellSources {
    Write-Host '[FlexShow release] Parse PowerShell scripts'
    $failures = [System.Collections.Generic.List[string]]::new()
    $files = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' -File |
        Sort-Object -Property Name)
    foreach ($file in $files) {
        $tokens = $null
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $file.FullName,
            [ref]$tokens,
            [ref]$parseErrors
        ) | Out-Null
        foreach ($parseError in @($parseErrors)) {
            $failures.Add(
                "$($file.Name):$($parseError.Extent.StartLineNumber): $($parseError.Message)"
            )
        }
    }
    if ($failures.Count -gt 0) {
        throw "PowerShell parsing failed:`n$($failures -join [Environment]::NewLine)"
    }
    Write-Host "[FlexShow release] parsed $($files.Count) PowerShell script(s)"
}

function Test-InitializerSmoke {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TemporaryDirectory,

        [Parameter(Mandatory = $true)]
        [string]$LocalConfig
    )

    Write-Host '[FlexShow release] Smoke-test machine initializer'
    $fakeSmi = Join-Path $TemporaryDirectory 'nvidia-smi.cmd'
    $fakeTouchDesigner = Join-Path $TemporaryDirectory 'TouchDesigner.exe'
    $smiOutput = @'
@echo off
echo 0, GPU-low, 00000000:01:00.0, NVIDIA GeForce RTX 3080 Ti Laptop GPU, 16384, 555.10
echo 1, GPU-high, 00000000:02:00.0, NVIDIA GeForce RTX 4090, 24564, 555.10
'@
    [System.IO.File]::WriteAllText(
        $fakeSmi,
        $smiOutput,
        [System.Text.Encoding]::ASCII
    )
    [System.IO.File]::WriteAllBytes($fakeTouchDesigner, [byte[]]@())

    $preview = & $initializer `
        -Topology auto `
        -NvidiaSmi $fakeSmi `
        -TouchDesignerExe $fakeTouchDesigner `
        -Output $LocalConfig `
        -WhatIf `
        -Json
    if (Test-Path -LiteralPath $LocalConfig) {
        throw 'Initializer -WhatIf unexpectedly wrote a local configuration.'
    }
    $previewResult = @($preview)[-1] | ConvertFrom-Json
    if ($previewResult.status -ne 'ok' -or $previewResult.topology -ne 'dual_local') {
        throw 'Initializer -WhatIf returned an unexpected plan.'
    }

    $output = & $initializer `
        -Topology auto `
        -NvidiaSmi $fakeSmi `
        -TouchDesignerExe $fakeTouchDesigner `
        -Output $LocalConfig `
        -Force `
        -Json
    if (-not (Test-Path -LiteralPath $LocalConfig -PathType Leaf)) {
        throw 'Initializer smoke test did not write its local configuration.'
    }
    $result = @($output)[-1] | ConvertFrom-Json
    if ($result.status -ne 'ok' -or $result.topology -ne 'dual_local' -or
        $result.tier -ne 'auto' -or $result.ai_tier -ne '4090' -or
        $result.render_tier -ne '3080ti_16gb' -or
        $result.ai_gpu.index -ne 1 -or $result.render_gpu.index -ne 0) {
        throw 'Initializer smoke test returned an unexpected GPU assignment.'
    }

    $written = Get-Content -LiteralPath $LocalConfig -Raw | ConvertFrom-Json
    if ($written.topology -ne $result.topology -or
        $written.tier -ne $result.tier -or
        $written.gpu.ai.uuid -ne $result.ai_gpu.uuid -or
        $written.gpu.render.uuid -ne $result.render_gpu.uuid -or
        $written.transport.type -ne 'touch_tcp') {
        throw 'Written initializer configuration does not match its result.'
    }

    Invoke-CheckedPython -Name 'Validate initializer output' -Arguments @(
        'tools/validate_configs.py',
        $LocalConfig
    )

    Write-Host '[FlexShow release] Smoke-test Start/Recover readiness arguments'
    foreach ($wrapper in @($startWrapper, $recoverWrapper)) {
        foreach ($testCase in @(
            [pscustomobject]@{ Name = 'omitted'; Include = $false; Expected = 0 },
            [pscustomobject]@{ Name = 'zero'; Include = $true; Expected = 0 },
            [pscustomobject]@{ Name = 'nonzero'; Include = $true; Expected = 2500 }
        )) {
            $wrapperArguments = @{
                Config = $LocalConfig
                NvidiaSmi = $fakeSmi
                Json = $true
            }
            if ($testCase.Include) {
                $wrapperArguments['WaitReadyMs'] = $testCase.Expected
            }
            $wrapperOutput = & $wrapper @wrapperArguments
            $wrapperResult = @($wrapperOutput)[-1] | ConvertFrom-Json
            if ($wrapperResult.status -ne 'dry-run' -or
                [int]$wrapperResult.runtime.wait_ready_ms -ne $testCase.Expected) {
                $wrapperName = [System.IO.Path]::GetFileName($wrapper)
                throw "$wrapperName failed the $($testCase.Name) WaitReadyMs preview contract."
            }
        }
    }
}

$temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$verificationId = [guid]::NewGuid().ToString('N')
$temporaryDirectory = [System.IO.Path]::GetFullPath(
    (Join-Path $temporaryRoot ("flexshow-release-$verificationId"))
)
$localConfig = Join-Path $root ("config\local-release-$verificationId.json")
$benchmarkSummary = Join-Path $temporaryDirectory 'benchmark-summary.json'

Push-Location $root
try {
    New-Item -ItemType Directory -Path $temporaryDirectory -Force | Out-Null

    Write-Host "[FlexShow release] Python source: $($python.Source)"
    try {
        Invoke-CheckedPython -Name 'Check pinned schema validator' -Arguments @(
            '-c',
            "import importlib.metadata as m; raise SystemExit(0 if m.version('jsonschema') == '4.17.3' else 1)"
        )
    }
    catch {
        throw "The selected Python runtime ($($python.Source)) must contain jsonschema==4.17.3. Install it into that exact interpreter or set FLEXSHOW_PYTHON. $($_.Exception.Message)"
    }

    try {
        Invoke-CheckedPython -Name 'Check NumPy source-test dependency' -Arguments @(
            '-c',
            "import numpy"
        )
    }
    catch {
        throw "The selected Python runtime ($($python.Source)) must contain NumPy. Install requirements-test.txt into that exact interpreter or set FLEXSHOW_PYTHON. $($_.Exception.Message)"
    }

    Invoke-CheckedPython -Name 'Compile Python sources' -Arguments @(
        '-m', 'compileall', '-q', 'src', 'tools', 'tests', 'touchdesigner'
    )
    Invoke-CheckedPython -Name 'Validate shipped configurations' -Arguments @(
        'tools/validate_configs.py'
    )
    Invoke-CheckedPython -Name 'Run unit tests' -Arguments @(
        '-m', 'unittest', 'discover', '-s', 'tests', '-v'
    )
    Invoke-CheckedPython -Name 'Run synthetic 3080 Ti benchmark' -Arguments @(
        'tools/benchmark_flexshow.py',
        'synthetic',
        '--tier', '3080ti_16gb',
        '--samples', '32',
        '--pattern', 'cycle',
        '--summary-json', $benchmarkSummary,
        '--compact'
    )
    if (-not (Test-Path -LiteralPath $benchmarkSummary -PathType Leaf)) {
        throw 'Synthetic benchmark did not write its summary.'
    }
    $benchmark = Get-Content -LiteralPath $benchmarkSummary -Raw | ConvertFrom-Json
    if ($benchmark.status -ne 'ok' -or $benchmark.processed_samples -ne 32) {
        throw 'Synthetic benchmark summary failed its result contract.'
    }

    Test-PowerShellSources
    Test-InitializerSmoke `
        -TemporaryDirectory $temporaryDirectory `
        -LocalConfig $localConfig

    if (-not $SkipPublicSync) {
        if (-not (Test-Path -LiteralPath $publicChecker -PathType Leaf)) {
            throw "Public-sync checker does not exist: $publicChecker"
        }
        if ($AllRefs) {
            Write-Host '[FlexShow release] Scan all local refs and public candidates'
            & $publicChecker -Scope All -SelfTest
        }
        else {
            Write-Host '[FlexShow release] Scan exact publish candidates and index'
            & $publicChecker -Scope Both -SelfTest
            Write-Host '[FlexShow release] Scan history reachable from HEAD'
            & $publicChecker -Scope History -Revision HEAD
        }
    }

    Write-Host '[FlexShow release] PASS: all selected source release checks completed.'
}
finally {
    if (Test-Path -LiteralPath $localConfig) {
        Remove-Item -LiteralPath $localConfig -Force -ErrorAction SilentlyContinue
    }
    $temporaryParent = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetDirectoryName($temporaryDirectory)
    )
    $temporaryLeaf = [System.IO.Path]::GetFileName($temporaryDirectory)
    if ($temporaryParent.TrimEnd('\') -eq $temporaryRoot.TrimEnd('\') -and
        $temporaryLeaf.StartsWith('flexshow-release-', [System.StringComparison]::Ordinal) -and
        (Test-Path -LiteralPath $temporaryDirectory -PathType Container)) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    Pop-Location
}
