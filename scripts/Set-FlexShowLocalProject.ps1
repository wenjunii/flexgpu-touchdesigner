<#
.SYNOPSIS
Retargets an ignored machine-local FlexShow config to an ignored working TOE.

.DESCRIPTION
Use this after TouchDesigner's Save As or versioned-save command creates a new
working .toe. The script updates only processes.world.project in one existing
local JSON config. Both files must live inside this checkout and be ignored by
Git, so the tracked synthetic project, public presets, private components, GPU
UUIDs, and another computer's local configuration remain untouched.

The write is atomic and SupportsShouldProcess provides -WhatIf. -ExpectedTier
adds an explicit guard against selecting a 3080, 4090, or 5090 config from the
wrong machine.
#>
#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [Parameter(Mandatory = $true)]
    [string]$Project,

    [ValidateSet('', '3080ti_16gb', '4090', '5090', 'custom')]
    [string]$ExpectedTier = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$repositoryPrefix = $repositoryRoot.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar

function Resolve-RepositoryPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) {
        $Path
    }
    else {
        Join-Path $repositoryRoot $Path
    }
    $fullPath = [System.IO.Path]::GetFullPath($candidate)
    if (-not $fullPath.StartsWith(
        $repositoryPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "$Label must remain inside this repository: $fullPath"
    }
    return $fullPath
}

function Get-RepositoryRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return $Path.Substring($repositoryPrefix.Length).Replace('\', '/')
}

function Assert-GitIgnored {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $git) {
        throw 'git is required to verify that local files cannot be published.'
    }
    $relativePath = Get-RepositoryRelativePath -Path $Path
    & $git.Source -C $repositoryRoot check-ignore --quiet -- $relativePath
    if ($LASTEXITCODE -ne 0) {
        throw "$Label is not ignored by Git; refusing to modify it: $relativePath"
    }
}

function Assert-ProfileNameCompatibility {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Tier,

        [Parameter(Mandatory = $true)]
        [string[]]$Paths
    )

    $conflictingPattern = switch ($Tier) {
        '3080ti_16gb' { '(?i)(?:^|[-_.])(?:4090|5090)(?:[-_.]|$)' }
        '4090' { '(?i)(?:^|[-_.])(?:3080(?:ti)?|5090)(?:[-_.]|$)' }
        '5090' { '(?i)(?:^|[-_.])(?:3080(?:ti)?|4090)(?:[-_.]|$)' }
        default { '' }
    }
    if ([string]::IsNullOrWhiteSpace($conflictingPattern)) {
        return
    }
    foreach ($path in $Paths) {
        $name = [System.IO.Path]::GetFileName($path)
        if ($name -match $conflictingPattern) {
            throw "Tier $Tier conflicts with local filename '$name'. Keep 3080, 4090, and 5090 working files separate."
        }
    }
}

$configPath = Resolve-RepositoryPath -Path $Config -Label 'Config'
$projectPath = Resolve-RepositoryPath -Path $Project -Label 'Project'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Config does not exist: $configPath"
}
if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf)) {
    throw "Project does not exist: $projectPath"
}
if ([System.IO.Path]::GetExtension($configPath) -ine '.json') {
    throw "Config must be a JSON file: $configPath"
}
if ([System.IO.Path]::GetExtension($projectPath) -ine '.toe') {
    throw "Project must be a .toe file: $projectPath"
}
if ([System.IO.Path]::GetFileName($configPath) -notmatch '(?i)^local-.+\.json$') {
    throw 'Config filename must use the ignored local-*.json convention.'
}

Assert-GitIgnored -Path $configPath -Label 'Config'
Assert-GitIgnored -Path $projectPath -Label 'Project'

try {
    $document = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
}
catch {
    throw "Config is not valid JSON: $($_.Exception.Message)"
}
if ($null -eq $document.PSObject.Properties['tier'] -or
    [string]::IsNullOrWhiteSpace([string]$document.tier)) {
    throw 'Config has no tier.'
}
if ($null -eq $document.PSObject.Properties['processes'] -or
    $null -eq $document.processes.PSObject.Properties['world'] -or
    $null -eq $document.processes.world.PSObject.Properties['project']) {
    throw 'Config has no processes.world.project field.'
}

$tier = [string]$document.tier
if (-not [string]::IsNullOrWhiteSpace($ExpectedTier) -and
    $tier -cne $ExpectedTier) {
    throw "Config tier '$tier' does not match -ExpectedTier '$ExpectedTier'."
}
Assert-ProfileNameCompatibility -Tier $tier -Paths @($configPath, $projectPath)

$currentProject = [string]$document.processes.world.project
if ([string]::IsNullOrWhiteSpace($currentProject)) {
    throw 'Config processes.world.project is empty.'
}
$currentFullPath = if ([System.IO.Path]::IsPathRooted($currentProject)) {
    [System.IO.Path]::GetFullPath($currentProject)
}
else {
    [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $currentProject))
}
$changed = -not $currentFullPath.Equals(
    $projectPath,
    [System.StringComparison]::OrdinalIgnoreCase
)

$status = 'unchanged'
if ($changed -and $PSCmdlet.ShouldProcess(
    (Get-RepositoryRelativePath -Path $configPath),
    "Set processes.world.project to $(Get-RepositoryRelativePath -Path $projectPath)"
)) {
    $document.processes.world.project = $projectPath
    $json = $document | ConvertTo-Json -Depth 32
    $temporaryPath = Join-Path (
        [System.IO.Path]::GetDirectoryName($configPath)
    ) ('.{0}.{1}.tmp' -f [System.IO.Path]::GetFileName($configPath), [guid]::NewGuid().ToString('N'))
    try {
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            $json + [Environment]::NewLine,
            $utf8
        )
        Move-Item -LiteralPath $temporaryPath -Destination $configPath -Force
        $status = 'updated'
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}
elseif ($changed) {
    $status = 'preview'
}

[ordered]@{
    status = $status
    tier = $tier
    config = Get-RepositoryRelativePath -Path $configPath
    project = Get-RepositoryRelativePath -Path $projectPath
    changed = $changed
} | ConvertTo-Json
