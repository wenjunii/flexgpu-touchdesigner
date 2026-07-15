<#
.SYNOPSIS
Checks every public-sync candidate, the Git index, and repository history.

.DESCRIPTION
This read-only guard rejects private/paid paths, model weights, key stores,
machine-local files, and high-confidence credential signatures. Findings show
only a path, rule, and line number; matched secret values are never printed.
Use -Revision HEAD to limit a history scan to the closure of the exact commit
being published; without it, history includes all local branches and tags.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Candidates', 'Index', 'History', 'Both', 'All')]
    [string]$Scope = 'All',

    [string[]]$Revision = @(),

    [switch]$SelfTest,

    [AllowNull()]
    [string]$InputText = $null,

    [string]$InputLabel = 'input',

    [switch]$Json,

    [switch]$ExitWithCode
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')

$root = Get-FlexShowRepositoryRoot
$toolPath = Join-Path $root 'tools/check_public_sync.py'
if (-not (Test-Path -LiteralPath $toolPath -PathType Leaf)) {
    throw "Public-sync checker does not exist: $toolPath"
}

$python = Get-FlexShowPython
$arguments = [System.Collections.Generic.List[string]]::new()
foreach ($prefixArg in $python.PrefixArgs) {
    $arguments.Add($prefixArg)
}
$arguments.Add($toolPath)
$arguments.Add('--root')
$arguments.Add($root)
$arguments.Add('--scope')
$arguments.Add($Scope.ToLowerInvariant())
foreach ($historyRevision in $Revision) {
    if ([string]::IsNullOrWhiteSpace($historyRevision)) {
        throw 'Revision values must not be empty.'
    }
    $arguments.Add('--revision')
    $arguments.Add($historyRevision)
}
if ($SelfTest) {
    $arguments.Add('--self-test')
}
$hasInputText = $PSBoundParameters.ContainsKey('InputText')
if ($hasInputText) {
    if ([string]::IsNullOrWhiteSpace($InputLabel)) {
        throw 'InputLabel must not be empty when InputText is supplied.'
    }
    $arguments.Add('--stdin-label')
    $arguments.Add($InputLabel)
}
if ($Json) {
    $arguments.Add('--json')
}

$previousErrorActionPreference = $ErrorActionPreference
$hadNativePreference = Test-Path Variable:PSNativeCommandUseErrorActionPreference
$previousNativePreference = $null
if ($hadNativePreference) {
    $previousNativePreference = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
}
try {
    $ErrorActionPreference = 'Continue'
    if ($hasInputText) {
        $InputText | & $python.Executable @arguments
    }
    else {
        & $python.Executable @arguments
    }
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
    throw "Public-sync check failed with code $exitCode. Nothing is approved for push."
}
