<#
.SYNOPSIS
Verifies the approved local Home Podcast .41 audio assets for a 3080 FlexGPU sync.
.DESCRIPTION
Read-only for the Home Podcast checkout and all working TOEs. Hashes all three
WAVs and checks the exact clock, separators and unchanged visual plans outside
TouchDesigner. Writes only an ignored JSON report; no audio is copied.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PodcastRoot = '',
    [string]$Report = 'runtime/validation/podcast41-audio-preflight.json'
)

. (Join-Path $PSScriptRoot '_FlexShow.Common.ps1')
. (Join-Path $PSScriptRoot '_LocalVerification.Common.ps1')

$root = Get-FlexShowRepositoryRoot
if (-not $PodcastRoot) { $PodcastRoot = Join-Path (Split-Path $root -Parent) 'home-podcast' }
$reportPath = Resolve-FlexGpuLocalReportPath -Root $root -Path $Report
$relativeReport = $reportPath.Substring($root.Length + 1).Replace('\', '/')
& git -C $root check-ignore --quiet -- $relativeReport
if ($LASTEXITCODE -ne 0) { throw 'Audio preflight reports must be ignored by Git.' }
if (Test-Path -LiteralPath $reportPath) {
    throw 'Choose a new -Report path so a failed preflight cannot be mistaken for an old pass.'
}

$python = Get-FlexShowPython
$script = Join-Path $root 'touchdesigner/sync_combined_podcast_audio_3080.py'
$previous = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    $json = & $python.Executable @($python.PrefixArgs) $script $PodcastRoot | Out-String
    $exitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previous
}
if ($exitCode -ne 0) { throw "Home Podcast audio preflight failed with code $exitCode; no receipt created." }
$result = $json | ConvertFrom-Json
if ($result.version -cne 'flexgpu-podcast41-audio-preflight/v1' -or $result.status -cne 'pass') {
    throw 'Expected a successful audio preflight receipt; no receipt created.'
}
Write-FlexGpuLocalReport -Root $root -Path $reportPath -Value $result
$result | ConvertTo-Json -Depth 8
Write-Host "[FlexShow] Verified local audio assets. Receipt: $reportPath"
