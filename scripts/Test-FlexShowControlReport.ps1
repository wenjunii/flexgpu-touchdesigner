<#
.SYNOPSIS
Checks a machine-local live SHOW_CONTROL validation report.

.DESCRIPTION
Reads the ignored JSON report produced by touchdesigner\validate_show_controls.py
inside TouchDesigner. The check rejects failed controls, failed restoration,
the wrong GPU profile, and cooked wall or mosaic TOP dimensions that differ
from the requested venue output. Actual TOP width and height are authoritative;
this detects license-tier clamping even when resolution parameters still show
1920 by 1080.

This command is read-only. It does not start TouchDesigner, launch workers,
change Git state, or inspect private component internals.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Report,

    [ValidateSet('3080ti_16gb', '4090_24gb', '5090_32gb')]
    [string]$ExpectedProfile = '3080ti_16gb'
)

$ErrorActionPreference = 'Stop'
$reportPath = (Resolve-Path -LiteralPath $Report -ErrorAction Stop).Path
$result = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json

if ($result.version -ne 'flexgpu-show-controls-validation/v2') {
    throw "Unsupported SHOW_CONTROL report version: $($result.version)"
}
if ($result.profile -ne $ExpectedProfile) {
    throw (
        "SHOW_CONTROL report profile '$($result.profile)' does not match " +
        "expected profile '$ExpectedProfile'."
    )
}
if ($null -eq $result.output_dimensions) {
    throw 'SHOW_CONTROL report does not contain cooked output dimensions.'
}
if ($result.output_dimensions.status -ne 'pass') {
    $mismatches = @(
        $result.output_dimensions.mismatches.psobject.Properties |
            ForEach-Object {
                "$($_.Name): actual=$($_.Value.actual -join 'x') " +
                "expected=$($_.Value.expected -join 'x')"
            }
    )
    throw (
        "Cooked wall output dimensions do not match the requested venue " +
        "profile. $($mismatches -join '; ')"
    )
}
if ($result.status -ne 'pass' -or
    [int]$result.summary.fail -ne 0 -or
    [int]$result.summary.restoration_failures -ne 0) {
    throw 'SHOW_CONTROL report contains control or restoration failures.'
}

[ordered]@{
    status = 'ok'
    report = $reportPath
    profile = $result.profile
    controls_passed = [int]$result.summary.pass
    wall_width = [int]$result.output_dimensions.wall_width
    wall_height = [int]$result.output_dimensions.wall_height
    output_dimensions = $result.output_dimensions.status
    worker_buttons = $result.worker_buttons.status
    adapter_ui_buttons = $result.adapter_ui_buttons.status
} | ConvertTo-Json -Depth 5
